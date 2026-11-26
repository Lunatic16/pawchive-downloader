#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pawdl - interactive downloader for the pawchive.pw public archive API.

Built against the public API (https://pawchive.pw/api/schema):
  GET /api/v1/creators                          all creators (~99k, patreon + fanbox)
  GET /api/v1/posts?o=<150*n>&q=<query>         recent posts / global search
  GET /api/v1/{service}/user/{id}/posts?o=<50*n>&q=<query>   posts of one creator
  GET /api/v1/{service}/user/{id}/post/{pid}    full single post

Files are served from  {server}/data{path}?f={name}  (n1..n3.pawchive.pw).
The file server requires download etiquette:
  * an identifiable User-Agent (NOT a browser string) with contact info,
  * no more than 1 request per second,
  * no more than 5 active downloads.
This script enforces all three by default.

Python 3.8+, standard library only.
"""

import concurrent.futures
import datetime
import html as htmllib
import http.client
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# constants / configuration
# ---------------------------------------------------------------------------

API_BASE = "https://pawchive.pw/api/v1"
SITE_BASE = "https://pawchive.pw"
FALLBACK_FILE_SERVER = "https://n1.pawchive.pw"

RECENT_PAGE = 150          # /posts offset step (enforced by the API)
CREATOR_PAGE = 50          # /{service}/user/{id}/posts offset step
MAX_WORKERS = 5            # etiquette: max 5 active downloads
FILE_MIN_INTERVAL = 1.05   # etiquette: >= 1 request/second on file servers
API_MIN_INTERVAL = 0.30    # be gentle with the API as well

DEFAULT_MAX_RETRIES = 4
RETRY_BACKOFF = [5, 15, 40]  # seconds between retries


def _config_dir():
    override = os.environ.get("PAWDL_CONFIG_DIR")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "pawdl")
    return os.path.join(os.path.expanduser("~"), ".config", "pawdl")


CONFIG_DIR = _config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CREATORS_CACHE = os.path.join(CONFIG_DIR, "creators_cache.json")

DEFAULT_CONFIG = {
    "user_agent": "",
    "download_dir": os.path.join(os.getcwd(), "pawdl_downloads"),
    "creators_cache_hours": 24,
    "save_post_json": True,
    "save_content_txt": True,
    "save_post_md": True,
    "download_inline_images": True,
    "download_preview_files": False,
    "max_retries": DEFAULT_MAX_RETRIES,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update(stored)
    except (OSError, ValueError):
        pass
    # a hand-edited config must not crash the program later on
    for key, minimum in (("creators_cache_hours", 1), ("max_retries", 0)):
        try:
            cfg[key] = max(minimum, int(cfg[key]))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_CONFIG[key]
    for key in ("user_agent", "download_dir"):
        if not isinstance(cfg[key], str):
            cfg[key] = DEFAULT_CONFIG[key]
    return cfg


def write_json_atomic(path, data, indent=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    write_json_atomic(CONFIG_FILE, cfg, indent=2)


# ---------------------------------------------------------------------------
# console / TUI helpers (ANSI theme, boxes, styled prompts, progress bar)
# ---------------------------------------------------------------------------

def _enable_windows_vt():
    """Best effort: make ANSI escapes work in the classic Windows console."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def _detect_color():
    if os.environ.get("NO_COLOR"):
        return False
    if (os.environ.get("TERM") or "") == "dumb":
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        _enable_windows_vt()
    return True


def _detect_unicode():
    try:
        "\u2500\u256d\u256e\u2502\u2570\u256f\u2588\u2591\u2714\u203a\u00b7".encode(
            getattr(sys.stdout, "encoding", "") or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


COLOR = _detect_color()
UNICODE = _detect_unicode()

# palette (SGR codes)
DIM = "37"          # light grey - secondary details
PROMPT = "1;97"     # bold bright white - input prompts
ACCENT = "1;96"     # bold bright cyan - titles
ACCENT2 = "1;95"    # bold bright magenta - numbers / keys
GOOD = "92"         # green - success
WARNC = "93"        # yellow - warnings
BAD = "91"          # red - errors / off

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def c(text, *codes):
    """Wrap text in ANSI SGR codes (no-op when colour is unavailable)."""
    if not COLOR or not codes:
        return text
    return "\033[%sm%s\033[0m" % (";".join(str(x) for x in codes), text)


def vlen(text):
    """Visible length of a string (ANSI escapes removed)."""
    return len(_ANSI_RE.sub("", text))


def term_width():
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:
        return 80


if UNICODE:
    R_TL, R_TR, R_BL, R_BR = "\u256d", "\u256e", "\u2570", "\u256f"  # ╭ ╮ ╰ ╯
    R_MID = "\u2502"                                     # │
    R_H = "\u2500"                                        # ─
    BAR_FULL, BAR_REST = "\u2588", "\u2591"               # █ ░
    SYM_OK, SYM_INFO = "\u2714", "\u203a"                 # ✔ ›
    SYM_ASK, SYM_DOT = "\u203a", "\u00b7"                 # › ·
else:
    R_TL = R_TR = R_BL = R_BR = "+"
    R_MID = "|"
    R_H = "-"
    BAR_FULL, BAR_REST = "#", "."
    SYM_OK, SYM_INFO = "+", ">"
    SYM_ASK, SYM_DOT = ">", "-"


def cls():
    if COLOR:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
    else:
        print("\n" * 2)


def rule():
    print("  " + c(R_H * min(term_width() - 4, 76), DIM))


def head(title):
    print("")
    print("  " + c(title.upper(), ACCENT))
    rule()


def banner_box(lines):
    """Draw a rounded box around styled lines (padding uses visible width)."""
    inner = max(vlen(l) for l in lines) + 2
    print("  " + c(R_TL + R_H * inner + R_TR, DIM))
    for l in lines:
        print("  %s %s%s %s" % (c(R_MID, DIM), l,
                                " " * (inner - 2 - vlen(l)), c(R_MID, DIM)))
    print("  " + c(R_BL + R_H * inner + R_BR, DIM))


def ask(prompt, default=None):
    suffix = ""
    if default not in (None, ""):
        suffix = c(" [%s]" % default, DIM)
    try:
        val = input("  %s %s%s%s " % (
            c("?", ACCENT2), c(prompt, PROMPT), suffix, c(SYM_ASK, DIM))).strip()
    except EOFError:
        return default
    return val or default


_CONSOLE_LOCK = threading.RLock()
_progress_len = 0          # width of the progress line currently on screen


def clear_progress():
    global _progress_len
    with _CONSOLE_LOCK:
        if _progress_len:
            sys.stdout.write("\r" + " " * _progress_len + "\r")
            sys.stdout.flush()
            _progress_len = 0


def warn(msg):
    with _CONSOLE_LOCK:
        clear_progress()
        print("  %s %s" % (c("!", WARNC), c(msg, WARNC)))


def info(msg):
    print("  %s %s" % (c(SYM_INFO, ACCENT), msg))


def ok(msg):
    print("  %s %s" % (c(SYM_OK, GOOD), c(msg, GOOD)))


def pause():
    try:
        input("\n  %s " % c("press Enter to continue", DIM))
    except EOFError:
        pass


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def printable(text):
    """Remote text made safe for the terminal (no control/escape codes)."""
    return _CTRL_RE.sub(" ", htmllib.unescape(str(text or ""))).strip()


def onoff(v):
    return c("on ", GOOD) if v else c("off", BAD)


# ---------------------------------------------------------------------------
# filename sanitising
# ---------------------------------------------------------------------------

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.I)


def _clip_bytes(text, maxbytes):
    return text.encode("utf-8", "ignore")[:maxbytes].decode("utf-8", "ignore")


def sanitize(name, fallback="untitled", maxlen=120, maxbytes=240,
             keep_ext=False):
    """Make a safe path component. Linux limits names to 255 *bytes*, so
    long non-Latin names are clipped by encoded size as well as length."""
    name = htmllib.unescape(str(name or "")).strip()
    name = _SAFE_RE.sub("_", name)
    name = name.encode("utf-8", "replace").decode("utf-8")   # lone surrogates
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen or len(name.encode("utf-8")) > maxbytes:
        root, ext = os.path.splitext(name) if keep_ext else (name, "")
        if len(ext) > 16:
            root, ext = name, ""
        root = _clip_bytes(root[:maxlen - len(ext)],
                           maxbytes - len(ext.encode("utf-8")))
        name = root.rstrip(" .") + ext
    if os.name == "nt" and _WIN_RESERVED.match(name):
        name = "_" + name
    return name or fallback


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def fmt_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def parse_selection(raw, count):
    """Parse '1,3-5 8' style input into a sorted list of 0-based indexes."""
    picks = set()
    for token in raw.replace(",", " ").split():
        m = re.match(r"^(\d+)(?:-(\d+))?$", token)
        if not m:
            raise ValueError("bad selection token: %r" % token)
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1 or b < a or b > count:
            raise ValueError("selection out of range 1-%d: %s" % (count, token))
        picks.update(range(a - 1, b))
    return sorted(picks)


# ---------------------------------------------------------------------------
# global rate limiter (shared between download workers)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        # On some virtualized hosts the monotonic clock can stall while wall
        # time advances, which would spin here forever. Cap every wait call
        # by both an iteration budget and a wall-clock deadline; on healthy
        # systems the first check already succeeds (delay <= min_interval).
        with self._lock:
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.min_interval
        wall_deadline = time.time() + 15.0
        for _ in range(50):
            if slot - time.monotonic() <= 0 or time.time() >= wall_deadline:
                return
            time.sleep(min(max(slot - time.monotonic(), 0.01), 5.0))


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class HttpError(Exception):
    pass


class DownloadCancelled(Exception):
    """Raised via the progress callback to abort a running download."""
    pass


class HttpClient:
    def __init__(self, user_agent, max_retries=DEFAULT_MAX_RETRIES):
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.api_limiter = RateLimiter(API_MIN_INTERVAL)
        self.file_limiter = RateLimiter(FILE_MIN_INTERVAL)

    def _opener_headers(self):
        return {"User-Agent": self.user_agent}

    def _should_retry(self, code):
        return code in (403, 408, 425, 429) or code >= 500

    def _backoff(self, attempt, code, body=""):
        # the file server answers 403 with an etiquette message when the
        # rate limit is being pushed; back off longer in that case
        if code == 403 and "Are you a bot" in (body or ""):
            return RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)] + 15
        return RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]

    def get_json(self, url, limiter, limit=None):
        """GET a URL, parse JSON. Returns (data, final_url)."""
        last_err = None
        for attempt in range(self.max_retries + 1):
            limiter.wait()
            req = urllib.request.Request(url, headers=self._opener_headers())
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read(limit) if limit else resp.read()
                return json.loads(raw.decode("utf-8", "replace")), url
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                if not self._should_retry(e.code):
                    raise HttpError("HTTP %s: %s" % (e.code, body.strip()))
                last_err = HttpError("HTTP %s: %s" % (e.code, body.strip()))
                if attempt < self.max_retries:
                    d = self._backoff(attempt, e.code, body)
                    warn("HTTP %s on %s - retrying in %ds" % (e.code, url, d))
                    time.sleep(d)
            except (urllib.error.URLError, http.client.HTTPException,
                    OSError) as e:
                last_err = HttpError(str(e))
                if attempt < self.max_retries:
                    d = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    warn("network error on %s (%s) - retrying in %ds" % (url, e, d))
                    time.sleep(d)
            except ValueError as e:
                raise HttpError("invalid JSON from %s: %s" % (url, e))
        raise last_err or HttpError("request failed: %s" % url)

    def download_to_file(self, url, dest_path, progress_cb=None, cancel=None):
        """Stream a file to dest_path. Returns (status, bytes_written).
        status in {'downloaded', 'skipped', 'failed', 'cancelled'}."""
        cancel = cancel or threading.Event()
        tmp = dest_path + ".part"

        def discard_partial():
            try:
                os.remove(tmp)
            except OSError:
                pass

        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return ("skipped", os.path.getsize(dest_path))
        for attempt in range(self.max_retries + 1):
            if cancel.is_set():
                return ("cancelled", 0)
            self.file_limiter.wait()
            if cancel.is_set():
                return ("cancelled", 0)
            try:
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            except OSError as e:
                warn("cannot create directory for %s: %s" % (dest_path, e))
                return ("failed", 0)
            try:
                req = urllib.request.Request(url,
                                             headers=self._opener_headers())
                total = None
                with urllib.request.urlopen(req, timeout=90) as resp:
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        total = int(cl)
                    got = 0
                    with open(tmp, "wb") as fh:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            fh.write(chunk)
                            got += len(chunk)
                            if progress_cb:
                                progress_cb(got, total)
                # a dropped connection just ends the stream: verify the size,
                # or a truncated file would be kept and skipped forever
                if total is not None and got != total:
                    raise HttpError("incomplete download (%d of %d bytes)"
                                    % (got, total))
                if got == 0:
                    raise HttpError("empty response body")
                os.replace(tmp, dest_path)
                return ("downloaded", got)
            except DownloadCancelled:
                # user abort: drop the useless partial (server has no Range
                # support, so .part files cannot be resumed)
                discard_partial()
                return ("cancelled", 0)
            except (urllib.error.URLError, http.client.HTTPException,
                    OSError, ValueError, HttpError) as e:
                code = getattr(e, "code", None)
                body = ""
                if code:
                    try:
                        body = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                discard_partial()
                name = os.path.basename(dest_path)
                if code and not self._should_retry(code):
                    warn("HTTP %s for %s - file not available "
                         "(dead or expired URL)" % (code, name))
                    return ("failed", 0)
                if attempt >= self.max_retries:
                    warn("giving up on %s: %s" % (name, e))
                else:
                    d = self._backoff(attempt, code or 0, body)
                    warn("download error (%s) - retrying in %ds" % (e, d))
                    if cancel.wait(d):
                        return ("cancelled", 0)
        return ("failed", 0)


# ---------------------------------------------------------------------------
# pawchive API wrapper
# ---------------------------------------------------------------------------

class PawchiveAPI:
    def __init__(self, http):
        self.http = http

    def creators(self):
        data, _ = self.http.get_json(API_BASE + "/creators",
                                     self.http.api_limiter)
        return data

    def recent_or_search(self, query=None, offset=0):
        params = {"o": str(offset)}
        if query:
            params["q"] = query
        url = API_BASE + "/posts?" + urllib.parse.urlencode(params)
        data, _ = self.http.get_json(url, self.http.api_limiter)
        return data

    def creator_posts(self, service, user_id, query=None, offset=0):
        params = {"o": str(offset)}
        if query:
            params["q"] = query
        url = "%s/%s/user/%s/posts?%s" % (
            API_BASE, urllib.parse.quote(service), urllib.parse.quote(str(user_id)),
            urllib.parse.urlencode(params))
        data, _ = self.http.get_json(url, self.http.api_limiter)
        return data

    def post(self, service, user_id, post_id):
        url = "%s/%s/user/%s/post/%s" % (
            API_BASE, urllib.parse.quote(service),
            urllib.parse.quote(str(user_id)), urllib.parse.quote(str(post_id)))
        data, _ = self.http.get_json(url, self.http.api_limiter)
        return data


def parse_post_url(text):
    """Accept a full post URL like
    https://pawchive.pw/patreon/user/436247/post/176350173
    or the shorthand  patreon/user/436247/post/176350173."""
    m = re.search(r"([a-z]+)/user/([^/?#\s]+)/post/([^/?#\s]+)", text or "")
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def file_url(item):
    """Build the direct download URL for a file/attachment dict."""
    server = (item.get("server") or FALLBACK_FILE_SERVER).rstrip("/")
    path = item.get("path") or ""
    if not path:
        return None
    name = item.get("name") or path.rsplit("/", 1)[-1]
    if not path.startswith("/"):
        path = "/" + path
    return "%s/data%s?f=%s" % (server, urllib.parse.quote(path, safe="/%"),
                               urllib.parse.quote(name, safe=""))


_THUMB_RE = re.compile(r"^https://img\.pawchive\.pw/thumbnail(/data/.+)$")
_IMG_RE = re.compile(r"<img\b[^>]*?\ssrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)


def inline_image_urls(post):
    """Extract full-size inline images referenced inside the post HTML body."""
    content = post.get("content") or ""
    urls = []
    seen = set()
    for _, src in _IMG_RE.findall(content):
        src = htmllib.unescape(src.strip())
        if src.startswith("//"):
            src = "https:" + src
        m = _THUMB_RE.match(src)
        if m:
            src = FALLBACK_FILE_SERVER + m.group(1)
        elif src.startswith("/data/"):
            src = FALLBACK_FILE_SERVER + src
        src = urllib.parse.quote(src, safe=":/?&=%#+,;@!$'()*[]~")
        parts = urllib.parse.urlsplit(src)
        host = parts.hostname or ""
        if ("/data/" in parts.path and src not in seen
                and (host == "pawchive.pw" or host.endswith(".pawchive.pw"))):
            seen.add(src)
            urls.append(src)
    return urls


# names written by write_post_metadata (plus our marker): attachments must
# not use them, or they would be skipped as "already downloaded"
RESERVED_NAMES = ("post.json", "post.md", "content.txt", ".pawdl_post_id")


def collect_post_files(post, include_previews=False, include_inline=True):
    """Return an ordered list of (url, filename) for one post.

    Handles three file kinds:
      * regular path-based files/attachments ({server}/data{path}?f=name),
      * deferred attachments (videos) that only carry a signed temporary
        download URL (temp_download_url, expires after temp_expires),
      * inline images inside the post HTML body (if include_inline).
    Files marked preview_only (e.g. thumbnail.jpg) are skipped unless
    include_previews is set - they are preview placeholders, not content,
    and their data URLs are frequently dead (nginx 404).
    A file referenced more than once is only downloaded once.
    """
    files = []
    fold = str.casefold if os.name == "nt" or sys.platform == "darwin" else str
    used_names = {fold(n) for n in RESERVED_NAMES}
    seen_sources = set()

    def add(url, name, source):
        if not url or not name:
            return
        base = sanitize(name, "file", 100, 230, keep_ext=True)
        i, cand = 2, base
        while fold(cand) in used_names:
            root, ext = os.path.splitext(base)
            cand = "%s (%d)%s" % (root, i, ext)
            i += 1
        # a duplicate still reserves its name slot, so that the numbering of
        # later files matches what earlier versions already saved to disk
        used_names.add(fold(cand))
        if source not in seen_sources:
            seen_sources.add(source)
            files.append((url, cand))

    def add_item(item):
        if item.get("preview_only") and not include_previews:
            return
        if item.get("deferred"):
            url = item.get("temp_download_url") or item.get("temp_url")
            add(url, item.get("name") or "video.mp4", url)
        elif item.get("path"):
            add(file_url(item),
                item.get("name") or item["path"].rsplit("/", 1)[-1],
                "/" + item["path"].lstrip("/"))

    add_item(post.get("file") or {})
    for att in post.get("attachments") or []:
        add_item(att)
    if include_inline:
        for url in inline_image_urls(post):
            path = urllib.parse.urlsplit(url).path
            name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            add(url, name or "inline_image",
                urllib.parse.unquote(path[path.index("/data/") + 5:]))
    return files


# ---------------------------------------------------------------------------
# HTML -> text conversion (stdlib only)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "blockquote", "pre")


def html_to_text(content):
    if not content:
        return ""
    s = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", "", content)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    for t in _BLOCK_TAGS:
        s = re.sub(r"(?i)</?%s\b[^>]*>" % t, "\n", s)
    s = re.sub(r"(?i)<img\b[^>]*?\salt=([\"'])((?:(?!\1)[^>])+)\1[^>]*>",
               r"[image: \2]", s)
    s = re.sub(r"(?i)<img[^>]*>", "[image]", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------------------
# download engine
# ---------------------------------------------------------------------------

class DownloadStats:
    def __init__(self):
        # RLock: the progress callback and draw() both enter under one thread
        self.lock = threading.RLock()
        self.done = 0
        self.total = 0
        self.bytes_ok = 0      # bytes of files downloaded in this run
        self.bytes_now = 0     # in-flight bytes (streaming)
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.cancelled = 0
        self.failed_names = []
        self._printed = 0.0
        self.speed = 0.0          # bytes per second (smoothed)
        self._speed_t0 = None
        self._speed_b0 = 0

    def add_total(self, n):
        with self.lock:
            self.total += n

    def update(self, status, name, size):
        with self.lock:
            if status == "downloaded":
                self.downloaded += 1
                self.bytes_ok += size
            elif status == "skipped":
                self.skipped += 1
            elif status == "cancelled":
                self.cancelled += 1
            else:
                self.failed += 1
                self.failed_names.append(name)
            self.done += 1

    def draw(self):
        global _progress_len
        with self.lock, _CONSOLE_LOCK:
            now = time.time()
            total_b = self.bytes_ok + self.bytes_now
            if self._speed_t0 is None:
                self._speed_t0, self._speed_b0 = now, total_b
            dt = now - self._speed_t0
            if dt >= 0.5:
                self.speed = (total_b - self._speed_b0) / dt
                self._speed_t0, self._speed_b0 = now, total_b
            frac = min(max((float(self.done) / self.total) if self.total
                           else 0.0, 0.0), 1.0)
            width = 22
            fill = int(round(frac * width))
            bar = c(BAR_FULL * fill, GOOD) + c(BAR_REST * (width - fill), DIM)
            msg = "  %s %s  %d/%d files  %s  %s/s" % (
                bar, c("%3.0f%%" % (frac * 100), "1;97"),
                self.done, self.total, c(fmt_size(total_b), ACCENT),
                c(fmt_size(max(self.speed, 0.0)), GOOD))
            if self.failed:
                msg += "  " + c("%d failed" % self.failed, BAD)
            if self.cancelled:
                msg += "  " + c("%d cancelled" % self.cancelled, WARNC)
            # a line wider than the terminal wraps and cannot be redrawn
            if vlen(msg) >= term_width():
                msg = "  %3.0f%%  %d/%d" % (frac * 100, self.done, self.total)
            vis = vlen(msg)
            pad = " " * max(_progress_len - vis, 0)
            sys.stdout.write("\r" + msg + pad)
            sys.stdout.flush()
            _progress_len = vis

    def finish_line(self):
        with self.lock:
            clear_progress()
            parts = [c("%d downloaded" % self.downloaded, GOOD)
                     + c(" (%s)" % fmt_size(self.bytes_ok), DIM),
                     c("%d skipped" % self.skipped, ACCENT)]
            if self.failed:
                parts.append(c("%d failed" % self.failed, BAD))
            if self.cancelled:
                parts.append(c("%d cancelled" % self.cancelled, WARNC))
            print("  %s %s" % (c(SYM_OK, GOOD), ", ".join(parts)))
            for name in self.failed_names[:10]:
                warn("failed: %s" % name)
            if len(self.failed_names) > 10:
                warn("... and %d more" % (len(self.failed_names) - 10))


class Downloader:
    def __init__(self, http):
        self.http = http
        self.stats = DownloadStats()
        self._cancel = threading.Event()

    def reset(self):
        """Start counting from zero for a new download session."""
        self.stats = DownloadStats()
        self._cancel.clear()

    def _worker(self, url, dest):
        last = [0]
        name = os.path.basename(dest)

        def cb(got, total):
            if self._cancel.is_set():
                raise DownloadCancelled()
            now = time.time()
            with self.stats.lock:
                # cumulative for THIS attempt; retries restart from 0, so
                # the delta goes negative and self-corrects bytes_now
                self.stats.bytes_now += got - last[0]
                last[0] = got
                if now - self.stats._printed >= 0.5:
                    self.stats._printed = now
                    self.stats.draw()
        try:
            status, size = self.http.download_to_file(url, dest, cb,
                                                      self._cancel)
        except Exception as e:  # defensive: never kill the pool
            status, size = "failed", 0
            warn("exception downloading %s: %s" % (url, e))
        with self.stats.lock:
            self.stats.bytes_now -= last[0]
            last[0] = 0
            self.stats.update(status, name, size)
        self.stats.draw()

    def run(self, jobs):
        """jobs: list of (url, absolute_dest_path).
        Returns False if the user interrupted the run."""
        if not jobs:
            return True
        self.stats.add_total(len(jobs))
        interrupted = False
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
        futures = []
        try:
            for url, dest in jobs:
                futures.append(pool.submit(self._worker, url, dest))
            pending = set(futures)
            while pending:   # short timeouts keep Ctrl+C responsive
                _, pending = concurrent.futures.wait(pending, timeout=0.5)
        except KeyboardInterrupt:
            interrupted = True
            self._cancel.set()
            warn("interrupted - aborting downloads (partials are "
                 "discarded: the server cannot resume them)...")
            for f in futures:
                if f.cancel():
                    self.stats.update("cancelled", "", 0)
        finally:
            pool.shutdown(wait=True)
        return not interrupted


# ---------------------------------------------------------------------------
# post persistence (directory layout + metadata export)
# ---------------------------------------------------------------------------

def post_dir_path(cfg, post, creator_name=None):
    dd = cfg["download_dir"]
    service = post.get("service", "?")
    user = post.get("user", "?")
    cname = sanitize(creator_name or user, str(user), 60, 200)
    published = post.get("published") or post.get("added") or ""
    date = sanitize(str(published)[:10], "unknown-date")
    title = sanitize(post.get("title"), "untitled", 80)
    folder = "%s_%s" % (date, title)
    return os.path.join(dd, sanitize(service, "unknown"),
                        "%s (%s)" % (cname, sanitize(user, "unknown", 60)),
                        folder)


POST_MARKER = ".pawdl_post_id"


def folder_owner(path):
    """ID of the post stored in `path`, or None if it cannot be told."""
    try:
        with open(os.path.join(path, POST_MARKER), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):
        pass
    # folders written by earlier versions only have the metadata files
    try:
        with open(os.path.join(path, "post.json"), encoding="utf-8") as fh:
            return str(json.load(fh)["id"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with open(os.path.join(path, "post.md"), encoding="utf-8") as fh:
            m = re.search(r"^- \*\*Post ID\*\*: (.+)$", fh.read(), re.M)
            return m.group(1).strip() if m else None
    except (OSError, ValueError):
        return None


def claim_post_dir(cfg, post, creator_name=None):
    """Create and return the folder of a post. Two posts with the same date
    and title must not share one, so the later one gets its ID appended."""
    base = post_dir_path(cfg, post, creator_name)
    pid = str(post.get("id"))
    dest = base
    if folder_owner(base) not in (None, pid):
        parent, leaf = os.path.split(base)
        suffix = "_" + sanitize(pid, "post", 40, 40)
        dest = os.path.join(parent, _clip_bytes(leaf, 250 - len(
            suffix.encode("utf-8"))) + suffix)
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, POST_MARKER), "w", encoding="utf-8") as fh:
        fh.write(pid + "\n")
    return dest


def write_post_metadata(cfg, post, dest_dir, creator_name=None):
    """Write the metadata files that are enabled in the settings."""
    os.makedirs(dest_dir, exist_ok=True)

    if cfg.get("save_post_json"):
        with open(os.path.join(dest_dir, "post.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(post, fh, indent=2, ensure_ascii=False)

    text = html_to_text(post.get("content"))

    if cfg.get("save_content_txt") and text:
        with open(os.path.join(dest_dir, "content.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")

    if cfg.get("save_post_md"):
        rows = []
        f = post.get("file") or {}
        if f.get("name"):
            if f.get("preview_only") and not cfg.get("download_preview_files"):
                rows.append("- %s (preview only, not archived)" % f["name"])
            else:
                rows.append("- %s" % f["name"])
        for a in post.get("attachments") or []:
            if a.get("deferred"):
                rows.append("- %s (video, temporary signed URL)"
                            % (a.get("name") or "video.mp4"))
            elif a.get("name"):
                rows.append("- %s" % a["name"])
        embed = post.get("embed") or {}
        lines = [
            "# %s" % (post.get("title") or "untitled"),
            "",
            "- **Service**: %s" % post.get("service"),
            "- **Creator**: %s (%s)" % (creator_name or "?", post.get("user")),
            "- **Post ID**: %s" % post.get("id"),
            "- **Published**: %s" % (post.get("published") or "?"),
            "- **URL**: %s/%s/user/%s/post/%s" % (
                SITE_BASE, post.get("service"), post.get("user"),
                post.get("id")),
        ]
        tags = post.get("tags")
        if tags:
            lines.append("- **Tags**: %s" % ", ".join(map(str, tags)))
        lines.append("- **URL page**: %s" % (embed.get("url") or "-"))
        lines.append("")
        lines.append("## Files")
        lines.extend(rows or ["- (none)"])
        lines.append("")
        lines.append("## Content")
        lines.append("")
        lines.append(text or "(no text content)")
        with open(os.path.join(dest_dir, "post.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def post_files(cfg, post):
    """collect_post_files() with the user's settings applied."""
    return collect_post_files(post, cfg.get("download_preview_files"),
                              cfg.get("download_inline_images"))


def build_post_jobs(cfg, post, creator_name=None):
    """Return (dest_dir, [(url, dest), ...]) for one full post.

    NOTE: existing files keep their original path so the downloader's
    skip-if-exists logic works on re-runs. Same-post name collisions are
    already resolved inside collect_post_files().
    """
    dest = claim_post_dir(cfg, post, creator_name)
    jobs = [(url, os.path.join(dest, name))
            for url, name in post_files(cfg, post)]
    return dest, jobs


# ---------------------------------------------------------------------------
# creators cache (the /creators payload is ~16 MB, cache it locally)
# ---------------------------------------------------------------------------

class CreatorCache:
    def __init__(self, cfg, api):
        self.cfg = cfg
        self.api = api
        self._data = None

    def _fresh(self, path):
        try:
            age = time.time() - os.path.getmtime(path)
            return age < self.cfg["creators_cache_hours"] * 3600
        except OSError:
            return False

    def _read(self):
        try:
            with open(CREATORS_CACHE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def get(self, force=False):
        if self._data and not force and self._fresh(CREATORS_CACHE):
            return self._data
        if not force and self._fresh(CREATORS_CACHE):
            self._data = self._read()
            if self._data:
                return self._data
        info("downloading creator index (~16 MB, then cached)...")
        try:
            data = self.api.creators()
            if not isinstance(data, list):
                raise HttpError("unexpected creator index format")
        except HttpError as e:
            stale = None if force else (self._data or self._read())
            if not stale:
                raise
            warn("could not refresh creator index (%s) - using old copy" % e)
            self._data = stale
            return stale
        self._data = data
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            write_json_atomic(CREATORS_CACHE, data)
            info("cached %d creators to %s" % (len(data), CREATORS_CACHE))
        except OSError as e:
            warn("could not save creator cache: %s" % e)
        return data

    def search(self, query, service=None, limit=60):
        data = self.get()
        q = query.lower().strip()
        out = []
        for cr in data:
            if service and cr.get("service") != service:
                continue
            if q and q not in (cr.get("name") or "").lower() \
                    and q not in str(cr.get("public_id") or "").lower() \
                    and q not in str(cr.get("id") or "").lower():
                continue
            out.append(cr)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# interactive interface
# ---------------------------------------------------------------------------

HELP_TEXT = """
  pawdl is an interactive downloader for the pawchive.pw archive.

  What you can do from the main menu:
    [1] Search creators       - search ~99k patreon/fanbox creators by name,
                                browse their posts page by page (50/page)
                                and download any selection.
    [2] Recent / search posts - global post search (index full-text),
                                browse 150-per-page results, download picks.
    [3] Download single post  - paste a post URL or  service/user/postid.
    [4] Download whole creator- enter service + creator id (or a creator URL)
                                and archive every post and file.
    [5] Settings              - User-Agent contact, download folder, toggles.
    [6] Help                  - this text.

  Download behaviour (enforced by default):
    * max 5 parallel downloads, >= 1 second between file requests
      (the archive asks for this; abusing it gets your IP blocked),
    * already-downloaded files are skipped on re-run,
    * automatic retries with backoff on transient errors,
    * every post is saved with post.json + post.md + content.txt metadata,
    * deferred video attachments (signed temporary MP4 URLs) are
      downloaded directly; the video server does not support resume,
      so an interrupted video restarts from the beginning next run,
    * inline <img> images inside post bodies are downloaded too
      (toggle in Settings [7]),
    * preview-only thumbnails are skipped (toggle in Settings [8]).

  Folder layout:
    {download_dir}/{service}/{creator} ({id})/{date}_{title}/
      file1.png, file2.png, ..., post.json, post.md, content.txt
    Posts sharing a date and title get _{post id} added to the folder name.
"""


def display_post_row(i, p):
    title = printable(p.get("title")) or "(untitled)"
    title = title[:56]
    f = p.get("file") or {}
    paths = set()
    for item in [f] + list(p.get("attachments") or []):
        if item.get("preview_only"):
            continue
        key = item.get("path") or (item.get("deferred") and item.get("name"))
        if key:
            paths.add(key)
    date = str(p.get("published") or "")[:10]
    print("  %s %s %s  %s  %s" % (
        c("%3d." % i, ACCENT2),
        c("[%s]" % p.get("service", "?"), DIM),
        c(str(p.get("user", "?")), DIM),
        title,
        c("%s | %d file(s)" % (date or "?", len(paths)), ACCENT)))


def creator_key(item, user_field="user"):
    # numeric IDs can repeat between services, so key on both
    return (item.get("service"), str(item.get(user_field)))


def creator_label(cr):
    return cr.get("name") or cr.get("public_id") or ""


def resolve_creator_names(cache, posts):
    """Best-effort lookup of creator names from the cached creator index."""
    wanted = {creator_key(p) for p in posts}
    names = {}
    try:
        for cr in cache.get():
            key = creator_key(cr, "id")
            if key in wanted and creator_label(cr):
                names[key] = creator_label(cr)
    except (HttpError, OSError) as e:
        warn("creator names unavailable (%s) - using IDs" % e)
    return names


DOWNLOAD_BATCH = 10   # posts fetched per round; signed video URLs expire


def download_posts(cfg, api, dl, posts, creator_names=None):
    """Download the selected posts, a batch at a time: fetch the full post,
    save its metadata, download its files. Returns False if interrupted."""
    unique, seen = [], set()
    for p in posts:
        key = (p.get("service"), str(p.get("user")), str(p.get("id")))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    if not unique:
        info("nothing selected")
        return True
    names = dict(creator_names or {})
    if any(creator_key(p) not in names for p in unique):
        found = resolve_creator_names(CreatorCache(cfg, api), unique)
        for key, name in found.items():
            names.setdefault(key, name)

    head("downloading %d post(s)" % len(unique))
    dl.reset()
    bad_posts, files = [], 0
    completed = True
    for start in range(0, len(unique), DOWNLOAD_BATCH):
        jobs = []
        for summary in unique[start:start + DOWNLOAD_BATCH]:
            try:
                # always fetch the full post: list entries may be truncated
                # and signed video URLs must be fresh
                post = summary if summary.get("_full") else api.post(
                    summary["service"], summary["user"], summary["id"])
                name = names.get(creator_key(summary))
                dest, post_jobs = build_post_jobs(cfg, post, name)
                write_post_metadata(
                    cfg, {k: v for k, v in post.items() if k != "_full"},
                    dest, name)
                jobs.extend(post_jobs)
            except (HttpError, OSError, KeyError, TypeError,
                    AttributeError) as e:
                warn("could not process post %s: %s" % (summary.get("id"), e))
                bad_posts.append(summary.get("id"))
        files += len(jobs)
        if len(unique) > DOWNLOAD_BATCH:
            info("posts %d-%d of %d: %d file(s)" % (
                start + 1, min(start + DOWNLOAD_BATCH, len(unique)),
                len(unique), len(jobs)))
        if not dl.run(jobs):
            completed = False
            break
        clear_progress()
    if files:
        dl.stats.finish_line()
    elif len(bad_posts) < len(unique):
        info("selected posts contain no downloadable files")
    if bad_posts:
        warn("%d post(s) could not be processed" % len(bad_posts))
    return completed


# --- 1. creator search ------------------------------------------------------

def search_creators_menu(cfg, api, cache, dl):
    head("creator search")
    query = ask("creator name / public_id / id (blank = browse)")
    service = ask("filter service: p=patreon, f=fanbox, blank=all")
    service = (service or "").lower()
    service = {"p": "patreon", "f": "fanbox"}.get(service, service or None)
    results = cache.search(query or "", service)
    if not results:
        warn("no creators matched")
        return
    print("")
    for idx, cr in enumerate(results, 1):
        # pad the plain text first: colour codes would upset the widths
        print("  %s %s %-40s %s  %s  %s" % (
            c("%3d." % idx, ACCENT2),
            c("%-9s" % ("[%s]" % cr.get("service")), DIM),
            printable(creator_label(cr) or "?")[:40],
            c("id=%s" % cr.get("id"), DIM),
            c("fav=%s" % cr.get("favorited"), DIM),
            c("upd=%s" % fmt_ts(cr.get("updated")), DIM)))
    print("")
    if len(results) >= 60:
        info("showing the first 60 matches - refine the search to see others")
    while True:
        raw = ask("number(s) to open (e.g. 2 or 1-3), blank=back")
        if not raw:
            return True
        try:
            picks = parse_selection(raw, len(results))
            break
        except ValueError as e:
            warn(e)
    for idx in picks:
        creator_menu(cfg, api, dl, results[idx])
    return True


def creator_menu(cfg, api, dl, creator):
    cname = creator_label(creator) or str(creator["id"])
    service = creator["service"]
    uid = creator["id"]
    name_map = {(service, str(uid)): cname}

    def fetch(offset):
        return api.creator_posts(service, uid, offset=offset)

    browse_posts(
        "%s - %s (id %s)" % (service, printable(cname), uid), fetch,
        CREATOR_PAGE, "download ALL posts of this creator",
        lambda picks: download_posts(cfg, api, dl, picks, name_map),
        lambda page: download_creator(cfg, api, dl, service, uid, cname))


def browse_posts(title, fetch, step, all_label, on_pick, on_all):
    """Paged post list shared by the creator and search menus."""
    offset = 0
    page = None
    while True:
        if page is None:
            try:
                page = fetch(offset)
            except HttpError as e:
                warn("could not list posts: %s" % e)
                if offset == 0:
                    pause()
                    return
                page = []
            if not page:
                if offset == 0:
                    warn("no posts found")
                    pause()
                    return
                warn("no more posts at this offset")
                offset = max(0, offset - step)
                page = None
                pause()
                continue
        head(title)
        info("%d posts from offset %d" % (len(page), offset))
        print("")
        for i, p in enumerate(page, 1):
            display_post_row(i, p)
        print("")
        print("  %s next page   %s prev page   %s %s"
              % (c("[n]", ACCENT2), c("[p]", ACCENT2), c("[a]", ACCENT2),
                 all_label))
        raw = ask("pick number(s) (e.g. 1,3-5) | n | p | a | q=menu")
        if not raw or raw.lower() == "q":
            return
        low = raw.lower()
        if low == "n":
            offset += step
            page = None
        elif low == "p":
            offset = max(0, offset - step)
            page = None
        elif low == "a":
            on_all(page)
            pause()
        else:
            try:
                picks = parse_selection(raw, len(page))
            except ValueError as e:
                warn(e)
                continue
            on_pick([page[i] for i in picks])
            pause()


def download_creator(cfg, api, dl, service, uid, cname=None):
    """Fetch every page of a creator's posts and download all files."""
    limit = None
    while True:
        limit_raw = ask("max posts to archive (blank = all)")
        if not limit_raw:
            break
        # never fall back to "all" on a typo
        if limit_raw.isdecimal() and int(limit_raw) > 0:
            limit = int(limit_raw)
            break
        warn("enter a positive number, or leave blank for all")
    if ask("confirm archive of %s/%s (%s)" % (
            service, uid, "first %d posts" % limit if limit else "all posts"),
            default="n").lower() not in ("y", "yes"):
        return
    name_map = {(service, str(uid)): cname} if cname else {}
    posts, seen = [], set()
    offset = 0
    while True:
        try:
            page = api.creator_posts(service, uid, offset=offset)
        except HttpError as e:
            warn("listing stopped early, the archive will be incomplete: %s"
                 % e)
            break
        # new posts shift the pages, so drop repeats; a page with nothing
        # new also ends the loop
        fresh = [p for p in page or [] if str(p.get("id")) not in seen]
        if not fresh:
            break
        seen.update(str(p.get("id")) for p in fresh)
        posts.extend(fresh)
        offset += CREATOR_PAGE
        if limit and len(posts) >= limit:
            posts = posts[:limit]
            break
        info("fetched %d posts so far..." % len(posts))
    if not posts:
        warn("no posts found for %s/%s" % (service, uid))
        return
    info("%d posts queued" % len(posts))
    download_posts(cfg, api, dl, posts, name_map)


# --- 2. recent / search -----------------------------------------------------

def recent_search_menu(cfg, api, dl):
    head("recent posts / global search")
    query = ask("search query (blank = recent posts)")

    def fetch(offset):
        return api.recent_or_search(query or None, offset)

    browse_posts(
        "search: %s" % query if query else "recent posts", fetch, RECENT_PAGE,
        "download ALL on this page",
        lambda picks: download_posts(cfg, api, dl, picks),
        lambda page: download_posts(cfg, api, dl, list(page)))
    return True


# --- 3. single post ---------------------------------------------------------

def single_post_menu(cfg, api, dl):
    head("download single post")
    raw = ask("post URL or  service/user/postid  (e.g. "
              "https://pawchive.pw/patreon/user/436247/post/176350173)")
    parsed = parse_post_url(raw)
    if not parsed:
        warn("could not parse - expected .../patreon/user/ID/post/POSTID")
        return
    service, uid, pid = parsed
    try:
        post = api.post(service, uid, pid)
    except HttpError as e:
        warn("could not fetch post: %s" % e)
        return
    info('fetched: "%s" (%s, %s)' % (printable(post.get("title")), service,
                                     post.get("published")))
    files = post_files(cfg, post)
    info("%d downloadable file(s), %d chars of text" %
         (len(files), len(html_to_text(post.get("content")))))
    if ask("download to %s?" % cfg["download_dir"], default="y").lower() \
            not in ("y", "yes"):
        return
    download_posts(cfg, api, dl, [dict(post, _full=True)])


# --- 4. whole creator by hand ----------------------------------------------

def quick_creator_menu(cfg, api, dl):
    head("download whole creator")
    raw = ask("creator URL (https://pawchive.pw/patreon/user/436247) or "
              "'service id' (e.g. patreon 436247)")
    if not raw:
        return
    m = re.search(r"([a-z]+)/user/([^/?#\s]+)", raw.strip())
    if m:
        service, uid = m.group(1), m.group(2)
    else:
        parts = raw.split()
        if len(parts) == 2:
            service, uid = parts[0].lower(), parts[1]
        else:
            warn("could not parse input")
            return
    key = (service, str(uid))
    cname = resolve_creator_names(
        CreatorCache(cfg, api), [{"service": service, "user": uid}]).get(key)
    if cname:
        info("creator: %s" % printable(cname))
    download_creator(cfg, api, dl, service, uid, cname)


# --- 5. settings ------------------------------------------------------------

def valid_user_agent(ua):
    """HTTP headers must be plain printable ASCII."""
    if all(32 <= ord(ch) < 127 for ch in ua):
        return True
    warn("the User-Agent may only contain plain ASCII characters")
    return False


def settings_menu(cfg, api):
    while True:
        head("settings")
        for key, label, val in (
                ("1", "User-Agent", c(cfg["user_agent"] or "(unset)", ACCENT)),
                ("2", "download folder", c(cfg["download_dir"], ACCENT)),
                ("3", "creators cache TTL", c("%s h" % cfg["creators_cache_hours"], ACCENT)),
                ("4", "save post.json", onoff(cfg["save_post_json"])),
                ("5", "save content.txt", onoff(cfg["save_content_txt"])),
                ("6", "save post.md", onoff(cfg["save_post_md"])),
                ("7", "inline images", onoff(cfg["download_inline_images"])),
                ("8", "preview thumbnails", onoff(cfg["download_preview_files"]))):
            print("  %s %-20s: %s" % (c("[%s]" % key, ACCENT2), label, val))
        print("  %s %-20s" % (c("[9]", ACCENT2), "refresh creator cache now"))
        print("  %s %-20s" % (c("[0]", ACCENT2), "back"))
        choice = ask("setting")
        if choice == "1":
            ua = ask("new User-Agent (include a contact, e.g. "
                     "'my-archiver/1.0 (personal backup; contact: you@example.com)')")
            if ua and valid_user_agent(ua):
                cfg["user_agent"] = ua
        elif choice == "2":
            d = ask("download folder")
            if d:
                cfg["download_dir"] = os.path.abspath(os.path.expanduser(d))
        elif choice == "3":
            h = ask("cache TTL hours")
            try:
                cfg["creators_cache_hours"] = max(1, int(h))
            except (TypeError, ValueError):
                warn("not a number")
        elif choice in ("4", "5", "6", "7", "8"):
            keys = {"4": "save_post_json", "5": "save_content_txt",
                    "6": "save_post_md", "7": "download_inline_images",
                    "8": "download_preview_files"}
            cfg[keys[choice]] = not cfg[keys[choice]]
        elif choice == "9":
            try:
                CreatorCache(cfg, api).get(force=True)
            except HttpError as e:
                warn("refresh failed: %s" % e)
            pause()
        elif choice in ("0", "", None):
            return
        try:
            save_config(cfg)
        except OSError as e:
            warn("could not save settings: %s" % e)
            pause()


def first_run_setup(cfg):
    if cfg["user_agent"]:
        return cfg
    print("")
    head("first-run setup")
    print("""  The pawchive file server requires an *identifiable* User-Agent
  (not a browser string) plus polite rate limits (1 req/s, max 5 downloads).
  This script enforces those limits. Please enter a contact identifier,
  for example:  pawdl/1.0 (myname@example.com)""")
    while True:
        ua = ask("User-Agent")
        if not ua:
            return cfg           # main() explains and exits; nothing saved
        if valid_user_agent(ua):
            break
    cfg["user_agent"] = ua
    d = ask("download folder", cfg["download_dir"])
    cfg["download_dir"] = os.path.abspath(os.path.expanduser(d or
                                              cfg["download_dir"]))
    try:
        save_config(cfg)
        info("config saved to %s" % CONFIG_FILE)
    except OSError as e:
        warn("could not save config: %s" % e)
    return cfg


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    # never crash on a title the console encoding cannot represent
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    cfg = load_config()
    if "you@example.com" in cfg["user_agent"]:
        cfg["user_agent"] = ""   # placeholder saved by earlier versions
    cfg = first_run_setup(cfg)
    if not cfg["user_agent"]:
        warn("a User-Agent with contact info is required - "
             "run pawdl again to enter one")
        return 1

    http = HttpClient(cfg["user_agent"], cfg.get("max_retries",
                                                 DEFAULT_MAX_RETRIES))
    api = PawchiveAPI(http)
    cache = CreatorCache(cfg, api)
    dl = Downloader(http)

    while True:
        cls()
        print("")
        banner_box([
            c("pawdl", "1;96") + c("  %s  " % R_MID, DIM)
            + c("pawchive.pw interactive downloader", "1;97"),
            c((" %s " % SYM_DOT).join((
                "max 5 parallel", "1 req/s", "skip existing",
                "retry+backoff")), DIM),
        ])
        print("")
        for key, label in (
                ("1", "Search creators"),
                ("2", "Recent posts / global search"),
                ("3", "Download single post (URL)"),
                ("4", "Download whole creator (URL or 'service id')"),
                ("5", "Settings"),
                ("6", "Help"),
                ("0", "Exit")):
            print("  %s %s" % (c("[%s]" % key, ACCENT2), label))
        print("")
        print("  %s %s" % (c("UA", DIM), c(cfg["user_agent"], DIM)))
        print("  %s %s" % (c("dir", DIM), c(cfg["download_dir"], DIM)))
        choice = ask("menu")
        if choice in ("0", "", None):
            print(c("bye", DIM))
            return 0
        # menus return True when they need no "press Enter" before the
        # screen is cleared
        quiet = False
        try:
            if choice == "1":
                quiet = search_creators_menu(cfg, api, cache, dl)
            elif choice == "2":
                quiet = recent_search_menu(cfg, api, dl)
            elif choice == "3":
                single_post_menu(cfg, api, dl)
            elif choice == "4":
                quick_creator_menu(cfg, api, dl)
            elif choice == "5":
                settings_menu(cfg, api)
                quiet = True
            elif choice == "6":
                head("help")
                print(HELP_TEXT)
            else:
                warn("unknown menu choice: %s" % choice)
        except KeyboardInterrupt:
            print("")
            warn("interrupted - returning to menu")
        except HttpError as e:
            print("")
            warn("API error: %s" % e)
        except OSError as e:
            print("")
            warn("file error: %s" % e)
        if not quiet:
            try:
                pause()
            except KeyboardInterrupt:
                print("")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        sys.exit(130)

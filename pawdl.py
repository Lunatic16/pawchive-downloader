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
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


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
        "\u2500\u256d\u256e\u2502\u2570\u256f\u2588\u2714".encode(
            getattr(sys.stdout, "encoding", "") or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


COLOR = _detect_color()
UNICODE = _detect_unicode()

# palette (SGR codes)
DIM = "90"          # grey details
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
else:
    R_TL = R_TR = R_BL = R_BR = "+"
    R_MID = "|"
    R_H = "-"
    BAR_FULL, BAR_REST = "#", "."
    SYM_OK, SYM_INFO = "+", ">"


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
            c("?", ACCENT2), c(prompt, "36"), suffix, c("\u203a", DIM))).strip()
    except EOFError:
        return default
    return val or default


def warn(msg):
    print("  %s %s" % (c("!", WARNC), c(msg, WARNC)))


def info(msg):
    print("  %s %s" % (c(SYM_INFO, ACCENT), msg))


def ok(msg):
    print("  %s %s" % (c(SYM_OK, GOOD), c(msg, GOOD)))


def onoff(v):
    return c("on ", GOOD) if v else c("off", BAD)


# ---------------------------------------------------------------------------
# filename sanitising
# ---------------------------------------------------------------------------

_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name, fallback="untitled", maxlen=120):
    name = htmllib.unescape(name or "").strip()
    name = _SAFE_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
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
        return code in (403, 408, 425, 429, 500, 502, 503, 504)

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
                if e.code in (400, 404):
                    raise HttpError("HTTP %s: %s" % (e.code, body.strip()))
                last_err = HttpError("HTTP %s: %s" % (e.code, body.strip()))
                if attempt < self.max_retries:
                    d = self._backoff(attempt, e.code, body)
                    warn("HTTP %s on %s - retrying in %ds" % (e.code, url, d))
                    time.sleep(d)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = HttpError(str(e))
                if attempt < self.max_retries:
                    d = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    warn("network error on %s (%s) - retrying in %ds" % (url, e, d))
                    time.sleep(d)
            except ValueError as e:
                raise HttpError("invalid JSON from %s: %s" % (url, e))
        raise last_err or HttpError("request failed: %s" % url)

    def download_to_file(self, url, dest_path, progress_cb=None):
        """Stream a file to dest_path. Returns (status, bytes_written).
        status in {'downloaded', 'skipped', 'failed'}."""
        tmp = dest_path + ".part"
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return ("skipped", os.path.getsize(dest_path))
        last_err = None
        for attempt in range(self.max_retries + 1):
            self.file_limiter.wait()
            try:
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            except OSError as e:
                warn("cannot create directory for %s: %s" % (dest_path, e))
                return ("failed", 0)
            req = urllib.request.Request(url, headers=self._opener_headers())
            try:
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
                if os.path.getsize(tmp) == 0:
                    raise HttpError("empty response body")
                os.replace(tmp, dest_path)
                return ("downloaded", os.path.getsize(dest_path))
            except DownloadCancelled:
                # user abort: drop the useless partial (server has no Range
                # support, so .part files cannot be resumed)
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                return ("cancelled", 0)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                    ConnectionError, OSError, HttpError) as e:
                code = getattr(e, "code", None)
                body = ""
                if code:
                    try:
                        body = e.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        pass
                last_err = e
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                if code in (400, 404):
                    warn("HTTP %s for %s - file not available "
                         "(may be a dead preview URL)" % (code, url))
                    return ("failed", 0)
                if attempt < self.max_retries:
                    d = self._backoff(attempt, code or 0, body)
                    warn("download error (%s) - retrying in %ds" % (e, d))
                    time.sleep(d)
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
    text = text.strip()
    m = re.search(r"([a-z]+)/user/(\S+)/post/(\w+)", text)
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
    return "%s/data%s?f=%s" % (server, path, urllib.parse.quote(name))


_THUMB_RE = re.compile(r"^https://img\.pawchive\.pw/thumbnail(/data/.+)$")
_IMG_RE = re.compile(r"<img[^>]*?src=[\"']([^\"']+)[\"']", re.I)


def inline_image_urls(post):
    """Extract full-size inline images referenced inside the post HTML body."""
    content = post.get("content") or ""
    urls = []
    seen = set()
    for src in _IMG_RE.findall(content):
        src = htmllib.unescape(src.strip())
        m = _THUMB_RE.match(src)
        if m:
            src = FALLBACK_FILE_SERVER + m.group(1)
        elif src.startswith("/data/"):
            src = FALLBACK_FILE_SERVER + src
        if "/data/" in src and "pawchive.pw" in src and src not in seen:
            seen.add(src)
            urls.append(src)
    return urls


def collect_post_files(post, include_previews=False):
    """Return an ordered list of (url, filename) for one post.

    Handles three file kinds:
      * regular path-based files/attachments ({server}/data{path}?f=name),
      * deferred attachments (videos) that only carry a signed temporary
        download URL (temp_download_url, expires after temp_expires),
      * inline images inside the post HTML body.
    Files marked preview_only (e.g. thumbnail.jpg) are skipped unless
    include_previews is set - they are preview placeholders, not content,
    and their data URLs are frequently dead (nginx 404).
    """
    files = []
    used_names = set()

    def add(url, name):
        if not url or not name:
            return
        base = sanitize(name, "file", 100)
        i, cand = 2, base
        while cand in used_names:
            root, ext = os.path.splitext(base)
            cand = "%s (%d)%s" % (root, i, ext)
            i += 1
        used_names.add(cand)
        files.append((url, cand))

    f = post.get("file") or {}
    if f.get("path") and (include_previews or not f.get("preview_only")):
        add(file_url(f), f.get("name") or f["path"].rsplit("/", 1)[-1])
    for att in post.get("attachments") or []:
        if att.get("deferred"):
            url = att.get("temp_download_url") or att.get("temp_url")
            add(url, att.get("name") or "video.mp4")
        elif att.get("path"):
            add(file_url(att), att.get("name") or att["path"].rsplit("/", 1)[-1])
    for url in inline_image_urls(post):
        name = url.split("?")[0].rsplit("/", 1)[-1] or "inline_image"
        add(url, name)
    return files


# ---------------------------------------------------------------------------
# HTML -> text conversion (stdlib only)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "blockquote", "pre")


def html_to_text(content):
    if not content:
        return ""
    s = content
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    for t in _BLOCK_TAGS:
        s = re.sub(r"(?i)</?%s[^>]*>" % t, "\n", s)
    s = re.sub(r"(?i)<img[^>]*?alt=[\"']([^\"']*)[\"'][^>]*>", r"[image: \1]", s)
    s = re.sub(r"(?i)<img[^>]*>", "[image]", s)
    s = re.sub(r"(?i)<(script|style)[^>]*>.*?</\1>", "", s, flags=re.S)
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
        self.bytes_ok = 0      # finished files
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
        self._last_len = 0        # visible length of the last progress line

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
                self.bytes_ok += size
            elif status == "cancelled":
                self.cancelled += 1
            else:
                self.failed += 1
                self.failed_names.append(name)
            self.done += 1

    def draw(self):
        with self.lock:
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
            vis = vlen(msg)
            if vis < self._last_len:
                msg += " " * (self._last_len - vis)
            self._last_len = vis
            sys.stdout.write("\r" + msg)
            sys.stdout.flush()

    def finish_line(self):
        with self.lock:
            sys.stdout.write("\r" + " " * (self._last_len + 2) + "\r")
            sys.stdout.flush()
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


class Downloader:
    def __init__(self, http, stats):
        self.http = http
        self.stats = stats
        self._cancel = threading.Event()

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
            status, size = self.http.download_to_file(url, dest, cb)
        except DownloadCancelled:
            status, size = "cancelled", 0
            warn("cancelled: %s" % name)
        except Exception as e:  # defensive: never kill the pool
            status, size = "failed", 0
            warn("exception downloading %s: %s" % (url, e))
        with self.stats.lock:
            self.stats.bytes_now -= last[0]
            last[0] = 0
        self.stats.update(status, name, size)
        self.stats.draw()

    def run(self, jobs):
        """jobs: list of (url, absolute_dest_path)."""
        if not jobs:
            info("nothing to download")
            return
        self.stats.add_total(len(jobs))
        self._cancel.clear()
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_WORKERS) as pool:
            futures = [pool.submit(self._worker, url, dest)
                       for url, dest in jobs]
            try:
                for f in concurrent.futures.as_completed(futures):
                    pass
            except KeyboardInterrupt:
                warn("interrupted - aborting downloads (partials are "
                     "discarded: the server cannot resume them)...")
                self._cancel.set()
                for f in futures:
                    f.cancel()
        self.stats.finish_line()


# ---------------------------------------------------------------------------
# post persistence (directory layout + metadata export)
# ---------------------------------------------------------------------------

def post_dir_path(cfg, post, creator_name=None):
    dd = cfg["download_dir"]
    service = post.get("service", "?")
    user = post.get("user", "?")
    cname = sanitize(creator_name or user, str(user), 60)
    published = post.get("published") or post.get("added") or ""
    date = str(published)[:10] or "unknown-date"
    title = sanitize(post.get("title"), "untitled", 80)
    folder = "%s_%s" % (date, title)
    return os.path.join(dd, service, "%s (%s)" % (cname, user), folder)


def write_post_metadata(cfg, post, dest_dir, creator_name=None):
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
            if f.get("preview_only"):
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
            "- **URL**: %s/patreon/user/%s/post/%s" % (
                SITE_BASE, post.get("user"), post.get("id")),
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


def build_post_jobs(cfg, post, creator_name=None):
    """Return (dest_dir, [(url, dest), ...]) for one full post.

    NOTE: existing files keep their original path so the downloader's
    skip-if-exists logic works on re-runs. Same-post name collisions are
    already resolved inside collect_post_files().
    """
    dest = post_dir_path(cfg, post, creator_name)
    jobs = []
    for url, name in collect_post_files(post, cfg.get("download_preview_files")):
        jobs.append((url, os.path.join(dest, name)))
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

    def get(self, force=False):
        if self._data and not force:
            return self._data
        if not force and self._fresh(CREATORS_CACHE):
            try:
                with open(CREATORS_CACHE, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
                return self._data
            except (OSError, ValueError):
                pass
        info("downloading creator index (~16 MB, one-time, then cached)...")
        data = self.api.creators()
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CREATORS_CACHE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        self._data = data
        info("cached %d creators to %s" % (len(data), CREATORS_CACHE))
        return data

    def search(self, query, service=None, limit=60):
        data = self.get()
        q = query.lower().strip()
        out = []
        for c in data:
            if service and c.get("service") != service:
                continue
            if q and q not in (c.get("name") or "").lower() \
                    and q not in str(c.get("public_id") or "").lower() \
                    and q not in str(c.get("id") or ""):
                continue
            out.append(c)
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
    * 3 automatic retries with backoff on transient errors,
    * every post is saved with post.json + post.md + content.txt metadata,
    * deferred video attachments (signed temporary MP4 URLs) are
      downloaded directly; the video server does not support resume,
      so an interrupted video restarts from the beginning next run,
    * inline <img> images inside post bodies are downloaded too
      (preview-only thumbnails are skipped; toggle in Settings [8]).

  Folder layout:
    {download_dir}/{service}/{creator} ({id})/{date}_{title}/
      file1.png, file2.png, ..., post.json, post.md, content.txt
"""


def display_post_row(i, p, creator_names=None):
    title = sanitize(p.get("title") or "(untitled)", "(untitled)", 56)
    f = p.get("file") or {}
    files = (1 if f.get("path") and not f.get("preview_only") else 0) \
        + len(p.get("attachments") or [])
    date = str(p.get("published") or "")[:10]
    print("  %s %s %s  %s  %s" % (
        c("%3d." % i, ACCENT2),
        c("[%s]" % p.get("service", "?"), DIM),
        c(str(p.get("user", "?")), DIM),
        title,
        c("%s | %d file(s)" % (date or "?", files), ACCENT)))


def fetch_full_posts(api, summaries):
    """Fetch full versions of the selected post summaries."""
    posts = []
    for p in summaries:
        try:
            posts.append(api.post(p["service"], p["user"], p["id"]))
        except HttpError as e:
            warn("could not fetch post %s: %s" % (p.get("id"), e))
    return posts


def resolve_creator_names(api, cache, posts):
    """Best-effort lookup of creator names from the cached creator index."""
    names = {}
    try:
        creators = cache.get()
        index = {(c.get("service"), str(c.get("id"))):
                 (c.get("name") or c.get("public_id") or "") for c in creators}
        for p in posts:
            key = (p.get("service"), str(p.get("user")))
            if key in index and index[key]:
                names[str(p.get("user"))] = index[key]
    except HttpError:
        pass
    return names


def download_posts(cfg, api, dl, posts, creator_names=None):
    """Fetch full posts (if summaries), then download everything."""
    if not posts:
        info("nothing selected")
        return
    creator_names = dict(creator_names or {})
    if any("content" not in p for p in posts):
        posts = fetch_full_posts(api, posts)
    missing = {str(p.get("user")) for p in posts
               if str(p.get("user")) not in creator_names}
    if missing and not creator_names:
        creator_names.update(resolve_creator_names(api,
                CreatorCache(cfg, api), posts))
    all_jobs = []
    for post in posts:
        dest, jobs = build_post_jobs(cfg, post,
                                     creator_names.get(str(post.get("user"))))
        if cfg.get("save_post_json") or cfg.get("save_content_txt") \
                or cfg.get("save_post_md"):
            write_post_metadata(cfg, post, dest,
                                creator_names.get(str(post.get("user"))))
        all_jobs.extend(jobs)
    if not all_jobs:
        info("selected posts contain no downloadable files "
             "(metadata still saved)")
        return
    head("downloading %d file(s) from %d post(s)"
         % (len(all_jobs), len(posts)))
    dl.run(all_jobs)


# --- 1. creator search ------------------------------------------------------

def search_creators_menu(cfg, api, cache, dl):
    head("creator search")
    query = ask("creator name / public_id / id (blank = browse all)")
    service = ask("filter service: p=patreon, f=fanbox, blank=all")
    service = {"p": "patreon", "f": "fanbox"}.get((service or "").lower(),
                                                  service or None)
    results = cache.search(query or "", service)
    if not results:
        warn("no creators matched")
        return
    print("")
    for idx, cr in enumerate(results, 1):
        name = (cr.get("name") or "?")[:40]
        print("  %s %-9s %-40s %s  %s  %s" % (
            c("%3d." % idx, ACCENT2),
            c("[%s]" % cr.get("service"), DIM), name,
            c("id=%s" % cr.get("id"), DIM),
            c("fav=%s" % cr.get("favorited"), DIM),
            c("upd=%s" % fmt_ts(cr.get("updated")), DIM)))
    print("")
    raw = ask("number(s) to open (e.g. 2 or 1-3), blank=back")
    if not raw:
        return
    try:
        picks = parse_selection(raw, len(results))
    except ValueError as e:
        warn(e)
        return
    for idx in picks:
        creator_menu(cfg, api, cache, dl, results[idx])


def creator_menu(cfg, api, cache, dl, creator):
    cname = creator.get("name") or creator.get("public_id") or creator["id"]
    service = creator["service"]
    uid = creator["id"]
    name_map = {str(uid): cname}
    offset = 0
    while True:
        head("%s - %s (id %s)" % (service, cname, uid))
        info("showing posts from offset %d (50 per page)" % offset)
        try:
            posts = api.creator_posts(service, uid, offset=offset)
        except HttpError as e:
            warn("could not list posts: %s" % e)
            return
        if not posts:
            if offset == 0:
                warn("this creator has no visible posts")
                return
            warn("no more posts at this offset")
            offset -= CREATOR_PAGE
            continue
        print("")
        for i, p in enumerate(posts, 1):
            display_post_row(i, p, name_map)
        print("")
        print("  %s next page   %s prev page   %s download ALL posts of this creator"
              % (c("[n]", ACCENT2), c("[p]", ACCENT2), c("[a]", ACCENT2)))
        raw = ask("pick number(s) (e.g. 1,3-5) | n | p | a | q=menu")
        if not raw or raw.lower() == "q":
            return
        low = raw.lower()
        if low == "n":
            offset += CREATOR_PAGE
            continue
        if low == "p":
            offset = max(0, offset - CREATOR_PAGE)
            continue
        if low == "a":
            download_creator(cfg, api, dl, service, uid, cname)
            continue
        try:
            picks = parse_selection(raw, len(posts))
        except ValueError as e:
            warn(e)
            continue
        download_posts(cfg, api, dl, [posts[i] for i in picks], name_map)


def download_creator(cfg, api, dl, service, uid, cname=None):
    """Fetch every page of a creator's posts and download all files."""
    limit_raw = ask("max posts to archive (blank = all)")
    limit = None
    if limit_raw:
        try:
            limit = max(1, int(limit_raw))
        except ValueError:
            warn("not a number - archiving all posts")
    if ask("confirm archive of %s/%s" % (service, uid),
           default="n").lower() not in ("y", "yes"):
        return
    name_map = {str(uid): cname} if cname else {}
    posts = []
    offset = 0
    while True:
        try:
            page = api.creator_posts(service, uid, offset=offset)
        except HttpError as e:
            warn("stopped: %s" % e)
            break
        if not page:
            break
        for p in page:
            name_map.setdefault(str(p.get("user")),
                                cname or p.get("user"))
        posts.extend(page)
        offset += CREATOR_PAGE
        if limit and len(posts) >= limit:
            posts = posts[:limit]
            break
        info("fetched %d posts so far..." % len(posts))
    if not posts:
        warn("no posts found for %s/%s" % (service, uid))
        return
    info("%d posts queued - full post data will be fetched one by one" %
         len(posts))
    download_posts(cfg, api, dl, posts, name_map)


# --- 2. recent / search -----------------------------------------------------

def recent_search_menu(cfg, api, cache, dl):
    head("recent posts / global search")
    query = ask("search query (blank = recent posts)")
    offset = 0
    while True:
        try:
            posts = api.recent_or_search(query or None, offset)
        except HttpError as e:
            warn("could not list posts: %s" % e)
            return
        if not posts:
            if offset == 0:
                warn("no posts found")
                return
            warn("no more posts at this offset")
            offset -= RECENT_PAGE
            continue
        info("%d posts (offset %d). %s" % (len(posts), offset,
             "search=%r" % query if query else "recent"))
        print("")
        for i, p in enumerate(posts, 1):
            display_post_row(i, p)
        print("")
        print("  %s next page   %s prev page   %s download ALL on this page"
              % (c("[n]", ACCENT2), c("[p]", ACCENT2), c("[a]", ACCENT2)))
        raw = ask("pick number(s) (e.g. 1,3-5) | n | p | a | q=menu")
        if not raw or raw.lower() == "q":
            return
        low = raw.lower()
        if low == "n":
            offset += RECENT_PAGE
            continue
        if low == "p":
            offset = max(0, offset - RECENT_PAGE)
            continue
        if low == "a":
            download_posts(cfg, api, dl, list(posts))
            continue
        try:
            picks = parse_selection(raw, len(posts))
        except ValueError as e:
            warn(e)
            continue
        download_posts(cfg, api, dl, [posts[i] for i in picks])


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
    info('fetched: "%s" (%s, %s)' % (post.get("title"), service,
                                     post.get("published")))
    files = collect_post_files(post)
    info("%d downloadable file(s), %d chars of text" %
         (len(files), len(html_to_text(post.get("content")))))
    if ask("download to %s?" % cfg["download_dir"], default="y").lower() \
            not in ("y", "yes"):
        return
    download_posts(cfg, api, dl, [post])


# --- 4. whole creator by hand ----------------------------------------------

def quick_creator_menu(cfg, api, dl):
    head("download whole creator")
    raw = ask("creator URL (https://pawchive.pw/patreon/user/436247) or "
              "'service id' (e.g. patreon 436247)")
    if not raw:
        return
    m = re.search(r"([a-z]+)/user/(\w+)", raw.strip())
    if m:
        service, uid = m.group(1), m.group(2)
    else:
        parts = raw.split()
        if len(parts) == 2:
            service, uid = parts[0].lower(), parts[1]
        else:
            warn("could not parse input")
            return
    cname = None
    try:
        cache_match = None
        for c in CreatorCache(cfg, api).get():
            if c.get("service") == service and str(c.get("id")) == str(uid):
                cache_match = c.get("name")
                break
        if cache_match:
            cname = cache_match
            info("creator: %s" % cname)
    except HttpError:
        pass
    download_creator(cfg, api, dl, service, uid, cname)


# --- 5. settings ------------------------------------------------------------

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
            if ua:
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
        elif choice in ("0", "", None):
            save_config(cfg)
            return
        save_config(cfg)


def first_run_setup(cfg):
    if cfg["user_agent"]:
        return cfg
    print("")
    head("first-run setup")
    print("""  The pawchive file server requires an *identifiable* User-Agent
  (not a browser string) plus polite rate limits (1 req/s, max 5 downloads).
  This script enforces those limits. Please enter a contact identifier,
  for example:  pawdl/1.0 (myname@example.com)""")
    ua = ask("User-Agent")
    if not ua:
        ua = "my-archiver/1.0 (personal backup; contact: you@example.com)"
        warn("no contact given - using %r; downloads may be blocked" % ua)
    cfg["user_agent"] = ua
    d = ask("download folder", cfg["download_dir"])
    cfg["download_dir"] = os.path.abspath(os.path.expanduser(d or
                                              cfg["download_dir"]))
    save_config(cfg)
    info("config saved to %s" % CONFIG_FILE)
    return cfg


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    cfg = first_run_setup(cfg)
    if not cfg["user_agent"]:
        warn("no User-Agent set - downloads on pawchive will likely fail "
             "(run Settings to set one)")
        return 1

    http = HttpClient(cfg["user_agent"], cfg.get("max_retries",
                                                 DEFAULT_MAX_RETRIES))
    api = PawchiveAPI(http)
    cache = CreatorCache(cfg, api)
    dl = Downloader(http, DownloadStats())

    while True:
        cls()
        print("")
        banner_box([
            c("pawdl", "1;96") + c("  \u2502  ", DIM)
            + c("pawchive.pw interactive downloader", "1;97"),
            c("max 5 parallel \u00b7 1 req/s \u00b7 skip existing \u00b7 retry+backoff", DIM),
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
        try:
            if choice == "1":
                search_creators_menu(cfg, api, cache, dl)
            elif choice == "2":
                recent_search_menu(cfg, api, cache, dl)
            elif choice == "3":
                single_post_menu(cfg, api, dl)
            elif choice == "4":
                quick_creator_menu(cfg, api, dl)
            elif choice == "5":
                settings_menu(cfg, api)
            elif choice == "6":
                head("help")
                print(HELP_TEXT)
            elif choice in ("0", "", None):
                print(c("bye", DIM))
                return 0
        except KeyboardInterrupt:
            print("")
            warn("interrupted - returning to menu")
        except HttpError as e:
            print("")
            warn("API error: %s" % e)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        sys.exit(130)


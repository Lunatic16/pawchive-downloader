# 🐾 pawdl

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg) ![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg) ![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

An interactive command-line downloader for the [pawchive.pw](https://pawchive.pw) archive API. Browse creators, search posts, open your favourites, and archive files with a terminal UI — using nothing but the Python standard library.

![pawdl terminal UI](docs/screenshots/screenshot.png)

## ✨ Features

- 🖥️ **Interactive terminal UI** — ANSI-styled menus, rounded boxes, high-contrast status output for dark terminals, and a live progress bar with percentage, file count, and transfer speed.
- 🔍 **Creator search** — search ~99k patreon/fanbox creators by name, public ID, or numeric ID (first 60 matches), then browse their posts 50 per page with `[n]`/`[p]` paging.
- 🌐 **Global post search** — full-text search across the post index or browse recent uploads, 150 results per page.
- ⭐ **Favourites** — sign in with your session cookie to browse and download the creators and posts you favourited on the site.
- 🎯 **Single post download** — paste any post URL (`https://pawchive.pw/patreon/user/ID/post/POSTID`) and grab everything in it.
- 📚 **Whole-creator archiving** — queue every post of a creator (or only the first N) from their page, or enter a creator URL / `service id` directly.
- ☑️ **Multi-select** — lists accept selections like `1,3-5 8`.
- 🗂️ **Complete file coverage** — regular attachments, deferred video attachments (temporary signed MP4 URLs), and inline `<img>` images inside post bodies. A file referenced twice in a post is downloaded once. Preview-only thumbnails are skipped by default.
- 📝 **Metadata export** — each post can be saved with `post.json`, `post.md`, and `content.txt` alongside its files.
- ⏯️ **Safe re-runs** — already-downloaded files are skipped, and incomplete transfers are never kept as finished files.
- 🔁 **Retries with backoff** — rate-limit, server, and network errors are retried automatically; permanent errors such as 404 or 410 stop immediately.
- ⚡ **Creator cache** — the ~16 MB creator index is cached locally (24 hours by default). If a refresh fails, the older copy is used.

## 📦 Installation

pawdl is a single Python script with zero third-party dependencies, so installing it is just a matter of getting the file onto your machine.

**1. Get Python**

Check that Python 3.8 or newer is available:

```sh
python3 --version
```

**2. Get the script**

Either clone the repository:

```sh
git clone https://github.com/Lunatic16/pawchive-downloader.git
cd pawchive-downloader
```

or grab only the script itself:

```sh
curl -O https://raw.githubusercontent.com/Lunatic16/pawchive-downloader/main/pawdl.py
```

**3. Run it**

```sh
python3 pawdl.py
```

**Optional — install onto your PATH**

To launch pawdl from anywhere with a single command:

```sh
sudo install -m 755 pawdl.py /usr/local/bin/pawdl
pawdl
```

On Windows without WSL, download `pawdl.py` and run it with `py pawdl.py` from PowerShell or CMD.

## 🚀 Quick start

```sh
python3 pawdl.py
```

The first run asks for two things:

1. **User-Agent** — press Enter to accept the default, `my-archiver/1.0 (personal backup; contact: you@example.com)`, or type your own. `you@example.com` is a placeholder; the file server asks for a real contact, so replacing it is recommended.
2. **Download folder** — defaults to `pawdl_downloads` inside the directory you run the script from.

Settings are stored in `~/.config/pawdl/config.json` (`%APPDATA%\pawdl\config.json` on Windows). Set the `PAWDL_CONFIG_DIR` environment variable to use another location.

## 🧭 Menu overview

| Key | Action |
| --- | --- |
| `1` | Search creators and browse their posts |
| `2` | Recent posts / global post search |
| `3` | Download a single post by URL |
| `4` | Download a whole creator by URL or `service id` |
| `5` | Settings (User-Agent, folder, cache TTL, toggles, session cookie) |
| `6` | Help |
| `7` | My favourites (needs your session cookie) |
| `0` | Exit |

Post lists support multi-select (`1,3-5 8`), page navigation (`[n]` / `[p]`), bulk download (`[a]`), and `q` to go back. In a creator's post list, `[a]` archives **all** of that creator's posts; in search results and favourite posts, it downloads the current page.

## ⭐ Favourites

Menu `7` shows the creators and posts favourited on your pawchive.pw account, newest favourite first where the site provides that information.

### Getting your session cookie

1. Log in at [pawchive.pw](https://pawchive.pw) in your browser.
2. Open the developer tools (`F12`).
3. Go to **Storage** (Firefox) or **Application** (Chrome), then **Cookies** → `https://pawchive.pw`.
4. Copy the value of the cookie named `session`.
5. Choose `7` in pawdl and paste it. The input is hidden.

pawdl accepts the bare value, `session=…`, or a full `Cookie:` header line.

### Favourites menu

| Key | Action |
| --- | --- |
| `1` | Favourite creators — open one to browse and download its posts |
| `2` | Favourite posts — download selected posts or a whole page |
| `3` | Reload from the site |
| `4` | Change the session cookie |
| `5` | Forget the session cookie |
| `0` | Back |

### Cookie security

> [!WARNING]
> Your session cookie gives access to your account. Treat it like a password: never share it, paste it into an issue, or commit your `config.json`.

- You choose whether to save it. By default it is kept only until you quit.
- A saved cookie is stored **in plain text** in `config.json`. On Linux and macOS the file is readable only by your user.
- It is sent only with favourites requests to the pawchive.pw API. It is not sent to file servers, does not follow redirects, and is not printed.

## 🤝 Server etiquette

pawchive.pw asks automated clients to behave politely, and pawdl enforces these limits:

- maximum of **5 parallel downloads**
- at least **1 second between file requests** (rate-limited globally across all workers)
- an **identifiable User-Agent** — browser strings and empty agents can be rejected with 403

Please do not disable or bypass these limits; abusive traffic gets IPs blocked and hurts the archive.

## 📥 Download behaviour

### Folder layout

```text
{download_dir}/
└── {service}/
    └── {creator} ({id})/
        └── {date}_{title}/
            ├── file1.png
            ├── file2.png
            ├── post.json
            ├── post.md
            ├── content.txt
            └── .pawdl_post_id
```

- `.pawdl_post_id` is a small hidden file that records which post owns a folder. If two posts share a date and title, the later one gets `_{post id}` added to its folder name.
- Long file and folder names are shortened to fit filesystem limits; file extensions are kept.
- Attachments named like a metadata file (for example `content.txt`) are renamed so they don't clash.

### Transfers

- Posts are processed in batches of 10, so temporary video links are less likely to expire while queued. The progress total grows as each batch is added.
- Each file downloads to a `.part` file and is renamed only once complete. When the server reports a size, it must match.
- Downloads use a global rate limiter, a 5-worker thread pool, and up to 4 retries with escalating delays (`max_retries` in `config.json`).
- The server does not support resume, so an interrupted file restarts from the beginning on the next run.
- `Ctrl+C` stops the current job, discards partial files, and returns to the menu.
- Inline images in the post body are rewritten from thumbnail URLs to full-size originals. Only images hosted on pawchive.pw are fetched.

## 🖥️ Terminal UI details

The interface auto-detects its capabilities and degrades gracefully:

- Colors are disabled automatically when output is not a terminal, when `NO_COLOR` is set, or when `TERM=dumb` — piped and logged output stays clean.
- Unicode box and bar glyphs fall back to plain ASCII on terminals that cannot encode them.
- On narrow terminals, the progress bar shrinks to a short counter.
- On Windows, the classic console is switched to ANSI escape mode at startup.

## ⚙️ Settings reference

Open menu `5` and enter the key. Changes are saved immediately.

| Key | Setting | Description | Default |
| --- | --- | --- | --- |
| `1` | User-Agent | Contact identifier sent with every request (plain ASCII) | `my-archiver/1.0 (personal backup; contact: you@example.com)` |
| `2` | Download folder | Root directory for all archived posts | `./pawdl_downloads` |
| `3` | Creators cache TTL | Hours before the creator index is re-fetched | 24 h |
| `4` | `save_post_json` | Raw API response per post | on |
| `5` | `save_content_txt` | Post body converted to plain text | on |
| `6` | `save_post_md` | Formatted Markdown summary per post | on |
| `7` | `download_inline_images` | Fetch images embedded in the post body | on |
| `8` | `download_preview_files` | Fetch preview-only thumbnails (often dead links) | off |
| `s` | Session cookie | Set or remove the cookie used for favourites | not set |
| `9` | Refresh creator cache | Re-download the creator index now | — |
| `0` | Back | Return to the main menu | — |

## 🩺 Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `not logged in … cookie is wrong or expired` | Log in again in your browser and paste the new `session` value. |
| `unexpected favourites format` or `unrecognised favourite entries` | The site's response differs from what pawdl expects. Open an issue with the message, but never include your cookie. |
| HTTP 403 on downloads | Set a User-Agent with a real contact, wait a while, and try again. |
| `dead or expired URL` | The file is unavailable. For videos, run the post again to get a fresh link. |
| An old folder holds files from two posts | Delete the folder and download both posts again. |

## ⚠️ Disclaimer

This tool is intended for personal archiving. Respect the archive's terms of service, the file server's rate limits, and the rights of the original creators.

## 📄 License

Released under the [MIT License](LICENSE).

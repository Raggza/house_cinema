<div align="center">

# 🎬 House Cinema

**A self-hosted video hub for your home network.**
Point it at your movie folders on one computer, then watch from any phone, computer or smart TV
on the same Wi-Fi, right in the browser.

**English** · [Tiếng Việt](README.vi.md)

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)
![Version](https://img.shields.io/badge/version-0.1.0-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

## Table of contents

- [Why House Cinema?](#why-house-cinema)
- [Features](#features)
- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Watching on a smart TV](#watching-on-a-smart-tv)
- [Configuration](#configuration)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgements](#acknowledgements)

## Why House Cinema?

- **Nothing to install on the devices.** Every phone, tablet, computer and TV just opens a web page.
- **No Internet, no cloud, no account.** Everything stays on your local network; nothing is uploaded.
- **Read-only on your files.** The hub never moves, renames or changes your videos.
- **Plays (almost) anything.** Formats a browser can't play are converted on the fly with ffmpeg.

> The web interface is in **Vietnamese**. UI labels quoted in this README are shown as
> *Vietnamese label* (English meaning).

## Features

| | |
|---|---|
| 📁 **Library** | Several folders (local drives, USB, network shares), thumbnails, video details, folder browsing, search that ignores Vietnamese diacritics, sorting. Supports `.mp4 .m4v .webm .mkv .avi .mov` |
| ▶️ **Player** | Seek, fullscreen, picture-in-picture, keyboard shortcuts, **resume where you stopped** on each device |
| 💬 **Subtitles** | `.srt` / `.vtt` next to the video, language detection from the file name, legacy encodings (e.g. Windows-1258) decoded correctly |
| 🔄 **On-the-fly conversion** | Remux (lossless, cached) or transcode to H.264/HLS when a browser can't play the file (MKV on iPhone, HEVC, AV1, AC-3…). Optional GPU encoders |
| 📜 **Playlists** | Play a whole folder or a hand-picked selection, auto-next, shuffle, repeat |
| 📺 **Smart TV + phone remote** | The TV shows the film, the phone controls it (play/pause, seek, subtitles, volume, playlist) |
| 🔐 **Access control** | LAN-only by default, optional PIN, admin page reachable only from the server |
| 🖥️ **Desktop app** | Windows installer and macOS disk image with ffmpeg bundled, tray / menu-bar icon |
| 📱 **Easy connection** | The hub finds its LAN address and shows a QR code to scan with a phone |

## How it works

```
                 ┌──────────────────────────────┐
  Movie folders ─┤  Server (Windows / macOS)    │
  D:\Movies      │  House Cinema hub  :8000     │
  \\nas\Films    │  FastAPI + ffmpeg + SQLite   │
                 └──────────────┬───────────────┘
                                │  home Wi-Fi / Ethernet (HTTP)
            ┌───────────────────┼────────────────────┐
            ▼                   ▼                    ▼
       📱 Phone            💻 Laptop             📺 Smart TV
       browser / remote    browser               browser at /tv
```

The hub scans your folders into a local SQLite index, serves the files with HTTP `Range`
(so players can seek), and, when a device can't decode a file, converts it to HLS segments in
real time.

## Quick start

**Desktop app (recommended):** download the installer from this repository's **Releases**
page, install it, and the admin page opens in your browser. Add a folder, press *Quét lại*
(Rescan), then scan the QR code with your phone.

**From source (Windows):**

```powershell
winget install --id=astral-sh.uv -e        # Python toolchain (downloads Python by itself)
winget install Gyan.FFmpeg                 # optional, recommended
git clone <repository-url> house_cinema
cd house_cinema
.\run.cmd                                  # then open http://localhost:8000/admin
```

## Installation

### Option 1 — Desktop app (Windows / macOS)

Everything is bundled, ffmpeg included.

| System | File | Notes |
|---|---|---|
| Windows 10/11 x64 | `HouseCinema-<version>-Windows-x64-Setup.exe` | Unsigned: on *"Windows protected your PC"* click **More info › Run anyway**. Keep **"Allow phones, computers and TVs on my home network to connect"** ticked (adds a firewall rule for private networks) |
| macOS 11+ (Apple Silicon) | `HouseCinema-<version>-macOS-arm64.dmg` | Drag to **Applications**. Not notarized: first time, **right-click › Open › Open** (macOS 15+: *System Settings › Privacy & Security › Open Anyway*) |
| macOS 11+ (Intel) | `HouseCinema-<version>-macOS-x86_64.dmg` | Same as above. Click **Allow** if macOS asks about incoming connections / local network |

The app runs in the background with an icon in the **system tray** (Windows) or **menu bar** (macOS):

- **Double-click** the tray icon (Windows): show/hide the console window (address, QR code, logs).
  Closing that window does **not** stop the hub.
- **Right-click** (Windows) / click (macOS): open the library or admin page, copy the address for
  phones, open the data/settings folder, restart the hub, **Thoát** (Quit).

Uninstalling keeps your library and settings; delete the [data folder](#configuration) to remove them.

### Option 2 — From source

Requirements:

- [**uv**](https://docs.astral.sh/uv/): installs the right Python (≥ 3.12) and dependencies automatically.
  Without winget: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.
- [**ffmpeg**](https://ffmpeg.org/) *(optional, recommended)*: thumbnails, video details and conversion.
  Without it the hub still runs, lists what is disabled at startup and on the admin page, and uses a
  default thumbnail.

Then double-click **`run.cmd`** (or `.\run.cmd` in a terminal). The first run takes 1–2 minutes
while uv downloads Python and the libraries. If Windows Firewall asks about Python, allow
**Private networks**. Stop with **Ctrl+C**.

On macOS/Linux, run the hub directly: `uv run --no-dev python -m source.pipeline`.

## Usage

### 1. Add your folders

Open **`http://localhost:8000/admin`** on the server:

- Press **Thêm** (Add) with an empty box to open the system folder picker, **or**
- paste a path, e.g. `D:\Movies` or `\\nas\Films`, and press **Thêm**.

**Bỏ** (Remove) only removes a folder from the hub; files on disk are never touched.

### 2. Scan the library

On `http://localhost:8000/`, press **Quét lại** (Rescan). A progress bar shows the two steps:
finding videos, then reading details and generating thumbnails. Rescan after adding or removing films.

### 3. Watch on other devices

The terminal / console window and the admin page show the address for other devices
(e.g. `http://192.168.1.10:8000/`) and a **QR code**. Scan it and bookmark the page.

| In the player | |
|---|---|
| Keyboard | `Space`/`K` play-pause · `←`/`→` (`J`/`L`) ±10 s · `F` fullscreen · `C` subtitles · `M` mute · `P`/`N` previous/next |
| **Phát trên TV** | Continue on a smart TV |
| **Link cho VLC** | Copy the stream URL for VLC (*Media › Open Network Stream*) |

### Playlists

- **Phát tất cả** (Play all) in a folder plays its videos in natural order ("Ep 2" before "Ep 10").
- **Chọn** (Select) lets you pick videos across folders and search results; they play in the order picked.
- Auto-next after 5 s, **Ngẫu nhiên** (shuffle), repeat off / all / one. Unplayable videos are
  skipped (the playlist stops after 3 in a row). Up to 500 videos; survives a page reload.

### Subtitles

Put `.srt` or `.vtt` files next to the video with the same name, optionally with a language tag —
no rescan needed:

```
Movie.mkv
Movie.srt            Movie.vi.srt
Movie.eng.srt        Movie.Vietnamese.vtt
```

Vietnamese subtitles are preferred when available; the user's choice is remembered.

### Unsupported formats

When a browser can't play a file, the hub converts it while you watch (needs ffmpeg) and the page
shows *"Đang chuẩn bị video…"* (Preparing video…) for a few seconds:

- **Remux** when the video stream can be kept: fast, lossless, cached in `cache/transcode/`.
- **Transcode** to H.264 otherwise.
- At most 2 conversions at a time (`transcode.max_sessions`); extra requests get
  *"Hub đang bận"* (Hub is busy) with a retry button.

Typical cases: iPhone Safari (MKV, AV1), Edge without HEVC Video Extensions (HEVC), Chrome and
some TVs (AC-3 audio without sound).

## Watching on a smart TV

TV browsers are clumsy with a remote, so the TV only shows the film and **your phone is the remote**.

1. On the TV browser, open **`http://<address>:8000/tv`**. Type `http://` in full — TV browsers
   often switch to `https://`, which the hub does not serve.
2. Press **Bắt đầu** (Start) once with the TV cursor (enables fullscreen and sound).
3. On the phone, connect by scanning the QR code on the TV, via **Điều khiển TV** (TV remote) on
   the library page, or by typing the 4-letter code.
4. Open a film or a selection on the phone and press **Phát trên TV**. The phone controls
   play/pause, ±10 s, seeking, subtitles, volume, mute, stop and the playlist.

Reloading `/tv` or restarting the hub keeps the same TV pairing. Tested on a Sony Android TV with
BrowseHere and TV Bro.

## Configuration

Most people need no configuration. Settings are read in this order (later wins):

`source/configs/config.yml` → `source/configs/.env` (copy from `.env.example`) → environment
variables → command-line options (e.g. `.\run.cmd --port 8080`). Restart the hub after a change.

The **desktop app** stores settings (`configs/`), `data/`, `cache/` and `logs/` in a per-user data folder:

| System | Data folder |
|---|---|
| Windows | `%LOCALAPPDATA%\HouseCinema` |
| macOS | `~/Library/Application Support/HouseCinema` |

Set `HOUSE_CINEMA_HOME` to use another location (app and `run.cmd`).

| Setting | Where | Default | Description |
|---|---|---|---|
| Library folders | admin page | — | Saved in `data/library-paths.json`. `library.paths` / `HUB_LIBRARY_PATHS` only seed the first run. `--path` overrides them for one run (admin page becomes read-only) |
| Port | `server.port`, `--port` | `8000` | Open the new port in the firewall too |
| PIN | `HUB_PIN` in `.env` | off | 4–32 characters, see [Security](#security) |
| ffmpeg | `media.ffmpeg_path` | from `PATH` | ffprobe is looked up next to it |
| Conversion | `transcode.*` | on | `enabled`, `max_sessions` (2), `video_encoder` (`libx264`, or `h264_nvenc` / `h264_qsv` / `h264_amf`), `max_height` (1080), `idle_seconds` (60), `cache_gb` (20) |
| LAN only | `access.lan_only` | on | Public IP addresses get `403` |

## Security

- **Home network only.** Private ranges (`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`), link-local
  and VPN ranges such as Tailscale (`100.64.0.0/10`) are allowed. Only the socket address counts,
  never a header.
- ⚠️ **Never forward the hub's port on your router.** It is designed for the LAN and serves plain HTTP.
- **Optional PIN:** set `HUB_PIN=...` in `source/configs/.env`. Every device except the server must
  enter it; it is remembered for 30 days. 5 wrong attempts lock that device out for 5 minutes.
  Changing the PIN or deleting `data/hub-secret.key` signs every device out.
- **Admin page is server-only** (`localhost` or the server's own LAN address); other devices get
  `403` and the attempt is logged.
- **Only library files are served.** The API takes IDs, never paths. Path traversal, symlink /
  junction swaps and removed folders are refused with a uniform `404`, and logged.

## Troubleshooting

| Problem | Solution |
|---|---|
| `run.cmd`: *"Không tìm thấy uv"* (uv not found) | Install uv, then open a **new** terminal window |
| A phone can't connect | Same Wi-Fi (not a guest network)? Open `http://<address>:8000/health` on the phone — it should return `{"status":"ok"…}`. Otherwise check the firewall (below) |
| Terminal warns the network is **Public** | Windows *Settings › Network & Internet › Network profile* → **Private** |
| TV browser can't connect | Type `http://…` in full |
| No thumbnails / *"Định dạng chưa hỗ trợ"* (Unsupported format) / *"Không tìm thấy ffmpeg"* | Install ffmpeg, restart the hub, press **Quét lại** |
| *"Hub đang bận"* (Hub is busy) | Wait, use VLC, or raise `transcode.max_sessions` |
| *"port 8000 is already in use"* | The hub is already running, or use `.\run.cmd --port 8080` |
| Videos show *"Ổ đang ngắt"* (Drive disconnected) | Reconnect the drive and rescan (a different drive letter is fine) |
| App: grey tray icon with a red dot | The hub failed to start; the console shows why. Fix it, then right-click › **Khởi động lại hub** (Restart) |
| App: need the log | `<data folder>/logs/house-cinema.log` |
| *"the index was damaged"* | Nothing to do: it is backed up and rebuilt on the next rescan. Deleting `data/library.db` is always safe |

**Windows Firewall rule** (PowerShell as administrator):

```powershell
New-NetFirewallRule -DisplayName "House Cinema (TCP 8000)" -Direction Inbound -Protocol TCP `
  -LocalPort 8000 -Action Allow -Profile Private
# remove: Remove-NetFirewallRule -DisplayName "House Cinema (TCP 8000)"
```

## Development

### Tech stack

Python 3.12 · [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) ·
Pydantic · SQLite · ffmpeg/ffprobe · vanilla HTML/CSS/JS (no external front-end library, works
offline) · [segno](https://github.com/heuer/segno) (QR codes) · pystray (tray icon) ·
PyInstaller + Inno Setup (packaging) · pytest + Playwright (tests).

### Project structure

```
house_cinema/
├── run.cmd, run.ps1          # one-command start
├── pyproject.toml, uv.lock   # dependencies (uv)
├── source/
│   ├── pipeline.py           # entry point: server + CLI
│   ├── configs/              # config.yml, .env.example
│   ├── modules/
│   │   ├── library/          # scanning, SQLite index, media info
│   │   ├── server/           # API, streaming, access control, network, TV screens
│   │   ├── subtitles/        # discovery, encoding detection, SRT→VTT
│   │   └── transcode/        # remux / transcode plans and sessions
│   ├── desktop/              # tray / menu-bar app
│   ├── web/                  # web pages (library, player, admin, TV, remote)
│   ├── schemas/, utils/, image/
├── packaging/                # Windows installer & macOS dmg build scripts
├── scripts/                  # device_matrix.py
├── demo/                     # soak test, subtitle demo page
├── test/                     # pytest suite + sample library fixtures
└── .github/workflows/        # release builds
```

### Setup

```powershell
uv sync                                                  # .venv with dev tools
copy source\configs\.env.example source\configs\.env     # optional
uv run python -m source.pipeline                         # start the hub
uv run --group desktop python -m source.desktop          # the tray app, from source
```

If `.venv` is recreated, keep its prompt name: `uv venv --prompt house_cinema`, then `uv sync`.

### Command-line interface

```powershell
uv run python -m source.pipeline --check-config   # effective settings, ffmpeg status
uv run python -m source.pipeline scan             # scan and update the index (+ added, − removed, ~ renamed, ! offline)
uv run python -m source.pipeline scan --dry-run   # show changes only
uv run python -m source.pipeline list             # list the index
uv run python -m source.pipeline info 601fdf      # one video by ID prefix
```

`scan` options: `--no-media` (skip ffprobe/thumbnails), `--refresh-media` (redo all),
`--progress` / `--no-progress`, `--path` (override folders).

| Exit code | Meaning |
|---|---|
| `0` | OK |
| `2` | Configuration error |
| `3` | Server could not start (e.g. port in use) |
| `4` | Library index unusable |
| `5` | `info`: no single video matches the ID |

### API

Interactive OpenAPI docs: **`http://localhost:8000/docs`**. Main endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/library` | Paged video list (`q`, `folder`, `recursive`, `library`, `sort`, `offset`, `limit` ≤ 200) |
| `GET /api/folders` | Libraries and folders with counts |
| `GET /api/media/<id>` | Video details, stream / thumbnail URLs, subtitles |
| `GET`/`POST /api/rescan` | Rescan progress / start a background rescan |
| `GET /stream/<id>` | The video file, with HTTP `Range` |
| `GET /thumbnails/<id>.jpg` | Thumbnail |
| `GET /subtitles/<id>[/<key>.vtt]` | Subtitle tracks / one track as WebVTT |
| `POST /api/play/<id>` | Playback decision: `direct`, `remux` or `transcode` (HLS) |
| `POST /api/pin` | Unlock this device with the PIN |
| `/api/screens/...` | TV registration, phone → TV commands (SSE), TV state |
| `/api/admin/...` | Libraries, network/QR, tools, folder picker, running conversions — **server only** |

### Tests

```powershell
uv run python test/fixtures/make_library.py   # generate the sample library (needs ffmpeg)
uv run pytest
```

- Browser tests use Playwright with the installed **Microsoft Edge** (or Chrome); Playwright's
  bundled Chromium can't play H.264. Set `HUB_E2E_RECORD=1` to record traces into `test-artifacts/`.
- **Device test page:** `http://<address>:8000/diagnostics` on any device (disabled when
  `APP_ENV=production`) checks which formats it plays and records TV remote keys. Reports go to
  `data/device-reports/`; `uv run python scripts/device_matrix.py` builds a compatibility table.
- **Soak test** (leak detection under load):

  ```powershell
  uv run python demo/soak.py --minutes 60
  uv run python demo/soak.py --hub http://127.0.0.1:8000 --process HouseCinema.exe   # against the installed app
  ```

### Building the installers

```powershell
# Windows — needs uv and Inno Setup 6 (winget install JRSoftware.InnoSetup)
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   → dist\HouseCinema-<version>-Windows-x64-Setup.exe   (-SkipInstaller, -FfmpegDir <folder>)
```

```bash
# macOS — run on a Mac, builds for its architecture
bash packaging/build_macos.sh       # → dist/HouseCinema-<version>-macOS-<arm64|x86_64>.dmg
```

On GitHub: *Actions › Build apps › Run workflow*, or push a `v*` tag to attach the builds to a
release. The version comes from `pyproject.toml`.

> **ffmpeg licensing:** the bundled ffmpeg builds are **GPL** (they include x264). The app ships
> ffmpeg's licence and a `SOURCE.txt` pointing to its source; keep them if you change the build.
>
> **Code signing:** builds are unsigned, so SmartScreen / Gatekeeper warn on first launch.

## Roadmap

- [ ] Docker image (v2)
- [ ] Signed Windows builds and notarized macOS builds

## Contributing

Contributions are welcome!

1. Fork the repository and create a branch: `git checkout -b feature/my-change`.
2. Run `uv sync` and make sure `uv run pytest` passes.
3. Keep the web front-end dependency-free (it must work offline).
4. Open a pull request describing the change and how you tested it.

Please report bugs and ideas through the issue tracker, including your OS, browser/device and
the hub log when relevant. **Do not attach logs or device reports without removing LAN IP
addresses and other personal information.**

## License

House Cinema's source code is released under the [MIT License](LICENSE).

The desktop installers bundle third-party ffmpeg builds, which are distributed under the
**GPL**, not MIT (see [Building the installers](#building-the-installers)).

## Acknowledgements

[FastAPI](https://fastapi.tiangolo.com/) · [Uvicorn](https://www.uvicorn.org/) ·
[FFmpeg](https://ffmpeg.org/) · [uv](https://docs.astral.sh/uv/) ·
[segno](https://github.com/heuer/segno) · [pystray](https://github.com/moses-palmer/pystray) ·
[PyInstaller](https://pyinstaller.org/) · [Inno Setup](https://jrsoftware.org/isinfo.php) ·
[Playwright](https://playwright.dev/python/) · ffmpeg builds by
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) and Martin Riedl.

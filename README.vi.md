<div align="center">

# 🎬 House Cinema

**Hub video tự host cho mạng gia đình.**
Chỉ cho hub các thư mục phim trên một máy tính, rồi xem trên mọi điện thoại, máy tính hay smart TV
cùng Wi-Fi, ngay trong trình duyệt.

[English](README.md) · **Tiếng Việt**

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey)
![Version](https://img.shields.io/badge/version-0.1.0-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

## Mục lục

- [Vì sao dùng House Cinema?](#vì-sao-dùng-house-cinema)
- [Tính năng](#tính-năng)
- [Cách hoạt động](#cách-hoạt-động)
- [Bắt đầu nhanh](#bắt-đầu-nhanh)
- [Cài đặt](#cài-đặt)
- [Sử dụng](#sử-dụng)
- [Xem trên smart TV](#xem-trên-smart-tv)
- [Cấu hình](#cấu-hình)
- [Bảo mật](#bảo-mật)
- [Xử lý sự cố](#xử-lý-sự-cố)
- [Phát triển](#phát-triển)
- [Lộ trình](#lộ-trình)
- [Đóng góp](#đóng-góp)
- [Giấy phép](#giấy-phép)
- [Cảm ơn](#cảm-ơn)

## Vì sao dùng House Cinema?

- **Không cài gì trên thiết bị xem.** Điện thoại, máy tính bảng, máy tính, TV chỉ cần mở một trang web.
- **Không cần Internet, không cloud, không tài khoản.** Mọi thứ ở trong mạng nhà bạn, không tải lên đâu cả.
- **Chỉ đọc file của bạn.** Hub không bao giờ di chuyển, đổi tên hay sửa video.
- **Phát (gần như) mọi định dạng.** Định dạng trình duyệt không phát được sẽ được ffmpeg chuyển đổi ngay khi xem.

## Tính năng

| | |
|---|---|
| 📁 **Thư viện** | Nhiều thư mục (ổ trong, USB, thư mục mạng), ảnh thu nhỏ, thông tin video, duyệt theo thư mục, tìm kiếm không cần gõ dấu, sắp xếp. Hỗ trợ `.mp4 .m4v .webm .mkv .avi .mov` |
| ▶️ **Trình phát** | Tua, toàn màn hình, picture-in-picture, phím tắt, **xem tiếp chỗ đã dừng** trên từng thiết bị |
| 💬 **Phụ đề** | `.srt` / `.vtt` cạnh video, nhận ngôn ngữ theo tên file, đọc đúng bảng mã cũ (Windows-1258…) |
| 🔄 **Chuyển đổi khi xem** | Remux (không giảm chất lượng, có cache) hoặc transcode sang H.264/HLS khi trình duyệt không phát được (MKV trên iPhone, HEVC, AV1, AC-3…). Hỗ trợ encoder GPU |
| 📜 **Danh sách phát** | Phát cả thư mục hoặc các video tự chọn, tự chuyển bài, ngẫu nhiên, lặp |
| 📺 **Smart TV + điện thoại làm remote** | TV chỉ chiếu phim, điện thoại điều khiển (phát/dừng, tua, phụ đề, âm lượng, danh sách) |
| 🔐 **Kiểm soát truy cập** | Mặc định chỉ mạng LAN, PIN tuỳ chọn, trang quản trị chỉ mở được trên máy chủ |
| 🖥️ **Ứng dụng desktop** | Bộ cài Windows và file dmg macOS, có sẵn ffmpeg, biểu tượng ở khay hệ thống / thanh menu |
| 📱 **Kết nối dễ dàng** | Hub tự tìm địa chỉ LAN và hiện mã QR để quét bằng điện thoại |

## Cách hoạt động

```
                 ┌──────────────────────────────┐
  Thư mục phim  ─┤  Máy chủ (Windows / macOS)   │
  D:\Phim        │  Hub House Cinema  :8000     │
  \\nas\Phim     │  FastAPI + ffmpeg + SQLite   │
                 └──────────────┬───────────────┘
                                │  Wi-Fi / Ethernet trong nhà (HTTP)
            ┌───────────────────┼────────────────────┐
            ▼                   ▼                    ▼
       📱 Điện thoại       💻 Laptop             📺 Smart TV
       trình duyệt/remote  trình duyệt           trình duyệt tại /tv
```

Hub quét các thư mục vào một chỉ mục SQLite cục bộ, phát file qua HTTP `Range` (để trình phát tua
được) và khi thiết bị không giải mã được file thì chuyển đổi sang các đoạn HLS theo thời gian thực.

## Bắt đầu nhanh

**Ứng dụng desktop (khuyên dùng):** tải bộ cài ở trang [**Releases**](https://github.com/Raggza/house_cinema/releases), cài đặt, trang quản
trị sẽ tự mở trong trình duyệt. Thêm thư mục, bấm **Quét lại**, rồi quét mã QR bằng điện thoại.

**Từ mã nguồn (Windows):**

```powershell
winget install --id=astral-sh.uv -e        # công cụ Python (tự tải Python)
winget install Gyan.FFmpeg                 # tuỳ chọn, nên cài
git clone https://github.com/Raggza/house_cinema.git
cd house_cinema
.\run.cmd                                  # rồi mở http://localhost:8000/admin
```

## Cài đặt

### Cách 1 — Ứng dụng desktop (Windows / macOS)

Đã có đủ mọi thứ, kể cả ffmpeg.

| Hệ điều hành | File | Ghi chú |
|---|---|---|
| Windows 10/11 x64 | `HouseCinema-<version>-Windows-x64-Setup.exe` | Chưa ký số: khi thấy *"Windows protected your PC"* bấm **More info › Run anyway**. Giữ dấu tick **"Allow phones, computers and TVs on my home network to connect"** (thêm luật tường lửa cho mạng riêng) |
| macOS 11+ (Apple Silicon) | `HouseCinema-<version>-macOS-arm64.dmg` | Kéo vào **Applications**. Chưa notarize: lần đầu **chuột phải › Open › Open** (macOS 15+: *System Settings › Privacy & Security › Open Anyway*) |
| macOS 11+ (Intel) | `HouseCinema-<version>-macOS-x86_64.dmg` | Như trên. Bấm **Allow** nếu macOS hỏi về kết nối đến / mạng cục bộ |

Ứng dụng chạy nền với biểu tượng ở **khay hệ thống** (Windows) hoặc **thanh menu** (macOS):

- **Nhấp đúp** biểu tượng (Windows): hiện/ẩn cửa sổ console (địa chỉ, mã QR, nhật ký).
  Đóng cửa sổ này **không** tắt hub.
- **Chuột phải** (Windows) / nhấp (macOS): mở thư viện, trang quản trị, sao chép địa chỉ cho
  điện thoại, mở thư mục dữ liệu/cấu hình, khởi động lại hub, **Thoát**.

Gỡ cài đặt vẫn giữ thư viện và cấu hình; xoá [thư mục dữ liệu](#cấu-hình) nếu muốn xoá hết.

### Cách 2 — Từ mã nguồn

Yêu cầu:

- [**uv**](https://docs.astral.sh/uv/): tự cài đúng phiên bản Python (≥ 3.12) và thư viện.
  Không có winget: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`.
- [**ffmpeg**](https://ffmpeg.org/) *(tuỳ chọn, nên cài)*: ảnh thu nhỏ, thông tin video và chuyển đổi.
  Không có ffmpeg hub vẫn chạy, báo các tính năng bị tắt khi khởi động và trên trang quản trị,
  và dùng ảnh thu nhỏ mặc định.

Sau đó nhấp đúp **`run.cmd`** (hoặc chạy `.\run.cmd` trong terminal). Lần đầu mất 1–2 phút để uv
tải Python và thư viện. Nếu Windows Firewall hỏi về Python, cho phép **Private networks**.
Dừng bằng **Ctrl+C**.

Trên macOS/Linux, chạy trực tiếp: `uv run --no-dev python -m source.pipeline`.

## Sử dụng

### 1. Thêm thư mục phim

Mở **`http://localhost:8000/admin`** trên máy chủ:

- Bấm **Thêm** khi ô trống để mở hộp chọn thư mục của hệ điều hành, **hoặc**
- dán đường dẫn, ví dụ `D:\Phim` hay `\\nas\Phim`, rồi bấm **Thêm**.

**Bỏ** chỉ gỡ thư mục khỏi hub; file trên ổ đĩa không bao giờ bị động đến.

### 2. Quét thư viện

Tại `http://localhost:8000/`, bấm **Quét lại**. Thanh tiến trình hiển thị hai bước: tìm video,
rồi đọc thông tin và tạo ảnh thu nhỏ. Quét lại mỗi khi thêm hoặc xoá phim.

### 3. Xem trên thiết bị khác

Cửa sổ terminal / console và trang quản trị hiển thị địa chỉ cho thiết bị khác
(ví dụ `http://192.168.1.10:8000/`) cùng **mã QR**. Quét mã và lưu trang vào dấu trang.

| Trong trình phát | |
|---|---|
| Phím tắt | `Space`/`K` phát-dừng · `←`/`→` (`J`/`L`) ±10 giây · `F` toàn màn hình · `C` phụ đề · `M` tắt tiếng · `P`/`N` video trước/sau |
| **Phát trên TV** | Xem tiếp trên smart TV |
| **Link cho VLC** | Sao chép địa chỉ video cho VLC (*Media › Open Network Stream*) |

### Danh sách phát

- **Phát tất cả** trong một thư mục phát các video theo thứ tự tự nhiên ("Tập 2" trước "Tập 10").
- **Chọn** cho phép chọn video ở nhiều thư mục và kết quả tìm kiếm; phát theo thứ tự đã chọn.
- Tự chuyển video sau 5 giây, **Ngẫu nhiên**, **Không lặp** / **Lặp danh sách** / **Lặp 1 video**.
  Video không phát được sẽ bị bỏ qua (dừng sau 3 video lỗi liên tiếp). Tối đa 500 video; tải lại
  trang vẫn giữ nguyên vị trí.

### Phụ đề

Đặt file `.srt` hoặc `.vtt` cạnh video, cùng tên, có thể kèm mã ngôn ngữ — không cần quét lại:

```
Phim.mkv
Phim.srt             Phim.vi.srt
Phim.eng.srt         Phim.Vietnamese.vtt
```

Phụ đề tiếng Việt được ưu tiên; lựa chọn của bạn sẽ được ghi nhớ.

### Định dạng trình duyệt không hỗ trợ

Khi trình duyệt không phát được, hub chuyển đổi ngay lúc xem (cần ffmpeg), trang hiện
*"Đang chuẩn bị video…"* trong vài giây:

- **Remux** khi giữ được luồng hình: nhanh, không giảm chất lượng, lưu cache ở `cache/transcode/`.
- **Transcode** sang H.264 trong các trường hợp còn lại.
- Tối đa 2 phiên chuyển đổi cùng lúc (`transcode.max_sessions`); thiết bị thêm vào sẽ thấy
  **"Hub đang bận"** kèm nút thử lại.

Trường hợp thường gặp: Safari trên iPhone (MKV, AV1), Edge chưa cài HEVC Video Extensions (HEVC),
Chrome và một số TV (âm thanh AC-3 bị câm).

## Xem trên smart TV

Trình duyệt trên TV dùng remote rất chậm, nên TV chỉ chiếu phim và **điện thoại làm remote**.

1. Trên trình duyệt TV, mở **`http://<địa-chỉ>:8000/tv`**. Gõ đầy đủ `http://` — trình duyệt TV
   hay tự chuyển sang `https://`, mà hub không phục vụ.
2. Bấm **Bắt đầu** một lần bằng con trỏ của TV (để bật toàn màn hình và âm thanh).
3. Trên điện thoại, kết nối bằng cách quét mã QR trên TV, mở **Điều khiển TV** ở trang thư viện,
   hoặc gõ mã 4 chữ cái hiện trên TV.
4. Mở một phim hoặc danh sách đã chọn trên điện thoại rồi bấm **Phát trên TV**. Điện thoại điều
   khiển phát/dừng, ±10 giây, tua, phụ đề, âm lượng, tắt tiếng, dừng và danh sách phát.

Tải lại `/tv` hoặc khởi động lại hub vẫn giữ nguyên TV đã ghép. Đã thử trên Sony Android TV với
BrowseHere và TV Bro.

## Cấu hình

Đa số người dùng không cần cấu hình gì. Cấu hình được đọc theo thứ tự (cái sau ghi đè cái trước):

`source/configs/config.yml` → `source/configs/.env` (sao chép từ `.env.example`) → biến môi
trường → tham số dòng lệnh (ví dụ `.\run.cmd --port 8080`). Khởi động lại hub sau khi sửa.

**Ứng dụng desktop** lưu cấu hình (`configs/`), `data/`, `cache/` và `logs/` trong thư mục dữ
liệu riêng của từng người dùng:

| Hệ điều hành | Thư mục dữ liệu |
|---|---|
| Windows | `%LOCALAPPDATA%\HouseCinema` |
| macOS | `~/Library/Application Support/HouseCinema` |

Đặt biến `HOUSE_CINEMA_HOME` để dùng vị trí khác (cho cả ứng dụng và `run.cmd`).

| Thiết lập | Ở đâu | Mặc định | Mô tả |
|---|---|---|---|
| Thư mục thư viện | trang quản trị | — | Lưu ở `data/library-paths.json`. `library.paths` / `HUB_LIBRARY_PATHS` chỉ dùng cho lần chạy đầu. `--path` thay thế trong một lần chạy (trang quản trị chuyển sang chỉ đọc) |
| Cổng | `server.port`, `--port` | `8000` | Nhớ mở cổng mới trên tường lửa |
| PIN | `HUB_PIN` trong `.env` | tắt | 4–32 ký tự, xem [Bảo mật](#bảo-mật) |
| ffmpeg | `media.ffmpeg_path` | lấy từ `PATH` | ffprobe được tìm cùng thư mục |
| Chuyển đổi | `transcode.*` | bật | `enabled`, `max_sessions` (2), `video_encoder` (`libx264`, hoặc `h264_nvenc` / `h264_qsv` / `h264_amf`), `max_height` (1080), `idle_seconds` (60), `cache_gb` (20) |
| Chỉ LAN | `access.lan_only` | bật | Địa chỉ IP công cộng nhận `403` |

## Bảo mật

- **Chỉ mạng trong nhà.** Cho phép dải địa chỉ riêng (`192.168.x.x`, `10.x.x.x`, `172.16–31.x.x`),
  link-local và dải VPN như Tailscale (`100.64.0.0/10`). Chỉ xét địa chỉ kết nối thực, không tin
  header.
- ⚠️ **Không bao giờ mở (forward) cổng của hub trên router.** Hub được thiết kế cho LAN và dùng HTTP thường.
- **PIN tuỳ chọn:** đặt `HUB_PIN=...` trong `source/configs/.env`. Mọi thiết bị trừ máy chủ phải
  nhập PIN; được ghi nhớ 30 ngày. Sai 5 lần liên tiếp thì thiết bị đó phải chờ 5 phút. Đổi PIN
  hoặc xoá `data/hub-secret.key` sẽ đăng xuất mọi thiết bị.
- **Trang quản trị chỉ dùng được trên máy chủ** (`localhost` hoặc địa chỉ LAN của chính máy chủ);
  thiết bị khác nhận `403` và bị ghi nhật ký.
- **Chỉ phục vụ file trong thư viện.** API nhận ID, không nhận đường dẫn. Truy cập vượt thư mục,
  tráo symlink / junction và thư mục đã gỡ đều bị từ chối bằng một mã `404` giống nhau và được
  ghi nhật ký.

## Xử lý sự cố

| Vấn đề | Cách xử lý |
|---|---|
| `run.cmd` báo **"Không tìm thấy uv"** | Cài uv, rồi mở cửa sổ terminal **mới** |
| Điện thoại không kết nối được | Cùng Wi-Fi (không phải mạng khách)? Mở `http://<địa-chỉ>:8000/health` trên điện thoại — phải trả về `{"status":"ok"…}`. Nếu không, kiểm tra tường lửa (bên dưới) |
| Terminal cảnh báo mạng là **Public** | Windows *Settings › Network & Internet › Network profile* → **Private** |
| Trình duyệt TV báo không kết nối được | Gõ đầy đủ `http://…` |
| Không có ảnh thu nhỏ / **"Định dạng chưa hỗ trợ"** / **"Không tìm thấy ffmpeg"** | Cài ffmpeg, khởi động lại hub, bấm **Quét lại** |
| **"Hub đang bận"** | Chờ, dùng VLC, hoặc tăng `transcode.max_sessions` |
| *"port 8000 is already in use"* | Hub đã chạy rồi, hoặc dùng `.\run.cmd --port 8080` |
| Video hiện **"Ổ đang ngắt"** | Cắm lại ổ và quét lại (khác ký tự ổ đĩa cũng không sao) |
| Ứng dụng: biểu tượng khay màu xám có chấm đỏ | Hub không khởi động được; console hiện lý do. Sửa xong, chuột phải › **Khởi động lại hub** |
| Ứng dụng: cần xem nhật ký | `<thư mục dữ liệu>/logs/house-cinema.log` |
| *"the index was damaged"* | Không cần làm gì: chỉ mục được sao lưu và dựng lại khi quét. Xoá `data/library.db` luôn an toàn |

**Thêm luật Windows Firewall** (PowerShell chạy với quyền quản trị):

```powershell
New-NetFirewallRule -DisplayName "House Cinema (TCP 8000)" -Direction Inbound -Protocol TCP `
  -LocalPort 8000 -Action Allow -Profile Private
# gỡ: Remove-NetFirewallRule -DisplayName "House Cinema (TCP 8000)"
```

## Phát triển

### Công nghệ

Python 3.12 · [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) ·
Pydantic · SQLite · ffmpeg/ffprobe · HTML/CSS/JS thuần (không thư viện front-end ngoài, chạy
offline) · [segno](https://github.com/heuer/segno) (mã QR) · pystray (biểu tượng khay) ·
PyInstaller + Inno Setup (đóng gói) · pytest + Playwright (kiểm thử).

### Cấu trúc dự án

```
house_cinema/
├── run.cmd, run.ps1          # khởi động bằng một lệnh
├── pyproject.toml, uv.lock   # thư viện (uv)
├── source/
│   ├── pipeline.py           # điểm vào: server + CLI
│   ├── configs/              # config.yml, .env.example
│   ├── modules/
│   │   ├── library/          # quét, chỉ mục SQLite, thông tin media
│   │   ├── server/           # API, streaming, kiểm soát truy cập, mạng, màn hình TV
│   │   ├── subtitles/        # tìm phụ đề, nhận bảng mã, SRT→VTT
│   │   └── transcode/        # kế hoạch và phiên remux / transcode
│   ├── desktop/              # ứng dụng khay hệ thống / thanh menu
│   ├── web/                  # trang web (thư viện, trình phát, quản trị, TV, remote)
│   ├── schemas/, utils/, image/
├── packaging/                # script build bộ cài Windows & dmg macOS
├── scripts/                  # device_matrix.py
├── demo/                     # soak test, trang thử phụ đề
├── test/                     # bộ test pytest + thư viện mẫu
└── .github/workflows/        # build bản phát hành
```

### Thiết lập môi trường

```powershell
uv sync                                                  # .venv kèm công cụ dev
copy source\configs\.env.example source\configs\.env     # tuỳ chọn
uv run python -m source.pipeline                         # chạy hub
uv run --group desktop python -m source.desktop          # chạy ứng dụng khay từ mã nguồn
```

Nếu tạo lại `.venv`, giữ tên prompt: `uv venv --prompt house_cinema`, rồi `uv sync`.

### Giao diện dòng lệnh

```powershell
uv run python -m source.pipeline --check-config   # cấu hình hiệu lực, trạng thái ffmpeg
uv run python -m source.pipeline scan             # quét và cập nhật chỉ mục (+ thêm, − xoá, ~ đổi tên, ! mất kết nối)
uv run python -m source.pipeline scan --dry-run   # chỉ hiện thay đổi
uv run python -m source.pipeline list             # liệt kê chỉ mục
uv run python -m source.pipeline info 601fdf      # một video theo tiền tố ID
```

Tuỳ chọn của `scan`: `--no-media` (bỏ qua ffprobe/ảnh thu nhỏ), `--refresh-media` (làm lại tất cả),
`--progress` / `--no-progress`, `--path` (ghi đè thư mục).

| Mã thoát | Ý nghĩa |
|---|---|
| `0` | Thành công |
| `2` | Lỗi cấu hình |
| `3` | Server không khởi động được (ví dụ trùng cổng) |
| `4` | Chỉ mục thư viện không dùng được |
| `5` | `info`: không có đúng một video khớp ID |

### API

Tài liệu OpenAPI tương tác: **`http://localhost:8000/docs`**. Các endpoint chính:

| Endpoint | Mô tả |
|---|---|
| `GET /api/library` | Danh sách video phân trang (`q`, `folder`, `recursive`, `library`, `sort`, `offset`, `limit` ≤ 200) |
| `GET /api/folders` | Thư viện và thư mục kèm số video |
| `GET /api/media/<id>` | Thông tin video, URL stream / ảnh thu nhỏ, phụ đề |
| `GET`/`POST /api/rescan` | Tiến trình quét / bắt đầu quét nền |
| `GET /stream/<id>` | File video, hỗ trợ HTTP `Range` |
| `GET /thumbnails/<id>.jpg` | Ảnh thu nhỏ |
| `GET /subtitles/<id>[/<key>.vtt]` | Danh sách phụ đề / một phụ đề dạng WebVTT |
| `POST /api/play/<id>` | Quyết định cách phát: `direct`, `remux` hoặc `transcode` (HLS) |
| `POST /api/pin` | Mở khoá thiết bị bằng PIN |
| `/api/screens/...` | Đăng ký TV, lệnh điện thoại → TV (SSE), trạng thái TV |
| `/api/admin/...` | Thư viện, mạng/QR, công cụ, hộp chọn thư mục, phiên chuyển đổi — **chỉ trên máy chủ** |

### Kiểm thử

```powershell
uv run python test/fixtures/make_library.py   # tạo thư viện mẫu (cần ffmpeg)
uv run pytest
```

- Test trình duyệt dùng Playwright với **Microsoft Edge** (hoặc Chrome) đã cài sẵn; Chromium đi kèm
  Playwright không phát được H.264. Đặt `HUB_E2E_RECORD=1` để ghi trace vào `test-artifacts/`.
- **Trang kiểm tra thiết bị:** `http://<địa-chỉ>:8000/diagnostics` trên bất kỳ thiết bị nào (tắt khi
  `APP_ENV=production`) kiểm tra định dạng phát được và ghi lại phím remote TV. Báo cáo lưu ở
  `data/device-reports/`; `uv run python scripts/device_matrix.py` tạo bảng tương thích.
- **Soak test** (dò rò rỉ tài nguyên khi chạy lâu):

  ```powershell
  uv run python demo/soak.py --minutes 60
  uv run python demo/soak.py --hub http://127.0.0.1:8000 --process HouseCinema.exe   # với ứng dụng đã cài
  ```

### Build bộ cài

```powershell
# Windows — cần uv và Inno Setup 6 (winget install JRSoftware.InnoSetup)
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#   → dist\HouseCinema-<version>-Windows-x64-Setup.exe   (-SkipInstaller, -FfmpegDir <thư mục>)
```

```bash
# macOS — chạy trên máy Mac, build cho kiến trúc của máy đó
bash packaging/build_macos.sh       # → dist/HouseCinema-<version>-macOS-<arm64|x86_64>.dmg
```

Trên GitHub: *Actions › Build apps › Run workflow*, hoặc push tag `v*` để đính kèm bản build vào
release. Phiên bản lấy từ `pyproject.toml`.

> **Giấy phép ffmpeg:** bản ffmpeg đi kèm là bản **GPL** (có x264). Ứng dụng kèm giấy phép của
> ffmpeg và file `SOURCE.txt` trỏ tới mã nguồn; hãy giữ các file này nếu thay đổi cách build.
>
> **Ký số:** bản build chưa được ký, nên SmartScreen / Gatekeeper sẽ cảnh báo ở lần mở đầu tiên.

## Lộ trình

- [ ] Docker image (v2)
- [ ] Bản Windows có ký số và bản macOS đã notarize

## Đóng góp

Mọi đóng góp đều được hoan nghênh!

1. Fork repo và tạo nhánh: `git checkout -b feature/thay-doi-cua-ban`.
2. Chạy `uv sync` và đảm bảo `uv run pytest` đều qua.
3. Giữ front-end không phụ thuộc thư viện ngoài (phải chạy được offline).
4. Mở pull request mô tả thay đổi và cách bạn đã kiểm thử.

Báo lỗi và đề xuất qua mục Issues, kèm hệ điều hành, trình duyệt/thiết bị và nhật ký hub nếu cần.
**Không đính kèm nhật ký hay báo cáo thiết bị khi chưa xoá địa chỉ IP LAN và thông tin cá nhân.**

## Giấy phép

Mã nguồn House Cinema được phát hành theo [Giấy phép MIT](LICENSE).

Bộ cài desktop có kèm bản build ffmpeg của bên thứ ba, được phân phối theo **GPL** chứ không phải
MIT (xem [Build bộ cài](#build-bộ-cài)).

## Cảm ơn

[FastAPI](https://fastapi.tiangolo.com/) · [Uvicorn](https://www.uvicorn.org/) ·
[FFmpeg](https://ffmpeg.org/) · [uv](https://docs.astral.sh/uv/) ·
[segno](https://github.com/heuer/segno) · [pystray](https://github.com/moses-palmer/pystray) ·
[PyInstaller](https://pyinstaller.org/) · [Inno Setup](https://jrsoftware.org/isinfo.php) ·
[Playwright](https://playwright.dev/python/) · bản build ffmpeg của
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) và Martin Riedl.

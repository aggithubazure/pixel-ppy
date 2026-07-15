# Hướng dẫn triển khai chi tiết

Tài liệu này hướng dẫn cài đặt và chạy **Pixel Gemini Bot** trên máy mới:
Windows (chạy trực tiếp) hoặc Linux/Server (Docker, Coolify).

> **Lưu ý quan trọng về điều kiện ưu đãi**
> Bot mô phỏng thiết bị Pixel, đăng nhập Google One và tìm liên kết ưu đãi
> Gemini Pro 12 tháng. Việc có nhận được ưu đãi hay không do **máy chủ Google**
> quyết định (lịch sử tài khoản + khu vực + chứng thực thiết bị thật). Nếu
> Google trả về `LOCKED:BARD_ADVANCED_MODE:...:TIER0` thì tài khoản **không đủ
> điều kiện** — đây không phải lỗi của bot. Xem mục [Chẩn đoán](#8-nhật-ký--chẩn-đoán)
> và [Xử lý sự cố](#9-xử-lý-sự-cố).

---

## 1. Tổng quan

| Thành phần | Mô tả |
|---|---|
| `main.py` | Điểm khởi động bot Telegram + điều phối phiên |
| `google_automation.py` | Đăng nhập Google One + phát hiện ưu đãi (Selenium) |
| `device_simulator.py` | Giả lập thiết bị Pixel (UA, Client Hints, WebGL, kích thước…) |
| `config.py` | Cấu hình, preset thiết bị, tự nhận diện phiên bản Chrome |

**Tính năng chính:** 6 preset Pixel (9/10 Pro, XL, Fold) tự động **xoay vòng**
qua từng lần thử `/check_offer`; hỗ trợ 2FA bằng mã TOTP tự động; ghi nhật ký
chẩn đoán chi tiết khi không thấy ưu đãi.

---

## 2. Yêu cầu chung

- **Telegram Bot Token** (lấy từ @BotFather).
- **Google Chrome** (hoặc Chromium) + **chromedriver** cùng phiên bản *major*.
- **Python 3.10+** (khi chạy trực tiếp) hoặc **Docker** (khi chạy bằng container).
- Tài khoản Google dùng **mã xác thực TOTP** (Authenticator). **Không hỗ trợ**
  tài khoản dùng passkey / khóa bảo mật vật lý hoặc xác nhận qua điện thoại.

---

## 3. Tạo Telegram Bot Token

1. Mở Telegram, tìm **@BotFather**.
2. Gửi `/newbot`, đặt tên và username cho bot.
3. Sao chép token dạng `123456789:ABC-DEF...` — dùng cho biến `TELEGRAM_BOT_TOKEN`.

> Nếu token đã từng bị lộ (ví dụ commit nhầm), hãy gửi `/revoke` cho BotFather để
> cấp token mới.

---

## 4. Biến môi trường

| Biến | Bắt buộc | Ý nghĩa | Ví dụ |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token bot Telegram | `123456789:ABC...` |
| `CHROME_BIN` | ✅ (nếu không có trong PATH) | Đường dẫn Chrome/Chromium | Win: `C:\Program Files\Google\Chrome\Application\chrome.exe`<br>Linux: `/usr/bin/chromium` |
| `CHROMEDRIVER_PATH` | ✅ (nếu không có trong PATH) | Đường dẫn chromedriver | Linux: `/usr/bin/chromedriver` |
| `DEVICE_PROFILE` | ❌ | Preset mặc định (khi xem trước). Cơ chế xoay vòng vẫn chạy đủ 6 preset | `pixel_9_pro_fold` |

Các preset hợp lệ cho `DEVICE_PROFILE`: `pixel_10_pro`, `pixel_10_pro_xl`,
`pixel_10_pro_fold`, `pixel_9_pro`, `pixel_9_pro_xl`, `pixel_9_pro_fold`.

> Chế độ **headless** được bật mặc định trong `config.py` (`HEADLESS = True`).
> Muốn xem trình duyệt hiển thị khi gỡ lỗi, sửa `HEADLESS = False` trong `config.py`.

---

## 5. Cách A — Chạy trực tiếp trên Windows

### 5.1. Cài Python và Chrome

- Cài **Python 3.10+**: <https://www.python.org/downloads/> (nhớ tích *Add Python to PATH*).
- Cài **Google Chrome** bản mới nhất.

### 5.2. Cài chromedriver đúng phiên bản

Cách nhanh nhất bằng WinGet (PowerShell):

```powershell
winget install --id Chromium.ChromeDriver -e
```

Đường dẫn cài đặt thường là:

```
C:\Users\<TÊN>\AppData\Local\Microsoft\WinGet\Packages\Chromium.ChromeDriver_Microsoft.Winget.Source_8wekyb3d8bbwe\chromedriver-win64\chromedriver.exe
```

> **Quan trọng:** chromedriver phải **cùng số phiên bản major với Chrome**
> (ví dụ Chrome 149 → chromedriver 149). Nếu lệch, đăng nhập sẽ lỗi. Nếu WinGet
> cài bản cũ, tải bản khớp tại **Chrome for Testing**:
> <https://googlechromelabs.github.io/chrome-for-testing/>

### 5.3. Tải mã nguồn và cài thư viện

```powershell
git clone https://github.com/aggithubazure/pixel-ppy.git
cd pixel-ppy

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 5.4. Đặt biến môi trường và chạy

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456789:ABC-DEF..."
$env:CHROME_BIN = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$env:CHROMEDRIVER_PATH = "C:\Users\<TÊN>\AppData\Local\Microsoft\WinGet\Packages\Chromium.ChromeDriver_Microsoft.Winget.Source_8wekyb3d8bbwe\chromedriver-win64\chromedriver.exe"

python main.py
```

Khi log hiện `Bot is running` và `getMe ... 200 OK` là bot đã sẵn sàng.

### 5.5. (Tùy chọn) Script khởi động nhanh

Tạo file `run.ps1` để không phải gõ lại mỗi lần:

```powershell
$env:TELEGRAM_BOT_TOKEN = "123456789:ABC-DEF..."
$env:CHROME_BIN = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$env:CHROMEDRIVER_PATH = "C:\...\chromedriver.exe"
python main.py
```

Chạy bằng: `powershell -ExecutionPolicy Bypass -File .\run.ps1`

---

## 6. Cách B — Chạy trên Linux/Server bằng Docker

Docker đã đóng gói sẵn Chromium + chromedriver nên **không cần cài trình duyệt thủ công**.

```bash
git clone https://github.com/aggithubazure/pixel-ppy.git
cd pixel-ppy

cp .env.example .env
nano .env            # đặt TELEGRAM_BOT_TOKEN=<token của bạn>

docker compose up -d --build
```

Các lệnh quản lý:

```bash
docker compose logs -f        # xem log trực tiếp
docker compose ps             # trạng thái container
docker compose restart        # khởi động lại
docker compose up -d --build  # build lại sau khi cập nhật mã
docker compose down           # dừng và xóa container
```

> Container dùng chính sách `restart: on-failure:3` (chỉ tự khởi động lại tối đa
> 3 lần khi thoát bất thường). Dừng thủ công bằng `stop`/`down` sẽ không tự chạy lại.

---

## 7. Triển khai trên Coolify

1. Trong Coolify, tạo **New Resource → Docker Compose** (hoặc **Application** trỏ
   tới repo GitHub `aggithubazure/pixel-ppy`).
2. Coolify sẽ tự đọc `docker-compose.yml` trong repo.
3. Vào phần **Environment Variables**, thêm:
   - `TELEGRAM_BOT_TOKEN` = token của bạn.
4. Đảm bảo server còn đủ RAM (khuyến nghị ≥ 2 GB; compose đã đặt `shm_size: 512m`
   cho Chrome headless).
5. Bấm **Deploy** và theo dõi log tới khi thấy `Bot is running`.

> Bot chạy nền theo kiểu long-polling, **không cần expose cổng/domain** — không
> cần cấu hình proxy hay port mapping trong Coolify.

---

## 8. Nhật ký & chẩn đoán

Khi không tìm thấy ưu đãi, bot tự lưu vào thư mục `logs/`:

| File | Nội dung |
|---|---|
| `offer_page_<timestamp>.html` | Toàn bộ HTML trang gói dịch vụ Google trả về |
| `debug_offer_not_found_<timestamp>.png` | Ảnh chụp màn hình trang lúc đó |
| Log dòng `Found LOCKED benefit link: ...` | Link ưu đãi kèm **mã trạng thái** |

**Giải mã link trạng thái**, ví dụ:

```
LOCKED:BARD_ADVANCED_MODE:BENEFIT_OFFERING_MEMBER:SG:TIER0
```

| Thành phần | Ý nghĩa |
|---|---|
| `LOCKED` | Ưu đãi bị khóa, tài khoản chưa claim được |
| `BARD_ADVANCED_MODE` | Chính là ưu đãi Gemini Advanced/Pro |
| `SG` | Khu vực Google đang xét tài khoản (ví dụ Singapore) |
| `TIER0` | Bậc 0 = **không đủ điều kiện** (không có ưu đãi) |

> `logs/` đã được đưa vào `.gitignore` nên ảnh/HTML/nhật ký **không bị commit** lên Git.

---

## 9. Xử lý sự cố

| Hiện tượng | Nguyên nhân & cách khắc phục |
|---|---|
| Máy tự mở nhiều cửa sổ Chrome "New Tab" | Đã xử lý trong `config.py` (Windows không gọi `chrome.exe --version`). Nếu vẫn gặp, đảm bảo đang chạy bản mã mới nhất. |
| Đăng nhập lỗi/timeout ngay bước nhập email | chromedriver **lệch phiên bản** với Chrome. Cài lại chromedriver khớp major (mục 5.2). |
| `Chưa cài đặt chromedriver / Chromium` | Chưa đặt `CHROME_BIN` / `CHROMEDRIVER_PATH` hoặc đường dẫn sai. |
| `Unsupported 2FA ... khóa bảo mật / passkey` | Tài khoản dùng passkey → **không tự động được**. Hãy dùng tài khoản bật **Authenticator (mã TOTP 6 số)**. |
| Bot báo "chưa phát hiện ưu đãi" liên tục | Kiểm tra `logs/`. Nếu thấy `...:TIER0` nghĩa là **tài khoản không đủ điều kiện** phía Google — không sửa được bằng bot. |
| Chrome crash trong Docker | Server thiếu RAM/`shm`. Compose đã đặt `shm_size: 512m`; tăng RAM nếu cần. |

---

## 10. Sử dụng bot

| Lệnh | Chức năng |
|---|---|
| `/start` | Lời chào + danh sách lệnh |
| `/login` | Nhập email rồi mật khẩu (2 bước) |
| `/check_offer` | Giả lập thiết bị, đăng nhập, tìm ưu đãi (xoay vòng 3 lần, mỗi lần một model Pixel) |
| `/get_link` | Lấy lại liên kết ưu đãi gần nhất |
| `/status` | Xem thông tin phiên + hồ sơ thiết bị (model + màn hình + GPU) |
| `/logout` | Xóa an toàn thông tin đăng nhập |

### Đăng nhập kèm khóa TOTP (khuyến nghị để tự động 2FA)

Ở bước nhập mật khẩu, có thể gửi theo định dạng:

```
mật_khẩu|KHÓA_BÍ_MẬT_TOTP
```

- `KHÓA_BÍ_MẬT_TOTP` là chuỗi base32 khi bật Authenticator (mục "Không quét được
  mã QR? Nhập khóa"). Khi có khóa này, bot **tự sinh mã 6 số** mỗi lần đăng nhập.
- Nếu chỉ gửi mật khẩu (không có `|`), bot sẽ **hỏi mã 6 số** mỗi lần cần 2FA.

---

## 11. Bảo mật & lưu ý

- **Không commit** token hay mật khẩu vào mã nguồn. Chỉ đặt qua biến môi trường
  hoặc file `.env` (đã nằm trong `.gitignore`).
- Mật khẩu được lưu dạng `bytearray` trong bộ nhớ và **xóa sạch sau khi dùng**,
  không ghi ra đĩa. Phiên tự hủy sau 30 phút.
- Dự án chỉ dùng cho mục đích học tập/cá nhân, với tài khoản **do bạn sở hữu**.
  Tự động hóa truy cập tài khoản Google có thể vi phạm Điều khoản dịch vụ của Google.

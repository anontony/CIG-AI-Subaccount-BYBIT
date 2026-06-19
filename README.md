# CIG AI Subaccount

**CIG AI Subaccount** là dashboard AI dùng để quản lý Bybit sub-account theo từng user/workspace riêng. Mỗi user có API key riêng, prompt riêng, cấu hình rủi ro riêng, live log riêng và bảng theo dõi lệnh riêng.

Dự án được thiết kế để chạy local hoặc deploy lên Railway. Dữ liệu user/config/API key có thể lưu bền vững trên Railway Volume.

---

## 1. Tính năng chính

### Workspace riêng từng user

Mỗi tài khoản trong dashboard có không gian làm việc riêng:

- Prompt giao dịch riêng.
- Bybit API key riêng.
- OpenAI API key riêng.
- RSA private key riêng nếu dùng Bybit AI/OpenAPI RSA.
- Cài đặt rủi ro riêng.
- Live log riêng.
- Bảng theo dõi lệnh riêng.

### Hỗ trợ Bybit Spot + Futures

Bot có thể hiểu lệnh cho cả Spot và Futures:

```text
spot mua BTC 10 usdt
future long BTC 10u đòn 3 TP 70000 SL 65000
bán hết bitcoin spot
đóng hết BTC future
```

Với Spot, bot không dùng đòn bẩy. Với Futures, bot áp dụng đòn bẩy nhưng luôn đi qua Risk Guard.

### Prompt strategy loop

Anh có thể lưu prompt dạng chiến lược, ví dụ:

```text
Mua bitcoin spot 10 usdt/1h bắt đầu từ bây giờ.
Chỉ mua BTCUSDT spot, không dùng đòn bẩy.
TP 1.2%, SL 0.6%.
```

Bot sẽ:

1. Đọc prompt.
2. Parse thời gian/lệnh/chỉ báo/cấu hình chính.
3. Lưu prompt thay thế prompt cũ.
4. Tự đồng bộ chu kỳ chạy nếu prompt có thời gian như `/1h`, `mỗi 1 giờ`, `mỗi ngày`.
5. Khi đến đúng chu kỳ, lấy dữ liệu thị trường và gửi cho AI phân tích.
6. AI trả tín hiệu giao dịch.
7. Risk Guard kiểm tra.
8. Bot thực thi hoặc mô phỏng theo cấu hình.

### Direct Command

Ô **Lệnh trực tiếp** dùng để ra lệnh nhanh hoặc chỉnh bot bằng ngôn ngữ tự nhiên.

Ví dụ giao dịch:

```text
spot mua BTC 20u TP 3% SL 1%
short ETH 15u x2 TP 2800 SL 3100
bán hết bitcoin đang có
kiểm tra số dư
```

Ví dụ chỉnh bot:

```text
đổi đòn bẩy tối đa thành 5x
chỉ trade BTCUSDT, ETHUSDT
đặt TP mặc định 1.2% và SL mặc định 0.6%
bật mô phỏng
tắt mô phỏng
đổi prompt thành: Chỉ trade BTCUSDT khi xu hướng rõ, bắt buộc TP/SL.
```

### Risk Guard

Trước khi bot thực thi bất kỳ tín hiệu nào, Risk Guard sẽ kiểm tra:

- Symbol có nằm trong danh sách allowed symbols không.
- Market type hợp lệ: `spot`, `linear`.
- Spot không dùng đòn bẩy.
- Futures không vượt quá max leverage.
- Vốn mỗi lệnh không vượt giới hạn.
- Notional không vượt giới hạn.
- Có TP/SL nếu bật yêu cầu bắt buộc.
- Cooldown giữa các lệnh.
- Giới hạn số lệnh/ngày.

### Theo dõi lệnh

Bảng theo dõi lệnh hiển thị:

- Loại lệnh Spot/Futures.
- Entry price.
- Giá hiện tại.
- PNL %.
- PNL ước tính USDT.
- TP/SL.
- Khoảng cách tới TP/SL.
- Trạng thái: đang theo dõi, chạm TP, chạm SL, đã đóng.

### Hiển thị số dư

Đầu dashboard có khu số dư hiện đại:

- Tổng tài sản.
- Số dư khả dụng.
- USDT.
- BTC.

Số dư tự quét mỗi 30 giây và không ghi thông tin quét vào Live Log để tránh rối.

### Bybit Skill Auto Update

Bot có module Bybit Skill local cache:

- Tự cập nhật skill khi app khởi động.
- Có nút cập nhật thủ công trong dashboard.
- AI có thể nhận thêm context từ Bybit Skill để phân tích Spot/Futures chuẩn hơn.

### Bybit RSA Public Key

Bot hỗ trợ flow RSA:

1. User tạo RSA key riêng trong dashboard.
2. Bot hiển thị public key.
3. User dán public key vào Bybit AI/OpenAPI.
4. Bybit trả về API Key.
5. User dán API Key vào dashboard.
6. Bot dùng private key riêng của user để ký request.

Private key không hiển thị lại trên UI.

---

## 2. Cấu trúc thư mục

```text
cig_ai_subaccount_clean_v24/
├── app.py                 # FastAPI app chính
├── server.py              # Entry point chạy uvicorn
├── storage.py             # SQLite user/workspace/settings/log/trade store
├── bybit_client.py        # Bybit V5 REST client, HMAC/RSA signing
├── ai_engine.py           # OpenAI decision engine
├── risk_guard.py          # Risk Guard normalize + validate action
├── strategy_parser.py     # Parser prompt chiến lược, thời gian, RSI, TP/SL...
├── manual_parser.py       # Parser lệnh trực tiếp khi không dùng AI hoặc AI trả thiếu
├── control_parser.py      # Parser lệnh chỉnh bot/settings/prompt
├── trade_tracker.py       # Tính PNL và trạng thái TP/SL
├── indicator_engine.py    # Tính chỉ báo EMA, RSI, MACD, ATR, volume...
├── skill_sync.py          # Sync Bybit Skill
├── skill_runtime.py       # Load skill context cho AI
├── rsa_keys.py            # Tạo RSA key pair
├── session_auth.py        # Session cookie auth
├── state.py               # Runtime state cho từng user
├── templates/
│   └── index.html         # UI dashboard
├── static/
│   └── banner_bybit_cig_sidebar.png
├── bybit_skill_seed/
│   └── SKILL.md
├── requirements.txt
├── Dockerfile
├── railway.json
├── .env.example
└── README.md
```

---

## 3. Yêu cầu hệ thống

### Local

- Python 3.11 hoặc cao hơn.
- Windows, macOS hoặc Linux.
- Internet để gọi OpenAI và Bybit API.

### Railway

- Railway project.
- Railway service deploy từ GitHub hoặc zip source.
- Railway Volume nếu muốn lưu user/API/config bền vững.

---

## 4. Cài đặt local trên Windows

### Bước 1: Giải nén project

Ví dụ giải nén vào Desktop:

```text
C:\Users\tony\Desktop\cig_ai_subaccount_clean_v24
```

### Bước 2: Mở PowerShell trong thư mục project

```powershell
cd C:\Users\tony\Desktop\cig_ai_subaccount_clean_v24
```

### Bước 3: Tạo virtual environment

```powershell
python -m venv .venv
```

Kích hoạt môi trường:

```powershell
.\.venv\Scripts\activate
```

### Bước 4: Cài thư viện

```powershell
pip install -r requirements.txt
```

### Bước 5: Tạo file `.env`

```powershell
copy .env.example .env
```

Mở file `.env` và chỉnh:

```env
RUNTIME_DIR=./data
APP_SECRET=thay_bang_chuoi_bi_mat_rat_dai_va_co_dinh
```

Nếu muốn dùng OpenAI key mặc định toàn app thì có thể thêm:

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.5
```

Nhưng khuyến nghị nhập OpenAI API key trực tiếp trong dashboard theo từng user.

### Bước 6: Chạy app

```powershell
python server.py
```

Mở trình duyệt:

```text
http://localhost:8000
```

### Bước 7: Tạo user đầu tiên

Trong giao diện web:

1. Nhập username.
2. Nhập mật khẩu tối thiểu 6 ký tự.
3. Bấm **Tạo tài khoản mới**.
4. Vào **Cài đặt API & Rủi ro** để nhập API.

---

## 5. Cài đặt local trên macOS/Linux

```bash
cd ~/Desktop/cig_ai_subaccount_clean_v24
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python server.py
```

Mở:

```text
http://localhost:8000
```

---

## 6. Cấu hình Bybit API

### Khuyến nghị bảo mật

Nên tạo **sub-account riêng** trên Bybit cho bot.

API key nên bật:

```text
Read
Trade
```

Không bật:

```text
Withdraw
Transfer
```

Không dùng tài khoản chính để test bot.

### Cách dùng HMAC API Key

Trong Bybit:

1. Vào sub-account.
2. Tạo API key.
3. Chọn quyền Read + Trade.
4. Copy API Key và API Secret.
5. Trong dashboard CIG AI Subaccount, vào **Cài đặt API & Rủi ro**.
6. Chọn kiểu ký API: `HMAC` hoặc `Tự động`.
7. Dán API Key và API Secret.
8. Bấm **Lưu cài đặt**.
9. Bấm **Kiểm tra Bybit**.

### Cách dùng RSA Public Key

Trong dashboard:

1. Vào **Cài đặt API & Rủi ro**.
2. Bấm **Tạo RSA key riêng**.
3. Copy public key.
4. Dán public key vào Bybit AI/OpenAPI.
5. Bybit trả về API Key.
6. Dán API Key vào dashboard.
7. Chọn kiểu ký API: `RSA` hoặc `Tự động`.
8. Bấm **Lưu cài đặt**.
9. Bấm **Kiểm tra Bybit**.

Lưu ý: `APP_SECRET` phải giữ cố định. Nếu đổi `APP_SECRET`, private key/API key đã mã hoá có thể không giải mã được.

---

## 7. Cấu hình OpenAI

Trong dashboard, mỗi user có thể nhập OpenAI API key riêng tại **Cài đặt API & Rủi ro**.

Các model có thể chọn trong UI tuỳ phiên bản app. Nếu model mới không hỗ trợ tham số cũ, app đã có fallback để tránh lỗi `max_tokens`/`temperature`.

Nếu không nhập OpenAI API key:

- Direct Command có thể dùng parser đơn giản cho một số lệnh cơ bản.
- Prompt strategy loop cần OpenAI để phân tích thị trường và ra quyết định tốt hơn.

---

## 8. Cách dùng Prompt Strategy

### Ví dụ mua Spot theo chu kỳ

```text
Mua bitcoin spot 10 usdt/1h bắt đầu từ bây giờ.
Chỉ dùng BTCUSDT.
Không dùng đòn bẩy.
TP 1.2%, SL 0.6%.
```

Bot sẽ hiểu:

```text
Market: Spot
Symbol: BTCUSDT
Size: 10 USDT
Chu kỳ: 1 giờ
TP: 1.2%
SL: 0.6%
```

### Ví dụ Futures theo RSI

```text
Chỉ trade BTCUSDT futures khung 15m.
Nếu RSI dưới 30 và MACD bắt đầu đảo chiều tăng thì long.
Margin 10 USDT, đòn bẩy 3x.
TP 1.5%, SL 0.7%.
Mỗi 30 phút kiểm tra một lần.
```

### Ví dụ không vào lệnh khi sideway

```text
Chỉ trade ETHUSDT futures khung 15m.
Nếu EMA20 nằm trên EMA50 và volume cao hơn trung bình 20 nến thì long.
Nếu xu hướng không rõ hoặc sideway thì không vào lệnh.
Margin tối đa 15 USDT, đòn bẩy 2x, TP 1.2%, SL 0.6%.
```

### Lưu ý về thời gian

Bot có scheduler riêng để tránh mua liên tục. Các mẫu thời gian nên dùng:

```text
10 usdt/1h
10 usdt / 2h
mỗi 1 giờ
mỗi giờ
mỗi 30 phút
mỗi ngày
1 ngày 1 lần
every 1 hour
daily
```

Khi parser nhận được thời gian, app sẽ tự set:

```text
loop_interval_seconds
min_seconds_between_trades
```

---

## 9. Cách dùng Direct Command

### Lệnh Spot

```text
spot mua BTC 10 usdt
spot mua ETH 20u TP 3% SL 1%
bán hết bitcoin spot
```

### Lệnh Futures

```text
future long BTC 10u đòn 3 TP 70000 SL 65000
short ETH 15u x2 TP 2800 SL 3100
đóng hết BTC future
```

### Lệnh kiểm tra tài khoản

```text
kiểm tra số dư
xem balance
wallet còn bao nhiêu
```

### Lệnh chỉnh bot

```text
chỉ trade BTCUSDT, ETHUSDT
đổi đòn bẩy tối đa thành 5x
đặt vốn tối đa mỗi lệnh 20 USDT
đặt TP mặc định 1.2% và SL mặc định 0.6%
chu kỳ bot 3600 giây
bật mô phỏng
tắt mô phỏng
```

---

## 10. Deploy Railway

### Bước 1: Chuẩn bị source

Đưa source lên GitHub hoặc upload project vào Railway.

Các file cần có ở root project:

```text
Dockerfile
railway.json
requirements.txt
server.py
app.py
```

### Bước 2: Tạo Railway project

1. Vào Railway.
2. New Project.
3. Chọn Deploy from GitHub hoặc upload source.
4. Chọn repo chứa project.

### Bước 3: Tạo Volume

Nếu muốn lưu user/config/API key bền vững, tạo Volume:

```text
Service → Volumes → Add Volume
Mount path: /data
```

### Bước 4: Set biến môi trường

Trong Railway Variables, thêm:

```env
RUNTIME_DIR=/data
APP_SECRET=thay_bang_chuoi_bi_mat_rat_dai_va_giu_co_dinh
```

Khuyến nghị thêm nếu cần:

```env
PYTHONUNBUFFERED=1
```

Không bắt buộc nhập Bybit/OpenAI key trong Railway Variables vì mỗi user có thể nhập trong dashboard.

### Bước 5: Deploy

Railway sẽ dùng Dockerfile và `server.py` để chạy app.

Sau khi deploy xong, mở domain Railway và tạo user đầu tiên.

---

## 11. Lưu ý khi dùng Railway Volume

Nếu deploy bản code mới nhưng vẫn dùng Volume cũ `/data`, dữ liệu cũ vẫn còn:

```text
user
password hash
settings
API key đã mã hoá
RSA private key đã mã hoá
prompt
live log
tracked trades
```

Muốn reset sạch hoàn toàn:

```text
Cách 1: Xoá Railway Volume cũ rồi tạo Volume mới
Cách 2: Tạo Railway service mới + Volume mới
```

Không đổi `APP_SECRET` nếu muốn dùng tiếp dữ liệu cũ.

---

## 12. Chạy production an toàn

Checklist trước khi bật lệnh thật:

- Dùng Bybit sub-account riêng.
- API chỉ có Read + Trade.
- Không cấp Withdraw.
- Test trước với DRY_RUN bật.
- Test với số vốn nhỏ.
- Kiểm tra allowed symbols.
- Kiểm tra max leverage.
- Kiểm tra max margin/order.
- Kiểm tra TP/SL mặc định.
- Kiểm tra cooldown.
- Theo dõi Live Log và bảng lệnh.

---

## 13. Các biến môi trường

| Biến | Ý nghĩa | Khuyến nghị |
|---|---|---|
| `RUNTIME_DIR` | Thư mục lưu SQLite/config/cache | Local: `./data`, Railway: `/data` |
| `APP_SECRET` | Key mã hoá dữ liệu nhạy cảm | Chuỗi dài, giữ cố định |
| `OPENAI_API_KEY` | OpenAI key mặc định toàn app | Có thể để trống và nhập trong UI |
| `OPENAI_MODEL` | Model mặc định | Có thể để trống và chọn trong UI |
| `BYBIT_API_KEY` | Bybit API key mặc định | Không khuyến nghị, nên nhập trong UI |
| `BYBIT_API_SECRET` | Bybit API secret mặc định | Không khuyến nghị, nên nhập trong UI |
| `BYBIT_ENV` | `testnet` hoặc `mainnet` | Test trước bằng `testnet` |

---

## 14. Dữ liệu được lưu ở đâu?

Local mặc định:

```text
./data/app.db
```

Railway nếu set đúng:

```text
/data/app.db
```

Các bảng chính:

- `users`
- `workspace_settings`
- `live_logs`
- `tracked_trades`

---

## 15. Troubleshooting

### Lỗi port Railway

Nếu Railway báo:

```text
Invalid value for '--port': '${PORT:-8000}' is not a valid integer
```

Dự án đã dùng `server.py` để đọc `PORT` bằng Python. Start command nên là:

```bash
python server.py
```

### Không lưu được user sau redeploy

Kiểm tra:

```env
RUNTIME_DIR=/data
```

và Railway Volume đã mount tại:

```text
/data
```

### API key cũ không giải mã được

Có thể do đổi `APP_SECRET`. Cần dùng lại `APP_SECRET` cũ hoặc tạo lại API key/RSA key cho user.

### Bot mua liên tục dù prompt có `/1h`

Kiểm tra phần parser summary sau khi lưu prompt. Phải thấy:

```text
Chu kỳ: 1h / 3600 giây
```

Nếu chưa thấy, viết rõ hơn:

```text
mua bitcoin spot 10 usdt mỗi 1 giờ
```

hoặc:

```text
mua bitcoin spot 10 usdt/1h
```

### Spot bị dùng đòn bẩy

Spot không dùng đòn bẩy. Nếu AI trả leverage cho Spot, Risk Guard sẽ bỏ qua leverage.

### Live log quá nhiều

Số dư tự quét mỗi 30 giây nhưng không ghi log. Nếu log nhiều, kiểm tra prompt loop hoặc direct command có bị bấm nhiều lần không.

---

## 16. Khuyến cáo rủi ro

Đây là công cụ tự động hoá giao dịch. Crypto có rủi ro cao. Bot có thể hiểu sai prompt, thị trường có thể biến động mạnh, API có thể lỗi, sàn có thể từ chối lệnh hoặc trượt giá.

Không dùng toàn bộ vốn. Không cấp quyền rút tiền. Luôn test với mô phỏng trước.

## Ghi chú bản Clean V26

Bản V26 vá lỗi Direct Command futures trong trường hợp AI hiểu đúng hướng lệnh nhưng bỏ sót tham số viết tắt của user.

Ví dụ lệnh:

```text
long bitcoin hiện tại bẩy x10 vốn 10u
```

V26 sẽ dùng parser nội bộ để giữ lại các thông tin rõ ràng từ câu lệnh:

- `bitcoin` → `BTCUSDT`
- `bẩy x10` / `x10` → `leverage = 10`
- `vốn 10u` → `margin_usdt = 10`
- `long` → `OPEN_LONG`
- `future` hoặc lệnh long/short → `category = linear`

Nếu lệnh vẫn bị chặn, nguyên nhân thường là do giá BTC hiện tại làm cho `10 USDT x 10x` chưa đủ `minQty=0.001 BTC` của Bybit. Khi đó bot sẽ trả thông báo chi tiết hơn để biết cần tăng vốn, tăng đòn bẩy hợp lệ, hoặc tăng giới hạn `max_notional_usdt`.

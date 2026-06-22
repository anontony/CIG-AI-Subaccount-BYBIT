# CIG AI Subaccount Clean V49

Bản V49 dựa trên V48 và sửa đúng 2 điểm quan trọng:

1. **TP/SL dạng `%` cho Futures được tính theo % PNL/ROI trên margin**, không còn tính trực tiếp theo % giá BTC.
2. **Nút “Đóng theo dõi” trong bảng lệnh đổi thành “Đóng lệnh”** và khi bấm sẽ gửi lệnh reduce-only lên Bybit để đóng đúng vị thế/lượng đang được tracker ghi nhận.

> Khuyến nghị: deploy trước với `DRY_RUN=true`, test parser + nút đóng lệnh, sau đó mới bật live bằng `DRY_RUN=false`.

---

## 1. Fix TP/SL % theo PNL Futures

Trước V49:

```text
long BTC 10u x20 TP 10% SL 5%
```

Bot hiểu TP/SL là % giá:

```text
TP giá +10%
SL giá -5%
```

Cách này sai với futures scalping vì TP 10% giá ở BTC là quá xa.

Từ V49, với Futures/Linear:

```text
TP 10% = lời 10% trên margin
SL 5% = lỗ 5% trên margin
```

Bot tự quy đổi sang % giá theo leverage:

```text
price_move_pct = pnl_pct / leverage
```

Ví dụ:

```text
Entry: 60,000
Leverage: 20x
Margin: 10 USDT
TP: 10% PNL
SL: 5% PNL
```

Bot sẽ quy đổi:

```text
TP price move = 10% / 20 = 0.5%
SL price move = 5% / 20 = 0.25%
```

Với Long:

```text
TP ≈ 60,300
SL ≈ 59,850
```

Với Short:

```text
TP ≈ 59,700
SL ≈ 60,150
```

Spot vẫn giữ cách cũ: TP/SL `%` là % giá vì Spot không có leverage.

---

## 2. Nút “Đóng lệnh” gửi tín hiệu thật lên Bybit

Trước V49, nút trong bảng theo dõi chỉ đóng trạng thái local:

```text
Đóng theo dõi
```

Nó không gửi lệnh đóng vị thế lên Bybit.

Từ V49, nút đổi thành:

```text
Đóng lệnh
```

Khi bấm:

- Nếu `DRY_RUN=true`: chỉ đóng theo dõi local, không gửi Bybit.
- Nếu `DRY_RUN=false` và lệnh là Futures: gửi lệnh **Market reduce-only** để đóng đúng `symbol`, `side`, `qty` đang lưu trong tracker.
- Nếu tracker thiếu `qty`: fallback sang đóng toàn bộ vị thế cùng symbol/side.
- Nếu Bybit báo không còn position: bot đóng tracker local và ghi log.

Ví dụ log đúng:

```text
Đã gửi Bybit đóng lệnh #44: BTCUSDT short · qty 0.003 · reduceOnly market.
```

---

## 3. Tính năng kế thừa từ V48

V49 giữ toàn bộ fix của V48:

### 3.1. Tách rõ 3 luồng lệnh

| Luồng | Mục đích | Ví dụ |
|---|---|---|
| Bot Control | Đổi cấu hình bot | `bật dry-run`, `đổi max leverage thành 20` |
| Strategy Prompt | Prompt chạy vòng lặp | `Trade BTC khung 5m, RSI dưới 27...` |
| Direct Execution Command | Hành động ngay | `đóng hết lệnh future btc`, `long btc 10u x20` |

### 3.2. Direct command không bị AI tự biên chiến lược

Các lệnh rõ ràng như:

```text
đóng hết lệnh future btc
close all btc future
đóng short btc future
đóng long btc
long btc 10u x20 tp 10% sl 5%
mua spot btc 20u
```

được parser nội bộ xử lý, không gọi AI phân tích D1/H4/H1/EMA/MACD.

### 3.3. Close futures map chuẩn

| User nhập | Action | Category | Symbol |
|---|---|---|---|
| `đóng hết lệnh future btc` | `CLOSE_ALL` | `linear` | `BTCUSDT` |
| `đóng long btc` | `CLOSE_LONG` | `linear` | `BTCUSDT` |
| `đóng short btc` | `CLOSE_SHORT` | `linear` | `BTCUSDT` |

---

## 4. Biến môi trường khuyến nghị

```env
APP_SECRET=your-fixed-secret
RUNTIME_DIR=/data
DATABASE_URL=sqlite:////data/app.db
DRY_RUN=true
AI_DEBUG_LOGS=false

OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
OPENAI_FALLBACK_MODELS=gpt-4o-mini,gpt-4.1-mini
```

Risk settings nên set trong dashboard hoặc env:

```env
MAX_LEVERAGE=20
MAX_MARGIN_PER_TRADE_USDT=20
MAX_NOTIONAL_USDT=500
MAX_DAILY_TRADES=2
REQUIRE_TP_SL=true
DEFAULT_TAKE_PROFIT_PCT=10
DEFAULT_STOP_LOSS_PCT=5
```

Ghi chú: `DEFAULT_TAKE_PROFIT_PCT` và `DEFAULT_STOP_LOSS_PCT` với Futures được hiểu là `% PNL`, không phải `% giá`.

---

## 5. Cài đặt local

```bash
unzip cig_ai_subaccount_clean_v49.zip
cd cig_ai_subaccount_clean_v49
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Tạo `.env`:

```env
APP_SECRET=local-dev-secret
RUNTIME_DIR=./data
DATABASE_URL=sqlite:///./data/app.db
DRY_RUN=true
AI_DEBUG_LOGS=false
OPENAI_API_KEY=sk-your-key
```

Chạy:

```bash
mkdir -p data
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Mở:

```text
http://localhost:8000
```

---

## 6. Deploy Railway

Đảm bảo root repo có:

```text
Dockerfile
requirements.txt
app.py
railway.json
README.md
```

Railway Volume:

```text
/data
```

Không xoá:

```text
/data/app.db
/data/app.db-wal
/data/app.db-shm
```

Deploy:

```bash
git add .
git commit -m "CIG AI Subaccount Clean V49"
git push
```

---

## 7. Checklist test sau deploy

### Test TP/SL PNL

Nhập direct command:

```text
long btc 10u x20 tp 10% sl 5%
```

Kỳ vọng nếu BTC khoảng 60,000:

```text
TP khoảng 60,300
SL khoảng 59,850
```

Không còn TP 66,000 / SL 57,000.

### Test nút Đóng lệnh

1. Có một lệnh futures đang mở trong tracker.
2. Bấm **Đóng lệnh**.
3. Nếu `DRY_RUN=true`, log phải ghi không gửi Bybit.
4. Nếu `DRY_RUN=false`, log phải ghi đã gửi Bybit đóng lệnh reduce-only.

---

## 8. Lưu ý kỹ thuật

Bybit futures thường quản lý vị thế theo `symbol + side`. Nếu sàn đang ở chế độ net/hedge, tracker sẽ cố đóng đúng lượng `qty` của dòng lệnh. Nếu tracker thiếu qty hoặc Bybit position nhỏ hơn qty tracker, bot sẽ fallback an toàn theo vị thế thực tế.


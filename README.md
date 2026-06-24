# CIG AI Subaccount

CIG AI Subaccount là dashboard AI dùng để quản lý giao dịch Bybit theo từng workspace/sub-account riêng. Mỗi workspace có API key riêng, prompt riêng, cấu hình rủi ro riêng, live log riêng và bảng theo dõi lệnh riêng.

Bản **V60** tập trung vào nâng cấp phần **AI Backtest** để không còn bị kẹt ở parser RSI cũ. Backtest có thể đọc JSON strategy và sử dụng nhiều chỉ báo kỹ thuật khác nhau thông qua **Universal Indicator Backtest Engine**.

> Khuyến nghị: luôn chạy `DRY_RUN=true` khi cài lần đầu hoặc deploy bản mới. Chỉ bật live bằng `DRY_RUN=false` sau khi đã test parser, risk guard, API key và nút đóng lệnh.

---

## 1. Tính năng chính

### Dashboard workspace/sub-account

- Quản lý workspace/user riêng.
- Mỗi workspace có Bybit API key riêng.
- Mỗi workspace có prompt chiến lược riêng.
- Mỗi workspace có cấu hình risk riêng.
- Live log riêng cho từng workspace.
- Bảng theo dõi lệnh riêng.

### Bybit Futures/Spot

- Hỗ trợ Bybit V5.
- Futures Linear mặc định cho BTCUSDT và các cặp allowed symbols.
- Hỗ trợ dry-run để test không gửi lệnh thật.
- Có direct command để mở/đóng lệnh nhanh.
- Nút **Đóng lệnh** có thể gửi reduce-only market order khi live.

### AI Backtest Menu

- Menu riêng **AI Backtest**.
- Nhập symbol hoặc nhiều symbol.
- Chọn market, timeframe, start/end UTC.
- Nhập prompt hoặc JSON strategy để test.
- Nhập vốn test, margin, leverage, fee, slippage.
- Xuất kết quả: winrate, PNL USDT, PNL %, vốn cuối, tổng lệnh, lệnh thắng/thua, max drawdown.
- Có bảng từng lệnh và tải CSV.

### Universal Indicator Backtest Engine V60

V60 hỗ trợ generic condition evaluator để JSON strategy có thể dùng nhiều chỉ báo:

- EMA
- SMA
- WMA
- RSI
- MACD
- MACD Signal
- MACD Histogram
- Bollinger Bands
- VWAP rolling
- ATR
- ADX
- Stochastic %K / %D
- CCI
- ROC
- MFI
- Volume MA

Các biến nến có thể dùng trong condition:

- `open`
- `high`
- `low`
- `close`
- `previous_close`
- `volume_current`
- `body`
- `upper_wick`
- `lower_wick`
- `is_green`
- `is_red`
- `is_doji`

Kết quả đúng của V60 trong cột Reason sẽ có dạng:

```text
Generic indicators: LONG matched
Generic indicators: SHORT matched
```

Nếu vẫn thấy:

```text
Plan LONG: RSI trigger...
Plan SHORT: RSI trigger...
```

thì app vẫn đang chạy engine cũ hoặc JSON strategy chưa kích hoạt generic evaluator.

---

## 2. Cấu trúc project

```text
.
├── app.py
├── backtest_engine.py
├── bybit_client.py
├── ai_engine.py
├── indicator_engine.py
├── risk_guard.py
├── storage.py
├── requirements.txt
├── Dockerfile
├── railway.json
├── .env.example
├── templates/
├── static/
└── README.md
```

Các file quan trọng:

| File | Chức năng |
|---|---|
| `app.py` | FastAPI app, dashboard, route API, backtest endpoint |
| `backtest_engine.py` | Universal Indicator Backtest Engine V60 |
| `bybit_client.py` | Bybit V5 client |
| `ai_engine.py` | AI decision/compile logic |
| `indicator_engine.py` | Tính indicator cho live/backtest |
| `risk_guard.py` | Kiểm soát rủi ro |
| `storage.py` | SQLite storage |
| `templates/` | HTML dashboard |
| `static/` | CSS/assets |

---

## 3. Biến môi trường

Tạo file `.env` từ `.env.example`.

### Cấu hình app

```env
APP_SECRET=change-this-secret
RUNTIME_DIR=./data
DATABASE_URL=sqlite:///./data/app.db
DRY_RUN=true
AI_DEBUG_LOGS=false
```

### OpenAI

```env
OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
OPENAI_FALLBACK_MODELS=gpt-4o-mini,gpt-4.1-mini
```

### Bybit

API key có thể nhập trong dashboard theo từng workspace. Nếu muốn dùng env mặc định:

```env
BYBIT_API_KEY=your-bybit-api-key
BYBIT_API_SECRET=your-bybit-api-secret
BYBIT_TESTNET=false
```

### Risk settings

```env
DEFAULT_CATEGORY=linear
ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT
MAX_LEVERAGE=20
MAX_MARGIN_PER_TRADE_USDT=20
MAX_NOTIONAL_USDT=300
MAX_DAILY_TRADES=10
REQUIRE_TP_SL=true
DEFAULT_TAKE_PROFIT_PCT=10
DEFAULT_STOP_LOSS_PCT=5
```

Ghi chú:

- Với Futures, TP/SL trong live trading nên được hiểu theo `% PNL/ROI trên margin` nếu code route live đang dùng converter ROI.
- Trong backtest V60, nhiều strategy JSON dùng `take_profit_price_percent` và `stop_loss_price_percent`, tức là **% theo giá**. Với leverage 10x, `TP 0.75% giá` xấp xỉ `+7.5% margin` trước phí.

---

## 4. Cài đặt local

### 4.1. Yêu cầu

- Python 3.10+
- Git
- Tài khoản OpenAI nếu dùng AI compile/decision
- Tài khoản Bybit nếu test API hoặc trade thật

### 4.2. Clone hoặc giải nén source

Nếu dùng Git:

```bash
git clone <YOUR_REPO_URL>
cd <YOUR_REPO_FOLDER>
```

Nếu dùng file ZIP:

```bash
unzip cig_ai_subaccount_clean_v60_full_clean.zip
cd cig_ai_subaccount_clean_v60_full_clean
```

### 4.3. Tạo virtual environment

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows CMD:

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

### 4.4. Cài thư viện

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4.5. Tạo file `.env`

macOS/Linux:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
copy .env.example .env
```

Sau đó sửa `.env`:

```env
APP_SECRET=local-dev-secret
RUNTIME_DIR=./data
DATABASE_URL=sqlite:///./data/app.db
DRY_RUN=true
AI_DEBUG_LOGS=false
OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
```

### 4.6. Tạo thư mục data

macOS/Linux:

```bash
mkdir -p data
```

Windows PowerShell:

```powershell
mkdir data
```

### 4.7. Chạy app local

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Mở trình duyệt:

```text
http://localhost:8000
```

### 4.8. Kiểm tra compile trước khi chạy

```bash
python -m py_compile app.py backtest_engine.py bybit_client.py ai_engine.py indicator_engine.py storage.py risk_guard.py
```

Nếu lệnh này không báo lỗi là source Python cơ bản ổn.

---

## 5. Cài đặt bằng Docker local

Build image:

```bash
docker build -t cig-ai-subaccount:v60 .
```

Chạy container:

```bash
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/data \
  cig-ai-subaccount:v60
```

Trên Windows PowerShell, volume có thể dùng:

```powershell
docker run --rm -p 8000:8000 --env-file .env -v ${PWD}\data:/data cig-ai-subaccount:v60
```

Nếu dùng Docker với `/data`, `.env` nên set:

```env
RUNTIME_DIR=/data
DATABASE_URL=sqlite:////data/app.db
```

---

## 6. Deploy Railway

### 6.1. Chuẩn bị repo

Root repo cần có:

```text
Dockerfile
requirements.txt
app.py
railway.json
README.md
```

Commit code:

```bash
git add .
git commit -m "Deploy CIG AI Subaccount V60"
git push
```

### 6.2. Tạo Railway project

1. Vào Railway.
2. Chọn **New Project**.
3. Chọn **Deploy from GitHub repo**.
4. Chọn repo chứa source.
5. Railway sẽ dùng `Dockerfile` để build.

### 6.3. Tạo Railway Volume

Tạo volume mount vào:

```text
/data
```

Không xoá các file sau trong volume:

```text
/data/app.db
/data/app.db-wal
/data/app.db-shm
/data/strategy_state/
```

Đây là dữ liệu workspace, cấu hình user, state chiến lược và SQLite WAL/SHM.

### 6.4. Set biến môi trường trên Railway

Trong tab Variables, set:

```env
APP_SECRET=your-production-secret
RUNTIME_DIR=/data
DATABASE_URL=sqlite:////data/app.db
DRY_RUN=true
AI_DEBUG_LOGS=false
OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
OPENAI_FALLBACK_MODELS=gpt-4o-mini,gpt-4.1-mini
DEFAULT_CATEGORY=linear
ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT
MAX_LEVERAGE=20
MAX_MARGIN_PER_TRADE_USDT=20
MAX_NOTIONAL_USDT=300
MAX_DAILY_TRADES=10
REQUIRE_TP_SL=true
```

Nếu muốn live trading, sau khi test xong mới đổi:

```env
DRY_RUN=false
```

### 6.5. Deploy

Railway sẽ tự build khi repo có commit mới.

Sau deploy, mở domain Railway hoặc custom domain và kiểm tra:

- Dashboard load được.
- Vào được **AI Backtest**.
- SQLite không báo lỗi permission.
- Live log ghi được.
- Backtest có thể tải kline public.

---

## 7. Checklist test sau deploy

### 7.1. Test app boot

Mở Railway logs, kỳ vọng không có lỗi import hoặc lỗi SQLite.

Nếu có lỗi database path, kiểm tra:

```env
RUNTIME_DIR=/data
DATABASE_URL=sqlite:////data/app.db
```

### 7.2. Test DRY_RUN

Đảm bảo biến:

```env
DRY_RUN=true
```

Sau đó thử direct command hoặc backtest. Bot không được gửi order thật.

### 7.3. Test AI Backtest V60

Vào menu **AI Backtest** và dán JSON mẫu:

```json
{
  "strategy_name": "BTCUSDT_15M_EMA_TREND_PULLBACK_GENERIC_V1",
  "strategy_type": "generic_condition_engine",
  "market": {
    "symbol": "BTCUSDT",
    "market": "linear_futures",
    "timeframe": "15m",
    "mode": "backtest_ai_once"
  },
  "capital": {
    "margin_per_trade_usdt": 10,
    "max_leverage": 10
  },
  "tp_sl": {
    "take_profit_price_percent": 0.75,
    "stop_loss_price_percent": 0.45
  },
  "indicators": {
    "ema20": { "type": "ema", "period": 20 },
    "ema50": { "type": "ema", "period": 50 },
    "ema200": { "type": "ema", "period": 200 },
    "rsi14": { "type": "rsi", "period": 14 },
    "adx14": { "type": "adx", "period": 14 },
    "atr14": { "type": "atr", "period": 14 },
    "volume_ma20": { "type": "volume_ma", "period": 20 }
  },
  "global_filters": {
    "required": [
      "adx14 >= 15",
      "adx14 <= 38",
      "volume_current >= volume_ma20 * 0.8",
      "current candle is not doji"
    ]
  },
  "long_rule": {
    "enabled": true,
    "all_required": [
      "close > ema200",
      "ema20 > ema50",
      "ema50 > ema200",
      "ema20_slope > 0",
      "rsi14 >= 42",
      "rsi14 <= 62",
      "current candle is green",
      "current close > previous close",
      "distance_from_close_to_ema20_percent <= 0.75"
    ]
  },
  "short_rule": {
    "enabled": true,
    "all_required": [
      "close < ema200",
      "ema20 < ema50",
      "ema50 < ema200",
      "ema20_slope < 0",
      "rsi14 >= 38",
      "rsi14 <= 58",
      "current candle is red",
      "current close < previous close",
      "distance_from_close_to_ema20_percent <= 0.75"
    ]
  }
}
```

Kết quả đúng phải có Reason kiểu:

```text
Generic indicators: LONG matched
Generic indicators: SHORT matched
```

Nếu vẫn thấy:

```text
Plan LONG: RSI trigger...
```

thì Railway đang chạy bản cũ hoặc prompt không kích hoạt generic evaluator.

### 7.4. Test direct command futures TP/SL

Nhập thử:

```text
long btc 10u x20 tp 10% sl 5%
```

Với Futures, kỳ vọng TP/SL được quy đổi theo PNL/ROI trên margin nếu direct command converter đang bật:

```text
TP price move = 10% / 20 = 0.5%
SL price move = 5% / 20 = 0.25%
```

### 7.5. Test nút đóng lệnh

1. Có lệnh trong tracker.
2. Bấm **Đóng lệnh**.
3. Nếu `DRY_RUN=true`, log phải ghi không gửi Bybit.
4. Nếu `DRY_RUN=false`, log phải ghi đã gửi reduce-only market order.

---

## 8. JSON strategy condition syntax

V60 hỗ trợ condition dạng string:

```text
close > ema200
ema20 > ema50
rsi14 >= 55
adx14 <= 25
volume_current >= volume_ma20 * 0.8
distance_from_close_to_ema20_percent <= 0.75
current candle is green
current candle is red
current candle is not doji
current close > previous close
current close < previous close
ema20_slope > 0
ema20_slope < 0
```

Hoặc dạng object:

```json
{
  "left": "close",
  "op": ">",
  "right": "ema200"
}
```

Các toán tử hỗ trợ:

```text
>
>=
<
<=
==
!=
```

---

## 9. Prompt mẫu multi-symbol RSI 5M

```text
Bạn là CIG AI Trading Agent giao dịch Perpetual Futures trên Bybit.

CHẾ ĐỘ:
- Strategy type: Multi-Symbol RSI 5M Candle Confirmation.
- Chỉ được dùng dữ liệu khung 5m.
- Chỉ được dùng RSI 5m và màu nến 5m.
- Không được dùng D1, H4, H1, M15.
- Không được dùng EMA, MACD, ATR, VWAP, Support/Resistance.
- Không được tự thêm chiến lược khác.

THỊ TRƯỜNG:
- Market: Futures linear.
- Margin mode: Isolated.
- Danh sách cặp được phép trade: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT.

THIẾT LẬP:
- Leverage: 20x.
- Margin futures mỗi lệnh: 5 USDT.
- TP: 10% PNL trên margin.
- SL: 5% PNL trên margin.
- Timeframe chính: 5m.
- Vòng lặp quét: mỗi 5 phút.
- Chỉ dùng nến 5m đã đóng.
- Tối đa 2 lệnh mỗi vòng quét.

RULE LONG:
1. Với từng cặp, nếu RSI 5m của nến đã đóng nhỏ hơn 27 thì bật LONG_WATCH cho cặp đó.
2. Sau khi LONG_WATCH bật, đếm nến 5m tiếp theo của đúng cặp đó.
3. Nếu có 2 nến 5m đã đóng liên tiếp màu xanh thì OPEN_LONG.
4. Nến xanh là close > open.
5. Nếu xuất hiện nến đỏ hoặc doji trước khi đủ 2 nến xanh thì hủy LONG_WATCH.

RULE SHORT:
1. Với từng cặp, nếu RSI 5m của nến đã đóng lớn hơn 66 thì bật SHORT_WATCH cho cặp đó.
2. Sau khi SHORT_WATCH bật, đếm nến 5m tiếp theo của đúng cặp đó.
3. Nếu có 2 nến 5m đã đóng liên tiếp màu đỏ thì OPEN_SHORT.
4. Nến đỏ là close < open.
5. Nếu xuất hiện nến xanh hoặc doji trước khi đủ 2 nến đỏ thì hủy SHORT_WATCH.

QUẢN TRỊ RỦI RO:
- Không mở thêm nếu cặp đó đang có vị thế.
- Không mở quá giới hạn Risk Guard.
- Không martingale.
- Không nhồi lệnh.
- Không tăng margin sau khi thua.

YÊU CẦU ĐẦU RA:
Chỉ trả JSON hợp lệ. Không markdown.
```

---

## 10. Lịch sử nâng cấp chính

### V49

- Futures TP/SL `%` được tính theo `% PNL/ROI trên margin`, không còn tính trực tiếp theo `% giá BTC`.
- Nút **Đóng lệnh** gửi reduce-only order lên Bybit khi live.

### V54

- Clean live log cho Multi-Symbol RSI Watch Engine.
- Log đổi từ `AI:` sang `Rule Engine:` khi không gọi AI thật.
- Không còn hiển thị Python list bị cắt cụt trong log nến.

### V55

- Thêm menu **AI Backtest**.
- Backtest dùng dữ liệu Bybit public kline, không gửi order thật.

### V57

- Sửa range backtest cho AI-once/rule mode.
- Symbol input hỗ trợ một symbol hoặc nhiều symbol cách nhau bằng dấu phẩy/khoảng trắng.

### V58

- Thêm preset winrate cao.
- Thêm wait/block metrics.

### V59

- Fix JSON plan parse.
- Hỗ trợ dùng JSON strategy trực tiếp từ prompt, không cần AI compile.

### V60

- Thêm Universal Indicator Backtest Engine.
- Hỗ trợ generic condition evaluator.
- Có thể dùng EMA, SMA, WMA, RSI, MACD, Bollinger, VWAP, ATR, ADX, Stochastic, CCI, ROC, MFI, Volume MA.

---

## 11. Lưu ý an toàn

- Backtest không đảm bảo lợi nhuận khi live.
- Kết quả backtest có thể khác live do spread, slippage, funding, latency và orderbook.
- Không nên all-in hoặc martingale.
- Luôn test với `DRY_RUN=true` trước.
- Nếu dùng futures leverage, TP/SL phải kiểm tra đúng đơn vị: `% giá` khác `% PNL trên margin`.
- Không xoá Railway Volume `/data` nếu muốn giữ workspace/database.

---

## 12. Troubleshooting

### Lỗi merge conflict

Nếu thấy:

```text
<<<<<<< HEAD
=======
>>>>>>> branch-name
```

Không bấm Continue Merge. Chạy:

```bash
git merge --abort
git reset --hard HEAD
```

Sau đó dùng source sạch hoặc resolve thủ công.

### Backtest vẫn chạy RSI parser cũ

Dấu hiệu:

```text
Plan LONG: RSI trigger...
```

Cách xử lý:

1. Kiểm tra đã deploy đúng V60 chưa.
2. Kiểm tra `backtest_engine.py` có generic evaluator chưa.
3. Dùng JSON có:

```json
"strategy_type": "generic_condition_engine"
```

4. Deploy lại Railway và kiểm tra logs.

### JSON strategy không chạy

Nếu thấy:

```text
AI plan lỗi, dùng local parser plan: JSONDecodeError
```

Cách xử lý:

- Đảm bảo JSON không có markdown fence.
- Không thêm chữ trước/sau JSON.
- Dùng V59/V60 để parse JSON trực tiếp.

### SQLite database locked

- Không chạy nhiều process ghi cùng DB nếu không cần.
- Đảm bảo Railway chỉ chạy một replica nếu dùng SQLite.
- Không xoá WAL/SHM khi app đang chạy.

### Railway không lưu dữ liệu

Kiểm tra biến:

```env
RUNTIME_DIR=/data
DATABASE_URL=sqlite:////data/app.db
```

Kiểm tra Railway Volume đã mount vào `/data`.

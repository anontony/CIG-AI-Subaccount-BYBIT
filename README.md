<<<<<<< HEAD
# CIG AI Subaccount Clean V54

Bản V54 nâng cấp từ V53, tập trung vào **clean live log cho Multi-Symbol RSI Watch Engine** và sửa lỗi log `3 nến mới` bị hiển thị dạng Python list/cắt cụt.

## Fix chính trong V54
=======
# CIG AI Subaccount Clean V49

Bản V49 dựa trên V48 và sửa đúng 2 điểm quan trọng:

1. **TP/SL dạng `%` cho Futures được tính theo % PNL/ROI trên margin**, không còn tính trực tiếp theo % giá BTC.
2. **Nút “Đóng theo dõi” trong bảng lệnh đổi thành “Đóng lệnh”** và khi bấm sẽ gửi lệnh reduce-only lên Bybit để đóng đúng vị thế/lượng đang được tracker ghi nhận.

> Khuyến nghị: deploy trước với `DRY_RUN=true`, test parser + nút đóng lệnh, sau đó mới bật live bằng `DRY_RUN=false`.
>>>>>>> 78582718eada4d79132653992ba32f60d5dbdc92

### 0. Clean Live Log cho Rule Engine

<<<<<<< HEAD
V54 đổi log của prompt-only/rule-engine từ nhãn `AI:` sang `Rule Engine:` để tránh hiểu nhầm là bot đang gọi AI.

Trước V54:

```text
AI: WAIT MULTI · Chưa có cặp nào đủ điều kiện... BNBUSDT: ... 3 nến mới=['green', | BTCUSDT: ...
```

Sau V54:

```text
Rule Engine: WAIT MULTI · Chưa có cặp nào đủ điều kiện RSI 5m + 2 nến xác nhận. · BNBUSDT: RSI5=47.80 · watch=NONE · nến gần nhất=red · 3 nến=green,red,red · trigger LONG<27.0 / SHORT>66.0 | BTCUSDT: RSI5=46.99 · watch=NONE · nến gần nhất=red · 3 nến=red,green,red · trigger LONG<27.0 / SHORT>66.0
```

Điểm sửa:

- Không còn dùng Python list repr như `['green', 'red']` trong live log.
- Không còn bị cắt ở `['green',` làm tưởng chỉ có 1 nến.
- Mỗi symbol hiển thị đủ: RSI5, trạng thái watch, nến gần nhất, 3 nến gần nhất, trigger cần đạt.
- Khi rule engine xử lý xong, log ghi `Rule Engine đã xử lý xong`, không ghi `AI đã phân tích xong`.


### 1. Strategy loop trade nhiều cặp

V52 đã có `LONG_WATCH` / `SHORT_WATCH`, nhưng chủ yếu chạy đúng cho một symbol chính như `BTCUSDT`.

V53 sửa thành quét nhiều symbol futures linear theo `allowed_symbols` hoặc danh sách symbol trong prompt.

Ví dụ:

```text
BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT
```

Mỗi vòng quét, bot sẽ lấy snapshot riêng cho từng cặp:

```text
linear:BTCUSDT
linear:ETHUSDT
linear:SOLUSDT
linear:BNBUSDT
linear:XRPUSDT
```

### 2. State riêng cho từng cặp

Mỗi cặp có file trạng thái riêng trong Railway Volume:

```text
/data/strategy_state/user_<id>_BTCUSDT_rsi5m_watch.json
/data/strategy_state/user_<id>_ETHUSDT_rsi5m_watch.json
/data/strategy_state/user_<id>_SOLUSDT_rsi5m_watch.json
```

Logic đúng:

```text
RSI 5m < oversold
→ bật LONG_WATCH cho đúng symbol đó
→ đếm 2 nến 5m tiếp theo của đúng symbol đó
→ nếu đủ 2 nến xanh liên tiếp thì OPEN_LONG symbol đó
```

```text
RSI 5m > overbought
→ bật SHORT_WATCH cho đúng symbol đó
→ đếm 2 nến 5m tiếp theo của đúng symbol đó
→ nếu đủ 2 nến đỏ liên tiếp thì OPEN_SHORT symbol đó
```

### 3. Có thể mở nhiều tín hiệu trong cùng một vòng quét

Nếu nhiều cặp cùng hoàn tất xác nhận trong một vòng quét, engine có thể trả về `BATCH`:

```json
{
  "action": "BATCH",
  "symbol": "MULTI",
  "actions": [
    {"action": "OPEN_SHORT", "symbol": "BTCUSDT"},
    {"action": "OPEN_LONG", "symbol": "ETHUSDT"}
  ]
}
```

`analyze_and_execute` đã hỗ trợ batch và sẽ xử lý từng tín hiệu qua Risk Guard trước khi gửi Bybit.

### 4. Prompt-only vẫn được giữ

Nếu prompt chỉ yêu cầu:

```text
5m + RSI + màu nến 5m
```

bot vẫn không dùng:

```text
D1 / H4 / H1 / M15
EMA / MACD / ATR / VWAP / Support / Resistance
```

### 5. Live Log dễ đọc hơn cho multi-symbol

Log market sẽ hiển thị nhiều cặp:

```text
Market MULTI LINEAR · BTCUSDT giá ... · 5M: RSI ... | ETHUSDT giá ... · 5M: RSI ... | SOLUSDT giá ...
```

## Cấu hình cần set trong dashboard/Railway

Để trade nhiều cặp, cần set `allowed_symbols` trong dashboard hoặc env:

```env
ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT
DEFAULT_CATEGORY=linear
MAX_LEVERAGE=20
MAX_MARGIN_PER_TRADE_USDT=20
MAX_NOTIONAL_USDT=300
MAX_DAILY_TRADES=10
DRY_RUN=true
```

> Nếu workspace cũ đang lưu `allowed_symbols=BTCUSDT,ETHUSDT` trong `/data/app.db`, deploy code mới sẽ không tự ghi đè database cũ. Hãy vào dashboard sửa Allowed Symbols thủ công.

## Prompt mẫu multi-symbol

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

## Giữ nguyên các fix trước

- V48: Direct Command không bị control/allowed_symbols bắt nhầm.
- V49: TP/SL futures `%` tính theo `% PNL` trên margin.
- V50: fix lỗi thiếu `static/` khi deploy Railway.
- V51: Prompt-only không lấy D1/H4/H1/M15 hoặc EMA/MACD/ATR nếu prompt cấm.
- V52: Stateful RSI Watch cho một symbol.
- V53: Multi-Symbol RSI Watch + batch execution.

## Deploy Railway
=======
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
>>>>>>> 78582718eada4d79132653992ba32f60d5dbdc92

1. Giải nén zip.
2. Push repo lên GitHub.
3. Railway deploy từ repo.
4. Không xoá Railway Volume `/data`.
5. Không xoá `/data/app.db`.
6. Vào dashboard sửa Allowed Symbols nếu workspace cũ chưa có đủ cặp.

<<<<<<< HEAD

=======
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
>>>>>>> 78582718eada4d79132653992ba32f60d5dbdc92

## V55 - AI Backtest Menu

Bản này gắn thêm menu **AI Backtest** trực tiếp vào dashboard gốc V54.

Tính năng mới:

- Menu riêng `AI Backtest` trong sidebar.
- Nhập symbol, market, khung thời gian, start/end UTC.
- Nhập prompt muốn test hoặc dùng prompt đang lưu.
- Nhập vốn test, margin mỗi lệnh, leverage, fee, slippage.
- Dùng lại parser prompt, DecisionEngine/OpenAI và risk context của bot trade thật.
- Với prompt RSI 5m + 2 nến xác nhận, backtest dùng Rule Engine stateful riêng trong RAM, không đụng state live trong `/data/strategy_state`.
- Không gửi order thật lên Bybit; chỉ dùng dữ liệu public kline để giả lập.
- Xuất kết quả: winrate, PNL USDT, PNL %, vốn cuối, tổng lệnh, lệnh thắng/thua, max drawdown, từng lệnh chi tiết và CSV.

Endpoint mới:

```text
POST /api/backtest/run
```

Lưu ý:

<<<<<<< HEAD
- Backtest dùng OHLCV, không phải tick/orderbook.
- Nếu cùng một nến chạm cả TP và SL, engine tính SL trước để bảo thủ.
- Backtest nhiều nến bằng AI có thể tốn token; UI có trường giới hạn số nến gọi AI.


## V57 Backtest fixes

- Fixed ai_once/rule backtest range: `max_ai_candles` now limits only `AI từng nến`; AI-once and Rule mode process the full requested candle range.
- Added explicit result range fields: requested range, loaded data range, and actual processed range.
- Backtest Symbol input now accepts one symbol or a comma/space-separated list such as `BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT`.
- Multi-symbol backtest aggregates metrics and keeps the symbol column in every trade row. In multi-symbol mode each symbol receives the configured test capital separately.


## V58 High-Winrate Backtest Preset

- Added a Backtest preset button: `Preset Winrate cao`.
- Added deterministic high-winrate plan support for AI-once backtest mode.
- Added filters: RSI reclaim/reject, EMA20/EMA50 alignment, trend filter, distance to EMA50, ATR band, volume-not-low, and cooldown candles.
- Added `entry_cooldown_candles` to the Backtest API payload.
- Backtest metrics now include `wait_count` and `blocked_count` aliases for aggregation consistency.

Important: this preset is designed to reduce weak entries and improve measured winrate, not to guarantee profit or future live performance.
=======
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

>>>>>>> 78582718eada4d79132653992ba32f60d5dbdc92

# CIG AI Subaccount Clean V44

Bản V44 dọn lại live log để dashboard dễ đọc hơn sau khi tầng AI đã chạy ổn. Mặc định bot không còn in raw JSON snapshot/RAW AI response/PARSED SIGNAL ra Live Log. Các dòng đó chỉ hiện khi bật `AI_DEBUG_LOGS=true`.

## Điểm mới V44

- Live Log mặc định chỉ hiển thị các dòng ngắn:
  - Bot bắt đầu chạy
  - Bybit OK
  - Tóm tắt thị trường theo D1 / H4 / H1 / 15m
  - Quyết định AI: WAIT / OPEN_LONG / OPEN_SHORT
  - Kết quả lệnh hoặc lý do bị Risk Guard chặn
- Raw debug JSON được chuyển sang chế độ debug ẩn mặc định.
- Thêm biến môi trường `AI_DEBUG_LOGS=false`. Khi cần điều tra lỗi, đổi thành `true` để hiện lại:
  - `AI DEBUG · MARKET SNAPSHOT SENT TO AI`
  - `AI DEBUG · RAW AI RESPONSE`
  - `AI DEBUG · PARSED SIGNAL`
- Xoá `__pycache__` khỏi gói clean.

## Cấu hình khuyến nghị

```env
OPENAI_MODEL=gpt-4o-mini
OPENAI_FALLBACK_MODELS=gpt-4o-mini,gpt-4.1-mini
AI_DEBUG_LOGS=false
```

---

# CIG AI Subaccount Clean V43

Bản V43 sửa tầng AI: ưu tiên strict function calling `submit_trading_signal`, fallback model chain (`OPENAI_FALLBACK_MODELS`), và deterministic recovery rõ lý do khi model trả `{}`.

# CIG AI Subaccount Clean V41

Bản V41 vá lỗi AI vẫn trả `{}` sau khi đổi prompt nới lỏng.

## Sửa chính

- Structured Output chuyển sang schema strict hơn, bắt buộc có `action` và `reason`.
- Nếu AI vẫn trả `{}`, bot retry bằng payload rút gọn chỉ tập trung `linear:BTCUSDT`, không gửi lại toàn bộ prompt dài.
- Retry bắt buộc trả một trong: `WAIT`, `OPEN_LONG`, `OPEN_SHORT`.
- Nếu mở lệnh, bắt buộc có `stop_loss`, `take_profit`, `margin_usdt=8`, `risk_usdt=1`, `leverage=10`.
- Nếu không đủ điều kiện, trả `WAIT` với ít nhất 2 lý do cụ thể từ snapshot.
- Vẫn không dùng TP/SL mặc định khi prompt yêu cầu TP/SL theo ATR/RR/structure.

## Test cần thấy

`AI DEBUG · RAW AI RESPONSE` không nên còn là `{}`.  
Nếu setup chưa đủ, raw response nên là JSON `WAIT` có reason chi tiết.

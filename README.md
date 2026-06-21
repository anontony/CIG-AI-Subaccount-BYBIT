# CIG AI Subaccount Clean V48

## Fix chính trong V48

### 1. Tách rõ 3 luồng lệnh

V48 tách riêng:

- **Bot Control**: đổi prompt, đổi max leverage, đổi allowed symbols, bật/tắt dry-run.
- **Strategy Prompt**: prompt chạy vòng lặp tự động.
- **Direct Execution Command**: lệnh trực tiếp như `đóng hết lệnh future btc`, `long BTC 10u x20`, `mua spot BTC 20u`.

Trước V48, direct command có thể bị control parser hoặc AI strategy reasoning bắt nhầm.

### 2. Direct Command không còn bị AI tự biên chiến lược

Nếu câu lệnh là hành động trực tiếp rõ ràng, bot dùng parser nội bộ để tạo action, **không gọi AI**. Điều này tránh lỗi AI tự dùng D1/H4/H1, EMA, MACD hoặc prompt đang lưu để suy luận ngoài ý user.

Ví dụ các câu sau được xử lý trực tiếp:

```text
đóng hết lệnh future btc
close all btc future
đóng short btc future
đóng long btc
long btc 10u x20 tp 10% sl 5%
mua spot btc 20u
```

### 3. Fix lỗi allowed_symbols/control bắt nhầm

Những lệnh như:

```text
đóng hết lệnh future btc
```

sẽ không còn bị hiểu nhầm thành lệnh chỉnh `allowed_symbols` hoặc bot control.

### 4. Close futures chuẩn hơn

Các lệnh đóng vị thế:

```text
đóng hết lệnh future btc
đóng long btc
đóng short btc
```

sẽ map thành:

```json
CLOSE_ALL / CLOSE_LONG / CLOSE_SHORT
category: linear
symbol: BTCUSDT
```

## Ghi chú deploy Railway

- Dockerfile nằm ngay root.
- Không kèm database runtime thật.
- Giữ nguyên cấu trúc FastAPI cũ.
- Nếu đang chạy Railway Volume `/data`, không xoá `/data/app.db` khi deploy.

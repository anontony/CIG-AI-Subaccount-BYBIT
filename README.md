# CIG AI Subaccount Clean V30

Bản sạch của CIG AI Subaccount với vá lỗi TP/SL tracker.

## Điểm sửa chính V30

- TP/SL tracker không chỉ báo chạm nữa mà có thêm lớp `enforce_tracked_tp_sl`.
- Khi bot đang chạy, mỗi vòng sẽ kiểm tra các lệnh đang theo dõi.
- Nếu lệnh Futures chạm TP hoặc SL:
  - `dry_run=True`: chỉ đóng trạng thái theo dõi.
  - `dry_run=False`: gửi lệnh đóng vị thế live qua Bybit `close_position`, sau đó đóng toàn bộ tracking cùng symbol/side.
- Nếu Bybit báo không còn position, bot hiểu có thể Bybit TP/SL đã tự đóng và sẽ đóng tracking tương ứng.
- Spot TP/SL vẫn chỉ theo dõi hiển thị, chưa tự OCO/auto sell.

## Lưu ý quan trọng

Nếu anh đang test, nên bật Dry Run. Nếu `dry_run=False`, khi tracker phát hiện SL/TP chạm, bot có thể gửi lệnh đóng vị thế thật.

## Railway

Dùng Railway Volume `/data` nếu muốn lưu user/config/API. Muốn sạch hoàn toàn thì tạo Volume mới.

## Local

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python server.py
```

Mở: `http://localhost:8000`

## Ghi chú V30.1

- Bot loop giờ kiểm tra TP/SL tracker trong lúc chờ vòng chiến lược tiếp theo.
- Mặc định kiểm tra khoảng mỗi 10 giây khi bot đang Start.
- Điều này tránh lỗi chiến lược chạy mỗi 30 phút nhưng SL/TP đã chạm mà bot chưa đóng.

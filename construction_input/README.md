# Dữ liệu đầu vào công trình (ví dụ)

Thư mục này chứa CSV mẫu để gắn vào popup **Công trình** (cột *File điều khiển* + *Cột dữ liệu*).

Đường dẫn trong khai báo dùng tương đối so với thư mục `flood_model`, ví dụ:

`construction_input/gate_opening_GT1.csv`

## File chuỗi thời gian

| File | Cột giá trị | Dùng cho | Ý nghĩa |
|------|-------------|----------|---------|
| `gate_opening_GT1.csv` | `gate_opening_m` | Cống điều tiết (`gate`) | Độ mở cửa (m) theo giờ |
| `pump_Q_PM1.csv` | `q_m3s` | Trạm bơm (`pump`) | Lưu lượng bơm (m³/s) |
| `reservoir_level_RS1.csv` | `level_m` | Đập / hồ (`reservoir`) | Mực nước hồ tham chiếu (m) |
| `weir_control_WR1.csv` | `opening_factor` | Đập/tràn (tùy chọn) | Hệ số 0–1 (ví dụ điều khiển) |

Định dạng chung (giống biên MIKE / biên mô hình):

```csv
hour,gate_opening_m
0,0.3
1,0.3
...
```

- `hour`: giờ mô phỏng (số thực)
- Cột giá trị: tên bất kỳ, nhưng phải khớp **Cột dữ liệu** trên form

Chuỗi mẫu dài **168 bước** (`hour` = 0 … 167), bước 1 giờ — cùng độ dài với biên H / Q.

## File khai báo công trình

`constructions_demo.csv` — bảng đầy đủ các công trình mẫu, đã trỏ sẵn file trên.

## Cách dùng nhanh trong UI

1. Mở **Công trình**
2. Với dòng **GT_1**: File = `construction_input/gate_opening_GT1.csv`, Cột = `gate_opening_m`
3. Với dòng **PM_1**: File = `construction_input/pump_Q_PM1.csv`, Cột = `q_m3s`
4. Với dòng **RS_1**: File = `construction_input/reservoir_level_RS1.csv`, Cột = `level_m`
5. **Lưu công trình**

Hoặc nhập hàng loạt từ `constructions_demo.csv` vào DB/`constructions.csv` (cùng schema cột).

- Xóa từng dòng (×) đến hết rồi **Lưu công trình**: danh sách rỗng — Model 1D chạy sông tự do (không cấu trúc).
- **Mặc định**: đưa lại bộ công trình mẫu.

## Loại nào cần / không cần file

| Loại | File time-series |
|------|------------------|
| Đê / tràn bãi | Không bắt buộc (chỉ hình học đỉnh, dài, C) |
| Đập / tràn | Không bắt buộc nếu chế độ *Tự do* |
| Cống điều tiết | Có nếu *Điều khiển* theo giờ |
| Cống hộp | Không bắt buộc |
| Đập / hồ chứa | Có thể gắn chuỗi mực nước tham chiếu |
| Trạm bơm | **Bắt buộc** chuỗi Q (hoặc điền Q max cố định) |

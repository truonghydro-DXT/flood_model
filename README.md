# Mô hình ngập lụt

Ứng dụng web Flask để xem địa hình DEM, khảo sát mặt cắt và mô phỏng dòng chảy/ngập lụt. Giao diện có bản đồ địa hình 2D/3D, lớp nền OpenStreetMap, công cụ đo đạc, công trình thủy lợi, mô hình mưa–dòng chảy và mô hình thủy lực.

## Tính năng

- Hiển thị DEM ở chế độ 2D và 3D; xem cao độ, đường đồng mức và mặt cắt.
- Vẽ mặt cắt ngang hoặc xem dọc sông; tra cứu tọa độ và đo chiều dài.
- Chạy mô hình mưa–dòng chảy RR (TANK) hoặc NAM.
- Chạy mô hình thủy lực 1D Saint-Venant và xem kết quả trên địa hình.
- Mô phỏng độ sâu ngập 2D từ kết quả thủy lực và DEM; phát kết quả theo thời gian hoặc xuất video.
- Khai báo/xem công trình và hồ chứa.
- Đọc và lưu dữ liệu chuỗi thời gian, mặt cắt và thủy văn bằng PostgreSQL khi được cấu hình.

## Yêu cầu

- Windows 10/11
- Python và Python Launcher (`py`)
- Git (chỉ cần khi clone hoặc đẩy dự án lên GitHub)
- PostgreSQL nếu cần các chức năng lưu trữ/đọc dữ liệu qua cơ sở dữ liệu

## Cài đặt

Mở PowerShell tại thư mục dự án:

```powershell
cd D:\WORK\flood_model
py -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Chạy ứng dụng web

Từ thư mục dự án, chạy một trong hai cách:

```powershell
.\venv\Scripts\python.exe app.py
```

hoặc:

```powershell
.\run.bat
```

Mở trình duyệt tại [http://127.0.0.1:5001/](http://127.0.0.1:5001/). Cổng mặc định là `5001`; có thể đổi bằng biến môi trường `FLOOD_PORT`.

Để dừng máy chủ, nhấn `Ctrl+C` trong cửa sổ PowerShell.

## Quy trình mô phỏng gợi ý

1. Mở ứng dụng và kiểm tra DEM, dữ liệu mặt cắt và dữ liệu đầu vào.
2. Chạy **Model RR** (TANK) hoặc **Model NAM** để tạo dòng chảy.
3. Chạy **Model 1D** để tính mực nước/dòng chảy trên sông.
4. Chọn **Ngập lụt** để tính và hiển thị vùng ngập trên DEM.
5. Dùng thanh thời gian để xem diễn biến; chọn **Tạo video** nếu cần xuất video.

Kết quả mô phỏng phụ thuộc vào DEM, dữ liệu thủy văn, hình học sông và bộ tham số đầu vào. Hãy kiểm tra và hiệu chỉnh dữ liệu trước khi sử dụng kết quả cho mục đích chuyên môn.

## Dữ liệu dự án

- DEM mặc định: `projects/data/dem-song-hong.tif`
- Dữ liệu mưa–dòng chảy: `rainfall_runoff_output/`
- Dữ liệu mô hình Saint-Venant: `saint_venant_output/`
- Kết quả mô phỏng ngập: `flood_output/`
- Kết quả MIKE: `mike_hd_output/`
- Dữ liệu Muskingum: `muskingum_output/`
- Tệp cơ sở dữ liệu mẫu/backup: `data_flood.sql`, `data_flood.dump`

Một số mô hình cần các tệp CSV đầu vào/kết quả tương ứng trong các thư mục trên. Nếu clone dự án mà thiếu dữ liệu, hãy bổ sung dữ liệu đầu vào trước khi chạy mô phỏng.

## Cấu hình PostgreSQL (nếu sử dụng)

Tạo tệp `.env` ở thư mục gốc dự án và khai báo thông tin kết nối:

```dotenv
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=thay-bang-mat-khau-cua-ban
FLOOD_DB_NAME=data_flood
```

Không đưa `.env`, mật khẩu, khóa API hoặc thông tin đăng nhập vào GitHub. Dùng `.env.example` nếu cần chia sẻ mẫu cấu hình, nhưng chỉ ghi tên biến và giá trị giả.

## Cấu trúc chính

| Đường dẫn | Vai trò |
| --- | --- |
| `app.py` | Khởi tạo Flask app, các trang và API |
| `templates/flood_app.html` | Giao diện chính |
| `static/` | JavaScript, CSS và thư viện giao diện |
| `dem_3d.py`, `basemap.py`, `gis.py` | DEM, bản đồ nền và xử lý GIS |
| `rainfall-runoff.py`, `mike-nam.py` | Mô hình mưa–dòng chảy |
| `saint-venant.py`, `routing.py` | Mô hình và định tuyến thủy lực 1D |
| `flood_3d.py` | Tính toán/kết xuất vùng ngập |
| `reservoir_web.py`, `reservoir.py` | Giao diện và mô hình hồ chứa |
| `requirements.txt` | Các thư viện Python cần cài |
| `run.bat`, `install.bat` | Script chạy ứng dụng và cài thư viện trên Windows |

## Đóng góp và đẩy lên GitHub

Không đưa môi trường ảo, thông tin bí mật hoặc dữ liệu lớn không cần thiết vào Git. Trước khi commit, kiểm tra danh sách tệp:

```powershell
git status
```

Nên thêm tối thiểu các mục sau vào `.gitignore`:

```gitignore
venv/
.venv/
__pycache__/
*.py[cod]
.env
```

Các tệp DEM, video và dữ liệu mô phỏng có thể rất lớn; chỉ đưa lên repository khi thực sự cần thiết. GitHub cảnh báo với tệp lớn hơn 50 MB và từ chối các tệp lớn hơn giới hạn của họ. Có thể dùng Git LFS cho dữ liệu lớn nếu repository cần lưu trữ các tệp đó.

## Ghi nhận

Lớp bản đồ nền sử dụng dữ liệu OpenStreetMap. © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).

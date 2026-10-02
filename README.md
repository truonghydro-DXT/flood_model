# flood_model — ứng dụng web thủy văn / ngập lụt độc lập

Gói gồm DEM 3D, mưa–dòng chảy (TANK), thủy lực 1D Saint-Venant và ngập 2D. Toàn bộ script và dữ liệu nằm trong `flood_model/`.

## Cài thư viện

Từ thư mục gốc dự án (dùng venv):

```bat
py -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Chạy web
```bat
flood_model\run.bat
```

hoặc:

```bat
.\venv\Scripts\python.exe flood_model\app.py
py flood_model\app.py
.\venv\Scripts\python.exe -m flood_model --web

hoặc 
.\venv\Scripts\python.exe app.py
.\venv\Scripts\python.exe -m --web
```

Mở trình duyệt: **http://127.0.0.1:5001/**

Cổng mặc định `5001`. Đổi bằng biến `FLOOD_PORT`.

Mặc định mở **DEM 3D** (Three.js + nền OSM). Có thể đổi **Bản đồ 2D**.

Trên thanh công cụ:

1. **Model RR** — TANK Sugawara
2. **Model 1D** — Saint-venant hoặc Saint-venant-1D (`HYDRO1D_FORCE_MIKE` trong `routing.py`)
3. **Mặt cắt / Dọc sông** — vẽ trên DEM 3D
4. **Chạy mô phỏng** — ngập phủ lên DEM 3D

## Dữ liệu (trong `flood_model/`)

- DEM: `flood_model/projects/data/dem-song-hong.tif`
- Mưa / TANK: `flood_model/rainfall_runoff_output/`
- 1D: `flood_model/saint_venant_output/`
- H hạ lưu: `flood_model/saint_venant_output/demo_downstream_stage.csv`
- Ngập: `flood_model/flood_output/`

## CLI (không mở web)

```bat
.\venv\Scripts\python.exe -m flood_model
.\venv\Scripts\python.exe -m flood_model --hour 82 --no-plot
.\venv\Scripts\python.exe -m flood_model --video
.\venv\Scripts\python.exe flood_model\rainfall-runoff.py --no-plot
.\venv\Scripts\python.exe flood_model\saint-venant.py --no-plot
```

## Cấu trúc

| File | Vai trò |
|------|---------|
| `app.py` | Server Flask (`/api/flow-3d`, `/api/flood-3d`, DEM 3D) |
| `dem_3d.py` | API lưới cao độ + tile OSM |
| `basemap.py` | Proxy/cache tile OpenStreetMap |
| `templates/index.html` | DEM 3D + Model RR / 1D + ngập |
| `static/dem-3d.js` | Viewer Three.js |
| `static/flow-3d.js` | Mặt cắt, TANK, Saint-Venant |
| `static/flood-3d.js` | Lớp ngập phủ lên DEM 3D |
| `flow_3d.py` | API mưa–dòng chảy và 1D |
| `rainfall-runoff.py` | Mô hình TANK |
| `saint-venant.py` | Saint-Venant 1D |
| `flood_3d.py` | Nhân mô hình ngập |
| `run.bat` | Khởi động web |

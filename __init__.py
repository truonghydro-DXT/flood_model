"""Mo hinh ngap lut 2D (WSE 1D Saint-Venant + DEM).

Cai dat:
  .\\venv\\Scripts\\python.exe -m pip install -r flood_model/requirements.txt
  flood_model\\install.bat

Chay CLI:
  .\\venv\\Scripts\\python.exe -m flood_model
  .\\venv\\Scripts\\python.exe -m flood_model --hour 82 --no-plot
  .\\venv\\Scripts\\python.exe -m flood_model --video
"""

from flood_model.flood_3d import (
    DEFAULT_BUFFER_M,
    DEFAULT_DEM,
    DEFAULT_MAX_DIM,
    DEFAULT_OUT_DIR,
    FloodResult,
    clear_flood_result_caches,
    create_flood3d_blueprint,
    inundate_time,
    main,
    run_cached,
    simulate_flood,
    write_flood_video,
    write_outputs,
)

__all__ = [
    "DEFAULT_BUFFER_M",
    "DEFAULT_DEM",
    "DEFAULT_MAX_DIM",
    "DEFAULT_OUT_DIR",
    "FloodResult",
    "clear_flood_result_caches",
    "create_flood3d_blueprint",
    "inundate_time",
    "main",
    "run_cached",
    "simulate_flood",
    "write_flood_video",
    "write_outputs",
]

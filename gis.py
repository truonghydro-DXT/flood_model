"""DEM / CRS helpers cho mo hinh ngap lut (tach tu flow_3d, khong phu thuoc UI)."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import math
import numpy as np
import numpy.typing as npt
from rasterio.warp import transform as rio_transform

from flood_model.paths import DEFAULT_DEM, PROJECT_ROOT

WEB_MERCATOR_MAX = 20037508.342789244


def resolve_dem_path(file_id: str | None = None, dem_path: str | None = None) -> Path:
    raw = (dem_path or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        else:
            p = p.resolve()
        try:
            p.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError("DEM phai nam trong thu muc flood_model.") from exc
        if not p.is_file():
            raise FileNotFoundError(f"Khong tim thay DEM: {p}")
        return p

    fid = (file_id or "").strip().lower()
    if not fid or fid in {"dem", "default", "dem-song-hong"}:
        if DEFAULT_DEM.is_file():
            return DEFAULT_DEM
        raise FileNotFoundError(f"Khong tim thay DEM mac dinh: {DEFAULT_DEM}")

    candidate = Path(fid)
    candidate = candidate.resolve() if candidate.is_absolute() else (PROJECT_ROOT / fid).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError:
        candidate = None
    if candidate is not None and candidate.is_file():
        return candidate

    try:
        import importlib

        mapped = importlib.import_module("login.user_storage").resolve_uploaded_raster_path(file_id)
    except Exception:
        mapped = None
    if mapped:
        p = Path(mapped)
        if p.is_file():
            return p

    if DEFAULT_DEM.is_file():
        return DEFAULT_DEM
    raise FileNotFoundError(f"Khong tim thay DEM mac dinh: {DEFAULT_DEM}")


def open_dem(path: Path):
    import rasterio

    return rasterio.open(path)


def _is_web_mercator(src) -> bool:
    crs = src.crs
    if crs is None:
        return False
    s = str(crs).upper()
    if "3857" in s or "900913" in s or "PSEUDO-MERCATOR" in s or "WEB MERCATOR" in s:
        return True
    b = src.bounds
    return max(abs(b.left), abs(b.right), abs(b.bottom), abs(b.top)) > 180


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = lon * WEB_MERCATOR_MAX / 180.0
    y = math.log(math.tan(math.radians(90.0 + lat) / 2.0)) / (math.pi / 180.0)
    y = y * WEB_MERCATOR_MAX / 180.0
    return x, y


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = x / WEB_MERCATOR_MAX * 180.0
    lat = math.degrees(math.atan(math.sinh(y / WEB_MERCATOR_MAX * math.pi)))
    return lon, lat


def lonlat_to_dem_xy(src, lons: npt.ArrayLike, lats: npt.ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    lon_arr = np.asarray(lons, dtype=float)
    lat_arr = np.asarray(lats, dtype=float)
    if _is_web_mercator(src):
        xy = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lon_arr, lat_arr)]
        return np.array([p[0] for p in xy]), np.array([p[1] for p in xy])
    try:
        xy = rio_transform("EPSG:4326", src.crs, lon_arr.tolist(), lat_arr.tolist())
        return np.asarray(xy[0], dtype=float), np.asarray(xy[1], dtype=float)
    except Exception:
        xy = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lon_arr, lat_arr)]
        return np.array([p[0] for p in xy]), np.array([p[1] for p in xy])


def dem_xy_to_lonlat(src, xs: npt.ArrayLike, ys: npt.ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    x_arr = np.asarray(xs, dtype=float)
    y_arr = np.asarray(ys, dtype=float)
    if _is_web_mercator(src):
        ll = [mercator_to_lonlat(float(x), float(y)) for x, y in zip(x_arr, y_arr)]
        return np.array([p[0] for p in ll]), np.array([p[1] for p in ll])
    try:
        ll = rio_transform(src.crs, "EPSG:4326", x_arr.tolist(), y_arr.tolist())
        return np.asarray(ll[0], dtype=float), np.asarray(ll[1], dtype=float)
    except Exception:
        ll = [mercator_to_lonlat(float(x), float(y)) for x, y in zip(x_arr, y_arr)]
        return np.array([p[0] for p in ll]), np.array([p[1] for p in ll])

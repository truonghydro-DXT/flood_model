"""
Mo phong ngap lut 2D tu muc nuoc 1D Saint-Venant va DEM.

- H(s,t) doc long song chinh: saint_venant_output/saint_venant_result.csv
- Toa do tram chinh: saint_venant_output/demo_river_geometry.csv  (main, main_2, ...)
- Song nhanh: demo_tributary_geometry.csv + saint_venant_tributary_result.csv  (trib_1, ...)
- Song chinh doc lap: demo_extra_main_result.csv
- DEM: flood_model/projects/data/dem-song-hong.tif
- Do sau ngap: depth = max(0, WSE - z_DEM), chi o o noi lien thong voi long
  song/nhanh trong ban kinh buffer (mac dinh 1500 m).

Chay:
  .\\venv\\Scripts\\python.exe -m flood_model
  .\\venv\\Scripts\\python.exe -m flood_model --hour 82 --no-plot
  .\\venv\\Scripts\\python.exe -m flood_model --video
  .\\venv\\Scripts\\python.exe flood_3d.py

API (blueprint):
  GET  /api/flood-3d/meta
  POST /api/flood-3d/run             {time_index, file_id, buffer_m}
  GET  /api/flood-3d/video
  POST /api/flood-3d/video
"""

from __future__ import annotations

import argparse
import io
import math
import traceback
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, cast

import numpy as np
from flask import Blueprint, jsonify, request, send_file
from rasterio.transform import Affine, rowcol, xy

from flood_model.csv_io import write_csv_rows
from flood_model.gis import (
    dem_xy_to_lonlat,
    lonlat_to_dem_xy,
    lonlat_to_mercator,
    mercator_to_lonlat,
    open_dem as _open_dem,
    resolve_dem_path,
)
from flood_model.paths import DEFAULT_DEM, DEFAULT_OUT_DIR, PACKAGE_DIR
from flood_model.routing import (
    clear_route_cache,
    extra_main_routes,
    hydro1d_label,
    hydro1d_water_sources,
    normalize_reach_id,
    parse_hydro1d_source,
    pick_route_by_id,
    saint_venant_route,
    tributary_routes,
)
from flood_model.urls import public_path

DEFAULT_BUFFER_M = 1500.0
DEFAULT_MAX_DIM = 720
MIN_DEPTH_M = 0.05


@dataclass
class FloodResult:
    hours: np.ndarray
    time_index: int
    hour: float
    z: np.ndarray
    wse: np.ndarray
    depth: np.ndarray
    wet: np.ndarray
    transform: Affine
    crs: Any
    bounds_wgs84: dict[str, float]
    cell_m2: float
    area_km2: float
    volume_m3: float
    max_depth_m: float
    mean_depth_m: float
    n_wet: int
    png_bytes: bytes
    mesh: dict[str, Any]
    water_source: str = "saint-venant"


def _norm_source(raw: Any = None) -> str:
    return parse_hydro1d_source(raw, respect_force=False)


def _source_label(water_source: str = "saint-venant") -> str:
    return hydro1d_label(water_source, respect_force=False)


def _pick_route(water_source: Any = None) -> dict[str, Any]:
    return saint_venant_route(_norm_source(water_source))


def _require_hours(route: dict[str, Any], water_source: str = "saint-venant") -> tuple[np.ndarray, np.ndarray]:
    hours = np.asarray(route["hours"], dtype=float)
    h_st = np.asarray(route["h"], dtype=float)
    if hours.size < 1:
        label = _source_label(water_source)
        raise ValueError(f"Chua co chuoi H {label}. Chay Model 1D ({label}) truoc.")
    return hours, h_st


def _densify_ll(
    lon: np.ndarray,
    lat: np.ndarray,
    station_m: np.ndarray,
    step_m: float = 40.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mx, my = [], []
    for lo, la in zip(lon.tolist(), lat.tolist()):
        x, y = lonlat_to_mercator(float(lo), float(la))
        mx.append(x)
        my.append(y)
    mx = np.asarray(mx, dtype=float)
    my = np.asarray(my, dtype=float)
    s = np.asarray(station_m, dtype=float)
    if mx.size < 2:
        return lon, lat, s
    seg = np.hypot(np.diff(mx), np.diff(my))
    dist = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(dist[-1])
    n = max(2, int(math.ceil(total / max(step_m, 5.0))) + 1)
    td = np.linspace(0.0, total, n)
    mx2 = np.interp(td, dist, mx)
    my2 = np.interp(td, dist, my)
    s2 = np.interp(td, dist, s)
    ll = [mercator_to_lonlat(float(x), float(y)) for x, y in zip(mx2, my2)]
    return (
        np.array([p[0] for p in ll], dtype=float),
        np.array([p[1] for p in ll], dtype=float),
        s2,
    )


def _h_on_densified(station_src: np.ndarray, h_src: np.ndarray, station_q: np.ndarray) -> np.ndarray:
    st = np.asarray(station_src, dtype=float)
    hq = np.asarray(h_src, dtype=float)
    sq = np.asarray(station_q, dtype=float)
    if hq.ndim != 2 or st.size < 1 or sq.size < 1:
        return np.zeros((max(int(hq.shape[0]) if hq.ndim == 2 else 1, 1), max(sq.size, 1)), dtype=np.float32)
    return np.vstack([np.interp(sq, st, hq[t]) for t in range(int(hq.shape[0]))]).astype(np.float32)


def _append_reach_poly(
    lons: list[np.ndarray],
    lats: list[np.ndarray],
    hs: list[np.ndarray],
    rec: dict[str, Any],
    step_m: float,
    n_t: int,
) -> bool:
    lon_t, lat_t, st_t = _densify_ll(rec["lon"], rec["lat"], rec["station_m"], step_m)
    h_t = _h_on_densified(rec["station_m"], rec["h"], st_t)
    if int(h_t.shape[0]) != n_t:
        return False
    lons.append(lon_t)
    lats.append(lat_t)
    hs.append(h_t)
    return True


def _wse_on_profile(
    tprof: dict[str, Any], rec: dict[str, Any] | None, hours: np.ndarray, wse_fallback: np.ndarray
) -> np.ndarray:
    n_t = int(wse_fallback.shape[0])
    if rec is None or int(np.asarray(rec["h"]).shape[0]) != n_t:
        z = np.asarray(tprof.get("z_bed", tprof.get("z_dem")), dtype=float)
        if z.size:
            return np.repeat((z + 1.0)[None, :], n_t, axis=0)
        return np.repeat(wse_fallback[:, :1], max(int(tprof["lon"].size), 1), axis=1)
    t_route = dict(rec, hours=hours)
    try:
        from flood_model.flow_3d import attach_route_wse

        tpack = attach_route_wse(tprof, t_route, horizontal=False)
    except Exception:
        tpack = None
    if tpack is not None:
        return np.asarray(tpack[0], dtype=float)
    s_old = np.asarray(rec["station_m"], dtype=float)
    s_new = np.asarray(tprof["distance_m"], dtype=float)
    h_old = np.asarray(rec["h"], dtype=float)
    if s_old.size >= 2 and s_new.size >= 2:
        scale = float(s_old[-1]) / max(float(s_new[-1]), 1e-6)
        return np.stack([np.interp(s_new * scale, s_old, h_old[t]) for t in range(n_t)])
    return np.repeat(wse_fallback[:, :1], max(int(tprof["lon"].size), 1), axis=1)


def _network_polylines(
    step_m: float, water_source: str, dem_path: str | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ghep n song chinh + m song nhanh theo reach_id va H doc diem (n_t, n_pts)."""
    kind = _norm_source(water_source)
    mus = _pick_route(kind)
    hours = np.asarray(mus["hours"], dtype=float)
    trib_sv = list(tributary_routes(kind))
    extra_sv = list(extra_main_routes(kind))
    try:
        from flood_model import cross_section
        from flood_model.flow_3d import attach_route_wse, _open_dem as f3_open
        from flood_model.river_network import dem_river_network

        profile_from_lonlat = getattr(cross_section, "profile_from_lonlat")

        dem = resolve_dem_path(None, dem_path)
        with f3_open(dem) as src:
            net = dem_river_network(src, kind)
            mprof = profile_from_lonlat(src, net["main_lon"], net["main_lat"], step_m=step_m)
            lons: list[np.ndarray] = []
            lats: list[np.ndarray] = []
            hs: list[np.ndarray] = []
            main_route = dict(mus, id="main", hours=hours)
            pack = attach_route_wse(mprof, main_route, horizontal=False)
            if pack is None:
                raise ValueError("Khong gan duoc H long chinh")
            wse_m, _ = pack
            lons.append(np.asarray(mprof["lon"], dtype=float))
            lats.append(np.asarray(mprof["lat"], dtype=float))
            hs.append(np.asarray(wse_m, dtype=float))
            n_t = int(wse_m.shape[0])
            used_extra: set[str] = set()
            for tr in net.get("extra_mains") or []:
                lon_b = [float(v) for v in tr["lon"]]
                lat_b = [float(v) for v in tr["lat"]]
                if len(lon_b) < 2:
                    continue
                rid = normalize_reach_id(tr.get("id"), "main_2")
                rec = pick_route_by_id(extra_sv, rid, "main_2")
                tprof = profile_from_lonlat(src, lon_b, lat_b, step_m=step_m)
                wse_t = _wse_on_profile(tprof, rec, hours, wse_m)
                lons.append(np.asarray(tprof["lon"], dtype=float))
                lats.append(np.asarray(tprof["lat"], dtype=float))
                hs.append(np.asarray(wse_t, dtype=float))
                used_extra.add(rid)
            for rec in extra_sv:
                rid = normalize_reach_id(rec.get("id"), "main_2")
                if rid in used_extra:
                    continue
                if _append_reach_poly(lons, lats, hs, rec, step_m, n_t):
                    used_extra.add(rid)
            used_trib: set[str] = set()
            for i, tr in enumerate(net.get("branches") or []):
                lon_b = [float(v) for v in tr["lon"]]
                lat_b = [float(v) for v in tr["lat"]]
                if len(lon_b) < 2:
                    continue
                rid = normalize_reach_id(tr.get("id") or f"trib_{i + 1}", "trib_1")
                rec = pick_route_by_id(trib_sv, rid, "trib_1")
                tprof = profile_from_lonlat(src, lon_b, lat_b, step_m=step_m)
                wse_t = _wse_on_profile(tprof, rec, hours, wse_m)
                lons.append(np.asarray(tprof["lon"], dtype=float))
                lats.append(np.asarray(tprof["lat"], dtype=float))
                hs.append(np.asarray(wse_t, dtype=float))
                used_trib.add(rid)
            for rec in trib_sv:
                rid = normalize_reach_id(rec.get("id"), "trib_1")
                if rid in used_trib:
                    continue
                if _append_reach_poly(lons, lats, hs, rec, step_m, n_t):
                    used_trib.add(rid)
            if lons:
                return np.concatenate(lons), np.concatenate(lats), np.concatenate(hs, axis=1)
    except Exception:
        traceback.print_exc()

    lon_m, lat_m, st_m = _densify_ll(mus["lon"], mus["lat"], mus["station_m"], step_m)
    h_m = _h_on_densified(mus["station_m"], mus["h"], st_m)
    lons = [lon_m]
    lats = [lat_m]
    hs = [h_m]
    n_t = int(h_m.shape[0])
    for rec in extra_sv:
        _append_reach_poly(lons, lats, hs, rec, step_m, n_t)
    for rec in trib_sv:
        _append_reach_poly(lons, lats, hs, rec, step_m, n_t)
    return np.concatenate(lons), np.concatenate(lats), np.concatenate(hs, axis=1)


def _read_dem_grid(path: Path, max_dim: int) -> dict[str, Any]:
    import rasterio
    from rasterio.enums import Resampling

    with _open_dem(path) as src:
        scale = max(src.width, src.height) / float(max(32, max_dim))
        if scale > 1.0:
            out_w = max(32, int(round(src.width / scale)))
            out_h = max(32, int(round(src.height / scale)))
        else:
            out_w, out_h = int(src.width), int(src.height)
        z = np.empty((out_h, out_w), dtype=np.float32)
        src.read(1, out=z, resampling=Resampling.average)
        nodata = src.nodata
        if nodata is not None:
            z = np.where(z == nodata, np.nan, z)
        z[~np.isfinite(z)] = np.nan
        transform = src.transform * Affine.scale(src.width / out_w, src.height / out_h)
        from rasterio.warp import transform_bounds as rio_transform_bounds

        try:
            west, south, east, north = rio_transform_bounds(
                src.crs, "EPSG:4326", *src.bounds, densify_pts=5
            )
            bounds = {
                "west": float(west),
                "south": float(south),
                "east": float(east),
                "north": float(north),
            }
        except Exception:
            xs0, ys0 = xy(transform, 0, 0, offset="ul")
            xs1, ys1 = xy(transform, out_h, out_w, offset="ul")
            lon_c, lat_c = dem_xy_to_lonlat(
                src, [xs0, xs1, xs1, xs0], [ys0, ys0, ys1, ys1]
            )
            bounds = {
                "west": float(np.min(lon_c)),
                "east": float(np.max(lon_c)),
                "south": float(np.min(lat_c)),
                "north": float(np.max(lat_c)),
            }
        xs = np.array([xy(transform, 0, c, offset="center")[0] for c in range(out_w)], dtype=float)
        ys = np.array([xy(transform, r, 0, offset="center")[1] for r in range(out_h)], dtype=float)
        # Kich thuoc o (m) tu 1 pixel sang mercator
        x0, y0 = xy(transform, out_h // 2, out_w // 2, offset="center")
        x1, y1 = xy(transform, out_h // 2, out_w // 2 + 1, offset="center")
        lo0, la0 = dem_xy_to_lonlat(src, [x0], [y0])
        lo1, la1 = dem_xy_to_lonlat(src, [x1], [y1])
        mx0, my0 = lonlat_to_mercator(float(lo0[0]), float(la0[0]))
        mx1, my1 = lonlat_to_mercator(float(lo1[0]), float(la1[0]))
        cell_m = max(math.hypot(mx1 - mx0, my1 - my0), 1.0)
        crs = src.crs
        lon_x, _ = dem_xy_to_lonlat(src, xs, np.full_like(xs, ys[out_h // 2]))
        _, lat_y = dem_xy_to_lonlat(src, np.full_like(ys, xs[out_w // 2]), ys)
    return {
        "z": z,
        "transform": transform,
        "crs": crs,
        "lon_x": lon_x,
        "lat_y": lat_y,
        "bounds": bounds,
        "cell_m": cell_m,
        "cell_m2": cell_m * cell_m,
        "path": str(path),
    }


@lru_cache(maxsize=2)
def _cached_dem(path_str: str, max_dim: int) -> dict[str, Any]:
    return _read_dem_grid(Path(path_str), max_dim)


def _nearest_river_index(
    nrows: int,
    ncols: int,
    transform: Affine,
    src,
    lon_r: np.ndarray,
    lat_r: np.ndarray,
    buffer_m: float,
    cell_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    rx, ry = lonlat_to_dem_xy(src, lon_r, lat_r)
    rad = min(90, int(math.ceil(buffer_m / max(cell_m, 1.0))) + 1)
    idx = np.full((nrows, ncols), -1, dtype=np.int32)
    dmin = np.full((nrows, ncols), np.inf, dtype=np.float32)
    for i in range(int(lon_r.size)):
        r, c = rowcol(transform, float(rx[i]), float(ry[i]))
        r0, r1 = max(0, r - rad), min(nrows, r + rad + 1)
        c0, c1 = max(0, c - rad), min(ncols, c + rad + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        rr = np.arange(int(r0), int(r1), dtype=np.float32)[:, None]
        cc = np.arange(int(c0), int(c1), dtype=np.float32)[None, :]
        d = np.hypot(rr - r, cc - c) * np.float32(cell_m)
        block = dmin[r0:r1, c0:c1]
        closer = d < block
        if not np.any(closer):
            continue
        dmin[r0:r1, c0:c1] = np.where(closer, d, block)
        idx[r0:r1, c0:c1] = np.where(closer, i, idx[r0:r1, c0:c1])
    idx[dmin > buffer_m] = -1
    return idx, dmin


@lru_cache(maxsize=4)
def _cached_index(
    path_str: str, max_dim: int, buffer_m: float, water_source: str = "saint-venant"
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lon_r, lat_r, _h = _network_polylines(40.0, water_source, path_str)
    dem = _cached_dem(path_str, max_dim)
    with _open_dem(Path(path_str)) as src:
        idx, dmin = _nearest_river_index(
            dem["z"].shape[0],
            dem["z"].shape[1],
            dem["transform"],
            src,
            lon_r,
            lat_r,
            float(buffer_m),
            float(dem["cell_m"]),
        )
    return idx, np.arange(lon_r.size, dtype=np.float32), dmin


@lru_cache(maxsize=4)
def _cached_h_along(
    path_str: str, max_dim: int, buffer_m: float, water_source: str = "saint-venant"
) -> np.ndarray:
    _idx, _s_r, _dmin = _cached_index(path_str, max_dim, buffer_m, water_source)
    _lon, _lat, h = _network_polylines(40.0, water_source, path_str)
    h = np.asarray(h, dtype=np.float32)
    if h.ndim == 2 and h.size:
        for t in range(int(h.shape[0])):
            h[t] = _fill_nan_1d(h[t])
    return h


@lru_cache(maxsize=4)
def _cached_seed(path_str: str, max_dim: int, water_source: str = "saint-venant") -> np.ndarray:
    dem = _cached_dem(path_str, max_dim)
    dem_z = np.asarray(dem["z"])
    seed = np.zeros((int(dem_z.shape[0]), int(dem_z.shape[1])), dtype=bool)
    with _open_dem(Path(path_str)) as src:
        lon_r, lat_r, _h = _network_polylines(80.0, water_source, path_str)
        rx, ry = lonlat_to_dem_xy(src, lon_r, lat_r)
    rad = 2
    h, w = int(seed.shape[0]), int(seed.shape[1])
    for x, y in zip(rx, ry):
        r, c = rowcol(dem["transform"], float(x), float(y))
        r0, r1 = max(0, r - rad), min(h, r + rad + 1)
        c0, c1 = max(0, c - rad), min(w, c + rad + 1)
        seed[r0:r1, c0:c1] = True
    return seed


def _fill_nan_1d(values: np.ndarray) -> np.ndarray:
    """Noi suy tuyen tinh cac diem NaN tren chuoi 1D (H doc long)."""
    out = np.asarray(values, dtype=np.float32).copy()
    n = int(out.size)
    if n == 0:
        return out
    valid = np.isfinite(out)
    if bool(valid.all()) or not bool(valid.any()):
        return out
    idx = np.arange(n, dtype=np.float32)
    out[~valid] = np.interp(idx[~valid], idx[valid], out[valid]).astype(np.float32)
    return out


def _fill_nan_nearest2d(grid: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Gan WSE hop le gan nhat vao o nam trong `where` nhung dang NaN."""
    out = np.asarray(grid, dtype=np.float32).copy()
    mask = np.asarray(where, dtype=bool)
    need = mask & ~np.isfinite(out)
    valid = np.isfinite(out)
    if not bool(need.any()) or not bool(valid.any()):
        return out
    try:
        from scipy import ndimage

        transformed = cast(
            tuple[np.ndarray, np.ndarray],
            ndimage.distance_transform_edt(~valid, return_indices=True),
        )
        indices = np.asarray(transformed[1])
        ri, ci = indices[0], indices[1]
        out[need] = np.asarray(grid, dtype=np.float32)[ri[need], ci[need]]
        return out
    except Exception:
        src = np.asarray(grid, dtype=np.float32)
        for _ in range(12):
            need = mask & ~np.isfinite(out)
            if not bool(need.any()):
                break
            pad = np.pad(out, 1, constant_values=np.nan)
            neigh = np.stack(
                [
                    pad[0:-2, 0:-2],
                    pad[0:-2, 1:-1],
                    pad[0:-2, 2:],
                    pad[1:-1, 0:-2],
                    pad[1:-1, 2:],
                    pad[2:, 0:-2],
                    pad[2:, 1:-1],
                    pad[2:, 2:],
                ],
                axis=0,
            )
            with np.errstate(all="ignore"):
                fill = np.nanmean(neigh, axis=0)
            hit = need & np.isfinite(fill)
            out[hit] = fill[hit]
        return out


def _close_wet_gaps(wet: np.ndarray) -> np.ndarray:
    """Lap khe 1 o (closing 3x3) de vung ngap khong dut doan."""
    wet = np.asarray(wet, dtype=bool)
    try:
        from scipy import ndimage

        return ndimage.binary_closing(wet, structure=np.ones((3, 3), dtype=bool), iterations=1)
    except Exception:
        pad = np.pad(wet, 1, constant_values=False)
        dil = np.zeros(wet.shape, dtype=bool)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                dil |= pad[1 + dr : 1 + dr + wet.shape[0], 1 + dc : 1 + dc + wet.shape[1]]
        pad2 = np.pad(dil, 1, constant_values=True)
        ero = np.ones(wet.shape, dtype=bool)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                ero &= pad2[1 + dr : 1 + dr + wet.shape[0], 1 + dc : 1 + dc + wet.shape[1]]
        return ero


def _fill_interior_wet(
    wet: np.ndarray, z: np.ndarray, wse: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Lap lo hong ben trong vung ngap: noi suy WSE, chi giu o van ngap (WSE > z)."""
    wet = np.asarray(wet, dtype=bool)
    try:
        from scipy import ndimage

        enclosed = ndimage.binary_fill_holes(wet) & ~wet
    except Exception:
        return wet, wse
    if not bool(enclosed.any()):
        return wet, wse
    wse2 = _fill_nan_nearest2d(wse, wet | enclosed)
    extra = (
        enclosed
        & np.isfinite(z)
        & np.isfinite(wse2)
        & ((wse2 - z) >= np.float32(MIN_DEPTH_M))
    )
    wet2 = wet | extra
    wse2 = np.where(wet2, wse2, np.nan).astype(np.float32)
    return wet2, wse2


def inundate_time(
    dem_path: Path,
    time_index: int,
    *,
    buffer_m: float = DEFAULT_BUFFER_M,
    max_dim: int = DEFAULT_MAX_DIM,
    connected: bool = True,
    water_source: str = "saint-venant",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """depth, wet, wse, dem — dung lai index DEM, khong ve PNG."""
    src = _norm_source(water_source)
    dem = _cached_dem(str(dem_path), int(max_dim))
    idx, _s_r, _dmin = _cached_index(str(dem_path), int(max_dim), float(buffer_m), src)
    h_along = _cached_h_along(str(dem_path), int(max_dim), float(buffer_m), src)
    t = max(0, min(int(time_index), int(h_along.shape[0]) - 1))
    h_t = _fill_nan_1d(h_along[t])
    z = dem["z"]
    wse = np.full(z.shape, np.nan, dtype=np.float32)
    ok = idx >= 0
    wse[ok] = h_t[np.clip(idx[ok], 0, int(h_t.size) - 1)]
    wse = _fill_nan_nearest2d(wse, ok)
    depth = wse - z
    wet = np.isfinite(depth) & np.isfinite(z) & (depth >= MIN_DEPTH_M)
    wet = _close_wet_gaps(wet)
    if connected:
        seed = _cached_seed(str(dem_path), int(max_dim), src)
        filled = _connected_wet(wet, seed)
        if filled.any():
            wet = filled
    wet, wse = _fill_interior_wet(wet, z, wse)
    wse = _fill_nan_nearest2d(wse, wet)
    depth = np.where(wet, wse - z, np.nan).astype(np.float32)
    wse = np.where(wet, wse, np.nan).astype(np.float32)
    wet = wet & np.isfinite(depth) & (depth >= MIN_DEPTH_M)
    depth = np.where(wet, depth, np.nan).astype(np.float32)
    wse = np.where(wet, wse, np.nan).astype(np.float32)
    return depth, wet, wse, dem


def _connected_wet_bfs(wet: np.ndarray, seed: np.ndarray) -> np.ndarray:
    h, w = wet.shape
    out = np.zeros((h, w), dtype=bool)
    ys, xs = np.where(seed & wet)
    q: deque[tuple[int, int]] = deque(zip(ys.tolist(), xs.tolist()))
    while q:
        r, c = q.popleft()
        if r < 0 or c < 0 or r >= h or c >= w or out[r, c] or not wet[r, c]:
            continue
        out[r, c] = True
        q.append((r - 1, c))
        q.append((r + 1, c))
        q.append((r, c - 1))
        q.append((r, c + 1))
        q.append((r - 1, c - 1))
        q.append((r - 1, c + 1))
        q.append((r + 1, c - 1))
        q.append((r + 1, c + 1))
    return out


def _connected_wet(wet: np.ndarray, seed: np.ndarray) -> np.ndarray:
    try:
        from scipy import ndimage

        structure = np.ones((3, 3), dtype=np.uint8)
        labeled_result = cast(
            tuple[np.ndarray, int],
            ndimage.label(np.asarray(wet, dtype=np.uint8), structure=structure),
        )
        labeled = np.asarray(labeled_result[0])
        keep = np.unique(labeled[seed & wet])
        keep = keep[keep != 0]
        if keep.size == 0:
            return np.zeros(wet.shape, dtype=bool)
        return np.isin(labeled, keep)
    except Exception:
        return _connected_wet_bfs(wet, seed)


def _overlay_png(depth: np.ndarray, wet: np.ndarray) -> bytes:
    """PNG RGBA dung kich thuoc luoi DEM (khong thanh mau) de chong khop ban do."""
    from PIL import Image

    cls = np.digitize(np.nan_to_num(depth, nan=0.0), _FLOOD_BOUNDS[1:-1], right=True)
    cls = np.clip(cls, 0, len(_FLOOD_COLORS) - 1)
    rgba = np.zeros(depth.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = _FLOOD_COLORS[cls]
    rgba[..., 3] = np.where(wet, 200, 0).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(
        buf, format="PNG", compress_level=1, optimize=False
    )
    return buf.getvalue()


def _depth_png(depth: np.ndarray, wet: np.ndarray) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    colors = ["#c6dbef", "#6baed6", "#2171b5", "#084594", "#041c3a"]
    bounds = [0.05, 0.5, 1.0, 2.0, 4.0, 12.0]
    cmap = ListedColormap(colors)
    cmap.set_bad((0, 0, 0, 0))
    norm = BoundaryNorm(bounds, cmap.N)
    show = np.ma.masked_where(~wet, depth)

    fig, ax = plt.subplots(figsize=(8.2, 7.2))
    im = ax.imshow(show, cmap=cmap, norm=norm, interpolation="nearest")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("Độ sâu ngập (m)")
    ax.set_axis_off()
    ax.set_title("Bản đồ ngập")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, transparent=True, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


_FLOOD_COLORS = np.array(
    [
        [198, 219, 239],
        [107, 174, 214],
        [33, 113, 181],
        [8, 69, 148],
        [4, 28, 58],
    ],
    dtype=np.uint8,
)
_FLOOD_BOUNDS = np.array([0.05, 0.5, 1.0, 2.0, 4.0, 12.0], dtype=np.float32)


def _hillshade(z: np.ndarray, cell_m: float) -> np.ndarray:
    zf = np.asarray(z, dtype=np.float64)
    fill = float(np.nanmean(zf)) if np.isfinite(z).any() else 0.0
    zf = np.nan_to_num(zf, nan=fill)
    dy, dx = np.gradient(zf, max(cell_m, 1.0), max(cell_m, 1.0))
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    az = math.radians(315.0)
    alt = math.radians(45.0)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(0.25 + 0.75 * hs, 0.0, 1.0).astype(np.float32)


def _color_frame(
    z: np.ndarray,
    depth: np.ndarray,
    wet: np.ndarray,
    *,
    hour: float,
    area_km2: float,
    cell_m: float,
) -> np.ndarray:
    hs = _hillshade(z, cell_m)
    terrain = np.stack(
        [np.uint8(np.clip(hs * 210 + 25, 0, 255))] * 3,
        axis=-1,
    )
    cls = np.digitize(np.nan_to_num(depth, nan=0.0), _FLOOD_BOUNDS[1:-1], right=True)
    cls = np.clip(cls, 0, len(_FLOOD_COLORS) - 1)
    flood = _FLOOD_COLORS[cls]
    alpha = np.where(wet, 0.78, 0.0)[..., None]
    rgb = np.clip(terrain * (1.0 - alpha) + flood * alpha, 0, 255).astype(np.uint8)

    from PIL import Image, ImageDraw, ImageFont

    img = Image.fromarray(rgb, mode="RGB")
    top_h, bot_h = 36, 44
    canvas = Image.new("RGB", (img.width, img.height + top_h + bot_h), (18, 26, 36))
    canvas.paste(img, (0, top_h))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 16)
        font_s = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
        font_s = font
    draw.text(
        (10, 8),
        f"Gio {hour:.0f} h    Dien tich ngap {area_km2:.2f} km2",
        fill=(232, 241, 250),
        font=font,
    )
    labels = ["< 0,5 m", "0,5–1 m", "1–2 m", "2–4 m", "> 4 m"]
    n = len(labels)
    title = "Do sau ngap"
    title_w = 88
    gap = 8
    sw = max(36, (img.width - title_w - 24 - (n - 1) * gap) // n)
    x0 = 10 + title_w
    y0 = top_h + img.height + 6
    draw.text((10, y0 + 10), title, fill=(242, 246, 251), font=font_s)
    for i, (col, lab) in enumerate(zip(_FLOOD_COLORS, labels)):
        x = x0 + i * (sw + gap)
        draw.rectangle([x, y0, x + sw, y0 + 14], fill=tuple(int(v) for v in col))
        draw.text((x, y0 + 16), lab, fill=(200, 214, 228), font=font_s)
    return np.asarray(canvas, dtype=np.uint8)


def write_flood_video(
    *,
    dem_path: Optional[Path] = None,
    out_dir: Path = DEFAULT_OUT_DIR,
    buffer_m: float = DEFAULT_BUFFER_M,
    max_dim: int = 480,
    step: int = 1,
    fps: float = 10.0,
    water_source: str = "saint-venant",
) -> dict[str, Path]:
    """Ghi MP4 + GIF ngap theo thoi gian."""
    src = _norm_source(water_source)
    dem_path = resolve_dem_path(None, str(dem_path) if dem_path else None)
    mus = _pick_route(src)
    hours, _h_st = _require_hours(mus, src)
    n = int(hours.size)
    step = max(1, int(step))
    indices = list(range(0, n, step))
    if indices[-1] != n - 1:
        indices.append(n - 1)

    print(f"Tao video {len(indices)} khung (dt={step}h, fps={fps:.1f})...", flush=True)
    frames: list[np.ndarray] = []
    for k, t in enumerate(indices):
        depth, wet, _wse, dem = inundate_time(
            dem_path, t, buffer_m=buffer_m, max_dim=max_dim, connected=True, water_source=src
        )
        n_wet = int(np.count_nonzero(wet))
        area = n_wet * float(dem["cell_m2"]) / 1.0e6
        frame = _color_frame(
            dem["z"],
            depth,
            wet,
            hour=float(hours[t]),
            area_km2=area,
            cell_m=float(dem["cell_m"]),
        )
        frames.append(frame)
        if k == 0 or (k + 1) % 20 == 0 or k == len(indices) - 1:
            print(f"  khung {k + 1}/{len(indices)}  gio {hours[t]:.0f}  A={area:.2f} km2", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    h, w = frames[0].shape[:2]
    we, he = w - (w % 2), h - (h % 2)
    if we != w or he != h:
        frames = [fr[:he, :we] for fr in frames]
        h, w = he, we

    mp4 = out_dir / "flood_animation.mp4"
    try:
        import cv2

        # Ban OpenCV/FFmpeg tren Windows khong encode duoc H264 (libopenh264).
        # mp4v ghi duoc file; trinh duyet phat GIF (ben duoi).
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:
            pass
        fourcc = getattr(cv2, "VideoWriter_fourcc")
        vw = cv2.VideoWriter(str(mp4), fourcc(*"mp4v"), float(fps), (w, h))
        if not vw.isOpened():
            vw.release()
            raise RuntimeError("VideoWriter khong mo duoc (mp4v)")
        for fr in frames:
            vw.write(fr[:, :, ::-1])
        vw.release()
        if mp4.is_file() and mp4.stat().st_size > 0:
            written["mp4"] = mp4
        else:
            raise RuntimeError("File MP4 rong")
    except Exception as exc:
        print(f"Khong ghi MP4 (cv2): {exc}")

    gif = out_dir / "flood_animation.gif"
    try:
        from PIL import Image

        pal = getattr(Image, "ADAPTIVE", None) or Image.Palette.ADAPTIVE
        imgs = [
            Image.fromarray(fr, mode="RGB").convert("P", palette=pal, colors=64)
            for fr in frames
        ]
        dur = max(40, int(round(1000.0 / max(fps, 1.0))))
        imgs[0].save(
            gif,
            save_all=True,
            append_images=imgs[1:],
            duration=dur,
            loop=0,
            optimize=True,
        )
        written["gif"] = gif
    except Exception as exc:
        print(f"Khong ghi GIF: {exc}")

    if not written:
        raise RuntimeError("Khong ghi duoc video (can opencv hoac Pillow)")
    return written


def _mesh_payload(
    dem: dict[str, Any],
    wse: np.ndarray,
    wet: np.ndarray,
    depth: np.ndarray,
    max_n: int = 144,
) -> dict[str, Any]:
    from rasterio.warp import transform as rio_tf

    nr, nc = wet.shape
    step = max(1, int(math.ceil(max(nr, nc) / float(max_n))))
    rows = np.arange(0, nr, step)
    cols = np.arange(0, nc, step)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    T = dem["transform"]
    xs = T.c + (cc + 0.5) * T.a + (rr + 0.5) * T.b
    ys = T.f + (cc + 0.5) * T.d + (rr + 0.5) * T.e
    crs = dem.get("crs")
    if crs is not None:
        transformed = rio_tf(crs, "EPSG:4326", xs.ravel().tolist(), ys.ravel().tolist())
        lons, lats = transformed[0], transformed[1]
        lons = np.asarray(lons, dtype=float).reshape(xs.shape)
        lats = np.asarray(lats, dtype=float).reshape(ys.shape)
    else:
        lons = np.broadcast_to(dem["lon_x"][cols], xs.shape)
        lats = np.broadcast_to(dem["lat_y"][rows][:, None], xs.shape)
    w = wse[rows[:, None], cols]
    z = dem["z"][rows[:, None], cols]
    d = depth[rows[:, None], cols]
    m = wet[rows[:, None], cols]
    wse_out = np.where(m, w, np.nan).astype(np.float32)
    z_out = np.where(m, z, np.nan).astype(np.float32)
    d_out = np.where(m, d, np.nan).astype(np.float32)
    def nullable_round(values: np.ndarray, decimals: int) -> list[list[float | None]]:
        values = np.asarray(values, dtype=float)
        rounded = np.round(values, decimals).astype(object)
        rounded[~np.isfinite(values)] = None
        return rounded.tolist()

    return {
        "lons": nullable_round(lons, 6),
        "lats": nullable_round(lats, 6),
        "wse": nullable_round(wse_out, 3),
        "z": nullable_round(z_out, 3),
        "depth": nullable_round(d_out, 3),
    }


def simulate_flood(
    *,
    dem_path: Optional[Path] = None,
    time_index: Optional[int] = None,
    hour: Optional[float] = None,
    buffer_m: float = DEFAULT_BUFFER_M,
    max_dim: int = DEFAULT_MAX_DIM,
    connected: bool = True,
    include_mesh: bool = True,
    water_source: str = "saint-venant",
) -> FloodResult:
    src = _norm_source(water_source)
    dem_path = resolve_dem_path(None, str(dem_path) if dem_path else None)
    mus = _pick_route(src)
    hours, h_st = _require_hours(mus, src)

    if hour is not None:
        t = int(np.argmin(np.abs(hours - float(hour))))
    elif time_index is not None:
        t = int(time_index)
    else:
        t = int(np.nanargmax(np.nanmean(h_st, axis=1)))
    t = max(0, min(t, int(hours.size) - 1))

    depth, wet, wse, dem = inundate_time(
        dem_path,
        t,
        buffer_m=float(buffer_m),
        max_dim=int(max_dim),
        connected=connected,
        water_source=src,
    )
    z = dem["z"]

    n_wet = int(np.count_nonzero(wet))
    cell_m2 = float(dem["cell_m2"])
    area_km2 = n_wet * cell_m2 / 1.0e6
    vol = float(np.nansum(np.where(wet, depth, 0.0))) * cell_m2
    max_d = float(np.nanmax(depth)) if n_wet else 0.0
    mean_d = float(np.nanmean(depth[wet])) if n_wet else 0.0
    png = _overlay_png(depth, wet)
    mesh = _mesh_payload(dem, wse, wet, depth) if include_mesh else {}

    return FloodResult(
        hours=hours,
        time_index=t,
        hour=float(hours[t]),
        z=z,
        wse=wse,
        depth=depth,
        wet=wet,
        transform=dem["transform"],
        crs=dem["crs"],
        bounds_wgs84=dem["bounds"],
        cell_m2=cell_m2,
        area_km2=area_km2,
        volume_m3=vol,
        max_depth_m=max_d,
        mean_depth_m=mean_d,
        n_wet=n_wet,
        png_bytes=png,
        mesh=mesh,
        water_source=src,
    )


def write_outputs(res: FloodResult, out_dir: Path, *, write_tif: bool = True, write_png: bool = True) -> None:
    import rasterio

    out_dir.mkdir(parents=True, exist_ok=True)
    if write_png:
        (out_dir / "flood_map.png").write_bytes(res.png_bytes)
    if write_tif:
        depth = np.where(res.wet, res.depth, -9999.0).astype(np.float32)
        with rasterio.open(
            out_dir / "flood_depth.tif",
            "w",
            driver="GTiff",
            height=depth.shape[0],
            width=depth.shape[1],
            count=1,
            dtype="float32",
            crs=res.crs,
            transform=res.transform,
            nodata=-9999.0,
            compress="lzw",
        ) as dst:
            dst.write(depth, 1)
    summary = out_dir / "flood_summary.csv"
    write_csv_rows(
        summary,
        ["hour", "time_index", "area_km2", "volume_m3", "max_depth_m", "mean_depth_m", "n_wet"],
        [
            {
                "hour": f"{res.hour:.2f}",
                "time_index": res.time_index,
                "area_km2": f"{res.area_km2:.4f}",
                "volume_m3": f"{res.volume_m3:.1f}",
                "max_depth_m": f"{res.max_depth_m:.3f}",
                "mean_depth_m": f"{res.mean_depth_m:.3f}",
                "n_wet": res.n_wet,
            }
        ],
        rebuild_hydro=False,
    )


def result_json(res: FloodResult, *, include_png: bool = True) -> dict[str, Any]:
    import base64

    b = res.bounds_wgs84
    payload: dict[str, Any] = {
        "ok": True,
        "hour": round(res.hour, 2),
        "time_index": res.time_index,
        "n_times": int(res.hours.size),
        "hours": [round(float(v), 2) for v in res.hours.tolist()],
        "stats": {
            "area_km2": round(res.area_km2, 4),
            "volume_m3": round(res.volume_m3, 1),
            "max_depth_m": round(res.max_depth_m, 3),
            "mean_depth_m": round(res.mean_depth_m, 3),
            "n_wet": res.n_wet,
            "n_branches": len(tributary_routes(getattr(res, "water_source", "saint-venant"))),
            "n_extra_mains": len(extra_main_routes(getattr(res, "water_source", "saint-venant"))),
        },
        "bounds": b,
        "leaflet_bounds": [[b["south"], b["west"]], [b["north"], b["east"]]],
        "water_source": getattr(res, "water_source", "saint-venant"),
        "water_source_label": _source_label(getattr(res, "water_source", "saint-venant")),
    }
    if res.mesh:
        payload["mesh"] = res.mesh
    if include_png:
        payload["png_b64"] = base64.b64encode(res.png_bytes).decode("ascii")
    return payload


_FRAME_CACHE: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_MESH_CACHE: dict[tuple, dict[str, Any]] = {}
_FRAME_CACHE_MAX = 220
_CACHE_LOCK = threading.Lock()


def clear_flood_result_caches() -> None:
    """Xoa cache H/index sau khi chay lai mo hinh 1D."""
    _cached_index.cache_clear()
    _cached_h_along.cache_clear()
    _cached_seed.cache_clear()
    clear_route_cache()
    with _CACHE_LOCK:
        _FRAME_CACHE.clear()
        _MESH_CACHE.clear()


def _cache_key(
    dem_path: Path, time_index: int, buffer_m: float, max_dim: int, water_source: str
) -> tuple:
    return (
        str(dem_path),
        int(time_index),
        round(float(buffer_m), 1),
        int(max_dim),
        str(water_source),
        4,
    )


def _cache_put(key: tuple, payload: dict[str, Any]) -> None:
    _FRAME_CACHE[key] = payload
    _FRAME_CACHE.move_to_end(key)
    while len(_FRAME_CACHE) > _FRAME_CACHE_MAX:
        old, _val = _FRAME_CACHE.popitem(last=False)
        _MESH_CACHE.pop(old, None)


def _resolve_time_index(time_index: Any, hour: Any, water_source: str = "saint-venant") -> int:
    src = _norm_source(water_source)
    mus = _pick_route(src)
    hours, h_st = _require_hours(mus, src)
    if hour is not None:
        t = int(np.argmin(np.abs(hours - float(hour))))
    elif time_index is not None:
        t = int(time_index)
    else:
        t = int(np.nanargmax(np.nanmean(h_st, axis=1)))
    return max(0, min(t, int(hours.size) - 1))


def run_cached(
    dem_path: Path,
    *,
    time_index: Any = None,
    hour: Any = None,
    buffer_m: float = DEFAULT_BUFFER_M,
    max_dim: int = DEFAULT_MAX_DIM,
    include_mesh: bool = False,
    water_source: str = "saint-venant",
) -> dict[str, Any]:
    src = _norm_source(water_source)
    t = _resolve_time_index(time_index, hour, src)
    key = _cache_key(dem_path, t, buffer_m, max_dim, src)
    with _CACHE_LOCK:
        cached = _FRAME_CACHE.get(key)
        mesh = _MESH_CACHE.get(key) if include_mesh else None
    if cached is None:
        res = simulate_flood(
            dem_path=dem_path,
            time_index=t,
            buffer_m=float(buffer_m),
            max_dim=int(max_dim),
            include_mesh=include_mesh,
            water_source=src,
        )
        payload = result_json(res)
        mesh = payload.pop("mesh", None)
        with _CACHE_LOCK:
            _cache_put(key, payload)
            if mesh:
                _MESH_CACHE[key] = mesh
        cached = payload
    out = dict(cached)
    if include_mesh:
        if mesh is None:
            res = simulate_flood(
                dem_path=dem_path,
                time_index=t,
                buffer_m=float(buffer_m),
                max_dim=int(max_dim),
                include_mesh=True,
                water_source=src,
            )
            mesh = res.mesh
            with _CACHE_LOCK:
                _MESH_CACHE[key] = mesh
        out["mesh"] = mesh
    out["water_source"] = src
    out["water_source_label"] = _source_label(src)
    return out


def create_flood3d_blueprint(
    name: str = "flood3d",
    url_prefix: str = "/api/flood-3d",
    with_static: bool = True,
    static_url_path: str = "/static",
) -> Blueprint:
    bp = Blueprint(
        name,
        __name__,
        url_prefix=url_prefix,
        static_folder=str(PACKAGE_DIR / "static") if with_static else None,
        static_url_path=static_url_path if with_static else None,
    )

    def _fail(message: object, status: int = 500):
        return jsonify({"ok": False, "error": str(message)}), status

    @bp.route("/meta", methods=["GET"])
    def api_meta():
        try:
            src = _norm_source(request.args.get("water_source"))
            mus = _pick_route(src)
            hours, h = _require_hours(mus, src)
            peak = int(np.nanargmax(np.nanmean(h, axis=1))) if hours.size else 0
            branches = []
            extra_mains = []
            try:
                from flood_model.river_network import extra_main_centerlines, tributary_centerlines

                dem = resolve_dem_path(request.args.get("file_id"), request.args.get("dem_path"))
                with _open_dem(dem) as rio:
                    for tr in tributary_centerlines(rio, src):
                        branches.append(
                            {
                                "id": tr.get("id"),
                                "kind": tr.get("kind") or "outlet",
                                "lon": [round(float(v), 6) for v in list(tr["lon"])],
                                "lat": [round(float(v), 6) for v in list(tr["lat"])],
                            }
                        )
                for tr in extra_main_centerlines(src):
                    extra_mains.append(
                        {
                            "id": tr.get("id"),
                            "kind": "main",
                            "lon": [round(float(v), 6) for v in list(tr["lon"])],
                            "lat": [round(float(v), 6) for v in list(tr["lat"])],
                        }
                    )
            except Exception:
                traceback.print_exc()
            if not branches:
                for rec in tributary_routes(src):
                    branches.append(
                        {
                            "id": rec.get("id"),
                            "kind": rec.get("kind") or "outlet",
                            "lon": [round(float(v), 6) for v in np.asarray(rec["lon"]).tolist()],
                            "lat": [round(float(v), 6) for v in np.asarray(rec["lat"]).tolist()],
                        }
                    )
            if not extra_mains:
                for rec in extra_main_routes(src):
                    extra_mains.append(
                        {
                            "id": rec.get("id"),
                            "kind": "main",
                            "lon": [round(float(v), 6) for v in np.asarray(rec["lon"]).tolist()],
                            "lat": [round(float(v), 6) for v in np.asarray(rec["lat"]).tolist()],
                        }
                    )
            return jsonify(
                {
                    "ok": True,
                    "n_times": int(hours.size),
                    "hours": [round(float(v), 2) for v in hours.tolist()],
                    "peak_index": peak,
                    "peak_hour": round(float(hours[peak]), 2) if hours.size else 0,
                    "n_station": int(np.asarray(mus["station_m"]).size),
                    "n_branches": len(branches),
                    "n_extra_mains": len(extra_mains),
                    "n_mains": 1 + len(extra_mains),
                    "water_source": src,
                    "water_source_label": _source_label(src),
                    "water_sources": hydro1d_water_sources(),
                    "river": {
                        "lon": [round(float(v), 6) for v in np.asarray(mus["lon"]).tolist()],
                        "lat": [round(float(v), 6) for v in np.asarray(mus["lat"]).tolist()],
                    },
                    "river_branches": branches,
                    "river_extra_mains": extra_mains,
                }
            )
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/run", methods=["POST"])
    def api_run():
        try:
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            buf = float(data.get("buffer_m") or DEFAULT_BUFFER_M)
            t = data.get("time_index")
            hour = data.get("hour")
            include_mesh = bool(data.get("include_mesh", False))
            src = _norm_source(data.get("water_source"))
            payload = run_cached(
                dem,
                time_index=t,
                hour=hour,
                buffer_m=buf,
                include_mesh=include_mesh,
                water_source=src,
            )
            return jsonify(payload)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi ngap lut: {exc}", 500)

    @bp.route("/video", methods=["GET"])
    def api_video_get():
        mp4 = DEFAULT_OUT_DIR / "flood_animation.mp4"
        gif = DEFAULT_OUT_DIR / "flood_animation.gif"
        want = str(request.args.get("fmt") or "").strip().lower()
        if want == "mp4" and mp4.is_file():
            path = mp4
        elif gif.is_file():
            path = gif
        elif mp4.is_file():
            path = mp4
        else:
            return _fail("Chua co video. Bam Tao video hoac chay: python flood_3d.py --video", 404)
        mime = "video/mp4" if path.suffix.lower() == ".mp4" else "image/gif"
        return send_file(path, mimetype=mime, as_attachment=False, download_name=path.name)

    @bp.route("/video", methods=["POST"])
    def api_video_post():
        try:
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            buf = float(data.get("buffer_m") or DEFAULT_BUFFER_M)
            step = int(data.get("step") or 1)
            fps = float(data.get("fps") or 10)
            src = _norm_source(data.get("water_source"))
            written = write_flood_video(
                dem_path=dem,
                buffer_m=buf,
                max_dim=int(data.get("max_dim") or 480),
                step=step,
                fps=fps,
                water_source=src,
            )
            play = public_path("/api/flood-3d/video")
            if "gif" in written:
                play = public_path("/api/flood-3d/video") + "?fmt=gif"
            return jsonify(
                {
                    "ok": True,
                    "url": play,
                    "files": {k: str(v) for k, v in written.items()},
                }
            )
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi tao video: {exc}", 500)

    return bp


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mo phong ngap lut tu H 1D Saint-Venant va DEM")
    p.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    p.add_argument("--hour", type=float, default=None, help="Gio trong chuoi H (bo qua thi lay gio H TB lon nhat)")
    p.add_argument("--time-index", type=int, default=None)
    p.add_argument("--buffer", type=float, default=DEFAULT_BUFFER_M, help="Ban kinh vuong goc long song (m)")
    p.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--no-plot", action="store_true")
    p.add_argument("--video", action="store_true", help="Ghi video ngap theo thoi gian (mp4 + gif)")
    p.add_argument("--fps", type=float, default=10.0)
    p.add_argument("--step", type=int, default=1, help="Buoc gio khi lam video (1 = moi gio)")
    p.add_argument(
        "--water-source",
        default="saint-venant",
        choices=["saint-venant", "saint-venant-1d"],
        help="Model 1D: saint-venant hoac saint-venant-1d (MIKE HD)",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    src = _norm_source(args.water_source)
    print(f"DEM            : {args.dem}")
    print(f"Muc nuoc 1D    : {_source_label(src)}")
    print(f"Song nhanh     : {len(tributary_routes(src))}")
    print(f"Song chinh them: {len(extra_main_routes(src))}")
    print(f"Buffer         : {args.buffer:.0f} m")
    if args.video:
        written = write_flood_video(
            dem_path=args.dem,
            out_dir=args.out_dir,
            buffer_m=float(args.buffer),
            max_dim=int(args.max_dim),
            step=int(args.step),
            fps=float(args.fps),
            water_source=src,
        )
        for kind, path in written.items():
            print(f"Video {kind:4s}    : {path}")
        return 0
    res = simulate_flood(
        dem_path=args.dem,
        time_index=args.time_index,
        hour=args.hour,
        buffer_m=float(args.buffer),
        max_dim=int(args.max_dim),
        water_source=src,
    )
    write_outputs(res, args.out_dir, write_png=not args.no_plot)
    print(f"Gio            : {res.hour:.1f}  (index {res.time_index}/{res.hours.size - 1})")
    print(f"Dien tich ngap : {res.area_km2:.3f} km2")
    print(f"The tich       : {res.volume_m3:.3e} m3")
    print(f"Do sau max/TB  : {res.max_depth_m:.2f} / {res.mean_depth_m:.2f} m")
    print(f"GeoTIFF        : {args.out_dir / 'flood_depth.tif'}")
    if not args.no_plot:
        print(f"Ban do PNG     : {args.out_dir / 'flood_map.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Mo phong dong nuoc chay tu thuong nguon ve ha luu tren DEM 3D.

- Trich long chinh (hinh hoc 1D hoac thalweg DEM) + song nhanh (Song Duong).
- Tinh huong dong D8 + tich luy de lay nganh nhap.
- Gan Q(t), H(t) tu Saint-Venant (hoac Q TANK neu chua co 1D).
- Kem chuoi mua (rainfall-simulation.py) de ve hat roi tu 5 km tren DEM 3D.
- API / CLI tra polyline + hat nuoc + hat mua de Three.js animate.

Chay:
  .\\venv\\Scripts\\python.exe flow_run.py
  POST /api/flow-run/simulate
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import runpy
import sys
import traceback
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, cast

import numpy as np
import numpy.typing as npt
from flask import Blueprint, jsonify, request
from rasterio.enums import Resampling
from rasterio.transform import Affine, xy
from rasterio.warp import transform as rio_transform

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open
from flood_model.boundary import load_tank_q
from flood_model.routing import (
    extra_main_routes,
    hydro1d_csv_paths,
    hydro1d_label,
    is_main_reach_id,
    is_primary_main_reach,
    load_grouped_geom_csv,
    normalize_reach_id,
    parse_hydro1d_source,
    pick_route_by_id,
)

DEFAULT_DEM = ROOT / "projects" / "data" / "dem-song-hong.tif"
SAINT_VENANT_GEOM_CSV = ROOT / "saint_venant_output" / "demo_river_geometry.csv"
SAINT_VENANT_H_CSV = ROOT / "saint_venant_output" / "saint_venant_result.csv"
TANK_RESULT_CSV = ROOT / "rainfall_runoff_output" / "tank_result.csv"

D8_MAX_DIM = 220
CHANNEL_MAX_PTS = 420
TRACE_MAX = 22
TRACE_MIN_PTS = 8
WEB_MERCATOR_MAX = 20037508.342789244

D8 = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


def _paths(root: Path, water_source: str = "saint-venant") -> dict[str, Path]:
    root = Path(root)
    hp = hydro1d_csv_paths(water_source, root)
    return {
        "root": root,
        "dem": root / "projects" / "data" / "dem-song-hong.tif",
        "geom": Path(hp["geom"]),
        "h": Path(hp["h"]),
        "trib_geom": Path(hp["trib_geom"]),
        "trib_h": Path(hp["trib_h"]),
        "extra_h": Path(hp["extra_h"]),
        "tank": root / "rainfall_runoff_output" / "tank_result.csv",
        "rain": root / "rainfall_runoff_output" / "demo_rainfall.csv",
    }


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
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    if _is_web_mercator(src):
        pts = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lons, lats)]
        return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
    try:
        transformed = cast(
            tuple[list[float], list[float]],
            rio_transform("EPSG:4326", src.crs, lons.tolist(), lats.tolist()),
        )
        xs, ys = transformed[0], transformed[1]
        return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    except Exception:
        pts = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lons, lats)]
        return np.array([p[0] for p in pts]), np.array([p[1] for p in pts])


def dem_xy_to_lonlat(src, xs: npt.ArrayLike, ys: npt.ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if _is_web_mercator(src):
        ll = [mercator_to_lonlat(float(x), float(y)) for x, y in zip(xs, ys)]
        return np.array([p[0] for p in ll]), np.array([p[1] for p in ll])
    try:
        transformed = cast(
            tuple[list[float], list[float]],
            rio_transform(src.crs, "EPSG:4326", xs.tolist(), ys.tolist()),
        )
        lons, lats = transformed[0], transformed[1]
        return np.asarray(lons, dtype=float), np.asarray(lats, dtype=float)
    except Exception:
        ll = [mercator_to_lonlat(float(x), float(y)) for x, y in zip(xs, ys)]
        return np.array([p[0] for p in ll]), np.array([p[1] for p in ll])


def densify_xy(xs: np.ndarray, ys: np.ndarray, step_m: float, max_pts: int = CHANNEL_MAX_PTS) -> tuple[np.ndarray, np.ndarray]:
    """Giu cac nut XS goc, chi chen diem giua neu doan dai hon step_m."""
    if xs.size < 2:
        return xs, ys
    n0 = int(xs.size)
    if n0 >= max_pts:
        return xs, ys
    seg = np.hypot(np.diff(xs), np.diff(ys))
    budget = int(max_pts) - n0
    extra = np.zeros(n0 - 1, dtype=int)
    if budget > 0 and float(np.sum(seg)) > 0:
        want = np.maximum(0, np.floor(seg / max(float(step_m), 1.0)).astype(int))
        if int(np.sum(want)) > budget:
            w = np.maximum(seg, 1e-6)
            extra = np.floor(budget * w / float(np.sum(w))).astype(int)
            for _ in range(budget - int(np.sum(extra))):
                extra[int(np.argmax(seg / np.maximum(extra + 1, 1)))] += 1
        else:
            extra = want
    ox: list[float] = [float(xs[0])]
    oy: list[float] = [float(ys[0])]
    for i in range(n0 - 1):
        n_mid = int(extra[i])
        x0, y0, x1, y1 = float(xs[i]), float(ys[i]), float(xs[i + 1]), float(ys[i + 1])
        for k in range(1, n_mid + 1):
            t = k / (n_mid + 1)
            ox.append(x0 + t * (x1 - x0))
            oy.append(y0 + t * (y1 - y0))
        ox.append(x1)
        oy.append(y1)
    return np.asarray(ox, dtype=float), np.asarray(oy, dtype=float)


def sample_z(src, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    nodata = src.nodata
    coords = list(zip(xs.tolist(), ys.tolist()))
    z = np.array([v[0] for v in src.sample(coords)], dtype=float)
    invalid = ~np.isfinite(z)
    if nodata is not None:
        invalid |= z == nodata
    z[invalid] = np.nan
    if np.isfinite(z).sum() >= 2:
        idx = np.arange(z.size)
        good = np.isfinite(z)
        z[~good] = np.interp(idx[~good], idx[good], z[good])
    else:
        z = np.nan_to_num(z, nan=0.0)
    return z


def chainage_m(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    if xs.size == 0:
        return np.array([], dtype=float)
    return np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])


def _f(arr: np.ndarray, ndigits: int = 4) -> list[float]:
    out: list[float] = []
    for v in np.asarray(arr, dtype=float).ravel():
        if not np.isfinite(v):
            out.append(None)  # type: ignore[arg-type]
        else:
            out.append(round(float(v), ndigits))
    return out


def read_dem_grid(src, max_dim: int = D8_MAX_DIM) -> dict[str, Any]:
    scale = max(src.width, src.height) / float(max(32, max_dim))
    out_w = max(16, int(round(src.width / max(scale, 1.0))))
    out_h = max(16, int(round(src.height / max(scale, 1.0))))
    data = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.average).astype(float)
    nodata = src.nodata
    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    z = data.copy()
    z[~valid] = np.nan
    xres = src.res[0] * (src.width / out_w)
    yres = src.res[1] * (src.height / out_h)
    transform = Affine(xres, 0, src.bounds.left, 0, -abs(yres), src.bounds.top)
    return {
        "z": z,
        "valid": valid,
        "width": out_w,
        "height": out_h,
        "transform": transform,
        "cell_m": float(max(abs(xres), abs(yres))),
    }


def d8_flow(z: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h, w = z.shape
    fr = np.zeros((h, w), dtype=np.int8)
    fc = np.zeros((h, w), dtype=np.int8)
    for i in range(h):
        for j in range(w):
            if not valid[i, j]:
                continue
            z0 = z[i, j]
            best = 0.0
            for di, dj in D8:
                ni, nj = i + di, j + dj
                if ni < 0 or nj < 0 or ni >= h or nj >= w or not valid[ni, nj]:
                    continue
                drop = z0 - z[ni, nj]
                if drop <= 0:
                    continue
                slope = drop / (1.41421356 if di and dj else 1.0)
                if slope > best:
                    best = slope
                    fr[i, j] = di
                    fc[i, j] = dj
    return fr, fc


def flow_accumulation(z: np.ndarray, valid: np.ndarray, fr: np.ndarray, fc: np.ndarray) -> np.ndarray:
    h, w = z.shape
    acc = np.zeros((h, w), dtype=np.float64)
    acc[valid] = 1.0
    order = np.argsort((-np.where(valid, z, np.inf)).ravel(), kind="mergesort")
    for idx in order:
        i, j = divmod(int(idx), w)
        if not valid[i, j]:
            continue
        ni, nj = i + int(fr[i, j]), j + int(fc[i, j])
        if (ni, nj) == (i, j) or ni < 0 or nj < 0 or ni >= h or nj >= w:
            continue
        if valid[ni, nj]:
            acc[ni, nj] += acc[i, j]
    return acc


def _walk_down(i: int, j: int, fr: np.ndarray, fc: np.ndarray, valid: np.ndarray, limit: int) -> list[tuple[int, int]]:
    h, w = valid.shape
    path = [(i, j)]
    seen = {(i, j)}
    for _ in range(limit):
        ni, nj = i + int(fr[i, j]), j + int(fc[i, j])
        if (ni, nj) == (i, j) or ni < 0 or nj < 0 or ni >= h or nj >= w or not valid[ni, nj]:
            break
        if (ni, nj) in seen:
            break
        path.append((ni, nj))
        seen.add((ni, nj))
        i, j = ni, nj
    return path


def _walk_up_mainstem(outlet: tuple[int, int], acc: np.ndarray, fr: np.ndarray, fc: np.ndarray, valid: np.ndarray) -> list[tuple[int, int]]:
    """Di nguoc dong: o nao do vao o hien tai voi acc lon nhat."""
    h, w = valid.shape
    contrib: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i in range(h):
        for j in range(w):
            if not valid[i, j]:
                continue
            ni, nj = i + int(fr[i, j]), j + int(fc[i, j])
            if (ni, nj) == (i, j) or ni < 0 or nj < 0 or ni >= h or nj >= w:
                continue
            contrib.setdefault((ni, nj), []).append((i, j))
    path = [outlet]
    cur = outlet
    seen = {outlet}
    for _ in range(h * w):
        ups = contrib.get(cur) or []
        if not ups:
            break
        nxt = max(ups, key=lambda p: float(acc[p[0], p[1]]))
        if nxt in seen:
            break
        path.append(nxt)
        seen.add(nxt)
        cur = nxt
    path.reverse()
    return path


def cells_to_xy(cells: Sequence[tuple[int, int]], transform: Affine) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for i, j in cells:
        x, y = xy(transform, int(i), int(j), offset="center")
        xs.append(float(x))
        ys.append(float(y))
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def extract_stream_network(grid: dict[str, Any]) -> dict[str, Any]:
    z = grid["z"]
    valid = grid["valid"]
    fr, fc = d8_flow(z, valid)
    acc = flow_accumulation(z, valid, fr, fc)
    if not np.any(valid):
        raise ValueError("DEM khong co o hop le de tinh dong chay.")
    acc_valid = acc[valid]
    p94 = float(np.percentile(acc_valid, 93.5))
    thr = max(p94, 6.0)
    thr = min(thr, max(float(np.max(acc_valid)) * 0.03, 8.0))
    stream = valid & (acc >= thr)
    outlet_idx = int(np.nanargmax(np.where(valid, acc, -1.0)))
    oi, oj = divmod(outlet_idx, grid["width"])
    main = _walk_up_mainstem((oi, oj), acc, fr, fc, valid)
    if len(main) < 4:
        raise ValueError("Khong noi duoc long chinh tu D8.")

    heads: list[tuple[int, int, float]] = []
    h, w = valid.shape
    for i in range(h):
        for j in range(w):
            if not stream[i, j]:
                continue
            upstream = False
            for di, dj in D8:
                ni, nj = i - di, j - dj
                if ni < 0 or nj < 0 or ni >= h or nj >= w:
                    continue
                if not stream[ni, nj]:
                    continue
                if int(fr[ni, nj]) == di and int(fc[ni, nj]) == dj:
                    upstream = True
                    break
            if not upstream:
                heads.append((i, j, float(acc[i, j])))
    heads.sort(key=lambda t: t[2], reverse=True)
    if len(heads) < 6:
        hi = max(2, h // 2)
        extra = []
        for i in range(hi):
            for j in range(w):
                if valid[i, j] and acc[i, j] >= max(thr * 0.2, 4.0):
                    extra.append((i, j, float(acc[i, j])))
        extra.sort(key=lambda t: t[2], reverse=True)
        seen = {(i, j) for i, j, _ in heads}
        for item in extra:
            if (item[0], item[1]) in seen:
                continue
            heads.append(item)
            seen.add((item[0], item[1]))
            if len(heads) >= TRACE_MAX:
                break

    traces = []
    for i, j, _ in heads[: TRACE_MAX + 8]:
        cells = _walk_down(i, j, fr, fc, valid, limit=max(grid["height"], grid["width"]) * 2)
        if len(cells) < 4:
            continue
        traces.append(cells)
        if len(traces) >= TRACE_MAX:
            break

    return {
        "main": main,
        "traces": traces,
        "acc": acc,
        "outlet": (oi, oj),
        "threshold": thr,
        "n_stream": int(stream.sum()),
    }


def _xs_cols(fields: Sequence[str], kind: str) -> list[tuple[int, str]]:
    cols: list[tuple[int, str]] = []
    for name in fields:
        m = re.match(rf"^{kind}_xs(\d+)_", name or "")
        if m:
            cols.append((int(m.group(1)), name))
    cols.sort(key=lambda x: x[0])
    return cols


def load_geometry_csv(path: Path) -> dict[str, np.ndarray] | None:
    if not csv_available(path):
        return None
    stations, lons, lats, z_bed, widths = [], [], [], [], []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            if not is_primary_main_reach(row.get("reach_id") or row.get("reach")):
                continue
            try:
                stations.append(float(row["station_km"]) * 1000.0)
                lons.append(float(row["lon"]))
                lats.append(float(row["lat"]))
                z_bed.append(float(row.get("z_bed_m") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                widths.append(float(row.get("top_width_at_3m") or 200.0))
            except (TypeError, ValueError):
                widths.append(200.0)
    if len(stations) < 2:
        return None
    return {
        "station_m": np.asarray(stations, dtype=float),
        "lon": np.asarray(lons, dtype=float),
        "lat": np.asarray(lats, dtype=float),
        "z_bed": np.asarray(z_bed, dtype=float),
        "width_m": np.asarray(widths, dtype=float),
    }


def load_tributary_geoms(path: Path) -> list[dict[str, Any]]:
    """Hinh hoc 1D tung nhanh (demo_tributary_geometry.csv)."""
    return load_grouped_geom_csv(path, which="trib")


def load_tributary_series(path: Path) -> dict[str, dict[str, np.ndarray]]:
    """Q/H nut giao va dau xa: trib_1_h_join_m, trib_1_h_outer_m, ..."""
    if not csv_available(path):
        return {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        ids: list[str] = []
        for name in fields:
            m = re.match(r"^(.+)_h_join_m$", name or "")
            if m:
                ids.append(normalize_reach_id(m.group(1), "trib_1"))
        if not ids:
            return {}
        raw_by_norm: dict[str, str] = {}
        for name in fields:
            m = re.match(r"^(.+)_h_join_m$", name or "")
            if m:
                raw_by_norm[normalize_reach_id(m.group(1), "trib_1")] = m.group(1)
        hours: list[float] = []
        store = {rid: {"h_join": [], "h_outer": [], "q_join": [], "q_outer": []} for rid in ids}
        for row in reader:
            try:
                hours.append(float(row["hour"]))
            except (KeyError, TypeError, ValueError):
                continue
            for rid in ids:
                raw = raw_by_norm.get(rid, rid)

                def _cell(col: str, rec: dict[str, str] = row) -> float:
                    try:
                        return float(rec[col])
                    except (KeyError, TypeError, ValueError):
                        return float("nan")

                store[rid]["h_join"].append(_cell(f"{raw}_h_join_m"))
                store[rid]["h_outer"].append(_cell(f"{raw}_h_outer_m"))
                store[rid]["q_join"].append(_cell(f"{raw}_q_join_m3s"))
                store[rid]["q_outer"].append(_cell(f"{raw}_q_outer_m3s"))
    if not hours:
        return {}
    t = np.asarray(hours, dtype=float)
    out: dict[str, dict[str, np.ndarray]] = {}
    for rid, d in store.items():
        out[rid] = {
            "hours": t,
            "h_join": np.asarray(d["h_join"], dtype=float),
            "h_outer": np.asarray(d["h_outer"], dtype=float),
            "q_join": np.asarray(d["q_join"], dtype=float),
            "q_outer": np.asarray(d["q_outer"], dtype=float),
        }
    return out


def h_along_join_outer(
    station_m: np.ndarray,
    z_bed: np.ndarray,
    hours_out: np.ndarray,
    series: dict[str, np.ndarray],
) -> np.ndarray:
    """Noi suy H doc nhanh tu H nut giao -> H dau xa, bam gio long chinh."""
    s = np.asarray(station_m, dtype=float)
    s = s - float(s[0])
    length = float(s[-1]) if s.size else 1.0
    th = np.asarray(series["hours"], dtype=float)
    hj = np.interp(hours_out, th, np.nan_to_num(series["h_join"], nan=0.0))
    ho = np.interp(hours_out, th, np.nan_to_num(series["h_outer"], nan=0.0))
    z = np.asarray(z_bed, dtype=float)
    out = np.empty((hours_out.size, s.size), dtype=float)
    for t in range(int(hours_out.size)):
        row = np.interp(s, [0.0, max(length, 1.0)], [float(hj[t]), float(ho[t])])
        out[t] = np.maximum(row, z + 0.05)
    return out


def load_saint_venant_series(path: Path, n_x: int) -> dict[str, Any] | None:
    if not csv_available(path) or n_x < 2:
        return None
    hours: list[float] = []
    h_rows: list[list[float]] = []
    q_rows: list[list[float]] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        h_cols = _xs_cols(fields, "h")
        q_cols = _xs_cols(fields, "q")
        if len(h_cols) < 2:
            return None
        n = min(len(h_cols), n_x)
        q_by = {xs: name for xs, name in q_cols}
        q_aligned = [(xs, q_by[xs]) for xs, _ in h_cols[:n] if xs in q_by]
        have_q = len(q_aligned) == n
        for row in reader:
            try:
                hours.append(float(row["hour"]))
            except (KeyError, TypeError, ValueError):
                continue
            hv: list[float] = []
            for _, col in h_cols[:n]:
                try:
                    hv.append(float(row[col]))
                except (KeyError, TypeError, ValueError):
                    hv.append(float("nan"))
            h_rows.append(hv)
            if have_q:
                qv: list[float] = []
                for _, col in q_aligned:
                    try:
                        qv.append(float(row[col]))
                    except (KeyError, TypeError, ValueError):
                        qv.append(float("nan"))
                q_rows.append(qv)
    if not hours:
        return None
    return {
        "hours": np.asarray(hours, dtype=float),
        "h": np.asarray(h_rows, dtype=float),
        "q": np.asarray(q_rows, dtype=float) if q_rows else None,
    }


def _rainfall_for_hours(hours: np.ndarray, rain_csv: Path) -> dict[str, Any]:
    """Doc demo_rainfall.csv qua rainfall-simulation.py (cot hat 5 km)."""
    script = ROOT / "rainfall-simulation.py"
    n = int(np.asarray(hours).size)
    empty = {
        "rainfall_mm": np.zeros(n, dtype=float),
        "rain_peak_mm": 0.0,
        "cloud_height_m": 5000.0,
        "v_term_ms": 8.0,
        "wind_east_ms": 1.6,
        "wind_north_ms": 0.4,
        "rain_csv": str(rain_csv),
    }
    if not script.is_file():
        return empty
    try:
        mod = runpy.run_path(str(script), run_name="rainfall_simulation")
        fn = mod.get("rainfall_for_flow")
        if callable(fn):
            return cast(dict[str, Any], fn(hours, rain_csv))
    except Exception:
        traceback.print_exc()
    return empty


def _ensure_downstream(xs: np.ndarray, ys: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Diem 0 = thuong nguon (cao hon), diem cuoi = ha luu."""
    if z.size < 2:
        return xs, ys, z
    n = min(8, z.size)
    if float(np.nanmean(z[:n])) < float(np.nanmean(z[-n:])):
        return xs[::-1].copy(), ys[::-1].copy(), z[::-1].copy()
    return xs, ys, z


def polyline_from_cells(src, cells: Sequence[tuple[int, int]], transform: Affine, step_m: float, max_pts: int) -> dict[str, np.ndarray]:
    xs, ys = cells_to_xy(cells, transform)
    xs, ys = densify_xy(xs, ys, step_m, max_pts=max_pts)
    z = sample_z(src, xs, ys)
    xs, ys, z = _ensure_downstream(xs, ys, z)
    lon, lat = dem_xy_to_lonlat(src, xs, ys)
    return {
        "lon": lon,
        "lat": lat,
        "elev": z,
        "station_m": chainage_m(xs, ys),
        "x": xs,
        "y": ys,
    }


def channel_from_geometry(src, geom: dict[str, np.ndarray], step_m: float) -> dict[str, np.ndarray]:
    xs, ys = lonlat_to_dem_xy(src, geom["lon"], geom["lat"])
    xs, ys = densify_xy(xs, ys, step_m, max_pts=CHANNEL_MAX_PTS)
    z_dem = sample_z(src, xs, ys)
    st = chainage_m(xs, ys)
    g_st = geom["station_m"] - geom["station_m"][0]
    # Bam dung thu tu mat cat 1D (thuong nguon -> ha luu), khong lat nguoc.
    z_bed = np.interp(st, g_st, geom["z_bed"])
    width = np.interp(st, g_st, np.clip(geom["width_m"], 40.0, 2500.0))
    lon, lat = dem_xy_to_lonlat(src, xs, ys)
    return {
        "lon": lon,
        "lat": lat,
        "elev": z_dem,
        "z_bed": z_bed,
        "station_m": st,
        "width_m": width,
        "x": xs,
        "y": ys,
    }


def interp_series_along(station_m: np.ndarray, xs_st: np.ndarray, series_tx: np.ndarray) -> np.ndarray:
    xs_st = xs_st - xs_st[0]
    out = np.empty((series_tx.shape[0], station_m.size), dtype=float)
    for t in range(series_tx.shape[0]):
        row = np.asarray(series_tx[t], dtype=float)
        good = np.isfinite(row)
        if good.sum() < 2:
            out[t] = np.nan
            continue
        out[t] = np.interp(station_m, xs_st[good], row[good])
    return out


def simulate_flow(
    dem_path: str | Path | None = None,
    *,
    root: str | Path | None = None,
    geom_csv: str | Path | None = None,
    h_csv: str | Path | None = None,
    tank_csv: str | Path | None = None,
    water_source: str = "saint-venant",
) -> dict[str, Any]:
    """Tinh long chinh + nhanh 1D (Song Duong) va chuoi Q/H de animate tren DEM 3D."""
    import rasterio

    kind = parse_hydro1d_source(water_source)
    p = _paths(Path(root) if root else ROOT, kind)
    dem = Path(dem_path) if dem_path else p["dem"]
    geom_path = Path(geom_csv) if geom_csv else p["geom"]
    h_path = Path(h_csv) if h_csv else p["h"]
    tank_path = Path(tank_csv) if tank_csv else p["tank"]
    trib_geom_path = p["trib_geom"]
    trib_h_path = p["trib_h"]
    if not dem.is_file():
        raise FileNotFoundError(f"Khong tim thay DEM: {dem}")

    reservoirs: list[dict[str, Any]] = []
    with rasterio.open(dem) as rio:
        geom = load_geometry_csv(geom_path)
        trib_geoms = load_tributary_geoms(trib_geom_path)
        extra_geoms = load_grouped_geom_csv(geom_path, which="extra_main")
        traces: list[dict[str, np.ndarray]] = []
        trib_channels: list[tuple[dict[str, Any], dict[str, np.ndarray]]] = []
        extra_channels: list[tuple[dict[str, Any], dict[str, np.ndarray]]] = []
        channel: dict[str, np.ndarray] | None = None
        channel_src = ""
        try:
            from flood_model.flow_3d import profile_from_lonlat
            from flood_model.river_network import dem_river_network

            step = max(float(abs(rio.res[0])), 40.0)
            net = dem_river_network(rio, kind)
            mprof = profile_from_lonlat(rio, net["main_lon"], net["main_lat"], step_m=step)
            n_m = int(mprof["lon"].size)
            width = 180.0
            if geom is not None and int(geom["width_m"].size):
                good_w = geom["width_m"][np.isfinite(geom["width_m"])]
                if good_w.size:
                    width = float(np.median(good_w))
            channel = {
                "lon": mprof["lon"],
                "lat": mprof["lat"],
                "elev": mprof["z_dem"],
                "z_bed": mprof["z_bed"],
                "station_m": mprof["distance_m"],
                "width_m": np.full(n_m, max(width, 40.0)),
                "x": mprof["x"],
                "y": mprof["y"],
            }
            channel_src = "dem-thalweg"
            trib_by_id = {normalize_reach_id(tg["id"], "trib_1"): tg for tg in trib_geoms}
            used_trib: set[str] = set()
            for i, tr in enumerate(net.get("branches") or []):
                lon_b = [float(v) for v in tr["lon"]]
                lat_b = [float(v) for v in tr["lat"]]
                if len(lon_b) < 2:
                    continue
                tprof = profile_from_lonlat(rio, lon_b, lat_b, step_m=step)
                rid = normalize_reach_id(tr.get("id") or f"trib_{i + 1}", "trib_1")
                tg = trib_by_id.get(rid)
                if tg is None:
                    tg = {
                        "id": rid,
                        "kind": "outlet",
                        "bc": "H",
                        "station_m": tprof["distance_m"],
                        "lon": tprof["lon"],
                        "lat": tprof["lat"],
                        "z_bed": tprof["z_bed"],
                        "width_m": np.full(tprof["lon"].size, 280.0),
                    }
                else:
                    used_trib.add(rid)
                    tg = dict(tg)
                    tg["id"] = rid
                tch = {
                    "lon": tprof["lon"],
                    "lat": tprof["lat"],
                    "elev": tprof["z_dem"],
                    "z_bed": tprof["z_bed"],
                    "station_m": tprof["distance_m"],
                    "width_m": np.full(tprof["lon"].size, 280.0),
                    "x": tprof["x"],
                    "y": tprof["y"],
                }
                tg["lon"] = tprof["lon"]
                tg["lat"] = tprof["lat"]
                tg["station_m"] = tprof["distance_m"]
                tg["z_bed"] = tprof["z_bed"]
                trib_channels.append((tg, tch))
            for tg in trib_geoms:
                rid = normalize_reach_id(tg["id"], "trib_1")
                if rid in used_trib:
                    continue
                item = dict(tg)
                item["id"] = rid
                trib_channels.append((item, channel_from_geometry(rio, item, step)))
                used_trib.add(rid)
            extra_by_id = {normalize_reach_id(g["id"], "main_2"): g for g in extra_geoms}
            used_extra: set[str] = set()
            for tr in net.get("extra_mains") or []:
                lon_b = [float(v) for v in tr["lon"]]
                lat_b = [float(v) for v in tr["lat"]]
                if len(lon_b) < 2:
                    continue
                tprof = profile_from_lonlat(rio, lon_b, lat_b, step_m=step)
                rid = normalize_reach_id(tr.get("id"), "main_2")
                tg = extra_by_id.get(rid)
                if tg is None:
                    tg = {
                        "id": rid,
                        "kind": "main",
                        "bc": "Q/H",
                        "station_m": tprof["distance_m"],
                        "lon": tprof["lon"],
                        "lat": tprof["lat"],
                        "z_bed": tprof["z_bed"],
                        "width_m": np.full(tprof["lon"].size, 180.0),
                    }
                else:
                    used_extra.add(rid)
                    tg = dict(tg)
                    tg["id"] = rid
                tch = {
                    "lon": tprof["lon"],
                    "lat": tprof["lat"],
                    "elev": tprof["z_dem"],
                    "z_bed": tprof["z_bed"],
                    "station_m": tprof["distance_m"],
                    "width_m": np.full(tprof["lon"].size, 180.0),
                    "x": tprof["x"],
                    "y": tprof["y"],
                }
                extra_channels.append((tg, tch))
            for g in extra_geoms:
                rid = normalize_reach_id(g["id"], "main_2")
                if rid in used_extra:
                    continue
                item = dict(g)
                item["id"] = rid
                extra_channels.append((item, channel_from_geometry(rio, item, step)))
                used_extra.add(rid)
        except Exception:
            traceback.print_exc()
            channel = None
            trib_channels = []
            extra_channels = []
        if channel is None and geom is not None:
            step = max(float(abs(rio.res[0])), 40.0)
            channel = channel_from_geometry(rio, geom, step)
            channel_src = hydro1d_label(kind) + "-geometry"
            trib_channels = []
            extra_channels = []
            for tg in trib_geoms:
                item = dict(tg)
                item["id"] = normalize_reach_id(tg["id"], "trib_1")
                trib_channels.append((item, channel_from_geometry(rio, item, step)))
            for g in extra_geoms:
                item = dict(g)
                item["id"] = normalize_reach_id(g["id"], "main_2")
                extra_channels.append((item, channel_from_geometry(rio, item, step)))
        elif channel is None:
            grid = read_dem_grid(rio)
            d8 = extract_stream_network(grid)
            step = max(float(grid["cell_m"]), 40.0)
            channel = polyline_from_cells(rio, d8["main"], grid["transform"], step, CHANNEL_MAX_PTS)
            channel["width_m"] = np.full(channel["lon"].size, 180.0)
            channel["z_bed"] = channel["elev"].copy()
            channel_src = "d8-mainstem"
            for cells in d8["traces"]:
                tr = polyline_from_cells(rio, cells, grid["transform"], step * 1.4, 160)
                if tr["lon"].size >= 5:
                    traces.append(tr)

    hydro_src = "none"
    hours = np.arange(0.0, 24.0, 1.0)
    q_t = np.full(hours.size, 400.0)
    h_ts = None
    sv = None
    if geom is not None:
        sv = load_saint_venant_series(h_path, int(geom["station_m"].size))
    if kind == "saint-venant-1d" and (geom is None or sv is None):
        raise FileNotFoundError(
            "Thieu mike_hd_output (mike_hd_result.csv / mike_hd_river_geometry.csv). "
            "Chay Model 1D Saint-venant-1D truoc."
        )
    if sv is not None and geom is not None:
        hours = sv["hours"]
        hydro_src = kind
        if sv["q"] is not None:
            q_t = np.nanmean(sv["q"], axis=1)
            q_along = interp_series_along(channel["station_m"], geom["station_m"], sv["q"])
        else:
            q_along = None
        h_ts = None
        try:
            from flood_model.flow_3d import attach_route_wse

            pack = attach_route_wse(
                {"lon": channel["lon"], "lat": channel["lat"]},
                {
                    "station_m": geom["station_m"],
                    "lon": geom["lon"],
                    "lat": geom["lat"],
                    "h": sv["h"],
                    "q": sv.get("q"),
                    "hours": hours,
                },
                horizontal=False,
            )
            if pack is not None:
                h_ts = pack[0]
        except Exception:
            h_ts = None
        if h_ts is None:
            try:
                from flood_model.flow_3d import wse_along_stations

                h_ts = wse_along_stations(
                    channel["station_m"].tolist(),
                    route={"station_m": geom["station_m"], "h": sv["h"], "q": sv.get("q")},
                )
            except Exception:
                h_ts = None
        if h_ts is None:
            h_ts = interp_series_along(channel["station_m"], geom["station_m"], sv["h"])
    else:
        q_along = None
        tank = load_tank_q(tank_path)
        if tank is not None:
            hours = tank["hours"]
            q_t = tank["q"]
            hydro_src = "tank"
        depth = np.clip(0.012 * np.sqrt(np.maximum(q_t, 1.0)), 0.35, 6.0)
        h_ts = channel["elev"][None, :] + depth[:, None]

    v_mps = np.clip(0.35 + 0.00035 * np.nan_to_num(q_t, nan=400.0), 0.4, 3.2)
    rain = _rainfall_for_hours(hours, p["rain"])

    xs_payload = None
    if geom is not None and sv is not None:
        xs_payload = {
            "station_m": _f(geom["station_m"], 1),
            "lon": _f(geom["lon"], 6),
            "lat": _f(geom["lat"], 6),
            "z_bed": _f(geom["z_bed"], 3),
            "width_m": _f(geom["width_m"], 1),
            "h_m": [_f(row, 3) for row in sv["h"]],
        }

    trib_series = load_tributary_series(trib_h_path)
    extra_sv = []
    try:
        extra_sv = list(extra_main_routes(kind))
    except Exception:
        extra_sv = []
    branches: list[dict[str, Any]] = []

    def _branch_item(tg: dict[str, Any], tch: dict[str, np.ndarray], th: np.ndarray) -> dict[str, Any]:
        rid = str(tg["id"])
        return {
            "id": rid,
            "kind": tg.get("kind") or "outlet",
            "bc": tg.get("bc") or "H",
            "length_m": round(float(tch["station_m"][-1]), 1) if tch["station_m"].size else 0.0,
            "channel": {
                "lon": _f(tch["lon"], 6),
                "lat": _f(tch["lat"], 6),
                "elev": _f(tch["elev"], 3),
                "z_bed": _f(tch.get("z_bed", tch["elev"]), 3),
                "station_m": _f(tch["station_m"], 1),
                "width_m": _f(tch["width_m"], 1),
            },
            "h_wse_m": [_f(row, 3) for row in th],
            "xs": {
                "station_m": _f(tg["station_m"], 1),
                "lon": _f(tg["lon"], 6),
                "lat": _f(tg["lat"], 6),
                "z_bed": _f(tg["z_bed"], 3),
            },
        }

    for tg, tch in trib_channels:
        rid = normalize_reach_id(tg["id"], "trib_1")
        tg = dict(tg)
        tg["id"] = rid
        ser = trib_series.get(rid)
        if ser is not None and hours.size >= 2:
            th = h_along_join_outer(tch["station_m"], tch["z_bed"], hours, ser)
        else:
            depth_b = np.clip(0.012 * np.sqrt(np.maximum(q_t, 1.0)), 0.25, 4.0)
            th = tch["elev"][None, :] + depth_b[:, None]
        branches.append(_branch_item(tg, tch, th))
    for tg, tch in extra_channels:
        rid = normalize_reach_id(tg["id"], "main_2")
        tg = dict(tg)
        tg["id"] = rid
        tg["kind"] = "main"
        rec = pick_route_by_id(extra_sv, rid, "main_2")
        th = None
        if rec is not None and hours.size >= 2:
            try:
                from flood_model.flow_3d import attach_route_wse

                pack = attach_route_wse(
                    {"lon": tch["lon"], "lat": tch["lat"]},
                    dict(rec, hours=hours),
                    horizontal=False,
                )
                if pack is not None:
                    th = pack[0]
            except Exception:
                th = None
            if th is None:
                th = interp_series_along(tch["station_m"], rec["station_m"], rec["h"])
        if th is None:
            depth_b = np.clip(0.012 * np.sqrt(np.maximum(q_t, 1.0)), 0.25, 4.0)
            th = tch["elev"][None, :] + depth_b[:, None]
        branches.append(_branch_item(tg, tch, th))

    reservoirs: list[dict[str, Any]] = []
    if channel is not None:
        try:
            with rasterio.open(dem) as rio:
                reservoirs = _reservoirs_for_flowrun(
                    rio,
                    channel,
                    hours=hours,
                    geom=geom,
                    h_grid=None if sv is None else sv.get("h"),
                    geom_station_m=None if geom is None else geom.get("station_m"),
                    reservoir_csv=_reservoir_csv_path(p, kind),
                )
        except Exception:
            traceback.print_exc()
            reservoirs = []

    payload = {
        "ok": True,
        "dem_path": str(dem),
        "channel_source": channel_src,
        "hydro_source": hydro_src,
        "water_source": kind,
        "water_source_label": hydro1d_label(kind),
        "length_m": round(float(channel["station_m"][-1]), 1),
        "hours": _f(hours, 2),
        "q_m3s": _f(q_t, 2),
        "velocity_mps": _f(v_mps, 3),
        "rainfall_mm": _f(np.asarray(rain.get("rainfall_mm", []), dtype=float), 4),
        "rain_peak_mm": round(float(rain.get("rain_peak_mm") or 0.0), 4),
        "rain_peak_minute_mm": round(float(rain.get("rain_peak_minute_mm") or 0.0), 6),
        "rain": {
            "cloud_height_m": float(rain.get("cloud_height_m") or 5000.0),
            "v_term_ms": float(rain.get("v_term_ms") or 8.0),
            "wind_east_ms": float(rain.get("wind_east_ms") or 1.6),
            "wind_north_ms": float(rain.get("wind_north_ms") or 0.4),
            "step_min": int(rain.get("step_min") or 15),
            "minute_weights": list(rain.get("minute_weights") or []),
            "minute_weight_max": float(rain.get("minute_weight_max") or 0.0),
            "pattern": rain.get("pattern") or "chicago-hanoi-hadong-15min",
            "idf_b_min": float(rain.get("idf_b_min") or 9.0),
            "idf_n": float(rain.get("idf_n") or 0.633),
            "chicago_r": float(rain.get("chicago_r") or 0.38),
        },
        "channel": {
            "lon": _f(channel["lon"], 6),
            "lat": _f(channel["lat"], 6),
            "elev": _f(channel["elev"], 3),
            "z_bed": _f(channel.get("z_bed", channel["elev"]), 3),
            "station_m": _f(channel["station_m"], 1),
            "width_m": _f(channel["width_m"], 1),
        },
        "xs": xs_payload,
        "h_wse_m": [_f(row, 3) for row in h_ts] if h_ts is not None else None,
        "traces": [
            {"lon": _f(tr["lon"], 6), "lat": _f(tr["lat"], 6)}
            for tr in traces
        ],
        "branches": branches,
        "n_traces": len(traces),
        "n_branches": len(branches),
        "n_extra_mains": sum(1 for b in branches if str(b.get("kind") or "") == "main"),
        "n_mains": 1 + sum(1 for b in branches if str(b.get("kind") or "") == "main"),
        "reservoirs": reservoirs,
        "n_reservoirs": len(reservoirs),
    }
    return payload


CACHE_VER = 25


def _num_or_none(raw: Any) -> float | None:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        v = float(raw)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _reservoir_csv_path(paths: dict[str, Path], kind: str) -> Path:
    """File muc ho xuat tu Model 1D (neu co)."""
    h_path = Path(paths.get("h") or "")
    parent = h_path.parent if h_path.name else Path(paths.get("root") or ROOT)
    if "mike" in str(h_path).lower() or kind == "saint-venant-1d":
        return parent / "mike_hd_reservoir.csv"
    return parent / "saint_venant_reservoir.csv"


def _h_series_at_station(
    h_grid: np.ndarray | None,
    station_m_geom: np.ndarray | None,
    station_m: float,
    n_hours: int,
) -> list[float] | None:
    """Noi suy H(t) tai tram cong trinh tu luoi H Model 1D."""
    if h_grid is None or station_m_geom is None or n_hours < 1:
        return None
    h = np.asarray(h_grid, dtype=float)
    xs = np.asarray(station_m_geom, dtype=float)
    if h.ndim != 2 or xs.size < 1 or h.shape[1] != xs.size:
        return None
    s = float(station_m)
    out: list[float] = []
    for t in range(int(h.shape[0])):
        row = h[t]
        good = np.isfinite(row) & np.isfinite(xs)
        if not np.any(good):
            out.append(float("nan"))
            continue
        out.append(float(np.interp(s, xs[good], row[good])))
    if len(out) < n_hours:
        last = out[-1] if out else float("nan")
        out.extend([last] * (n_hours - len(out)))
    return out[:n_hours]


def _reservoirs_for_flowrun(
    rio,
    channel: dict[str, np.ndarray],
    *,
    hours: np.ndarray | None = None,
    geom: dict[str, Any] | None = None,
    h_grid: np.ndarray | None = None,
    geom_station_m: np.ndarray | None = None,
    reservoir_csv: Path | None = None,
) -> list[dict[str, Any]]:
    """Khoi nuoc ho chua (flood-fill DEM) de ve cung Dòng chảy 3D.

    Muc nuoc dung chuoi dien toan Model 1D (mike_hd_reservoir.csv hoac H tai tram dap).
    """
    try:
        from flood_model.construction import (
            load_constructions,
            load_reservoir_levels_csv,
            normalize_structure_type,
        )
        from flood_model.flow_3d import (
            _en_tangent_at,
            _reservoir_water_level_m,
            _reservoir_water_surface_mesh,
        )
    except Exception:
        return []

    lon_c = np.asarray(channel.get("lon"), dtype=float)
    lat_c = np.asarray(channel.get("lat"), dtype=float)
    st_c = np.asarray(channel.get("station_m"), dtype=float)
    if lon_c.size < 2 or lat_c.size != lon_c.size:
        return []

    try:
        rows = load_constructions(seed=False)
    except Exception:
        return []

    hh = np.asarray(hours if hours is not None else [], dtype=float).ravel()
    n_t = int(hh.size) if hh.size else 1
    file_levels: dict[str, np.ndarray] = {}
    if reservoir_csv is not None:
        try:
            file_levels = load_reservoir_levels_csv(reservoir_csv)
        except Exception:
            file_levels = {}
    xs_st = None
    if geom_station_m is not None:
        xs_st = np.asarray(geom_station_m, dtype=float)
    elif geom is not None and geom.get("station_m") is not None:
        xs_st = np.asarray(geom["station_m"], dtype=float)

    out: list[dict[str, Any]] = []
    for rec in rows:
        if normalize_structure_type(rec.get("type")) != "reservoir":
            continue
        lon_s = _num_or_none(rec.get("lon"))
        lat_s = _num_or_none(rec.get("lat"))
        st_km = _num_or_none(rec.get("station_km"))
        if (lon_s is None or lat_s is None) and st_km is not None and st_c.size:
            s = float(st_km) * 1000.0
            lon_s = float(np.interp(s, st_c, lon_c))
            lat_s = float(np.interp(s, st_c, lat_c))
        if lon_s is None or lat_s is None:
            continue
        idx = int(np.argmin(np.abs(st_c - (float(st_km) * 1000.0 if st_km is not None else 0.0))))
        if st_km is None:
            d2 = (lon_c - float(lon_s)) ** 2 + (lat_c - float(lat_s)) ** 2
            idx = int(np.argmin(d2))
        flow_e, flow_n = _en_tangent_at(lon_c, lat_c, idx)
        init_lv = _num_or_none(rec.get("initial_level_m"))
        crest_lv = _num_or_none(rec.get("crest_m"))
        level0 = _reservoir_water_level_m(
            initial_level=init_lv,
            crest=crest_lv,
            invert=_num_or_none(rec.get("invert_m")),
        )
        if level0 is None:
            continue

        sid = str(rec.get("id") or "")
        h_series: list[float] | None = None
        reservoir_series_id = "st_4" if sid.upper() in ("RS_1", "ST_4") else sid
        if reservoir_series_id and reservoir_series_id in file_levels:
            arr = np.asarray(file_levels[reservoir_series_id], dtype=float).ravel()
            h_series = [float(v) for v in arr.tolist()]
            if len(h_series) < n_t:
                last = h_series[-1] if h_series else float(level0)
                h_series = h_series + [last] * (n_t - len(h_series))
            h_series = h_series[:n_t]
        if h_series is None:
            st_m = float(st_km) * 1000.0 if st_km is not None else float(st_c[idx])
            h_series = _h_series_at_station(h_grid, xs_st, st_m, n_t)
        if h_series is None:
            h_series = [float(level0)] * n_t

        finite = [v for v in h_series if v is not None and math.isfinite(float(v))]
        level_mesh = max(finite) if finite else float(level0)

        storage = _num_or_none(rec.get("storage_area_m2"))
        width_m = _num_or_none(rec.get("width_m"))
        try:
            mesh = _reservoir_water_surface_mesh(
                rio,
                lon=float(lon_s),
                lat=float(lat_s),
                level_m=float(level_mesh),
                flow_e=float(flow_e),
                flow_n=float(flow_n),
                path_lon=None,
                path_lat=None,
                storage_area_m2=storage,
                width_m=width_m,
                max_out=100,
                max_side=420,
                include_rings=True,
            )
        except Exception:
            mesh = None
        if not mesh:
            continue
        out.append({
            "id": sid,
            "name": str(rec.get("name") or ""),
            "lon": round(float(lon_s), 6),
            "lat": round(float(lat_s), 6),
            "storage_area_m2": round(float(storage), 1) if storage is not None else None,
            "initial_level_m": round(float(init_lv if init_lv is not None else level0), 3),
            "crest_m": round(float(crest_lv), 3) if crest_lv is not None else None,
            "level_m": round(float(h_series[0] if finite else level0), 3),
            "h_level_m": [round(float(v), 3) if v is not None and math.isfinite(float(v)) else None for v in h_series],
            "water_surface": mesh,
        })
    return out


@lru_cache(maxsize=8)
def _simulate_cached(
    ver: int,
    track_ver: str,
    dem: str,
    mtime: float,
    root: str,
    water_source: str,
    geom_m: float,
    h_m: float,
    tank_m: float,
    trib_g: float,
    trib_h: float,
    extra_h: float,
    rain_m: float,
    constructions_m: float,
    reservoir_m: float,
) -> str:
    data = simulate_flow(dem, root=root, water_source=water_source)
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def clear_flow_run_cache() -> None:
    _simulate_cached.cache_clear()


def simulate_flow_cached(
    dem_path: Path,
    root: Path | None = None,
    water_source: str = "saint-venant",
) -> dict[str, Any]:
    root = Path(root) if root else ROOT
    src = parse_hydro1d_source(water_source)
    p = _paths(root, src)
    dem = Path(dem_path)

    def _mt(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    from flood_model.river_network import DUONG_TRACK_VER

    raw = _simulate_cached(
        CACHE_VER,
        DUONG_TRACK_VER,
        str(dem.resolve()),
        _mt(dem),
        str(root.resolve()),
        src,
        _mt(p["geom"]),
        _mt(p["h"]),
        _mt(p["tank"]),
        _mt(p["trib_geom"]),
        _mt(p["trib_h"]),
        _mt(p["extra_h"]),
        _mt(p["rain"]),
        _mt(root / "constructions.csv"),
        _mt(_reservoir_csv_path(p, src)),
    )
    return json.loads(raw)


def resolve_dem_for_api(
    file_id: str | None,
    dem_path: str | None,
    root: Path,
    resolve_dem: Optional[Callable[..., Path]] = None,
) -> Path:
    if resolve_dem is not None:
        return Path(resolve_dem(file_id, dem_path))
    try:
        from flood_model.gis import resolve_dem_path

        return Path(resolve_dem_path(file_id, dem_path))
    except Exception:
        pass
    try:
        from flood_model.flow_3d import resolve_dem_path as resolve_fm_dem

        return Path(resolve_fm_dem(file_id, dem_path))
    except Exception:
        raw = (dem_path or "").strip()
        if raw:
            p = Path(raw)
            if not p.is_absolute():
                p = (root / p).resolve()
            if p.is_file():
                return p
        dem = root / "projects" / "data" / "dem-song-hong.tif"
        if dem.is_file():
            return dem
        raise FileNotFoundError(f"Khong tim thay DEM mac dinh: {dem}")


def create_flowrun_blueprint(
    name: str = "flowrun",
    url_prefix: str = "/api/flow-run",
    root: str | Path | None = None,
    resolve_dem: Optional[Callable[..., Path]] = None,
) -> Blueprint:
    pkg_root = Path(root) if root else ROOT
    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    def _fail(message: object, status: int = 500):
        return jsonify({"ok": False, "error": str(message)}), status

    @bp.route("/", methods=["GET"])
    @bp.route("", methods=["GET"])
    def api_ping():
        return jsonify({
            "ok": True,
            "service": "flow-run",
            "routes": ["/api/flow-run/simulate"],
        })

    @bp.errorhandler(Exception)
    def _bp_exc(err):
        traceback.print_exc()
        return _fail(f"Loi mo phong dong chay: {err}", 500)

    @bp.route("/simulate", methods=["POST", "GET"])
    def api_simulate():
        try:
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_for_api(
                data.get("file_id") or request.args.get("file_id"),
                data.get("dem_path") or request.args.get("dem_path"),
                pkg_root,
                resolve_dem,
            )
            src = parse_hydro1d_source(
                data.get("water_source") or request.args.get("water_source")
            )
            payload = simulate_flow_cached(dem, root=pkg_root, water_source=src)
            payload["file_id"] = data.get("file_id") or request.args.get("file_id")
            return jsonify(payload)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mo phong dong chay: {exc}", 500)

    return bp


def plot_network_png(payload: dict[str, Any], path: Path) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.2, 7.2))
    ch = payload["channel"]
    ax.plot(ch["lon"], ch["lat"], color="#1d4ed8", lw=2.4, label="Long chinh (ha luu)")
    ax.scatter(ch["lon"][0], ch["lat"][0], c="#22c55e", s=36, zorder=3, label="Thuong nguon")
    ax.scatter(ch["lon"][-1], ch["lat"][-1], c="#ef4444", s=36, zorder=3, label="Ha luu")
    for tr in payload.get("traces") or []:
        ax.plot(tr["lon"], tr["lat"], color="#38bdf8", lw=0.9, alpha=0.75)
    for i, br in enumerate(payload.get("branches") or []):
        chb = br.get("channel") or {}
        ax.plot(
            chb.get("lon") or [],
            chb.get("lat") or [],
            color="#f59e0b",
            lw=2.0,
            label=f"{br.get('id') or 'nhanh'} ({br.get('kind') or ''})",
        )
    ax.set_xlabel("Kinh do")
    ax.set_ylabel("Vi do")
    ax.set_title("Dong chay DEM: long chinh + song nhanh")
    ax.set_aspect("equal", adjustable="datalim")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Mo phong dong nuoc tren DEM (D8 + long chinh).")
    parser.add_argument("--dem", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "flow_run_output")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--water-source",
        default="saint-venant",
        choices=["saint-venant", "saint-venant-1d"],
        help="Model 1D: saint-venant hoac saint-venant-1d",
    )
    args = parser.parse_args(argv)
    payload = simulate_flow(args.dem, water_source=args.water_source)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "flow_run.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"Long chinh : {payload['length_m']:.0f} m  ({payload['channel_source']})")
    print(f"Q / H      : {payload['hydro_source']}")
    print(f"Song nhanh : {payload.get('n_branches', 0)}")
    print(f"Nganh D8   : {payload.get('n_traces', 0)}")
    print(f"JSON       : {json_path}")
    if not args.no_plot:
        png = out / "flow_run_network.png"
        plot_network_png(payload, png)
        print(f"Bieu do    : {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Long song chinh + nhanh — dung chung cho Theo dọc sông, Model 1D, ngap, dong chay 3D."""

from __future__ import annotations

import csv
import heapq
import math
import traceback
from pathlib import Path
from typing import Any, Sequence, cast

import numpy as np
import numpy.typing as npt
from rasterio.enums import Resampling
from rasterio.transform import Affine, xy

from flood_model.csv_io import csv_available, csv_open
from flood_model.gis import dem_xy_to_lonlat, lonlat_to_dem_xy, lonlat_to_mercator
from flood_model.routing import (
    hydro1d_csv_paths,
    is_main_reach_id,
    is_primary_main_reach,
    load_grouped_geom_csv,
    normalize_reach_id,
    parse_hydro1d_source,
)

THALWEG_MAX_DIM = 480
TRIB_Z_BED_MAX_M = 2.5
DUONG_LAT_MIN = 21.048
DUONG_LAT_MAX = 21.095
# Cho tach Hong–Đuống: cat ngang long Hong, khong bam xuoi ha luu.
DUONG_JOIN_LAT = 21.081
DUONG_JOIN_LON = 105.840
DUONG_JOIN_LAT_BAND = 0.006
DUONG_TRACK_VER = "v14"
# Mep tay cua cho nhap Hong–Đuống (khong bam them long Hong).
DUONG_OFFTAKE_LON_MIN = 105.836
# Bãi nhập Hồng–Đuống cao hơn đáy lòng; walker phải leo qua đây.
DUONG_BAI_Z_CAP = 8.5

_DUONG_TRACK_CACHE: dict[str, list[dict[str, Any]]] = {}


def clear_network_cache() -> None:
    _DUONG_TRACK_CACHE.clear()


def _hydro1d_files(src: Any = None) -> dict[str, Path]:
    raw = hydro1d_csv_paths(parse_hydro1d_source(src))
    return {key: Path(val) for key, val in raw.items() if key not in ("kind",)}


def _smooth_series(vals: np.ndarray, k: int = 7) -> np.ndarray:
    k = max(3, int(k) | 1)
    pad = k // 2
    kernel = np.ones(k) / k
    return np.convolve(np.pad(vals.astype(float), pad, mode="edge"), kernel, mode="valid")[: vals.size]


def _thalweg_smooth(vals: np.ndarray, k: int) -> np.ndarray:
    return _smooth_series(vals, k)


def _thalweg_cost_grid(z: np.ndarray, win: int = 15) -> np.ndarray:
    from scipy.ndimage import maximum_filter

    z_hi = np.where(np.isfinite(z), z, -1e9)
    bank = maximum_filter(z_hi, size=max(5, int(win) | 1))
    relief = np.clip(bank - z, 0.0, 18.0)
    cost = z - 0.9 * relief
    finite = np.isfinite(z)
    if not finite.any():
        return np.full(z.shape, np.inf, dtype=float)
    cost = cost - float(np.nanmin(cost[finite])) + 1.0
    cost[~finite] = np.inf
    return cost.astype(float)


def _thalweg_main_mask(z: np.ndarray, relief: np.ndarray) -> np.ndarray:
    from scipy.ndimage import binary_dilation, label

    finite = np.isfinite(z)
    pos = relief[finite & (relief >= 0.8)]
    if pos.size < 20:
        return finite
    thr = float(np.percentile(pos, 58))
    mask = finite & (relief >= max(1.0, thr))
    if int(mask.sum()) < 30:
        mask = finite & (relief >= max(0.7, float(np.percentile(pos, 45))))
    mask = binary_dilation(mask, iterations=1)
    labeled = cast(tuple[np.ndarray, int], label(mask))
    lab, n = labeled[0], labeled[1]
    if n < 1:
        return finite
    best_i = 1
    best_s = -1.0
    for i in range(1, n + 1):
        comp = lab == i
        score = float(comp.sum()) + 0.35 * float(np.nansum(relief[comp]))
        if score > best_s:
            best_s = score
            best_i = i
    return lab == best_i


def _thalweg_border_ends(cost: np.ndarray, mask: np.ndarray) -> tuple[tuple[int, int], tuple[int, int]]:
    h, w = cost.shape
    diag = math.hypot(h, w)
    cands: list[tuple[float, int, int]] = []
    for i, j in (
        *[(0, j) for j in range(w)],
        *[(h - 1, j) for j in range(w)],
        *[(i, 0) for i in range(h)],
        *[(i, w - 1) for i in range(h)],
    ):
        if not bool(mask[i, j]):
            continue
        c = float(cost[i, j])
        if not math.isfinite(c):
            continue
        cands.append((c, i, j))
    if len(cands) > 80:
        cands.sort()
        cheap = cands[:40]
        rest = cands[40:]
        extremes = []
        for key in (
            lambda t: t[1],
            lambda t: -t[1],
            lambda t: t[2],
            lambda t: -t[2],
        ):
            extremes.append(min(rest, key=key) if rest else cheap[0])
        seen = {(t[1], t[2]) for t in cheap}
        cands = list(cheap)
        for t in extremes:
            if (t[1], t[2]) not in seen:
                cands.append(t)
                seen.add((t[1], t[2]))
    if len(cands) < 2:
        finite = np.argwhere(mask & np.isfinite(cost))
        if finite.shape[0] < 2:
            raise ValueError("Khong noi duoc long song tu DEM.")
        a = finite[0]
        b = finite[-1]
        return (int(a[0]), int(a[1])), (int(b[0]), int(b[1]))
    best_pair = None
    for a in cands:
        for b in cands:
            dist = math.hypot(a[1] - b[1], a[2] - b[2])
            if dist < 0.32 * diag:
                continue
            score = dist / (1.0 + 0.25 * (a[0] + b[0]))
            if best_pair is None or score > best_pair[0]:
                best_pair = (score, (a[1], a[2]), (b[1], b[2]))
    if best_pair is None:
        far = max(cands[1:], key=lambda t: math.hypot(t[1] - cands[0][1], t[2] - cands[0][2]))
        return (cands[0][1], cands[0][2]), (int(far[1]), int(far[2]))
    return best_pair[1], best_pair[2]


def _thalweg_dijkstra(cost: np.ndarray, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    h, w = cost.shape
    inf = 1e30
    dist = np.full((h, w), inf, dtype=float)
    prev = np.full((h, w), -1, dtype=np.int32)
    sr, sc = start
    er, ec = end
    dist[sr, sc] = 0.0
    heap = [(0.0, sr, sc)]
    neigh = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    found = False
    while heap:
        d, r, c = heapq.heappop(heap)
        if d > dist[r, c] + 1e-9:
            continue
        if r == er and c == ec:
            found = True
            break
        for di, dj in neigh:
            nr, nc = r + di, c + dj
            if nr < 0 or nc < 0 or nr >= h or nc >= w:
                continue
            cv = float(cost[nr, nc])
            if not math.isfinite(cv):
                continue
            step = 1.41421356 if di and dj else 1.0
            nd = d + 0.5 * (float(cost[r, c]) + cv) * step
            if nd + 1e-9 < dist[nr, nc]:
                dist[nr, nc] = nd
                prev[nr, nc] = r * w + c
                heapq.heappush(heap, (nd, nr, nc))
    if not found or not math.isfinite(dist[er, ec]):
        raise ValueError("Khong noi duoc long song tu DEM.")
    path: list[tuple[int, int]] = []
    r, c = er, ec
    seen = set()
    while True:
        path.append((r, c))
        key = (r, c)
        if key in seen:
            break
        seen.add(key)
        if r == sr and c == sc:
            break
        p = int(prev[r, c])
        if p < 0:
            break
        r, c = divmod(p, w)
    path.reverse()
    if len(path) < 8:
        raise ValueError("Khong noi duoc long song tu DEM.")
    return path


def _thalweg_snap_bed(z: np.ndarray, path: Sequence[tuple[int, int]], rad: int = 2) -> list[tuple[int, int]]:
    h, w = z.shape
    out: list[tuple[int, int]] = []
    pr = pc = None
    for r, c in path:
        r0, r1 = max(0, r - rad), min(h, r + rad + 1)
        c0, c1 = max(0, c - rad), min(w, c + rad + 1)
        win = z[r0:r1, c0:c1]
        if not np.isfinite(win).any():
            nr, nc = r, c
        else:
            loc = np.unravel_index(np.nanargmin(win), win.shape)
            nr, nc = r0 + int(loc[0]), c0 + int(loc[1])
        if pr is not None and nr == pr and nc == pc:
            continue
        out.append((nr, nc))
        pr, pc = nr, nc
    return out


def thalweg_lonlat(src, max_dim: int = THALWEG_MAX_DIM) -> tuple[list[float], list[float]]:
    """Long song chinh: di theo day thung lung (least-cost)."""
    z, transform, cost, valley, out_h, out_w = _thalweg_grid(src, max_dim)
    return _thalweg_path_lonlat(src, z, transform, cost, valley, out_h, out_w)


def _thalweg_grid(src, max_dim: int = THALWEG_MAX_DIM):
    from scipy.ndimage import maximum_filter

    scale = max(src.width, src.height) / float(max(32, max_dim))
    out_w = max(8, int(round(src.width / max(scale, 1.0))))
    out_h = max(8, int(round(src.height / max(scale, 1.0))))
    data = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.average).astype(float)
    nodata = src.nodata
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    z = data.astype(float)
    z[invalid] = np.nan
    xres = float(src.res[0]) * (src.width / out_w)
    yres = abs(float(src.res[1])) * (src.height / out_h)
    transform = Affine(xres, 0, src.bounds.left, 0, -abs(yres), src.bounds.top)

    z_hi = np.where(np.isfinite(z), z, -1e9)
    bank = maximum_filter(z_hi, size=15)
    relief = np.clip(bank - z, 0.0, 18.0)
    relief[~np.isfinite(z)] = 0.0
    valley = _thalweg_main_mask(z, relief)
    cost = _thalweg_cost_grid(z, win=15)
    cost = np.where(valley, cost, np.inf)
    return z, transform, cost, valley, out_h, out_w


def _thalweg_path_lonlat(src, z, transform, cost, valley, out_h: int, out_w: int) -> tuple[list[float], list[float]]:
    start, end = _thalweg_border_ends(cost, valley)
    path = _thalweg_dijkstra(cost, start, end)
    path = _thalweg_snap_bed(z, path, rad=2)

    rows = np.array([p[0] for p in path], dtype=float)
    cols = np.array([p[1] for p in path], dtype=float)
    k = max(7, len(path) // 35) | 1
    rows = np.clip(np.round(_thalweg_smooth(rows, k)), 0, out_h - 1)
    cols = np.clip(np.round(_thalweg_smooth(cols, k)), 0, out_w - 1)

    xs, ys = [], []
    prev = None
    for r, c in zip(rows.astype(int), cols.astype(int)):
        if prev == (r, c):
            continue
        x, y = xy(transform, int(r), int(c), offset="center")
        xs.append(float(x))
        ys.append(float(y))
        prev = (r, c)
    if len(xs) < 2:
        raise ValueError("Khong noi duoc long song tu DEM.")
    lon, lat = dem_xy_to_lonlat(src, np.array(xs), np.array(ys))
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    if float(lat[0]) < float(lat[-1]) - 1e-5 or (
        abs(float(lat[0]) - float(lat[-1])) < 1e-4 and float(lon[0]) > float(lon[-1])
    ):
        lon, lat = lon[::-1], lat[::-1]
    return lon.tolist(), lat.tolist()


def extra_valley_centerlines(
    src,
    main_lon: Sequence[float],
    main_lat: Sequence[float],
    max_count: int,
    min_len_m: float = 5000.0,
    join_max_m: float = 3000.0,
    corridor_m: float = 200.0,
) -> list[dict[str, Any]]:
    """Them long chinh doc lap: thung lung khong noi vao long chinh thu nhat."""
    from scipy.ndimage import binary_dilation, label

    n_want = max(0, int(max_count))
    if n_want <= 0:
        return []
    z, transform, cost0, valley, out_h, out_w = _thalweg_grid(src)
    mlon = np.asarray(main_lon, dtype=float)
    mlat = np.asarray(main_lat, dtype=float)
    blocked = _rasterize_main_mask(z, transform, src, mlon, mlat, corridor_m)
    found: list[dict[str, Any]] = []
    for _ in range(n_want + 4):
        if len(found) >= n_want:
            break
        remain = valley & ~blocked & np.isfinite(cost0)
        if int(remain.sum()) < 48:
            break
        labeled = cast(tuple[np.ndarray, int], label(remain))
        lab, nlab = labeled[0], labeled[1]
        best_i, best_s = 0, 0
        for i in range(1, nlab + 1):
            s = int((lab == i).sum())
            if s > best_s:
                best_s, best_i = s, i
        if best_i < 1 or best_s < 48:
            break
        comp = lab == best_i
        cost_c = np.where(comp, cost0, np.inf)
        try:
            lon, lat = _thalweg_path_lonlat(src, z, transform, cost_c, comp, out_h, out_w)
        except Exception:
            blocked |= binary_dilation(comp, iterations=2)
            continue
        length = float(_chainage_lonlat(lon, lat)[-1]) if len(lon) >= 2 else 0.0
        corridor = _rasterize_main_mask(
            z, transform, src, np.asarray(lon, dtype=float), np.asarray(lat, dtype=float), corridor_m
        )
        if length < min_len_m:
            blocked |= corridor | binary_dilation(comp, iterations=1)
            continue
        step = max(1, len(lon) // 24)
        dmin = 1e12
        for lo, la in zip(lon[::step], lat[::step]):
            plon, plat = _project_on_main(float(lo), float(la), mlon, mlat)
            dmin = min(dmin, _gap_m(float(lo), float(la), plon, plat))
        if dmin < join_max_m:
            blocked |= corridor | binary_dilation(comp, iterations=1)
            continue
        found.append(
            {
                "id": f"main_{len(found) + 2}",
                "lon": lon,
                "lat": lat,
                "length_m": length,
            }
        )
        blocked |= corridor
    return found


class Centerline:
    def __init__(self, lon: npt.ArrayLike, lat: npt.ArrayLike, distance_m: npt.ArrayLike):
        self.lon = np.asarray(lon, dtype=float)
        self.lat = np.asarray(lat, dtype=float)
        self.distance_m = np.asarray(distance_m, dtype=float)


def _chainage_lonlat(lons: Sequence[float], lats: Sequence[float]) -> np.ndarray:
    xs, ys = [], []
    for lo, la in zip(lons, lats):
        x, y = lonlat_to_mercator(float(lo), float(la))
        xs.append(x)
        ys.append(y)
    d = np.zeros(len(xs), dtype=float)
    if len(xs) > 1:
        d[1:] = np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))
    return d


def _centerline_from_geom_csv(path: Path) -> Centerline | None:
    if not csv_available(path):
        return None
    stations: list[float] = []
    lons: list[float] = []
    lats: list[float] = []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            if not is_primary_main_reach(row.get("reach_id") or row.get("reach")):
                continue
            try:
                stations.append(float(row["station_km"]) * 1000.0)
                lons.append(float(row["lon"]))
                lats.append(float(row["lat"]))
            except (KeyError, TypeError, ValueError):
                continue
    if len(lons) < 2:
        return None
    return Centerline(lons, lats, stations)


def _centerline_from_dem(src) -> Centerline:
    lons, lats = thalweg_lonlat(src)
    return Centerline(lons, lats, _chainage_lonlat(lons, lats))


def _trib_centerlines_from_csv(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    path = _hydro1d_files(water_source)["trib_geom"]
    if not csv_available(path):
        return []
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            rid = normalize_reach_id(row.get("reach_id") or row.get("reach") or "trib", "trib_1")
            if is_main_reach_id(rid):
                continue
            if rid not in groups:
                groups[rid] = {"lon": [], "lat": [], "z_bed": []}
                order.append(rid)
            g = groups[rid]
            try:
                g["lon"].append(float(row["lon"]))
                g["lat"].append(float(row["lat"]))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                g["z_bed"].append(float(row.get("z_bed_m") or 0.0))
            except (TypeError, ValueError):
                g["z_bed"].append(float("nan"))
    out: list[dict[str, Any]] = []
    for rid in order:
        g = groups[rid]
        if len(g["lon"]) < 2:
            continue
        z = np.asarray(g["z_bed"], dtype=float)
        z = z[np.isfinite(z)]
        in_channel = bool(z.size >= 2 and float(np.max(z)) < TRIB_Z_BED_MAX_M)
        out.append(
            {
                "id": rid,
                "lon": g["lon"],
                "lat": g["lat"],
                "in_channel": in_channel,
                "kind": "outlet",
            }
        )
    return out


def _dem_downsample(src, max_dim: int = 400):
    scale = max(src.width, src.height) / float(max(32, max_dim))
    out_w = max(8, int(round(src.width / max(scale, 1.0))))
    out_h = max(8, int(round(src.height / max(scale, 1.0))))
    data = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.average).astype(float)
    nodata = src.nodata
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    z = data.astype(float)
    z[invalid] = np.nan
    xres = float(src.res[0]) * (src.width / out_w)
    yres = abs(float(src.res[1])) * (src.height / out_h)
    transform = Affine(xres, 0, src.bounds.left, 0, -yres, src.bounds.top)
    return z, transform, xres, out_w, out_h


def _lonlat_to_rowcol(lon: float, lat: float, src, transform) -> tuple[int, int]:
    from rasterio.warp import transform as rio_tf

    transformed = cast(
        tuple[list[float], list[float]],
        rio_tf("EPSG:4326", src.crs, [lon], [lat]),
    )
    xs, ys = transformed[0], transformed[1]
    col = (xs[0] - transform.c) / transform.a
    row = (ys[0] - transform.f) / transform.e
    return int(round(row)), int(round(col))


def _read_local_grid(
    src,
    lon_c: float,
    lat_c: float,
    west_m: float,
    east_m: float,
    south_m: float,
    north_m: float,
):
    from rasterio.windows import from_bounds
    from rasterio.warp import transform as rio_tf

    transformed = cast(
        tuple[list[float], list[float]],
        rio_tf("EPSG:4326", src.crs, [lon_c], [lat_c]),
    )
    xs, ys = transformed[0], transformed[1]
    cx, cy = float(xs[0]), float(ys[0])
    win = from_bounds(cx - west_m, cy - south_m, cx + east_m, cy + north_m, transform=src.transform)
    win = win.round_offsets().round_lengths()
    if win.width < 12 or win.height < 12:
        raise ValueError("Cua so DEM song nhanh qua nho")
    data = src.read(1, window=win).astype(float)
    nodata = src.nodata
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    data[invalid] = np.nan
    return data, src.window_transform(win)


def _nearest_main_lonlat(
    lon0: float, lat0: float, glon: np.ndarray, glat: np.ndarray
) -> tuple[float, float]:
    return _project_on_main(lon0, lat0, glon, glat)


def _project_on_main(
    lon0: float, lat0: float, glon: np.ndarray, glat: np.ndarray
) -> tuple[float, float]:
    """Chan vuong goc xuong polyline long chinh (khong truot theo dinh)."""
    glon = np.asarray(glon, dtype=float)
    glat = np.asarray(glat, dtype=float)
    lon0 = float(lon0)
    lat0 = float(lat0)
    if glon.size < 1:
        return lon0, lat0
    if glon.size == 1:
        return float(glon[0]), float(glat[0])
    best_d = 1e18
    best = (float(glon[0]), float(glat[0]))
    for i in range(int(glon.size) - 1):
        ax, ay = float(glon[i]), float(glat[i])
        vx, vy = float(glon[i + 1]) - ax, float(glat[i + 1]) - ay
        den = vx * vx + vy * vy
        if den < 1e-18:
            t = 0.0
        else:
            t = ((lon0 - ax) * vx + (lat0 - ay) * vy) / den
            t = min(1.0, max(0.0, t))
        px, py = ax + t * vx, ay + t * vy
        d = (px - lon0) ** 2 + (py - lat0) ** 2
        if d < best_d:
            best_d = d
            best = (px, py)
    return best


def floodplain_banks(
    src,
    lons: npt.ArrayLike,
    lats: npt.ArrayLike,
    *,
    z_cap: float = 7.6,
    max_m: float = 240.0,
    step_m: float = 18.0,
) -> dict[str, list[float]]:
    """Hai mep vung bai / long (z <= z_cap) vuong goc tam duong."""
    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    n = int(lons.size)
    empty = {"left_lon": [], "left_lat": [], "left_z": [], "right_lon": [], "right_lat": [], "right_z": []}
    if n < 2:
        return empty
    xs, ys = lonlat_to_dem_xy(src, lons, lats)
    nodata = src.nodata

    def z_at(x: float, y: float) -> float:
        rec = next(src.sample([(float(x), float(y))]))
        z = float(rec[0])
        if nodata is not None and z == float(nodata):
            return float("nan")
        return z

    def edge(i: int, nx: float, ny: float, sign: float) -> tuple[float, float, float]:
        x0, y0 = float(xs[i]), float(ys[i])
        last = (x0, y0, z_at(x0, y0))
        d = float(step_m)
        while d <= max_m:
            px, py = x0 + sign * nx * d, y0 + sign * ny * d
            z = z_at(px, py)
            if not math.isfinite(z) or z > z_cap:
                break
            last = (px, py, z)
            d += step_m
        return last

    llon, llat, lz, rlon, rlat, rz = [], [], [], [], [], []
    for i in range(n):
        if i == 0:
            dx, dy = float(xs[1] - xs[0]), float(ys[1] - ys[0])
        elif i == n - 1:
            dx, dy = float(xs[-1] - xs[-2]), float(ys[-1] - ys[-2])
        else:
            dx, dy = float(xs[i + 1] - xs[i - 1]), float(ys[i + 1] - ys[i - 1])
        hyp = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / hyp, dx / hyp
        lx, ly, lv = edge(i, nx, ny, -1.0)
        rx, ry, rv = edge(i, nx, ny, 1.0)
        lo_l, la_l = dem_xy_to_lonlat(src, np.array([lx]), np.array([ly]))
        lo_r, la_r = dem_xy_to_lonlat(src, np.array([rx]), np.array([ry]))
        llon.append(float(lo_l[0]))
        llat.append(float(la_l[0]))
        lz.append(float(lv) if math.isfinite(lv) else float("nan"))
        rlon.append(float(lo_r[0]))
        rlat.append(float(la_r[0]))
        rz.append(float(rv) if math.isfinite(rv) else float("nan"))
    return {
        "left_lon": llon,
        "left_lat": llat,
        "left_z": lz,
        "right_lon": rlon,
        "right_lat": rlat,
        "right_z": rz,
    }


def _gap_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    x0, y0 = lonlat_to_mercator(float(lon_a), float(lat_a))
    x1, y1 = lonlat_to_mercator(float(lon_b), float(lat_b))
    return float(math.hypot(x1 - x0, y1 - y0))


def _attach_lonlat_to_main(
    lon: np.ndarray,
    lat: np.ndarray,
    glon: np.ndarray,
    glat: np.ndarray,
    *,
    at_start: bool,
    target_lon: float | None = None,
    target_lat: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Gan dau nhanh dung len long chinh, cat ngang (khong ke day theo Hong)."""
    lon = np.asarray(lon, dtype=float).copy()
    lat = np.asarray(lat, dtype=float).copy()
    i = 0 if at_start else -1
    src_lon = float(target_lon) if target_lon is not None else float(lon[i])
    src_lat = float(target_lat) if target_lat is not None else float(lat[i])
    jlon, jlat = _project_on_main(src_lon, src_lat, glon, glat)
    if _gap_m(float(lon[i]), float(lat[i]), jlon, jlat) < 12.0:
        lon[i], lat[i] = jlon, jlat
        return lon, lat
    if at_start:
        return np.concatenate([[jlon], lon]), np.concatenate([[jlat], lat])
    return np.concatenate([lon, [jlon]]), np.concatenate([lat, [jlat]])


def _keep_perp_offtake(
    lon: np.ndarray, lat: np.ndarray, glon: np.ndarray, glat: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Bo doan bam Hong xuoi ha luu; giu mot doan cat ngang vao long."""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    if lon.size < 3:
        return lon, lat
    leave = 1
    for i in range(1, min(int(lon.size), 80)):
        plon, plat = _project_on_main(float(lon[i]), float(lat[i]), glon, glat)
        if _gap_m(float(lon[i]), float(lat[i]), plon, plat) > 90.0:
            leave = i
            break
    jlon, jlat = _project_on_main(float(lon[leave]), float(lat[leave]), glon, glat)
    return np.concatenate([[jlon], lon[leave:]]), np.concatenate([[jlat], lat[leave:]])


def _cell_lonlat(r: int, c: int, src, transform) -> tuple[float, float]:
    x, y = xy(transform, int(r), int(c), offset="center")
    lon, lat = dem_xy_to_lonlat(src, np.array([float(x)]), np.array([float(y)]))
    return float(lon[0]), float(lat[0])


def _rasterize_main_mask(z: np.ndarray, transform, src, glon: np.ndarray, glat: np.ndarray, radius_m: float) -> np.ndarray:
    h, w = z.shape
    mask = np.zeros((h, w), dtype=bool)
    rad = max(1, int(round(radius_m / max(abs(float(transform.a)), 1.0))))
    for lo, la in zip(glon, glat):
        rr, cc = _lonlat_to_rowcol(float(lo), float(la), src, transform)
        if rr < 0 or cc < 0 or rr >= h or cc >= w:
            continue
        r0, r1 = max(0, rr - rad), min(h, rr + rad + 1)
        c0, c1 = max(0, cc - rad), min(w, cc + rad + 1)
        mask[r0:r1, c0:c1] = True
    return mask


def _dijkstra_to_mask(
    z: np.ndarray,
    start: tuple[int, int],
    stop_mask: np.ndarray,
    *,
    z_cap: float,
    max_nodes: int = 90000,
) -> list[tuple[int, int]]:
    """Noi qua bai / long can toi o nam tren stop_mask (long Hong)."""
    h, w = z.shape
    sr, sc = int(start[0]), int(start[1])
    if sr < 0 or sc < 0 or sr >= h or sc >= w:
        return []
    if bool(stop_mask[sr, sc]):
        return [(sr, sc)]
    inf = 1e30
    dist = np.full((h, w), inf, dtype=float)
    prev = np.full((h, w), -1, dtype=np.int32)
    dist[sr, sc] = 0.0
    heap = [(0.0, sr, sc)]
    neigh = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    end: tuple[int, int] | None = None
    visited = 0
    while heap and visited < max_nodes:
        d, r, c = heapq.heappop(heap)
        if d > dist[r, c] + 1e-9:
            continue
        visited += 1
        if bool(stop_mask[r, c]):
            end = (r, c)
            break
        for di, dj in neigh:
            nr, nc = r + di, c + dj
            if nr < 1 or nc < 1 or nr >= h - 1 or nc >= w - 1:
                continue
            v = float(z[nr, nc])
            if not np.isfinite(v) or v > z_cap:
                continue
            step = 1.41421356 if di and dj else 1.0
            nd = d + step * (0.28 + 0.72 * max(0.0, v + 12.0) / 22.0)
            if nd + 1e-9 < dist[nr, nc]:
                dist[nr, nc] = nd
                prev[nr, nc] = r * w + c
                heapq.heappush(heap, (nd, nr, nc))
    if end is None:
        return []
    path: list[tuple[int, int]] = []
    r, c = end
    seen: set[tuple[int, int]] = set()
    while True:
        path.append((r, c))
        if (r, c) in seen:
            break
        seen.add((r, c))
        if r == sr and c == sc:
            break
        p = int(prev[r, c])
        if p < 0:
            break
        r, c = divmod(p, w)
    path.reverse()
    return path if len(path) >= 2 else path


def _dijkstra_on_cost(
    cost: np.ndarray,
    start: tuple[int, int],
    end: tuple[int, int],
) -> list[tuple[int, int]]:
    h, w = cost.shape
    sr, sc = int(start[0]), int(start[1])
    er, ec = int(end[0]), int(end[1])
    if not np.isfinite(cost[sr, sc]) or not np.isfinite(cost[er, ec]):
        return []
    inf = 1e30
    dist = np.full((h, w), inf, dtype=float)
    prev = np.full((h, w), -1, dtype=np.int32)
    dist[sr, sc] = 0.0
    heap = [(0.0, sr, sc)]
    neigh = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    found = False
    while heap:
        d, r, c = heapq.heappop(heap)
        if d > dist[r, c] + 1e-9:
            continue
        if r == er and c == ec:
            found = True
            break
        for di, dj in neigh:
            nr, nc = r + di, c + dj
            if nr < 0 or nc < 0 or nr >= h or nc >= w:
                continue
            cv = float(cost[nr, nc])
            if not math.isfinite(cv):
                continue
            step = 1.41421356 if di and dj else 1.0
            nd = d + 0.5 * (float(cost[r, c]) + cv) * step
            if nd + 1e-9 < dist[nr, nc]:
                dist[nr, nc] = nd
                prev[nr, nc] = r * w + c
                heapq.heappush(heap, (nd, nr, nc))
    if not found:
        return []
    path: list[tuple[int, int]] = []
    r, c = er, ec
    seen: set[tuple[int, int]] = set()
    while True:
        path.append((r, c))
        if (r, c) in seen or (r == sr and c == sc):
            break
        seen.add((r, c))
        p = int(prev[r, c])
        if p < 0:
            break
        r, c = divmod(p, w)
    path.reverse()
    return path


def _bai_corridor_path(z: np.ndarray, transform, src) -> list[tuple[int, int]]:
    """Truc giua vung bai (dat thap) tu mep tay Hong sang long Đuống."""
    from scipy.ndimage import distance_transform_edt

    h, w = z.shape
    corners = [
        _lonlat_to_rowcol(105.818, 21.062, src, transform),
        _lonlat_to_rowcol(105.818, 21.094, src, transform),
        _lonlat_to_rowcol(105.858, 21.062, src, transform),
        _lonlat_to_rowcol(105.858, 21.094, src, transform),
    ]
    r0 = min(max(p[0], 1) for p in corners)
    r1 = max(min(p[0], h - 2) for p in corners)
    c0 = min(max(p[1], 1) for p in corners)
    c1 = max(min(p[1], w - 2) for p in corners)
    if r1 <= r0 or c1 <= c0:
        return []
    box = np.zeros((h, w), dtype=bool)
    box[r0 : r1 + 1, c0 : c1 + 1] = True
    mask = box & np.isfinite(z) & (z <= 7.6)
    if int(mask.sum()) < 40:
        return []
    inside = np.asarray(distance_transform_edt(mask), dtype=float)
    cost = np.full((h, w), np.inf, dtype=float)
    cost[mask] = 1.15 + 2.4 / (0.35 + inside[mask])
    west_pts = []
    east_pts = []
    ys, xs = np.where(mask)
    for r, c in zip(ys[::2], xs[::2]):
        lo, la = _cell_lonlat(int(r), int(c), src, transform)
        v = float(z[r, c])
        if 21.082 <= la <= 21.093 and 105.818 <= lo <= 105.832:
            west_pts.append((lo, v, int(r), int(c)))
        if 21.066 <= la <= 21.076 and 105.846 <= lo <= 105.858:
            east_pts.append((v, lo, int(r), int(c)))
    if not west_pts or not east_pts:
        return []
    west_pts.sort()
    east_pts.sort()
    start = (west_pts[0][2], west_pts[0][3])
    end = (east_pts[0][2], east_pts[0][3])
    return _dijkstra_on_cost(cost, start, end)


def _walk_bed(
    z: np.ndarray,
    r0: int,
    c0: int,
    *,
    dc_bias: float,
    dr_bias: float,
    z_cap: float,
    max_steps: int,
    forbidden: np.ndarray | None = None,
    stop_mask: np.ndarray | None = None,
    climb_m: float = 3.0,
    axis_sign: int = 0,
) -> list[tuple[int, int]]:
    h, w = z.shape
    neigh = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
    path = [(int(r0), int(c0))]
    seen = {(int(r0), int(c0))}
    r, c = int(r0), int(c0)
    z_run = float(z[r, c])
    if not np.isfinite(z_run):
        return path
    for _ in range(max(8, int(max_steps))):
        if stop_mask is not None and bool(stop_mask[r, c]) and len(path) > 3:
            break
        best = None
        for di, dj in neigh:
            if axis_sign > 0 and dj < 0:
                continue
            if axis_sign < 0 and dj > 0:
                continue
            nr, nc = r + di, c + dj
            if nr < 2 or nc < 2 or nr >= h - 2 or nc >= w - 2:
                continue
            if (nr, nc) in seen:
                continue
            if forbidden is not None and bool(forbidden[nr, nc]) and stop_mask is None:
                continue
            v = float(z[nr, nc])
            if not np.isfinite(v) or v > z_cap or v > z_run + climb_m:
                continue
            score = -v + 0.85 * dc_bias * dj + 0.85 * dr_bias * di
            score -= 0.18 * (abs(di) + abs(dj) - 1)
            if stop_mask is not None and bool(stop_mask[nr, nc]):
                score += 6.0
            if best is None or score > best[0]:
                best = (score, nr, nc, v)
        if best is None:
            break
        r, c, v = int(best[1]), int(best[2]), float(best[3])
        path.append((r, c))
        seen.add((r, c))
        z_run = 0.82 * z_run + 0.18 * v
    return path


def _main_centerline_dense(main_geom, step_m: float = 200.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    s = np.asarray(main_geom.distance_m, dtype=float)
    lon = np.asarray(main_geom.lon, dtype=float)
    lat = np.asarray(main_geom.lat, dtype=float)
    if s.size < 2:
        return s, lon, lat
    step = max(50.0, float(step_m))
    s_out = np.arange(0.0, float(s[-1]) + 0.5 * step, step)
    if s_out.size < 2 or s_out[-1] < float(s[-1]) - 1.0:
        s_out = np.append(s_out, float(s[-1]))
    return s_out, np.interp(s_out, s, lon), np.interp(s_out, s, lat)


def _trim_along_main(cells: list[tuple[int, int]], main_mask: np.ndarray) -> list[tuple[int, int]]:
    """Bo doan bam theo Hong; giu 1 o nut giao + toan bo bai / long Đuống."""
    if len(cells) < 8:
        return cells
    on = [bool(main_mask[r, c]) for r, c in cells]
    if on[0]:
        i = 0
        while i + 8 < len(cells) and on[i]:
            i += 1
        if i > 0:
            cells = cells[max(0, i - 1) :]
    elif on[-1]:
        i = len(cells) - 1
        while i > 8 and on[i]:
            i -= 1
        cells = cells[: min(len(cells), i + 2)]
    return cells


def east_side_branch(src, main_geom) -> list[dict[str, Any]]:
    """Nhanh Song Duong: bam day ve dong, di het bai roi moi noi vao Hong."""
    _s_m, glon, glat = _main_centerline_dense(main_geom, 80.0)
    try:
        z, transform = _read_local_grid(
            src,
            DUONG_JOIN_LON,
            DUONG_JOIN_LAT,
            west_m=5200.0,
            east_m=14000.0,
            south_m=6200.0,
            north_m=6800.0,
        )
    except Exception:
        z, transform, _xres, _ow, _oh = _dem_downsample(src, max_dim=700)
    h, w = z.shape
    main_mask = _rasterize_main_mask(z, transform, src, glon, glat, radius_m=55.0)
    band = (glat >= DUONG_JOIN_LAT - DUONG_JOIN_LAT_BAND) & (
        glat <= DUONG_JOIN_LAT + DUONG_JOIN_LAT_BAND
    )
    offtake_mask = (
        _rasterize_main_mask(z, transform, src, glon[band], glat[band], radius_m=55.0)
        if bool(np.any(band))
        else main_mask
    )
    z_cap = 5.0
    corners = [
        _lonlat_to_rowcol(DUONG_JOIN_LON - 0.008, DUONG_LAT_MIN, src, transform),
        _lonlat_to_rowcol(DUONG_JOIN_LON - 0.008, DUONG_LAT_MAX, src, transform),
        _lonlat_to_rowcol(105.92, DUONG_LAT_MIN, src, transform),
        _lonlat_to_rowcol(105.92, DUONG_LAT_MAX, src, transform),
    ]
    r0b = min(max(p[0], 3) for p in corners)
    r1b = max(min(p[0], h - 4) for p in corners)
    c0b = min(max(p[1], 3) for p in corners)
    c1b = max(min(p[1], w - 4) for p in corners)
    if r1b < r0b:
        r0b, r1b = r1b, r0b
    if c1b < c0b:
        c0b, c1b = c1b, c0b
    rj, cj = _lonlat_to_rowcol(DUONG_JOIN_LON, DUONG_JOIN_LAT, src, transform)
    seeds: list[tuple[float, int, int, float]] = []
    for r in range(r0b, r1b + 1, 2):
        for c in range(c0b, c1b + 1, 2):
            if bool(main_mask[r, c]):
                continue
            v = float(z[r, c])
            if not np.isfinite(v) or v > z_cap or v < -14.0:
                continue
            patch = z[r - 2 : r + 3, c - 2 : c + 3]
            if v > float(np.nanmin(patch)) + 1.8:
                continue
            sc = -abs(v + 9.0) - 0.04 * abs(r - rj) - 0.010 * abs(c - (cj + 40))
            seeds.append((sc, r, c, v))
    seeds.sort(reverse=True)
    picked = None
    for _sc, sr, sc0, z_seed in seeds[:90]:
        path_w = _walk_bed(
            z,
            sr,
            sc0,
            dc_bias=-1.35,
            dr_bias=0.04,
            z_cap=DUONG_BAI_Z_CAP,
            max_steps=900,
            stop_mask=offtake_mask,
            climb_m=8.0,
            axis_sign=0,
        )
        if not path_w or not bool(offtake_mask[path_w[-1][0], path_w[-1][1]]):
            link = _dijkstra_to_mask(
                z,
                path_w[-1] if path_w else (sr, sc0),
                offtake_mask,
                z_cap=DUONG_BAI_Z_CAP,
            )
            if link:
                path_w = (path_w or [(sr, sc0)]) + link[1:]
        path_e = _walk_bed(
            z,
            sr,
            sc0,
            dc_bias=1.55,
            dr_bias=-0.22,
            z_cap=2.4,
            max_steps=1800,
            forbidden=main_mask,
            climb_m=5.5,
            axis_sign=1,
        )
        if path_e:
            more = _walk_bed(
                z,
                path_e[-1][0],
                path_e[-1][1],
                dc_bias=1.5,
                dr_bias=-0.18,
                z_cap=z_cap,
                max_steps=900,
                forbidden=main_mask,
                climb_m=7.0,
                axis_sign=1,
            )
            path_e = path_e + more[1:]
        cells = path_w[::-1][:-1] + path_e
        if len(cells) < 16:
            continue
        cells = _trim_along_main(cells, main_mask)
        if len(cells) < 16:
            continue
        lon0, lat0 = _cell_lonlat(cells[0][0], cells[0][1], src, transform)
        lon1, lat1 = _cell_lonlat(cells[-1][0], cells[-1][1], src, transform)
        span = abs(lon1 - lon0)
        hit_main = any(bool(main_mask[r, c]) for r, c in cells[:16]) or any(
            bool(main_mask[r, c]) for r, c in cells[-16:]
        )
        lat_hi, lat_lo = max(lat0, lat1), min(lat0, lat1)
        lon_east = max(lon0, lon1)
        if span < 0.010 or lat_lo < 21.050 or lon_east < 105.858:
            continue
        d0 = _gap_m(lon0, lat0, *_nearest_main_lonlat(lon0, lat0, glon, glat))
        d1 = _gap_m(lon1, lat1, *_nearest_main_lonlat(lon1, lat1, glon, glat))
        d_main = min(d0, d1)
        lat_hong = lat0 if d0 <= d1 else lat1
        score = (
            span * 900.0
            + 2200.0 * (lon_east - 105.84)
            + 0.015 * len(cells)
            + (150.0 if hit_main else 0.0)
            - 0.06 * d_main
            - 420.0 * max(0.0, abs(lat_hong - DUONG_JOIN_LAT) - 0.003)
            - 220.0 * max(0.0, 21.058 - lat_lo)
        )
        if picked is None or score > picked[0]:
            picked = (score, cells, z_seed)
    if picked is None:
        return []
    _score, cells, z_seed = picked

    while len(cells) > 16:
        r, c = cells[-1]
        vv = float(z[r, c]) if np.isfinite(z[r, c]) else 99.0
        if vv < 8.0:
            break
        cells.pop()
    if len(cells) < 8:
        return []

    lon0, lat0 = _cell_lonlat(cells[0][0], cells[0][1], src, transform)
    lon1, lat1 = _cell_lonlat(cells[-1][0], cells[-1][1], src, transform)
    d0 = _gap_m(lon0, lat0, *_nearest_main_lonlat(lon0, lat0, glon, glat))
    d1 = _gap_m(lon1, lat1, *_nearest_main_lonlat(lon1, lat1, glon, glat))
    if d1 + 40.0 < d0:
        cells = cells[::-1]
    cells = _trim_along_main(cells, main_mask)
    while len(cells) > 16:
        lo, _la = _cell_lonlat(cells[0][0], cells[0][1], src, transform)
        if lo < DUONG_OFFTAKE_LON_MIN:
            cells.pop(0)
            continue
        break
    keep = len(cells)
    for i in range(1, len(cells)):
        _lo, la = _cell_lonlat(cells[i][0], cells[i][1], src, transform)
        _lp, lap = _cell_lonlat(cells[i - 1][0], cells[i - 1][1], src, transform)
        if la < 21.050 and la < lap - 1e-5:
            keep = i
            break
    cells = cells[:keep]
    if len(cells) < 8:
        return []

    rows = _smooth_series(np.array([p[0] for p in cells], dtype=float), k=5)
    cols = _smooth_series(np.array([p[1] for p in cells], dtype=float), k=5)
    rows[0], cols[0] = float(cells[0][0]), float(cells[0][1])
    xs, ys, zs = [], [], []
    for r, c in zip(rows, cols):
        rr = int(round(float(np.clip(r, 0, h - 1))))
        cc = int(round(float(np.clip(c, 0, w - 1))))
        x, y = xy(transform, rr, cc, offset="center")
        xs.append(float(x))
        ys.append(float(y))
        zs.append(float(z[rr, cc]) if np.isfinite(z[rr, cc]) else z_seed)
    lon, lat = dem_xy_to_lonlat(src, np.asarray(xs), np.asarray(ys))
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    zs = np.asarray(zs, dtype=float)
    if lon.size < 8:
        return []
    lon, lat = _attach_lonlat_to_main(
        lon,
        lat,
        glon,
        glat,
        at_start=True,
        target_lon=float(lon[0]),
        target_lat=float(lat[0]),
    )
    lon, lat = _keep_perp_offtake(lon, lat, glon, glat)
    if zs.size == lon.size - 1:
        zs = np.concatenate([[zs[0]], zs])
    elif zs.size != lon.size:
        zs = np.resize(zs, lon.size)
    return [
        {
            "lon": lon,
            "lat": lat,
            "z": zs,
            "depth": np.full(max(int(lon.size), 1), max(3.0, 2.0 - z_seed)),
            "kind": "outlet",
            "n": int(lon.size),
        }
    ]


def live_duong_centerlines(
    src, water_source: str = "saint-venant", main_geom=None
) -> list[dict[str, Any]]:
    kind = parse_hydro1d_source(water_source)
    key = f"{getattr(src, 'name', '') or ''}|{kind}|{DUONG_TRACK_VER}"
    if main_geom is None and key and key in _DUONG_TRACK_CACHE:
        return _DUONG_TRACK_CACHE[key]
    main = main_geom
    if main is None:
        main = _centerline_from_geom_csv(_hydro1d_files(kind)["geom"])
    if main is None:
        try:
            main = _centerline_from_dem(src)
        except Exception:
            traceback.print_exc()
            return []
    try:
        tracks = east_side_branch(src, main)
    except Exception:
        traceback.print_exc()
        return []
    out: list[dict[str, Any]] = []
    for i, tr in enumerate(tracks or []):
        lon = np.asarray(tr.get("lon"), dtype=float)
        lat = np.asarray(tr.get("lat"), dtype=float)
        if lon.size < 2 or lat.size < 2:
            continue
        item = {
            "id": f"trib_{i + 1}",
            "lon": lon.tolist(),
            "lat": lat.tolist(),
            "in_channel": True,
        }
        if tr.get("z") is not None:
            item["z"] = tr["z"]
        if tr.get("depth") is not None:
            item["depth"] = tr["depth"]
        if tr.get("kind"):
            item["kind"] = tr["kind"]
        out.append(item)
    if main_geom is None and key:
        _DUONG_TRACK_CACHE[key] = out
    return out


def extra_main_centerlines(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    """Song chinh doc lap tu geometry 1D (main_2...)."""
    path = _hydro1d_files(water_source)["geom"]
    out: list[dict[str, Any]] = []
    for g in load_grouped_geom_csv(Path(path), which="extra_main"):
        out.append(
            {
                "id": str(g["id"]),
                "lon": np.asarray(g["lon"], dtype=float).tolist(),
                "lat": np.asarray(g["lat"], dtype=float).tolist(),
                "kind": "main",
                "in_channel": True,
            }
        )
    return out


def tributary_centerlines(
    src, water_source: str = "saint-venant", main_geom=None
) -> list[dict[str, Any]]:
    """Walker Đuống + cac song nhanh con lai tu CSV (trib_2...)."""
    live = live_duong_centerlines(src, water_source, main_geom=main_geom)
    csv_tracks = _trib_centerlines_from_csv(water_source)
    if not live:
        return csv_tracks
    used = {normalize_reach_id(tr.get("id"), "trib_1") for tr in live}
    out = list(live)
    for tr in csv_tracks:
        rid = normalize_reach_id(tr.get("id"), "trib_1")
        if rid in used:
            continue
        item = dict(tr)
        item["id"] = rid
        out.append(item)
        used.add(rid)
    return out


def main_centerline_lonlat(src, water_source: str = "saint-venant") -> tuple[list[float], list[float], str]:
    """Long chinh tu DEM (bam day thung lung)."""
    _ = water_source
    lons, lats = thalweg_lonlat(src)
    return lons, lats, "dem"


def dem_river_network(src, water_source: str = "saint-venant") -> dict[str, Any]:
    """Long chinh + song chinh doc lap + song nhanh."""
    lons, lats, src_name = main_centerline_lonlat(src, water_source)
    return {
        "main_lon": list(lons),
        "main_lat": list(lats),
        "main_source": src_name,
        "branches": tributary_centerlines(src, water_source),
        "extra_mains": extra_main_centerlines(water_source),
    }

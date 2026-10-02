"""Lay mat cat ngang / doc tu DEM va CSV 1D.

Dung chung cho Muskingum-Cunge, Saint-Venant, MIKE HD, Theo doc song.
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.gis import dem_xy_to_lonlat, lonlat_to_dem_xy, open_dem, resolve_dem_path
from flood_model.river_network import main_centerline_lonlat
from flood_model.routing import hydro1d_csv_paths, parse_hydro1d_source

XS_SPACING_M = 1500.0
MAX_PROFILE_POINTS = 800


@dataclass
class ChannelParams:
    manning_n: float = 0.030
    xs_spacing_m: float = XS_SPACING_M
    xs_half_width_m: float = 1500.0
    xs_step_m: float = 10.0
    min_slope: float = 2.0e-5
    q_min: float = 8.0
    dt_hours: float = 1.0
    k_scale: float = 1.0


@dataclass
class CrossSection:
    """Mat cat ngang DEM: offset (m, am = trai), z (m)."""

    index: int
    station_m: float
    lon: float
    lat: float
    offset_m: np.ndarray
    z: np.ndarray
    z_bed: float
    h_tbl: np.ndarray
    a_tbl: np.ndarray
    p_tbl: np.ndarray
    b_tbl: np.ndarray

    def props(self, h: float) -> tuple[float, float, float]:
        h = float(np.clip(h, self.h_tbl[0], self.h_tbl[-1]))
        a = float(np.interp(h, self.h_tbl, self.a_tbl))
        p = float(np.interp(h, self.h_tbl, self.p_tbl))
        b = float(np.interp(h, self.h_tbl, self.b_tbl))
        return max(a, 1e-3), max(p, 1e-3), max(b, 1.0)

    def manning_q(self, h: float, n: float, slope: float) -> float:
        a, p, _b = self.props(h)
        r = a / p
        return (1.0 / n) * a * (r ** (2.0 / 3.0)) * math.sqrt(max(slope, 1e-8))

    def stage_for_q(self, q: float, n: float, slope: float) -> float:
        q = max(float(q), 1e-6)
        lo, hi = float(self.h_tbl[0]), float(self.h_tbl[-1])
        if self.manning_q(hi, n, slope) < q:
            return hi
        for _ in range(36):
            mid = 0.5 * (lo + hi)
            if self.manning_q(mid, n, slope) < q:
                lo = mid
            else:
                hi = mid
        return hi

    def top_width(self, h: float) -> float:
        return self.props(h)[2]

    def celerity(self, q: float, n: float, slope: float) -> float:
        h = self.stage_for_q(q, n, slope)
        dh = 0.08
        h0 = max(h - dh, float(self.h_tbl[0]))
        h1 = min(h + dh, float(self.h_tbl[-1]))
        a0, _, _ = self.props(h0)
        a1, _, _ = self.props(h1)
        q0 = self.manning_q(h0, n, slope)
        q1 = self.manning_q(h1, n, slope)
        da = max(a1 - a0, 1e-3)
        return max((q1 - q0) / da, 0.15)


@dataclass
class RiverGeom:
    distance_m: np.ndarray
    z_bed: np.ndarray
    dx_m: np.ndarray
    slope: np.ndarray
    lon: np.ndarray
    lat: np.ndarray
    length_m: float
    sections: list[CrossSection] = field(default_factory=list)


def densify_xy(xs: np.ndarray, ys: np.ndarray, step_m: float) -> tuple[np.ndarray, np.ndarray]:
    if xs.size < 2:
        return xs, ys
    seg = np.hypot(np.diff(xs), np.diff(ys))
    dist = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(dist[-1])
    if total <= step_m:
        return xs, ys
    n = min(MAX_PROFILE_POINTS, max(2, int(math.ceil(total / step_m)) + 1))
    td = np.linspace(0.0, total, n)
    return np.interp(td, dist, xs), np.interp(td, dist, ys)


def sample_z(src, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    nodata = src.nodata
    coords = list(zip(xs.tolist(), ys.tolist()))
    z = np.array([v[0] for v in src.sample(coords)], dtype=float)
    invalid = ~np.isfinite(z)
    if nodata is not None:
        invalid |= z == nodata
    z[invalid] = np.nan
    return z


def chainage_m(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    if xs.size == 0:
        return np.zeros(0)
    d = np.zeros(xs.size, dtype=float)
    d[1:] = np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))
    return d


def lower_envelope(z: np.ndarray, window: int) -> np.ndarray:
    """Long song uoc luong: loc min + lam muot (bo NaN bang noi suy)."""
    z = np.asarray(z, dtype=float).copy()
    n = z.size
    if n == 0:
        return z
    idx = np.arange(n)
    good = np.isfinite(z)
    if good.sum() < 2:
        return np.nan_to_num(z, nan=0.0)
    z[~good] = np.interp(idx[~good], idx[good], z[good])
    w = max(3, int(window) | 1)
    half = w // 2
    bed = np.empty(n, dtype=float)
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        bed[i] = float(np.min(z[a:b]))
    k = max(3, (w // 2) | 1)
    kernel = np.ones(k) / k
    pad = k // 2
    ext = np.pad(bed, pad, mode="edge")
    smooth = np.convolve(ext, kernel, mode="valid")
    return np.minimum(smooth[:n], z)


def profile_from_lonlat(
    src,
    lons: Sequence[float],
    lats: Sequence[float],
    step_m: float | None = None,
) -> dict[str, np.ndarray]:
    xs0, ys0 = lonlat_to_dem_xy(src, lons, lats)
    step = float(step_m) if step_m else max(float(src.res[0]), 20.0)
    xs, ys = densify_xy(xs0, ys0, step)
    z = sample_z(src, xs, ys)
    dist = chainage_m(xs, ys)
    win = max(5, int(round(250.0 / max(step, 1.0))))
    bed = lower_envelope(z, win)
    lon, lat = dem_xy_to_lonlat(src, xs, ys)
    return {
        "x": xs,
        "y": ys,
        "lon": lon,
        "lat": lat,
        "distance_m": dist,
        "z_dem": z,
        "z_bed": bed,
    }


def _wet_props(offset: np.ndarray, z: np.ndarray, h: float) -> tuple[float, float, float]:
    a = p = b = 0.0
    n = int(offset.size)
    for i in range(n - 1):
        z0, z1 = float(z[i]), float(z[i + 1])
        if not (math.isfinite(z0) and math.isfinite(z1)):
            continue
        d0, d1 = h - z0, h - z1
        dx = float(offset[i + 1] - offset[i])
        dz = z1 - z0
        ds = math.hypot(dx, dz)
        adx = abs(dx)
        if d0 <= 0.0 and d1 <= 0.0:
            continue
        if d0 > 0.0 and d1 > 0.0:
            a += 0.5 * (d0 + d1) * adx
            p += ds
            b += adx
            continue
        t = d0 / (d0 - d1) if abs(d0 - d1) > 1e-12 else 0.5
        t = min(max(t, 0.0), 1.0)
        d_wet = d0 if d0 > 0.0 else d1
        a += 0.5 * d_wet * t * adx
        p += ds * t
        b += adx * t
    return a, p, b


def section_tables(offset: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    z = np.asarray(z, dtype=float)
    off = np.asarray(offset, dtype=float)
    zmin = float(np.nanmin(z))
    zmax = float(np.nanmax(z))
    h_top = min(zmax, zmin + 18.0)
    h_tbl = np.linspace(zmin + 0.05, max(h_top, zmin + 2.0), 90)
    a_tbl = np.zeros_like(h_tbl)
    p_tbl = np.zeros_like(h_tbl)
    b_tbl = np.zeros_like(h_tbl)
    for i, h in enumerate(h_tbl):
        a_tbl[i], p_tbl[i], b_tbl[i] = _wet_props(off, z, float(h))
    a_tbl = np.maximum.accumulate(np.maximum(a_tbl, 1e-3))
    p_tbl = np.maximum(p_tbl, 1e-3)
    b_tbl = np.maximum(b_tbl, 1.0)
    return h_tbl, a_tbl, p_tbl, b_tbl


def sample_cross_section(
    src,
    x0: float,
    y0: float,
    nx: float,
    ny: float,
    half_w: float,
    step: float,
) -> tuple[np.ndarray, np.ndarray]:
    n = max(21, int(round(2.0 * half_w / max(step, 1.0))) + 1)
    if n % 2 == 0:
        n += 1
    off = np.linspace(-half_w, half_w, n)
    xs = x0 + nx * off
    ys = y0 + ny * off
    z = sample_z(src, xs, ys)
    good = np.isfinite(z)
    if good.sum() < 8:
        z = np.where(good, z, np.nanmedian(z[good]) if good.any() else 0.0)
    else:
        idx = np.arange(z.size)
        z[~good] = np.interp(idx[~good], idx[good], z[good])
    mid = n // 2
    win = max(3, n // 20)
    i0 = max(0, mid - win)
    i1 = min(n, mid + win + 1)
    k = i0 + int(np.argmin(z[i0:i1]))
    off = off - off[k]
    return off, z


def geom_from_polyline(
    src,
    xs: np.ndarray,
    ys: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    z: np.ndarray,
    dist: np.ndarray,
    par: ChannelParams,
    half_width_m: Optional[float] = None,
    ensure_downhill: bool = True,
) -> RiverGeom:
    """Mat cat ngang deu theo polyline DEM (xs/ys toa do raster)."""
    good = np.isfinite(dist) & np.isfinite(z) & np.isfinite(xs) & np.isfinite(lon)
    dist, z, lon, lat, xs, ys = dist[good], z[good], lon[good], lat[good], xs[good], ys[good]
    if dist.size < 8:
        raise ValueError("DEM khong cho duoc long song.")

    if ensure_downhill and z[-1] > z[0] + 0.3:
        dist = dist[-1] - dist[::-1]
        z, lon, lat = z[::-1], lon[::-1], lat[::-1]
        xs, ys = xs[::-1], ys[::-1]
        dist = dist - dist[0]

    length = float(dist[-1])
    spacing = max(float(par.xs_spacing_m), 50.0)
    n_reaches = max(1, int(round(length / spacing)))
    n_node = n_reaches + 1
    s = np.linspace(0.0, length, n_node)
    z_s = np.interp(s, dist, z)
    lon_s = np.interp(s, dist, lon)
    lat_s = np.interp(s, dist, lat)
    x_s = np.interp(s, dist, xs)
    y_s = np.interp(s, dist, ys)
    dx = np.diff(s)
    slope = np.maximum((-np.diff(z_s)) / np.maximum(dx, 1.0), 0.0)
    half_w = float(par.xs_half_width_m if half_width_m is None else half_width_m)

    sections: list[CrossSection] = []
    step = max(float(src.res[0]), par.xs_step_m)
    for i in range(n_node):
        if i == 0:
            tx, ty = x_s[1] - x_s[0], y_s[1] - y_s[0]
        elif i == n_node - 1:
            tx, ty = x_s[-1] - x_s[-2], y_s[-1] - y_s[-2]
        else:
            tx, ty = x_s[i + 1] - x_s[i - 1], y_s[i + 1] - y_s[i - 1]
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L, tx / L
        off, zz = sample_cross_section(src, float(x_s[i]), float(y_s[i]), nx, ny, half_w, step)
        z_bed = float(np.nanmin(zz))
        h_tbl, a_tbl, p_tbl, b_tbl = section_tables(off, zz)
        sections.append(
            CrossSection(
                index=i,
                station_m=float(s[i]),
                lon=float(lon_s[i]),
                lat=float(lat_s[i]),
                offset_m=off,
                z=zz,
                z_bed=z_bed,
                h_tbl=h_tbl,
                a_tbl=a_tbl,
                p_tbl=p_tbl,
                b_tbl=b_tbl,
            )
        )

    return RiverGeom(
        distance_m=s,
        z_bed=np.array([sec.z_bed for sec in sections]),
        dx_m=dx,
        slope=slope,
        lon=lon_s,
        lat=lat_s,
        length_m=float(s[-1]),
        sections=sections,
    )


def extract_reach_from_lonlat(
    src,
    lons: Sequence[float],
    lats: Sequence[float],
    par: ChannelParams,
    half_width_m: Optional[float] = None,
    ensure_downhill: bool = True,
) -> RiverGeom:
    prof = profile_from_lonlat(src, list(lons), list(lats))
    xs, ys = lonlat_to_dem_xy(src, prof["lon"], prof["lat"])
    return geom_from_polyline(
        src,
        xs,
        ys,
        np.asarray(prof["lon"], dtype=float),
        np.asarray(prof["lat"], dtype=float),
        np.asarray(prof["z_bed"], dtype=float),
        np.asarray(prof["distance_m"], dtype=float),
        par,
        half_width_m=half_width_m,
        ensure_downhill=ensure_downhill,
    )


def extract_river(dem_path: Path, par: ChannelParams) -> RiverGeom:
    dem = resolve_dem_path(None, str(dem_path))
    with open_dem(dem) as src:
        lons, lats, _src = main_centerline_lonlat(src)
        return extract_reach_from_lonlat(src, lons, lats, par)


def extend_section_tables(geom, h_max: float) -> None:
    """Noi bang A(H) den H_max de bien ha luu khong tao doc nguoc gia."""
    h_max = float(h_max)
    dh = 0.25
    for sec in geom.sections:
        h_top = float(sec.h_tbl[-1])
        if h_top >= h_max - 1e-6:
            continue
        b = max(float(sec.b_tbl[-1]), 1.0)
        a = float(sec.a_tbl[-1])
        p = float(sec.p_tbl[-1])
        extra_h, extra_a, extra_p, extra_b = [], [], [], []
        h = h_top
        while h < h_max:
            h += dh
            a += b * dh
            p += 2.0 * dh
            extra_h.append(h)
            extra_a.append(a)
            extra_p.append(p)
            extra_b.append(b)
        if extra_h:
            sec.h_tbl = np.concatenate([sec.h_tbl, np.asarray(extra_h)])
            sec.a_tbl = np.concatenate([sec.a_tbl, np.asarray(extra_a)])
            sec.p_tbl = np.concatenate([sec.p_tbl, np.asarray(extra_p)])
            sec.b_tbl = np.concatenate([sec.b_tbl, np.asarray(extra_b)])


def write_cross_sections_csv(geom: RiverGeom, path: Path) -> None:
    rows = []
    for sec in geom.sections:
        for off, zz in zip(sec.offset_m, sec.z):
            rows.append(
                {
                    "xs_id": sec.index + 1,
                    "station_km": f"{sec.station_m / 1000.0:.4f}",
                    "offset_m": f"{float(off):.2f}",
                    "z_m": f"{float(zz):.4f}",
                    "lon": f"{sec.lon:.6f}",
                    "lat": f"{sec.lat:.6f}",
                }
            )
    write_csv_rows(path, ["xs_id", "station_km", "offset_m", "z_m", "lon", "lat"], rows)


@lru_cache(maxsize=4)
def load_official_xs(water_source: str = "saint-venant") -> dict[int, dict[str, np.ndarray]]:
    """Mat cat 1D: offset_m, z_m (CSV). lon/lat hien tai la toa do tram, khong phai offset."""
    path = hydro1d_csv_paths(parse_hydro1d_source(water_source))["xs"]
    path = Path(path)
    if not csv_available(path):
        return {}
    groups: dict[int, dict[str, list[float]]] = {}
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            try:
                xs_id = int(float(row["xs_id"]))
                off = float(row["offset_m"])
                z = float(row["z_m"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(off) and math.isfinite(z)):
                continue
            rec = groups.setdefault(
                xs_id, {"offset_m": [], "z_m": [], "lon": [], "lat": [], "station_km": []}
            )
            rec["offset_m"].append(off)
            rec["z_m"].append(z)
            try:
                rec["lon"].append(float(row["lon"]))
                rec["lat"].append(float(row["lat"]))
            except (KeyError, TypeError, ValueError):
                rec["lon"].append(float("nan"))
                rec["lat"].append(float("nan"))
            try:
                rec["station_km"].append(float(row["station_km"]))
            except (KeyError, TypeError, ValueError):
                rec["station_km"].append(float("nan"))
    out: dict[int, dict[str, np.ndarray]] = {}
    for xs_id, rec in groups.items():
        if len(rec["offset_m"]) < 5:
            continue
        out[xs_id] = {k: np.asarray(v, dtype=float) for k, v in rec.items()}
    return out

r"""
Dien toan 1D Saint-Venant (song dong luc) doc long song.

Phuong trinh kenh ho 1D:
  Lien tuc :  dA/dt + dQ/dx = 0
  Dong luong: dQ/dt + d(Q^2/A)/dx + g A dH/dx + g A Sf = 0
  Sf = n^2 Q|Q| / (A^2 R^{4/3})   (Manning)

Luoi lech (staggered): H, A tai mat cat; Q tai doan (giua hai XS).
Q doan = Manning theo doc mat nuoc (song khuech tan / can bang local-inertial).
Ma sat an. Buoc thoi gian thuy luc theo CFL, xuat theo dt gio.

Hinh hoc / bien:
  - Mang 1D: n song chinh (long thu nhat + long doc lap) + m song nhanh noi vao long thu nhat
  - --max-mains 8 --max-tribs 12   (mac dinh; DEM het song thi dung so tim duoc)
  - Mat cat ngang DEM: --xs-spacing (m), mac dinh 1500. Vi du: 500, 900, 1500
  - Nhanh Song Duong (tach ve dong, Gia Lam): thoat nuoc (phan luu); bien ngoai = H
  - Nhanh khac: z_sat_chinh > z_xa -> thoat (bien H); nguoc lai nhap luu (bien Q)
  - Nut nhap luu: H_nhanh = H_chinh; Q_nhanh cong vao lien tuc long chinh
  - Nut thoat: Q lay tu long chinh (weir); H bien o dau xa = cot h_na1_m
    (saint_venant_output/demo_downstream_stage.csv)
  - H ha luu long chinh: saint_venant_output/demo_downstream_stage.csv
  - --no-network  chi chay 1 long (cu)

Manning n theo mat cat:
  --n 0.030                 mac dinh khi CSV thieu cot
  --n-csv demo_manning_n.csv  n theo xs_id; thieu id thi noi suy theo station_km
                            (CLI/API tu dung file nay neu co)

Dieu kien ban dau Q0, H0:
  --q0 600                  Q dong nhat luc t=0 (m3/s). Mac dinh: Q vao luc t=0
  --h0 5.2                  H ha luu luc t=0 (m) de tinh mat nuoc backwater.
                            Mac dinh: H ha luu bien luc t=0
  --ic-csv demo_initial_qh.csv  Q0, H0 tung mat cat (xs_id,q0,h0)

Chay:
  python saint-venant.py
  python saint-venant.py --no-plot
  .\venv\Scripts\python.exe flood_model/saint-venant.py --xs-spacing 1500 --no-plot
  python saint-venant.py --n-csv saint_venant_output/demo_manning_n.csv
  python saint-venant.py --q0 60 --h0 1.2 --n-csv saint_venant_output/demo_manning_n.csv
  python saint-venant.py --ic-csv saint_venant_output/demo_initial_qh.csv
"""

from __future__ import annotations

import argparse
import csv
import math
import runpy
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.construction import (
    apply_structures_to_q_lat,
    init_structure_runtime,
    log_bound_structures,
)
from flood_model.boundary import (
    DEFAULT_H_CSV,
    DEFAULT_INFLOW_CSV,
    DEFAULT_TANK_CSV,
    DEFAULT_TRIB_H_CSV,
    TRIB_H_COL,
    load_downstream_stage,
    load_inflow_q,
    load_initial_qh_csv,
    load_trib_outlet_stage,
    resample_series,
    resolve_inflow_csv,
    write_downstream_csv,
    write_inflow_csv,
    write_initial_csv,
)
from flood_model import cross_section as _cross_section

ChannelParams = getattr(_cross_section, "ChannelParams")
extend_section_tables = getattr(_cross_section, "extend_section_tables")
extract_reach_from_lonlat = getattr(_cross_section, "extract_reach_from_lonlat")
extract_river = getattr(_cross_section, "extract_river")
write_cross_sections_csv = getattr(_cross_section, "write_cross_sections_csv")
from flood_model.gis import lonlat_to_mercator, open_dem, resolve_dem_path
from flood_model.river_network import _gap_m, _project_on_main, tributary_centerlines

DEFAULT_DEM = ROOT / "projects" / "data" / "dem-song-hong.tif"
DEFAULT_OUT_DIR = ROOT / "saint_venant_output"
DEFAULT_N_CSV = DEFAULT_OUT_DIR / "demo_manning_n.csv"
DEFAULT_MC = PACKAGE_DIR / "muskingum-cung.py"
XS_SPACING_M = 1500.0
G = 9.81


def parse_xs_spacing_m(raw: float, default_m: float = XS_SPACING_M) -> float:
    """--xs-spacing tinh bang met. Gia tri < 50 duoc hieu la km (CLI cu: 1.5 -> 1500 m)."""
    v = float(raw)
    if not math.isfinite(v) or v <= 0:
        return float(default_m)
    if v < 50.0:
        v *= 1000.0
    return max(50.0, v)


DT_HOURS_MIN = 1.0 / 60.0
DT_HOURS_MAX = 24.0


def parse_dt_hours(raw: float, default_h: float = 1.0) -> float:
    """--dt: buoc xuat ket qua (gio). Mac dinh 1 h. Cho phep 1 phut .. 24 gio."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(default_h)
    if not math.isfinite(v) or v <= 0:
        return float(default_h)
    return min(DT_HOURS_MAX, max(DT_HOURS_MIN, v))

_MC: Optional[dict[str, Any]] = None


def _mc() -> dict[str, Any]:
    global _MC
    if _MC is None:
        if not DEFAULT_MC.is_file():
            raise FileNotFoundError(f"Khong thay {DEFAULT_MC}")
        _MC = runpy.run_path(str(DEFAULT_MC), run_name="muskingum_cunge")
    return _MC


# ---------------------------------------------------------------------------
# Tham so
# ---------------------------------------------------------------------------

@dataclass
class SaintVenantParams:
    manning_n: float = 0.030
    xs_spacing_m: float = XS_SPACING_M
    xs_half_width_m: float = 1500.0
    xs_step_m: float = 10.0
    min_slope: float = 2.0e-5
    q_min: float = 8.0
    dt_hours: float = 1.0
    cfl: float = 0.45
    dt_hydro_max_s: float = 60.0
    y_min: float = 0.08
    unidirectional: bool = True
    q0: Optional[float] = None
    h0: Optional[float] = None


def copy_params(par, **overrides) -> SaintVenantParams:
    """Tao SaintVenantParams tu par (cho phep subclass co them truong, vd. theta)."""
    fields = {
        name: getattr(par, name)
        for name in SaintVenantParams.__dataclass_fields__
        if hasattr(par, name)
    }
    fields.update(overrides)
    return SaintVenantParams(**fields)


@dataclass
class ExtraMainResult:
    """Song chinh doc lap (khong noi vao long chinh thu nhat)."""

    reach_id: str
    geom: Any
    q: np.ndarray
    h: np.ndarray
    y: np.ndarray
    length_m: float
    lon: np.ndarray
    lat: np.ndarray


@dataclass
class TribResult:
    reach_id: str
    geom: Any
    q: np.ndarray
    h: np.ndarray
    q_in: np.ndarray
    join_station_m: float
    join_xs: int
    q_frac: float
    length_m: float
    lon: np.ndarray
    lat: np.ndarray
    kind: str = "outlet"
    bc: str = "H"
    z_near: float = 0.0
    z_far: float = 0.0


@dataclass
class RouteResult:
    hours: np.ndarray
    q_in: np.ndarray
    h_down: np.ndarray
    q: np.ndarray
    h: np.ndarray
    y: np.ndarray
    geom: Any
    params: SaintVenantParams
    station_km: np.ndarray
    peak_time_index: int
    mass_balance_m3: float
    dt_hydro_s: float = 0.0
    n_substep: int = 0
    q_main_up: Optional[np.ndarray] = None
    tribs: list = field(default_factory=list)
    extra_mains: list = field(default_factory=list)
    h_trib_down: Optional[np.ndarray] = None
    reservoir_levels: dict = field(default_factory=dict)


def _channel_params(par: SaintVenantParams):
    return ChannelParams(
        manning_n=par.manning_n,
        xs_spacing_m=par.xs_spacing_m,
        xs_half_width_m=par.xs_half_width_m,
        xs_step_m=par.xs_step_m,
        min_slope=par.min_slope,
        q_min=par.q_min,
        dt_hours=par.dt_hours,
    )


def stage_from_area(sec, area: float) -> float:
    """Noi suy H tu dien tich uot A. Ngoai bang: them tuong dung theo B dinh."""
    a_tbl = np.asarray(sec.a_tbl, dtype=float)
    h_tbl = np.asarray(sec.h_tbl, dtype=float)
    area = float(area)
    if area <= a_tbl[0]:
        return float(h_tbl[0])
    if area <= a_tbl[-1]:
        return float(np.interp(area, a_tbl, h_tbl))
    b = max(float(sec.b_tbl[-1]), 1.0)
    return float(h_tbl[-1] + (area - a_tbl[-1]) / b)


def section_n(sec, default: float) -> float:
    n = getattr(sec, "manning_n", None)
    if n is None or not math.isfinite(float(n)) or float(n) <= 0.0:
        return float(default)
    return float(n)


def reach_n(sections, default: float) -> np.ndarray:
    """n tren doan = trung binh hai mat cat ke."""
    nn = np.array([section_n(sec, default) for sec in sections], dtype=float)
    return 0.5 * (nn[:-1] + nn[1:])


def load_manning_n_table(
    path: Path, reach_id: str | None = None
) -> tuple[dict[int, float], np.ndarray, np.ndarray]:
    """CSV: xs_id,manning_n (co the them station_km, reach_id)."""
    by_id: dict[int, float] = {}
    stations: list[float] = []
    values: list[float] = []
    want = str(reach_id or "").strip().lower().replace("_", "-").replace(" ", "-")
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return by_id, np.array([], dtype=float), np.array([], dtype=float)
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        n_col = fields.get("manning_n") or fields.get("n")
        id_col = fields.get("xs_id") or fields.get("id")
        st_col = fields.get("station_km") or fields.get("station") or fields.get("s_km")
        r_col = fields.get("reach_id") or fields.get("reach")
        if n_col is None:
            raise ValueError(f"CSV Manning can cot manning_n: {path}")
        for i, row in enumerate(reader):
            if r_col and str(row.get(r_col, "")).strip():
                rid = str(row.get(r_col)).strip().lower().replace(" ", "-").replace("_", "-")
                if want:
                    if rid not in (want, want.replace("-", "_")) and not (
                        want in ("main", "main-1") and rid in ("main", "main-1", "song-chinh", "sông-chính")
                    ):
                        continue
                elif rid not in ("main", "main-1", "song-chinh", "sông-chính"):
                    continue
            elif want and want not in ("main", "main-1"):
                continue
            try:
                n = float(row[n_col])
            except (TypeError, ValueError, KeyError):
                continue
            if n <= 0.0 or not math.isfinite(n):
                continue
            if id_col and str(row.get(id_col, "")).strip():
                try:
                    xs_id = int(float(row[id_col]))
                except (TypeError, ValueError):
                    xs_id = i + 1
            else:
                xs_id = i + 1
            by_id[xs_id] = n
            if st_col and str(row.get(st_col, "")).strip():
                try:
                    st = float(row[st_col])
                except (TypeError, ValueError):
                    st = float("nan")
                if math.isfinite(st):
                    stations.append(st)
                    values.append(n)
    if stations:
        order = np.argsort(np.asarray(stations, dtype=float))
        st_arr = np.asarray(stations, dtype=float)[order]
        n_arr = np.asarray(values, dtype=float)[order]
    else:
        st_arr = np.array([], dtype=float)
        n_arr = np.array([], dtype=float)
    return by_id, st_arr, n_arr


def load_manning_n_csv(path: Path) -> dict[int, float]:
    """CSV: xs_id,manning_n  (co the them station_km)."""
    by_id, _st, _n = load_manning_n_table(path)
    return by_id


def apply_manning_n(
    geom,
    n_default: float,
    n_by_id: Optional[dict[int, float]] = None,
    station_km: Optional[np.ndarray] = None,
    n_along: Optional[np.ndarray] = None,
) -> None:
    """Gan n tung mat cat: uu tien n(x) theo ly trinh (station_km), roi xs_id, roi --n."""
    n_by_id = n_by_id or {}
    st_csv = np.asarray(station_km, dtype=float) if station_km is not None else np.array([], dtype=float)
    n_csv = np.asarray(n_along, dtype=float) if n_along is not None else np.array([], dtype=float)
    n_x = len(geom.sections)
    x = np.array([float(sec.station_m) / 1000.0 for sec in geom.sections], dtype=float)
    n_vals = np.full(n_x, np.nan, dtype=float)
    if st_csv.size >= 2 and n_csv.size == st_csv.size:
        n_vals = np.interp(x, st_csv, n_csv)
    elif st_csv.size == 1 and n_csv.size == 1:
        n_vals[:] = float(n_csv[0])
    else:
        for i, sec in enumerate(geom.sections):
            xs_id = int(sec.index) + 1
            if xs_id in n_by_id:
                n_vals[i] = float(n_by_id[xs_id])
    n_vals = _interp_nan(n_vals, x)
    n_vals[~np.isfinite(n_vals)] = float(n_default)
    n_vals = np.maximum(n_vals, 1e-4)
    for sec, n in zip(geom.sections, n_vals):
        sec.manning_n = float(n)


def resolve_n_csv(raw: Optional[Path | str] = None) -> Optional[Path]:
    """Tim demo_manning_n.csv: --n-csv, CWD, saint_venant_output, thu muc script."""
    tried: list[Path] = []
    if raw is not None and str(raw).strip():
        p = Path(str(raw).strip())
        tried.append(p)
        if not p.is_absolute():
            tried.append(Path.cwd() / p)
            tried.append(DEFAULT_OUT_DIR / p.name)
            tried.append(PACKAGE_DIR / p)
            tried.append(PACKAGE_DIR / "saint_venant_output" / p.name)
    tried.append(DEFAULT_N_CSV)
    seen: set[str] = set()
    for cand in tried:
        try:
            path = cand.expanduser()
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if csv_available(path):
                return path.resolve() if path.is_file() else path
        except OSError:
            continue
    return None


def write_manning_csv(geom, path: Path, reach_id: str = "main") -> None:
    write_manning_network_csv([(reach_id, geom)], path)


def write_manning_network_csv(items: Sequence[tuple[str, Any]], path: Path) -> None:
    rows: list[dict[str, str]] = []
    for reach_id, geom in items:
        rid = str(reach_id or "main").strip() or "main"
        for sec in geom.sections:
            rows.append(
                {
                    "reach_id": rid,
                    "xs_id": str(sec.index + 1),
                    "station_km": f"{sec.station_m / 1000.0:.4f}",
                    "manning_n": f"{section_n(sec, 0.03):.4f}",
                }
            )
    write_csv(path, ["reach_id", "xs_id", "station_km", "manning_n"], rows)


def _interp_nan(values: np.ndarray, x: np.ndarray) -> np.ndarray:
    out = np.array(values, dtype=float, copy=True)
    ok = np.isfinite(out)
    if ok.all() or not ok.any():
        return out
    out[~ok] = np.interp(x[~ok], x[ok], out[ok])
    return out


def section_state(sec, h: float) -> tuple[float, float, float, float]:
    a, p, b = sec.props(h)
    y = max(float(h) - float(sec.z_bed), 1e-4)
    return a, p, b, y


# ---------------------------------------------------------------------------
# Saint-Venant 1D — luoi lech: H tai XS, Q tai doan
# ---------------------------------------------------------------------------

def _node_dx(dx_m: np.ndarray) -> np.ndarray:
    n = int(dx_m.size) + 1
    dxn = np.zeros(n, dtype=float)
    dxn[0] = float(dx_m[0])
    dxn[-1] = float(dx_m[-1])
    if n > 2:
        dxn[1:-1] = 0.5 * (dx_m[:-1] + dx_m[1:])
    return np.maximum(dxn, 1.0)


def _states(sections, h: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n = int(h.size)
    a = np.zeros(n)
    p = np.zeros(n)
    b = np.zeros(n)
    y = np.zeros(n)
    for i, sec in enumerate(sections):
        a[i], p[i], b[i], y[i] = section_state(sec, float(h[i]))
    return a, p, b, y


def _node_q(qf: np.ndarray, q_up: float) -> np.ndarray:
    """Noi suy Q tai mat cat tu Q tren cac doan."""
    n = int(qf.size) + 1
    q = np.zeros(n, dtype=float)
    q[0] = float(q_up)
    q[-1] = float(qf[-1])
    if n > 2:
        q[1:-1] = 0.5 * (qf[:-1] + qf[1:])
    return q


def _hydro_dt(a: np.ndarray, b: np.ndarray, qf: np.ndarray, dx: np.ndarray, par: SaintVenantParams) -> float:
    af = 0.5 * (a[:-1] + a[1:])
    bf = 0.5 * (b[:-1] + b[1:])
    u = qf / np.maximum(af, 1e-3)
    c = np.abs(u) + np.sqrt(G * np.maximum(af / np.maximum(bf, 1.0), 0.05))
    dt_cfl = float(par.cfl) * float(np.min(dx / np.maximum(c, 0.2)))
    return float(min(max(dt_cfl, 1.0), par.dt_hydro_max_s))


def _backwater_h(geom, par: SaintVenantParams, q_node: np.ndarray, h_ds: float):
    mc = _mc()
    q_row = np.maximum(np.asarray(q_node, dtype=float), par.q_min)[None, :]
    h_row, y_row = mc["stages_backwater"](
        q_row,
        np.array([float(h_ds)], dtype=float),
        geom,
        _channel_params(par),
    )
    return h_row[0], y_row[0]


def _init_state(
    geom,
    par: SaintVenantParams,
    q_bc0: float,
    h_ds0: float,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Dieu kien ban dau luc t=0.
      Q0: --ic-csv (xs_id) -> --q0 -> Q vao luc t=0
      H0: --ic-csv (noi suy theo ly trinh) -> backwater tu --h0 hoac H ha luu luc t=0
    """
    sections = geom.sections
    n_x = int(geom.distance_m.size)
    z = np.asarray(geom.z_bed, dtype=float)
    station = np.array([float(sec.station_m) for sec in sections], dtype=float)
    q0_by_id = q0_by_id or {}
    h0_by_id = h0_by_id or {}

    q_default = float(par.q0) if par.q0 is not None else float(q_bc0)
    q_default = max(q_default, par.q_min)
    q_node = np.full(n_x, q_default, dtype=float)
    for i, sec in enumerate(sections):
        xs_id = int(sec.index) + 1
        if xs_id in q0_by_id:
            q_node[i] = max(float(q0_by_id[xs_id]), par.q_min)

    h_user = np.full(n_x, np.nan, dtype=float)
    for i, sec in enumerate(sections):
        xs_id = int(sec.index) + 1
        if xs_id in h0_by_id:
            h_user[i] = float(h0_by_id[xs_id])

    if np.isfinite(h_user).any():
        h = _interp_nan(h_user, station)
        h = np.maximum(h, z + par.y_min)
    else:
        h_ds = float(par.h0) if par.h0 is not None else float(h_ds0)
        h, _y = _backwater_h(geom, par, q_node, h_ds)
        h = np.maximum(h, z + par.y_min)

    qf = np.maximum(0.5 * (q_node[:-1] + q_node[1:]), par.q_min)
    y = h - z
    return qf, h, y, q_node


def saint_venant_step(
    qf: np.ndarray,
    h: np.ndarray,
    geom,
    par: SaintVenantParams,
    q_up: float,
    h_ds: float,
    dt: float,
    q_lat: Optional[np.ndarray] = None,
    structures=None,
    hour: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Lien tuc tai nut + Q doan theo Manning tu doc mat nuoc (song khuech tan /
    local-inertial can bang). Q > 0 ve ha luu.
    q_lat[i]: luu luong nhap luu (m3/s) vao nut i (nhanh do vao long chinh).
    structures: cong trinh MIKE — mat inline ep Q = f(H); lateral da nam trong q_lat.
    """
    from flood_model.construction import apply_inline_to_face_q, apply_reservoir_storage, inline_face_discharges

    sections = geom.sections
    z = geom.z_bed
    n_x = int(h.size)
    n = reach_n(sections, par.manning_n)
    dx = np.maximum(np.asarray(geom.dx_m, dtype=float), 1.0)
    dxn = _node_dx(dx)
    q_up = float(q_up)
    qf = qf.copy()
    lat = np.zeros(n_x, dtype=float) if q_lat is None else np.asarray(q_lat, dtype=float)

    a, _p, _b, _y = _states(sections, h)

    a_new = a.copy()
    a_new[0] = a[0] - dt * (qf[0] - q_up - float(lat[0])) / dxn[0]
    for i in range(1, n_x - 1):
        a_new[i] = a[i] - dt * (qf[i] - qf[i - 1] - float(lat[i])) / dxn[i]
    a_new = np.maximum(a_new, 1e-3)

    h_new = h.copy()
    for i in range(n_x - 1):
        h_new[i] = max(stage_from_area(sections[i], float(a_new[i])), float(z[i]) + par.y_min)
    h_new[-1] = max(float(h_ds), float(z[-1]) + par.y_min)
    # Song dong bang: WSE khong duoc tut duoi bien H ha luu (backwater).
    h_new = np.maximum(h_new, float(h_ds))

    a2, p2, _b2, _y2 = _states(sections, h_new)
    af = np.maximum(0.5 * (a2[:-1] + a2[1:]), 1e-3)
    pf = np.maximum(0.5 * (p2[:-1] + p2[1:]), 1e-3)
    rf = np.maximum(af / pf, 1e-4)
    dH = h_new[1:] - h_new[:-1]
    sf_ws = -dH / dx

    if par.unidirectional:
        sf = np.maximum(sf_ws, par.min_slope)
        q_man = (1.0 / n) * af * (rf ** (2.0 / 3.0)) * np.sqrt(sf)
        q_star = qf - dt * G * af * (dH / dx)
        denom = 1.0 + dt * G * (n ** 2) * np.abs(qf) / np.maximum((rf ** (4.0 / 3.0)) * af, 1e-6)
        q_inr = q_star / denom
        qf_new = np.maximum(0.35 * q_inr + 0.65 * q_man, par.q_min)
    else:
        sign = np.where(sf_ws >= 0.0, 1.0, -1.0)
        sf = np.maximum(np.abs(sf_ws), par.min_slope)
        qf_new = sign * (1.0 / n) * af * (rf ** (2.0 / 3.0)) * np.sqrt(sf)
    face_q = inline_face_discharges(structures, h_new, hour=float(hour))
    qf_new = apply_inline_to_face_q(qf_new, structures, h_new, hour=float(hour))
    apply_reservoir_storage(structures, face_q, dt_s=float(dt))
    return qf_new, h_new


def route_saint_venant(
    hours: np.ndarray,
    q_in: np.ndarray,
    h_down: np.ndarray,
    geom,
    par: SaintVenantParams,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
    reach_id: str = "main",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, int]:
    n_t = int(hours.size)
    n_x = int(geom.distance_m.size)
    q_out = np.zeros((n_t, n_x), dtype=float)
    h_out = np.zeros((n_t, n_x), dtype=float)
    y_out = np.zeros((n_t, n_x), dtype=float)

    qf, h, y, q_node = _init_state(
        geom, par, float(q_in[0]), float(h_down[0]), q0_by_id, h0_by_id
    )
    q_out[0] = q_node
    h_out[0], y_out[0] = h, y

    dx = np.maximum(np.asarray(geom.dx_m, dtype=float), 1.0)
    dt_out_s = float(par.dt_hours) * 3600.0
    dt_used = par.dt_hydro_max_s
    n_sub_max = 1

    t_abs = float(hours[0]) * 3600.0
    hours_s = hours * 3600.0
    st_runtime = init_structure_runtime(reach_id or "main", geom)
    log_bound_structures(st_runtime, label=f"SV:{reach_id or 'main'}")

    for k in range(1, n_t):
        t_end = float(hours_s[k])
        while t_abs < t_end - 1e-9:
            a, _p, b, _y = _states(geom.sections, h)
            dt = _hydro_dt(a, b, qf, dx, par)
            dt = min(dt, t_end - t_abs)
            w = (t_abs + dt - float(hours_s[k - 1])) / max(float(hours_s[k] - hours_s[k - 1]), 1.0)
            w = min(max(w, 0.0), 1.0)
            q_bc = float((1.0 - w) * q_in[k - 1] + w * q_in[k])
            h_bc = float((1.0 - w) * h_down[k - 1] + w * h_down[k])
            hour_now = float(hours[k - 1] + w * (hours[k] - hours[k - 1]))
            q_lat = apply_structures_to_q_lat(
                None, st_runtime, h, hour=hour_now, dt_s=dt
            )
            qf, h = saint_venant_step(
                qf, h, geom, par, q_bc, h_bc, dt, q_lat,
                structures=st_runtime, hour=hour_now,
            )
            t_abs += dt
            dt_used = min(dt_used, dt)
            n_sub_max = max(n_sub_max, int(math.ceil(dt_out_s / max(dt, 1.0))))
        q_node = _node_q(qf, float(q_in[k]))
        h[-1] = max(float(h_down[k]), float(geom.z_bed[-1]) + par.y_min)
        _a, _p, _b, y = _states(geom.sections, h)
        q_out[k] = q_node
        h_out[k] = h
        y_out[k] = y
        if k == 1 or k % 24 == 0 or k == n_t - 1:
            print(
                f"  t = {hours[k]:6.1f} h   Q_ds = {q_node[-1]:8.1f} m3/s   "
                f"H_us = {h[0]:6.2f} m",
                flush=True,
            )

    return q_out, h_out, y_out, float(dt_used), n_sub_max


# ---------------------------------------------------------------------------
# Mang 1D: long chinh + nhanh (nhap luu Q / thoat nuoc H)
# ---------------------------------------------------------------------------

BRANCH_Z_EPS_M = 0.15
WEIR_C_SI = 1.705
OFFTAKE_Q_FRAC_MAX = 0.45


def _mean_end_z(z: np.ndarray, at_end: bool, n: int = 2) -> float:
    z = np.asarray(z, dtype=float)
    n_use = min(max(int(n), 1), int(z.size))
    chunk = z[-n_use:] if at_end else z[:n_use]
    return float(np.mean(chunk))


def classify_branch_role(geom, z_eps_m: float = BRANCH_Z_EPS_M) -> tuple[str, str, float, float]:
    """Phan loai nhanh. Quy uoc geom: nut cuoi = sat long chinh.

    Day nhanh sat long chinh cao hon day phia ngoai -> thoat nuoc (bien H).
    Nguoc lai -> nhap luu (bien Q).
    Bo nut giao (de khong lay day long chinh) va o nodata ~0 o mep DEM.
    """
    z = np.asarray(geom.z_bed, dtype=float)
    n = int(z.size)
    if n >= 4:
        z_near = float(np.mean(z[-3:-1]))
    elif n >= 3:
        z_near = float(z[-2])
    else:
        z_near = float(z[-1])
    if n >= 3 and abs(float(z[0])) < 0.2 and float(z[1]) < -1.0:
        z_far = float(z[1]) if n == 3 else float(np.mean(z[1:min(3, n - 1)]))
    else:
        z_far = _mean_end_z(z, at_end=False)
    if z_near > z_far + float(z_eps_m):
        return "outlet", "H", z_near, z_far
    return "inflow", "Q", z_near, z_far


def flip_reach_geom(geom):
    """Dao chieu long: station_m = 0 tai dau moi, dx/slope tinh lai."""
    length = float(geom.length_m)
    secs = list(reversed(geom.sections))
    for i, sec in enumerate(secs):
        sec.index = i
        sec.station_m = length - float(sec.station_m)
    s0 = float(secs[0].station_m) if secs else 0.0
    for sec in secs:
        sec.station_m = max(0.0, float(sec.station_m) - s0)
    geom.sections = secs
    geom.distance_m = np.array([float(s.station_m) for s in secs], dtype=float)
    geom.z_bed = np.array([float(s.z_bed) for s in secs], dtype=float)
    geom.lon = np.asarray(geom.lon, dtype=float)[::-1].copy()
    geom.lat = np.asarray(geom.lat, dtype=float)[::-1].copy()
    geom.dx_m = np.diff(geom.distance_m)
    geom.slope = np.maximum((-np.diff(geom.z_bed)) / np.maximum(geom.dx_m, 1.0), 0.0)
    return geom


def _is_outlet(item: Any) -> bool:
    kind = item.get("kind") if isinstance(item, dict) else getattr(item, "kind", "inflow")
    return str(kind) == "outlet"


def _trib_join_idx(item: Any) -> int:
    return 0 if _is_outlet(item) else -1


def _trib_outer_idx(item: Any) -> int:
    return -1 if _is_outlet(item) else 0


def _weir_offtake_q(h_main: float, sec, z_sill: float, par: SaintVenantParams, q_cap: float) -> float:
    """Luu luong tach vao nhanh thoat: weir dinh rong, chan [q_min, q_cap]."""
    y = max(float(h_main) - float(z_sill), 0.0)
    qmin = float(par.q_min)
    if y < 0.02:
        return qmin
    _a, _p, b = sec.props(max(float(h_main), float(z_sill) + par.y_min))
    q = WEIR_C_SI * max(float(b), 1.0) * (y ** 1.5)
    return float(np.clip(q, qmin, max(float(q_cap), qmin)))


def _fr() -> dict[str, Any]:
    path = PACKAGE_DIR / "flow_run.py"
    if not path.is_file():
        raise FileNotFoundError(f"Khong thay {path}")
    return runpy.run_path(str(path), run_name="flow_run")


def _dist_point_polyline(
    px: float,
    py: float,
    xs: np.ndarray,
    ys: np.ndarray,
    station: np.ndarray,
) -> tuple[float, float]:
    best_d = float("inf")
    best_s = float(station[0]) if station.size else 0.0
    for i in range(int(xs.size) - 1):
        x0, y0 = float(xs[i]), float(ys[i])
        x1, y1 = float(xs[i + 1]), float(ys[i + 1])
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-12 else (px - x0) * dx + (py - y0) * dy
        t = min(max(t / max(L2, 1e-12), 0.0), 1.0)
        qx, qy = x0 + t * dx, y0 + t * dy
        d = math.hypot(px - qx, py - qy)
        if d < best_d:
            best_d = d
            best_s = float(station[i] + t * (station[i + 1] - station[i]))
    return best_d, best_s


def _main_xy_m(geom) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fr = _fr()
    xs, ys = [], []
    for lo, la in zip(geom.lon, geom.lat):
        x, y = fr["lonlat_to_mercator"](float(lo), float(la))
        xs.append(x)
        ys.append(y)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float), np.asarray(geom.distance_m, dtype=float)


def _row_valley_mins(row: np.ndarray, min_sep: int, k: int = 3) -> list[tuple[int, float, float]]:
    """Cac day thung lung tren 1 hang DEM, cach nhau >= min_sep o."""
    n = int(row.size)
    cands: list[tuple[float, int, float, float]] = []
    for j in range(2, n - 2):
        v = float(row[j])
        if not np.isfinite(v):
            continue
        if not (v <= row[j - 1] and v <= row[j + 1] and v <= row[j - 2] and v <= row[j + 2]):
            continue
        win = row[max(0, j - 10) : min(n, j + 11)]
        bank = float(np.nanmax(win)) if np.isfinite(win).any() else v + 1.0
        depth = bank - v
        if depth < 1.2:
            continue
        cands.append((v - 0.15 * depth, j, v, depth))
    cands.sort()
    picked: list[tuple[int, float, float]] = []
    for _score, j, v, depth in cands:
        if any(abs(j - p[0]) < min_sep for p in picked):
            continue
        picked.append((j, v, depth))
        if len(picked) >= k:
            break
    return picked


def _smooth_series(vals: np.ndarray, k: int = 7) -> np.ndarray:
    k = max(3, int(k) | 1)
    pad = k // 2
    kernel = np.ones(k) / k
    return np.convolve(np.pad(vals.astype(float), pad, mode="edge"), kernel, mode="valid")[: vals.size]


def _valley_tracks_from_dem(src, f3, min_sep_m: float = 1400.0, max_dim: int = 400) -> list[dict[str, Any]]:
    """Theo doi cac long song rieng (khong noi hai long o cung vi do thanh 1 thalweg)."""
    scale = max(src.width, src.height) / float(max(32, max_dim))
    out_w = max(8, int(round(src.width / max(scale, 1.0))))
    out_h = max(8, int(round(src.height / max(scale, 1.0))))
    data = src.read(1, out_shape=(out_h, out_w), resampling=f3.Resampling.average).astype(float)
    nodata = src.nodata
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    z = data.astype(float)
    z[invalid] = np.nan
    xres = float(src.res[0]) * (src.width / out_w)
    yres = abs(float(src.res[1])) * (src.height / out_h)
    transform = f3.Affine(xres, 0, src.bounds.left, 0, -yres, src.bounds.top)
    min_sep = max(10, int(round(min_sep_m / max(xres, 1.0))))

    rows_mins = [_row_valley_mins(z[i], min_sep, k=3) if np.isfinite(z[i]).any() else [] for i in range(out_h)]
    tracks: list[list[tuple[int, int, float, float]]] = []
    last: list[tuple[int, int]] = []

    def can_extend(ti: int, i: int, j: int) -> bool:
        li, lj = last[ti]
        if i - li > 8:
            return False
        return abs(j - lj) <= min_sep + 4

    for i, mins in enumerate(rows_mins):
        used: set[int] = set()
        order = sorted(range(len(tracks)), key=lambda t: -len(tracks[t]))
        for ti in order:
            best = None
            for mi, item in enumerate(mins):
                if mi in used or not can_extend(ti, i, item[0]):
                    continue
                dist = abs(item[0] - last[ti][1]) + 0.15 * abs(item[1] - tracks[ti][-1][2])
                if best is None or dist < best[0]:
                    best = (dist, mi, item)
            if best is None:
                continue
            _d, mi, item = best
            used.add(mi)
            tracks[ti].append((i, item[0], item[1], item[2]))
            last[ti] = (i, item[0])
        for mi, item in enumerate(mins):
            if mi in used:
                continue
            tracks.append([(i, item[0], item[1], item[2])])
            last.append((i, item[0]))

    out: list[dict[str, Any]] = []
    for tr in tracks:
        if len(tr) < 18:
            continue
        rows = np.array([p[0] for p in tr], dtype=float)
        cols = _smooth_series(np.array([p[1] for p in tr], dtype=float), k=7)
        zs = np.array([p[2] for p in tr], dtype=float)
        depths = np.array([p[3] for p in tr], dtype=float)
        xs, ys = [], []
        for r, c in zip(rows, cols):
            x, y = f3.xy(transform, int(round(r)), int(round(float(np.clip(c, 0, out_w - 1)))), offset="center")
            xs.append(float(x))
            ys.append(float(y))
        lon, lat = f3.dem_xy_to_lonlat(src, np.asarray(xs), np.asarray(ys))
        out.append(
            {
                "lon": np.asarray(lon, dtype=float),
                "lat": np.asarray(lat, dtype=float),
                "z": zs,
                "depth": depths,
                "n": len(tr),
            }
        )
    return out


def _track_main_distances(
    lon: np.ndarray, lat: np.ndarray, mx: np.ndarray, my: np.ndarray, ms: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    dists = np.empty(lon.size, dtype=float)
    stations = np.empty(lon.size, dtype=float)
    prev = None
    length = 0.0
    for i, (lo, la) in enumerate(zip(lon, lat)):
        x, y = lonlat_to_mercator(float(lo), float(la))
        d, s = _dist_point_polyline(x, y, mx, my, ms)
        dists[i] = d
        stations[i] = s
        if prev is not None:
            length += math.hypot(x - prev[0], y - prev[1])
        prev = (x, y)
    return dists, stations, length


def _crop_track_to_join(
    lon: np.ndarray,
    lat: np.ndarray,
    dists: np.ndarray,
    stations: np.ndarray,
    i_join: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Giu nganh tu nut giao den dau xa (bo phan vuot qua long chinh)."""
    n = int(lon.size)
    i_join = int(np.clip(i_join, 0, max(n - 1, 0)))
    left_n = i_join + 1
    right_n = n - i_join
    if right_n >= left_n and right_n >= 5:
        sl = slice(i_join, n)
    else:
        sl = slice(0, i_join + 1)
    return lon[sl], lat[sl], dists[sl], stations[sl]


def _attach_track_to_main(
    lon: np.ndarray,
    lat: np.ndarray,
    dists: np.ndarray,
    stations: np.ndarray,
    main_geom,
) -> tuple[np.ndarray, np.ndarray]:
    """Dat dau gan long chinh dung len polyline Hong."""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    at_start = float(dists[0]) <= float(dists[-1])
    s = float(stations[0] if at_start else stations[-1])
    jlon, jlat = _join_lonlat_on_main(main_geom, s)
    i = 0 if at_start else -1
    tlon, tlat = float(lon[i]), float(lat[i])
    mx = 111000.0 * math.cos(math.radians(0.5 * (tlat + jlat)))
    gap = math.hypot((tlon - jlon) * mx, (tlat - jlat) * 111000.0)
    if gap < 12.0:
        out_lon, out_lat = lon.copy(), lat.copy()
        out_lon[i], out_lat[i] = jlon, jlat
        return out_lon, out_lat
    if at_start:
        return np.concatenate([[jlon], lon]), np.concatenate([[jlat], lat])
    return np.concatenate([lon, [jlon]]), np.concatenate([lat, [jlat]])


def _orient_trib_geom(geom, mx, my, ms, kind: str):
    """Thoat: [0]=nut giao, [-1]=ha luu (bien H). Nhap luu: nguoc lai."""
    gx, gy, _gs = _main_xy_m(geom)
    d0, s0 = _dist_point_polyline(float(gx[0]), float(gy[0]), mx, my, ms)
    d1, s1 = _dist_point_polyline(float(gx[-1]), float(gy[-1]), mx, my, ms)
    join_at_start = d0 <= d1
    if kind == "outlet":
        if not join_at_start:
            geom = flip_reach_geom(geom)
            join_s = float(s1)
        else:
            join_s = float(s0)
    else:
        if join_at_start:
            geom = flip_reach_geom(geom)
            join_s = float(s0)
        else:
            join_s = float(s1)
    return geom, join_s


def _trib_end_z(geom, kind: str) -> tuple[float, float]:
    z = np.asarray(geom.z_bed, dtype=float)
    n = max(1, min(3, int(z.size)))
    z_start = float(np.mean(z[:n]))
    z_end = float(np.mean(z[-n:]))
    if kind == "outlet":
        return z_start, z_end
    return z_end, z_start


TRIB_XS_SPACING_M = 500.0


def extract_tributaries(
    dem_path: Path,
    main_geom,
    par: SaintVenantParams,
    max_tribs: int = 1,
    min_trib_m: float = 2000.0,
    trib_half_m: float = 600.0,
    join_max_m: float = 2500.0,
    water_source: str = "saint-venant",
) -> list[dict[str, Any]]:
    """Mot nhanh lon: Song Duong tach ve dong, thoat nuoc khoi Song Hong (bien H)."""
    dem = resolve_dem_path(None, str(dem_path))
    mx, my, ms = _main_xy_m(main_geom)
    trib_spacing = max(50.0, float(getattr(par, "xs_spacing_m", 0.0) or TRIB_XS_SPACING_M))
    trib_par = _channel_params(
        copy_params(par, xs_spacing_m=trib_spacing)
    )
    n_main = len(main_geom.sections)
    candidates: list[dict[str, Any]] = []
    with open_dem(dem) as src:
        tracks = tributary_centerlines(src, water_source, main_geom=main_geom)
        for tr in tracks:
            lon = np.asarray(tr["lon"], dtype=float)
            lat = np.asarray(tr["lat"], dtype=float)
            dists, stations, length = _track_main_distances(lon, lat, mx, my, ms)
            if length < min_trib_m:
                continue
            # Walker da cat ngang vao Hong: giu dau [0], khong cat xuoi ha luu.
            if float(dists[0]) <= 90.0 or float(dists[0]) <= float(dists[-1]) + 40.0:
                i_join = 0
            else:
                i_join = int(np.argmin(dists))
            d_join = float(dists[i_join])
            if d_join > join_max_m:
                continue
            if i_join > 0:
                lon, lat, dists, stations = _crop_track_to_join(lon, lat, dists, stations, i_join)
            if lon.size < 5:
                continue
            plon, plat = _project_on_main(float(lon[0]), float(lat[0]), main_geom.lon, main_geom.lat)
            if _gap_m(float(lon[0]), float(lat[0]), plon, plat) >= 12.0:
                lon = np.concatenate([[plon], lon])
                lat = np.concatenate([[plat], lat])
            else:
                lon = lon.copy()
                lat = lat.copy()
                lon[0], lat[0] = plon, plat
            try:
                geom = extract_reach_from_lonlat(
                    src,
                    lon,
                    lat,
                    trib_par,
                    half_width_m=trib_half_m,
                    ensure_downhill=False,
                )
            except Exception:
                continue
            if len(geom.sections) < 3 or float(geom.length_m) < min_trib_m:
                continue
            # Song Duong: nhanh thoat (phan luu). Bien H o dau xa = ha luu.
            kind, bc = "outlet", "H"
            geom, join_s = _orient_trib_geom(geom, mx, my, ms, kind)
            geom.lon = np.asarray(geom.lon, dtype=float).copy()
            geom.lat = np.asarray(geom.lat, dtype=float).copy()
            jlon, jlat = _project_on_main(float(geom.lon[0]), float(geom.lat[0]), main_geom.lon, main_geom.lat)
            slid = _join_lonlat_on_main(main_geom, join_s)
            if _gap_m(jlon, jlat, float(slid[0]), float(slid[1])) > 80.0:
                gx, gy, gs = _main_xy_m(main_geom)
                _d, join_s = _dist_point_polyline(
                    *lonlat_to_mercator(jlon, jlat), gx, gy, gs
                )
            else:
                jlon, jlat = float(slid[0]), float(slid[1])
            geom.lon[0], geom.lat[0] = jlon, jlat
            if geom.sections:
                geom.sections[0].lon = float(jlon)
                geom.sections[0].lat = float(jlat)
            join_xs = int(np.argmin(np.abs(np.asarray(main_geom.distance_m) - join_s)))
            join_xs = min(max(join_xs, 1), n_main - 2)
            depth = tr.get("depth")
            if depth is not None:
                darr = np.asarray(depth, dtype=float)
                mean_depth = float(np.nanmean(darr)) if darr.size and np.isfinite(darr).any() else 4.0
            else:
                mean_depth = 4.0
            z_near, z_far = _trib_end_z(geom, kind)
            q_frac = 0.0 if kind == "outlet" else float(
                np.clip(0.18 + 0.08 * (float(geom.length_m) / 8000.0), 0.15, 0.28)
            )
            score = float(geom.length_m) * max(mean_depth, 1.0)
            candidates.append(
                {
                    "geom": geom,
                    "join_station_m": join_s,
                    "join_xs": join_xs,
                    "q_frac": q_frac,
                    "kind": kind,
                    "bc": bc,
                    "z_near": z_near,
                    "z_far": z_far,
                    "length_m": float(geom.length_m),
                    "acc_head": 0.0,
                    "lon": np.asarray(geom.lon, dtype=float),
                    "lat": np.asarray(geom.lat, dtype=float),
                    "_score": score,
                }
            )
    candidates.sort(key=lambda t: t["_score"], reverse=True)
    found: list[dict[str, Any]] = []
    seen_joins: list[float] = []
    for t in candidates:
        if len(found) >= max(0, int(max_tribs)):
            break
        if any(abs(t["join_station_m"] - s0) < max(par.xs_spacing_m * 0.8, 800.0) for s0 in seen_joins):
            continue
        seen_joins.append(float(t["join_station_m"]))
        t.pop("_score", None)
        found.append(t)
    found.sort(key=lambda t: t["join_station_m"])
    total_frac = sum(t["q_frac"] for t in found if not _is_outlet(t))
    if total_frac > 0.45:
        scale = 0.45 / total_frac
        for t in found:
            if not _is_outlet(t):
                t["q_frac"] *= scale
    for i, t in enumerate(found, start=1):
        t["reach_id"] = f"trib_{i}"
    return found


def extract_extra_mains(
    dem_path: Path,
    main_geom,
    par: SaintVenantParams,
    max_extra: int = 0,
    min_len_m: float = 5000.0,
) -> list[dict[str, Any]]:
    """Them song chinh doc lap (khong noi vao long chinh thu nhat)."""
    from flood_model.river_network import extra_valley_centerlines

    n_extra = max(0, int(max_extra))
    if n_extra <= 0:
        return []
    dem = resolve_dem_path(None, str(dem_path))
    ch_par = _channel_params(par)
    out: list[dict[str, Any]] = []
    with open_dem(dem) as src:
        tracks = extra_valley_centerlines(
            src,
            getattr(main_geom, "lon"),
            getattr(main_geom, "lat"),
            max_count=n_extra,
            min_len_m=min_len_m,
        )
        for i, tr in enumerate(tracks, start=2):
            try:
                geom = extract_reach_from_lonlat(src, tr["lon"], tr["lat"], ch_par)
            except Exception:
                continue
            if geom is None or not getattr(geom, "sections", None) or len(geom.sections) < 3:
                continue
            if float(geom.length_m) < min_len_m:
                continue
            out.append(
                {
                    "reach_id": f"main_{i}",
                    "geom": geom,
                    "length_m": float(geom.length_m),
                }
            )
    return out


DEFAULT_MAX_MAINS = 8
DEFAULT_MAX_TRIBS = 12


def parse_max_mains(raw: Any, default: int = DEFAULT_MAX_MAINS) -> int:
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        v = int(default)
    return max(1, min(12, v))


def parse_max_tribs(raw: Any, default: int = DEFAULT_MAX_TRIBS) -> int:
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        v = int(default)
    return max(0, min(24, v))


def extract_model_network(
    dem_path: Path,
    par: SaintVenantParams,
    *,
    max_mains: int = DEFAULT_MAX_MAINS,
    max_tribs: int = DEFAULT_MAX_TRIBS,
    water_source: str = "saint-venant",
    no_network: bool = False,
    n_default: float | None = None,
    n_csv: Path | None = None,
    min_trib_m: float = 2000.0,
    trib_half_m: float = 600.0,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Trich 1..n song chinh va 0..m song nhanh theo khoang XS cua par."""
    n_use = float(n_default if n_default is not None else par.manning_n)
    geom = extract_river(dem_path, _channel_params(par))
    n_by_id, n_st, n_along = {}, np.array([], dtype=float), np.array([], dtype=float)
    if n_csv is not None and csv_available(n_csv):
        n_by_id, n_st, n_along = load_manning_n_table(n_csv, reach_id="main")
    apply_manning_n(geom, n_use, n_by_id, n_st, n_along)

    extra: list[dict[str, Any]] = []
    n_main_want = parse_max_mains(max_mains)
    if n_main_want > 1:
        extra = extract_extra_mains(dem_path, geom, par, max_extra=n_main_want - 1)
        for spec in extra:
            nb, ns, na = {}, np.array([], dtype=float), np.array([], dtype=float)
            if n_csv is not None and csv_available(n_csv):
                nb, ns, na = load_manning_n_table(n_csv, reach_id=str(spec["reach_id"]))
            apply_manning_n(spec["geom"], n_use, nb, ns, na)

    trib_specs: list[dict[str, Any]] = []
    n_trib_want = 0 if no_network else parse_max_tribs(max_tribs)
    if n_trib_want > 0:
        trib_specs = extract_tributaries(
            dem_path,
            geom,
            par,
            max_tribs=n_trib_want,
            min_trib_m=float(min_trib_m),
            trib_half_m=float(trib_half_m),
            water_source=water_source,
        )
        for spec in trib_specs:
            n_join = n_use
            join_km = float(spec["join_station_m"]) / 1000.0
            if n_st.size >= 2 and n_along.size == n_st.size:
                n_join = float(np.interp(join_km, n_st, n_along))
            elif n_by_id:
                n_join = float(next(iter(n_by_id.values())))
            apply_manning_n(spec["geom"], n_join)
    return geom, extra, trib_specs


def route_extra_main_reaches(
    hours: np.ndarray,
    q_in: np.ndarray,
    h_down: np.ndarray,
    extra_specs: Sequence[dict[str, Any]],
    par: SaintVenantParams,
    router,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
) -> list[ExtraMainResult]:
    """Giai 1D tung song chinh doc lap (cung bien Q/H)."""
    out: list[ExtraMainResult] = []
    for spec in extra_specs:
        rid = str(spec.get("reach_id") or "main_2")
        geom = spec["geom"]
        print(
            f"Dang giai song chinh {rid}  ({float(spec.get('length_m') or geom.length_m)/1000.0:.2f} km)...",
            flush=True,
        )
        z_ds = float(geom.z_bed[-1])
        h_e = np.maximum(np.asarray(h_down, dtype=float), z_ds + float(par.y_min))
        try:
            qe, he, ye, _dt, _ns, *_rest = router(
                hours, q_in, h_e, geom, par, q0_by_id, h0_by_id, reach_id=rid
            )
        except TypeError:
            qe, he, ye, _dt, _ns, *_rest = router(
                hours, q_in, h_e, geom, par, q0_by_id, h0_by_id
            )
        out.append(
            ExtraMainResult(
                reach_id=rid,
                geom=geom,
                q=qe,
                h=he,
                y=ye,
                length_m=float(spec.get("length_m") or geom.length_m),
                lon=np.asarray(geom.lon, dtype=float),
                lat=np.asarray(geom.lat, dtype=float),
            )
        )
    return out


def route_network(
    hours: np.ndarray,
    q_tank: np.ndarray,
    h_down: np.ndarray,
    main_geom,
    tribs: list[dict[str, Any]],
    par: SaintVenantParams,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
    h_outlet: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, int, np.ndarray, list[TribResult]]:
    """Long chinh + nhanh.

    Nhap luu: [0]=dau xa (bien Q), [-1]=nut (H = H_chinh); q_lat += Q_join.
    Thoat nuoc: [0]=nut (Q weir tu H_chinh), [-1]=dau xa (bien H = h_na1); q_lat -= Q_up.
    """
    n_t = int(hours.size)
    frac_sum = float(sum(float(t["q_frac"]) for t in tribs if not _is_outlet(t)))
    q_main_up = np.maximum(q_tank * max(1.0 - frac_sum, 0.08), par.q_min)
    z_main_ds = float(main_geom.z_bed[-1])
    depth_ds = np.maximum(np.asarray(h_down, dtype=float) - z_main_ds, par.y_min)
    h_out_bc = None if h_outlet is None else np.asarray(h_outlet, dtype=float)

    trib_qin = []
    trib_qmin = []
    trib_h_outer = []
    for t in tribs:
        outlet = _is_outlet(t)
        if outlet:
            qmin = max(0.4, par.q_min * 0.05)
            trib_qin.append(np.zeros(n_t, dtype=float))
            z_far = float(t["geom"].z_bed[-1])
            if h_out_bc is not None and h_out_bc.size == n_t:
                trib_h_outer.append(np.maximum(h_out_bc, z_far + par.y_min))
            else:
                trib_h_outer.append(z_far + depth_ds)
        else:
            qmin = max(0.4, par.q_min * max(float(t["q_frac"]), 0.05))
            trib_qin.append(np.maximum(q_tank * float(t["q_frac"]), qmin))
            trib_h_outer.append(None)
        trib_qmin.append(qmin)

    n_x_m = int(main_geom.distance_m.size)
    q_out = np.zeros((n_t, n_x_m), dtype=float)
    h_out = np.zeros((n_t, n_x_m), dtype=float)
    y_out = np.zeros((n_t, n_x_m), dtype=float)

    qf_m, h_m, _y_m, q_node_m = _init_state(
        main_geom, par, float(q_main_up[0]), float(h_down[0]), q0_by_id, h0_by_id
    )
    q_out[0] = q_node_m
    h_out[0] = h_m
    y_out[0] = h_m - main_geom.z_bed

    trib_states = []
    trib_res_q = []
    trib_res_h = []
    for t, q_in_t, qmin, h_outer in zip(tribs, trib_qin, trib_qmin, trib_h_outer):
        tpar = copy_params(par, q_min=qmin)
        join = int(t["join_xs"])
        geom = t["geom"]
        if _is_outlet(t):
            q_cap = OFFTAKE_Q_FRAC_MAX * float(q_main_up[0])
            q_up0 = _weir_offtake_q(
                float(h_m[join]), geom.sections[0], float(geom.z_bed[0]), tpar, q_cap
            )
            q_in_t[0] = q_up0
            qf, h, _y, q_node = _init_state(geom, tpar, q_up0, float(h_outer[0]))
        else:
            qf, h, _y, q_node = _init_state(geom, tpar, float(q_in_t[0]), float(h_m[join]))
        n_x = int(geom.distance_m.size)
        tq = np.zeros((n_t, n_x), dtype=float)
        th = np.zeros((n_t, n_x), dtype=float)
        tq[0], th[0] = q_node, h
        trib_states.append(
            {
                "qf": qf,
                "h": h,
                "par": tpar,
                "geom": geom,
                "join": join,
                "outlet": _is_outlet(t),
                "h_outer": h_outer,
            }
        )
        trib_res_q.append(tq)
        trib_res_h.append(th)

    dx_m = np.maximum(np.asarray(main_geom.dx_m, dtype=float), 1.0)
    dt_out_s = float(par.dt_hours) * 3600.0
    dt_used = par.dt_hydro_max_s
    n_sub_max = 1
    t_abs = float(hours[0]) * 3600.0
    hours_s = hours * 3600.0
    main_st = init_structure_runtime("main", main_geom)
    log_bound_structures(main_st, label="SV:main")
    trib_st = []
    for t in tribs:
        rid = str(t.get("reach_id") or "trib_1")
        rt = init_structure_runtime(rid, t["geom"])
        log_bound_structures(rt, label=f"SV:{rid}")
        trib_st.append(rt)

    for k in range(1, n_t):
        t_end = float(hours_s[k])
        q_up_last = [float(trib_qin[i][k - 1]) for i in range(len(tribs))]
        while t_abs < t_end - 1e-9:
            a, _p, b, _y = _states(main_geom.sections, h_m)
            dt = _hydro_dt(a, b, qf_m, dx_m, par)
            for st in trib_states:
                ta, _tp, tb, _ty = _states(st["geom"].sections, st["h"])
                tdx = np.maximum(np.asarray(st["geom"].dx_m, dtype=float), 1.0)
                dt = min(dt, _hydro_dt(ta, tb, st["qf"], tdx, st["par"]))
            dt = min(dt, t_end - t_abs)
            w = (t_abs + dt - float(hours_s[k - 1])) / max(float(hours_s[k] - hours_s[k - 1]), 1.0)
            w = min(max(w, 0.0), 1.0)
            q_bc = float((1.0 - w) * q_main_up[k - 1] + w * q_main_up[k])
            h_bc = float((1.0 - w) * h_down[k - 1] + w * h_down[k])
            hour_now = float(hours[k - 1] + w * (hours[k] - hours[k - 1]))
            q_lat = np.zeros(n_x_m, dtype=float)
            for i, st in enumerate(trib_states):
                h_j = float(h_m[st["join"]])
                if st["outlet"]:
                    q_cap = OFFTAKE_Q_FRAC_MAX * max(q_bc, par.q_min)
                    q_t = _weir_offtake_q(
                        h_j,
                        st["geom"].sections[0],
                        float(st["geom"].z_bed[0]),
                        st["par"],
                        q_cap,
                    )
                    h_ds_t = float((1.0 - w) * st["h_outer"][k - 1] + w * st["h_outer"][k])
                    q_lat_t = apply_structures_to_q_lat(
                        None, trib_st[i], st["h"], hour=hour_now, dt_s=dt
                    )
                    st["qf"], st["h"] = saint_venant_step(
                        st["qf"], st["h"], st["geom"], st["par"], q_t, h_ds_t, dt, q_lat_t,
                        structures=trib_st[i], hour=hour_now,
                    )
                    q_lat[st["join"]] -= q_t
                    q_up_last[i] = q_t
                else:
                    q_t = float((1.0 - w) * trib_qin[i][k - 1] + w * trib_qin[i][k])
                    q_lat_t = apply_structures_to_q_lat(
                        None, trib_st[i], st["h"], hour=hour_now, dt_s=dt
                    )
                    st["qf"], st["h"] = saint_venant_step(
                        st["qf"], st["h"], st["geom"], st["par"], q_t, h_j, dt, q_lat_t,
                        structures=trib_st[i], hour=hour_now,
                    )
                    q_lat[st["join"]] += float(st["qf"][-1])
            q_lat = apply_structures_to_q_lat(
                q_lat, main_st, h_m, hour=hour_now, dt_s=dt
            )
            qf_m, h_m = saint_venant_step(
                qf_m, h_m, main_geom, par, q_bc, h_bc, dt, q_lat,
                structures=main_st, hour=hour_now,
            )
            t_abs += dt
            dt_used = min(dt_used, dt)
            n_sub_max = max(n_sub_max, int(math.ceil(dt_out_s / max(dt, 1.0))))

        q_node_m = _node_q(qf_m, float(q_main_up[k]))
        h_m[-1] = max(float(h_down[k]), float(main_geom.z_bed[-1]) + par.y_min)
        h_m = np.maximum(h_m, float(h_down[k]))
        _a, _p, _b, y_m = _states(main_geom.sections, h_m)
        q_out[k] = q_node_m
        h_out[k] = h_m
        y_out[k] = y_m
        for i, st in enumerate(trib_states):
            if st["outlet"]:
                trib_qin[i][k] = q_up_last[i]
                tq = _node_q(st["qf"], float(trib_qin[i][k]))
                st["h"][-1] = max(float(st["h_outer"][k]), float(st["geom"].z_bed[-1]) + par.y_min)
            else:
                tq = _node_q(st["qf"], float(trib_qin[i][k]))
                st["h"][-1] = max(float(h_m[st["join"]]), float(st["geom"].z_bed[-1]) + par.y_min)
            trib_res_q[i][k] = tq
            trib_res_h[i][k] = st["h"]
        if k == 1 or k % 24 == 0 or k == n_t - 1:
            extra = "  ".join(
                f"{tribs[i]['reach_id']} {trib_res_q[i][k, _trib_join_idx(tribs[i])]:.0f}"
                for i in range(len(tribs))
            )
            print(
                f"  t = {hours[k]:6.1f} h   Q_ds = {q_node_m[-1]:8.1f} m3/s   "
                f"H_us = {h_m[0]:6.2f} m   {extra}",
                flush=True,
            )

    trib_out: list[TribResult] = []
    for i, t in enumerate(tribs):
        trib_out.append(
            TribResult(
                reach_id=str(t["reach_id"]),
                geom=t["geom"],
                q=trib_res_q[i],
                h=trib_res_h[i],
                q_in=trib_qin[i],
                join_station_m=float(t["join_station_m"]),
                join_xs=int(t["join_xs"]),
                q_frac=float(t["q_frac"]),
                length_m=float(t["length_m"]),
                lon=np.asarray(t["lon"], dtype=float),
                lat=np.asarray(t["lat"], dtype=float),
                kind=str(t.get("kind", "inflow")),
                bc=str(t.get("bc", "Q")),
                z_near=float(t.get("z_near", 0.0)),
                z_far=float(t.get("z_far", 0.0)),
            )
        )
    return q_out, h_out, y_out, float(dt_used), n_sub_max, q_main_up, trib_out


# ---------------------------------------------------------------------------
# Xuat file / bieu do
# ---------------------------------------------------------------------------

def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    write_csv_rows(path, fieldnames, rows)


def write_geometry_csv(geom, par: SaintVenantParams, path: Path, reach_id: str = "main") -> None:
    write_geometries_csv([(reach_id, geom)], par, path)


def write_geometries_csv(
    items: Sequence[tuple[str, Any]], par: SaintVenantParams, path: Path
) -> None:
    rows: list[dict[str, str]] = []
    for reach_id, geom in items:
        rid = str(reach_id or "main").strip() or "main"
        for i, sec in enumerate(geom.sections):
            _a, _p, b = sec.props(sec.z_bed + 3.0)
            rows.append(
                {
                    "reach_id": rid,
                    "xs_id": str(i + 1),
                    "station_km": f"{sec.station_m / 1000.0:.4f}",
                    "z_bed_m": f"{sec.z_bed:.4f}",
                    "dx_m": f"{geom.dx_m[i]:.2f}" if i < geom.dx_m.size else "",
                    "slope": f"{geom.slope[i]:.8f}" if i < geom.slope.size else "",
                    "top_width_at_3m": f"{b:.1f}",
                    "manning_n": f"{section_n(sec, par.manning_n):.4f}",
                    "lon": f"{sec.lon:.6f}",
                    "lat": f"{sec.lat:.6f}",
                }
            )
    write_csv(
        path,
        [
            "reach_id",
            "xs_id",
            "station_km",
            "z_bed_m",
            "dx_m",
            "slope",
            "top_width_at_3m",
            "manning_n",
            "lon",
            "lat",
        ],
        rows,
    )


def write_result_csv(res: RouteResult, path: Path) -> None:
    idx = np.arange(res.q.shape[1])
    fields = ["hour", "q_in_m3s", "h_down_m"]
    for i in idx:
        km = res.station_km[i]
        fields += [f"q_xs{i+1}_{km:.1f}km_m3s", f"h_xs{i+1}_{km:.1f}km_m"]
    rows = []
    for t in range(res.hours.size):
        row = {
            "hour": f"{res.hours[t]:.2f}",
            "q_in_m3s": f"{res.q_in[t]:.4f}",
            "h_down_m": f"{res.h_down[t]:.4f}",
        }
        for i in idx:
            km = res.station_km[i]
            row[f"q_xs{i+1}_{km:.1f}km_m3s"] = f"{res.q[t, i]:.4f}"
            row[f"h_xs{i+1}_{km:.1f}km_m"] = f"{res.h[t, i]:.4f}"
        rows.append(row)
    write_csv(path, fields, rows)


def write_network_reaches_csv(res: RouteResult, path: Path) -> None:
    rows = [
        {
            "reach_id": "main",
            "kind": "main",
            "bc": "Q/H",
            "n_xs": len(res.geom.sections),
            "length_km": f"{res.geom.length_m / 1000.0:.3f}",
            "join_station_km": "",
            "join_xs": "",
            "q_frac": f"{(1.0 if res.q_main_up is None else float(np.mean(res.q_main_up) / max(float(np.mean(res.q_in)), 1e-6))):.4f}",
            "z_near_m": "",
            "z_far_m": "",
        }
    ]
    for em in res.extra_mains or []:
        rows.append(
            {
                "reach_id": em.reach_id,
                "kind": "main",
                "bc": "Q/H",
                "n_xs": len(em.geom.sections),
                "length_km": f"{em.length_m / 1000.0:.3f}",
                "join_station_km": "",
                "join_xs": "",
                "q_frac": "1.0000",
                "z_near_m": "",
                "z_far_m": "",
            }
        )
    for t in res.tribs or []:
        rows.append(
            {
                "reach_id": t.reach_id,
                "kind": t.kind,
                "bc": t.bc,
                "n_xs": len(t.geom.sections),
                "length_km": f"{t.length_m / 1000.0:.3f}",
                "join_station_km": f"{t.join_station_m / 1000.0:.3f}",
                "join_xs": t.join_xs + 1,
                "q_frac": f"{t.q_frac:.4f}",
                "z_near_m": f"{t.z_near:.3f}",
                "z_far_m": f"{t.z_far:.3f}",
            }
        )
    write_csv(
        path,
        [
            "reach_id",
            "kind",
            "bc",
            "n_xs",
            "length_km",
            "join_station_km",
            "join_xs",
            "q_frac",
            "z_near_m",
            "z_far_m",
        ],
        rows,
    )


def write_tributary_geometry_csv(res: RouteResult, path: Path) -> None:
    rows = []
    for t in res.tribs or []:
        for i, sec in enumerate(t.geom.sections):
            rows.append(
                {
                    "reach_id": t.reach_id,
                    "xs_id": i + 1,
                    "station_km": f"{sec.station_m / 1000.0:.4f}",
                    "z_bed_m": f"{sec.z_bed:.4f}",
                    "lon": f"{sec.lon:.6f}",
                    "lat": f"{sec.lat:.6f}",
                    "join_station_km": f"{t.join_station_m / 1000.0:.4f}",
                    "join_xs_main": t.join_xs + 1,
                    "kind": t.kind,
                    "bc": t.bc,
                }
            )
    write_csv(
        path,
        [
            "reach_id",
            "xs_id",
            "station_km",
            "z_bed_m",
            "lon",
            "lat",
            "join_station_km",
            "join_xs_main",
            "kind",
            "bc",
        ],
        rows,
    )


def publish_live_duong_geometry(water_source: str = "saint-venant") -> dict[str, Any]:
    """Ghi hinh hoc nhanh walker (cat ngang Hong) vao data_flood, khong can chay lai 1D."""
    from flood_model import cross_section as _cross_section
    from flood_model.river_network import live_duong_centerlines
    from flood_model.routing import hydro1d_csv_paths, parse_hydro1d_source

    profile_from_lonlat = getattr(_cross_section, "profile_from_lonlat")

    kind = parse_hydro1d_source(water_source, respect_force=False)
    out_paths = [
        Path(hydro1d_csv_paths("saint-venant")["trib_geom"]),
        Path(hydro1d_csv_paths("saint-venant-1d")["trib_geom"]),
    ]
    dem = resolve_dem_path("dem")
    with open_dem(dem) as src:
        main = _centerline_from_csv_or_dem(src, kind)
        tracks = live_duong_centerlines(src, kind, main_geom=main)
        if not tracks:
            raise RuntimeError("Khong lay duoc long Song Duong tu DEM")
        tr = tracks[0]
        lon = np.asarray(tr["lon"], dtype=float)
        lat = np.asarray(tr["lat"], dtype=float)
        prof = profile_from_lonlat(src, lon, lat, step_m=TRIB_XS_SPACING_M)
    mx, my, ms = _main_xy_m(main)
    gx0, gy0 = lonlat_to_mercator(float(prof["lon"][0]), float(prof["lat"][0]))
    _d, join_s = _dist_point_polyline(gx0, gy0, mx, my, ms)
    join_xs = int(np.argmin(np.abs(np.asarray(main.distance_m) - join_s)))
    join_xs = min(max(join_xs, 1), max(int(np.asarray(main.distance_m).size) - 2, 1))
    rows = []
    for i, (lo, la, st, zb) in enumerate(
        zip(prof["lon"], prof["lat"], prof["distance_m"], prof["z_bed"])
    ):
        rows.append(
            {
                "reach_id": "trib_1",
                "xs_id": i + 1,
                "station_km": f"{float(st) / 1000.0:.4f}",
                "z_bed_m": f"{float(zb):.4f}",
                "lon": f"{float(lo):.6f}",
                "lat": f"{float(la):.6f}",
                "join_station_km": f"{float(join_s) / 1000.0:.4f}",
                "join_xs_main": join_xs + 1,
                "kind": "outlet",
                "bc": "H",
            }
        )
    fields = [
        "reach_id", "xs_id", "station_km", "z_bed_m", "lon", "lat",
        "join_station_km", "join_xs_main", "kind", "bc",
    ]
    for dest in out_paths:
        write_csv(dest, fields, rows)
    try:
        from flood_model.flow_3d import _clear_hydro_caches

        _clear_hydro_caches()
    except Exception:
        traceback.print_exc()
    return {
        "n": len(rows),
        "start": (float(prof["lon"][0]), float(prof["lat"][0])),
        "end": (float(prof["lon"][-1]), float(prof["lat"][-1])),
        "join_station_m": float(join_s),
        "path": str(out_paths[0] if kind != "saint-venant-1d" else out_paths[1]),
    }


def _centerline_from_csv_or_dem(src, kind: str):
    from flood_model.river_network import _centerline_from_dem, _centerline_from_geom_csv, _hydro1d_files

    main = _centerline_from_geom_csv(_hydro1d_files(kind)["geom"])
    if main is None:
        main = _centerline_from_dem(src)
    return main


def write_tributary_result_csv(res: RouteResult, path: Path) -> None:
    if not res.tribs:
        return
    fields = ["hour"]
    for t in res.tribs:
        fields += [
            f"{t.reach_id}_q_up_m3s",
            f"{t.reach_id}_q_join_m3s",
            f"{t.reach_id}_h_join_m",
            f"{t.reach_id}_q_outer_m3s",
            f"{t.reach_id}_h_outer_m",
        ]
    rows = []
    for k, hr in enumerate(res.hours):
        row = {"hour": f"{hr:.2f}"}
        for t in res.tribs:
            ji, oi = _trib_join_idx(t), _trib_outer_idx(t)
            row[f"{t.reach_id}_q_up_m3s"] = f"{t.q_in[k]:.4f}"
            row[f"{t.reach_id}_q_join_m3s"] = f"{t.q[k, ji]:.4f}"
            row[f"{t.reach_id}_h_join_m"] = f"{t.h[k, ji]:.4f}"
            row[f"{t.reach_id}_q_outer_m3s"] = f"{t.q[k, oi]:.4f}"
            row[f"{t.reach_id}_h_outer_m"] = f"{t.h[k, oi]:.4f}"
        rows.append(row)
    write_csv(path, fields, rows)


def write_extra_main_result_csv(res: RouteResult, path: Path) -> None:
    extras = list(res.extra_mains or [])
    if not extras:
        return
    fields = ["hour"]
    for em in extras:
        fields += [
            f"{em.reach_id}_q_up_m3s",
            f"{em.reach_id}_q_ds_m3s",
            f"{em.reach_id}_h_us_m",
            f"{em.reach_id}_h_ds_m",
        ]
    rows = []
    for k, hr in enumerate(res.hours):
        row = {"hour": f"{hr:.2f}"}
        for em in extras:
            row[f"{em.reach_id}_q_up_m3s"] = f"{em.q[k, 0]:.4f}"
            row[f"{em.reach_id}_q_ds_m3s"] = f"{em.q[k, -1]:.4f}"
            row[f"{em.reach_id}_h_us_m"] = f"{em.h[k, 0]:.4f}"
            row[f"{em.reach_id}_h_ds_m"] = f"{em.h[k, -1]:.4f}"
        rows.append(row)
    write_csv(path, fields, rows)


def reach_geom_items(res: RouteResult) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = [("main", res.geom)]
    for em in res.extra_mains or []:
        items.append((str(em.reach_id), em.geom))
    return items


def plot_result(res: RouteResult, out_dir: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("Thieu matplotlib - bo qua bieu do.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    hours = res.hours
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    ax = axes[0]
    ax.plot(hours, res.q_in, color="#c0392b", lw=1.8, label="Q thuong luu (TANK)")
    ax.plot(hours, res.q[:, -1], color="#1d4ed8", lw=1.8, label="Q ha luu (Saint-Venant)")
    for trib in res.tribs or []:
        role = "thoat nuoc" if _is_outlet(trib) else "nhap luu"
        ax.plot(
            hours,
            trib.q[:, _trib_join_idx(trib)],
            lw=1.1,
            ls=":",
            label=f"Q {trib.reach_id} {role} (nut giao)",
        )
    mid = res.q.shape[1] // 2
    ax.plot(
        hours,
        res.q[:, mid],
        color="#16a34a",
        lw=1.2,
        ls="--",
        label=f"Q XS{mid+1} ({res.station_km[mid]:.1f} km)",
    )
    ax.set_ylabel("Q (m3/s)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    ax.set_title(
        f"Saint-Venant 1D - mat cat DEM moi {res.params.xs_spacing_m / 1000:.1f} km"
    )

    ax = axes[1]
    ax.plot(hours, res.h_down, color="#7c3aed", lw=1.6, label="Bien muc nuoc ha luu (song chinh)")
    if res.h_trib_down is not None:
        ax.plot(
            hours,
            res.h_trib_down,
            color="#ea580c",
            lw=1.4,
            ls=":",
            label="H ha luu nhanh thoat (bien H)",
        )
    ax.plot(hours, res.h[:, 0], color="#b45309", lw=1.4, label="H thuong luu (XS1)")
    ax.plot(
        hours,
        res.h[:, -1],
        color="#0369a1",
        lw=1.2,
        ls="--",
        label=f"H ha luu (XS{res.q.shape[1]})",
    )
    ax.set_ylabel("Muc nuoc H (m)")
    ax.set_xlabel("Gio")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_dir / "saint_venant_hydrograph.png", dpi=140)
    plt.close(fig)

    pk = res.peak_time_index
    fig, ax = plt.subplots(figsize=(11, 4.4))
    km = res.station_km
    ax.fill_between(
        km,
        res.geom.z_bed,
        res.geom.z_bed.min() - 2,
        color="#c4a574",
        alpha=0.9,
        label="Day song (DEM)",
    )
    ax.plot(km, res.geom.z_bed, color="#6b4f2a", lw=1.2)
    ax.plot(km, res.h[pk], color="#1d4ed8", lw=1.8, label=f"Mat nuoc luc gio {res.hours[pk]:.0f}")
    ax.fill_between(km, res.geom.z_bed, res.h[pk], color="#3b82f6", alpha=0.35)
    n_xs = len(res.geom.sections)
    step_lbl = max(1, n_xs // 10)
    for i, sec in enumerate(res.geom.sections):
        ax.axvline(sec.station_m / 1000.0, color="#444", lw=0.5, alpha=0.35)
        if i == 0 or i == n_xs - 1 or i % step_lbl == 0:
            ax.text(sec.station_m / 1000.0, res.h[pk, i] + 0.25, f"{i+1}", ha="center", fontsize=7)
    ax.set_xlabel("Khoang cach tu thuong luu (km)")
    ax.set_ylabel("Cao do (m)")
    dx_km = float(np.mean(res.geom.dx_m) / 1000.0) if res.geom.dx_m.size else 0.0
    ax.set_title(f"Mat cat doc - {n_xs} mat cat ngang, TB {dx_km:.2f} km/tram")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_dir / "saint_venant_profile.png", dpi=140)
    plt.close(fig)

    cols = 5
    rows = int(math.ceil(n_xs / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(14, 1.85 * rows), sharey=True)
    axes_f = np.atleast_1d(axes).ravel()
    for i, sec in enumerate(res.geom.sections):
        ax = axes_f[i]
        ax.fill_between(sec.offset_m, sec.z, np.nanmin(sec.z) - 1, color="#c4a574", alpha=0.9)
        ax.plot(sec.offset_m, sec.z, color="#5c4324", lw=1.1)
        hw = res.h[pk, i]
        ax.axhline(hw, color="#1d4ed8", lw=1.4, label="Muc nuoc (dinh)")
        wet = sec.z <= hw
        if wet.any():
            ax.fill_between(
                sec.offset_m,
                sec.z,
                np.minimum(hw, np.nanmax(sec.z)),
                where=wet,
                color="#3b82f6",
                alpha=0.4,
            )
        ax.set_title(f"XS{i+1}  {sec.station_m/1000:.1f} km")
        ax.set_xlabel("Offset (m)")
        if i % cols == 0:
            ax.set_ylabel("z (m)")
        ax.grid(True, alpha=0.25)
    for j in range(n_xs, len(axes_f)):
        axes_f[j].set_visible(False)
    fig.suptitle(
        "Mat cat ngang DEM moi %.1f km (vuong goc long song)"
        % (res.params.xs_spacing_m / 1000.0),
        y=1.002,
    )
    fig.tight_layout()
    fig.savefig(out_dir / "saint_venant_cross_sections.png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    plot_network(res, out_dir, plt)


def _join_lonlat_on_main(main_geom, join_station_m: float) -> tuple[float, float]:
    """Toa do nut giao tren long chinh (noi suy ly trinh), khong snap ve XS gan nhat."""
    s = np.asarray(main_geom.distance_m, dtype=float)
    lon = np.asarray(main_geom.lon, dtype=float)
    lat = np.asarray(main_geom.lat, dtype=float)
    sm = float(join_station_m)
    if s.size == 0:
        return 0.0, 0.0
    return float(np.interp(sm, s, lon)), float(np.interp(sm, s, lat))


def plot_network(res: RouteResult, out_dir: Path, plt) -> None:
    if not res.tribs:
        return
    fig, ax = plt.subplots(figsize=(8.4, 7.2))
    ax.plot(res.geom.lon, res.geom.lat, color="#1d4ed8", lw=2.4, label="Long chinh")
    ax.scatter(res.geom.lon[0], res.geom.lat[0], c="#22c55e", s=36, zorder=4, label="Thuong luu")
    ax.scatter(res.geom.lon[-1], res.geom.lat[-1], c="#ef4444", s=36, zorder=4, label="Ha luu")
    colors = ["#f59e0b", "#8b5cf6", "#14b8a6", "#ec4899"]
    join_labeled = False
    for i, t in enumerate(res.tribs):
        c = colors[i % len(colors)]
        if _is_outlet(t):
            role = "thoat nuoc"
            ox, oy = float(t.lon[-1]), float(t.lat[-1])
            end_lbl = "Ha luu (bien H)"
        else:
            role = "nhap luu (bien Q)"
            ox, oy = float(t.lon[0]), float(t.lat[0])
            end_lbl = "Thuong luu (bien Q)"
        jx, jy = _join_lonlat_on_main(res.geom, t.join_station_m)
        tlon = np.asarray(t.lon, dtype=float)
        tlat = np.asarray(t.lat, dtype=float)
        if _is_outlet(t):
            plon = np.concatenate([[jx], tlon])
            plat = np.concatenate([[jy], tlat])
        else:
            plon = np.concatenate([tlon, [jx]])
            plat = np.concatenate([tlat, [jy]])
        ax.plot(plon, plat, color=c, lw=1.8, label=f"{t.reach_id} {role} ({t.length_m/1000:.1f} km)")
        ax.scatter(ox, oy, c=c, s=36, zorder=4, label=end_lbl)
        ax.annotate(
            end_lbl,
            (ox, oy),
            textcoords="offset points",
            xytext=(8, -10),
            fontsize=8,
            color=c,
        )
        ax.scatter(
            jx,
            jy,
            marker="*",
            c=c,
            s=110,
            zorder=5,
            edgecolors="#111",
            label="Nut giao" if not join_labeled else None,
        )
        join_labeled = True
    ax.set_xlabel("Kinh do")
    ax.set_ylabel("Vi do")
    ax.set_title("Mang 1D: long chinh + nhanh thoat nuoc (bien H ha luu)")
    ax.set_aspect("equal", adjustable="datalim")
    ax.legend(loc="best", fontsize=8)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_dir / "saint_venant_network.png", dpi=140)
    plt.close(fig)


def print_summary(res: RouteResult) -> None:
    pk = res.peak_time_index
    i_pk = int(np.argmax(res.q_in))
    o_pk = int(np.argmax(res.q[:, -1]))
    lag_h = float(res.hours[o_pk] - res.hours[i_pk])
    dx_km = float(np.mean(res.geom.dx_m) / 1000.0) if res.geom.dx_m.size else 0.0
    print(f"Mo hinh        : Saint-Venant 1D mang (long chinh + {len(res.tribs or [])} nhanh)" if res.tribs else "Mo hinh        : Saint-Venant 1D (dynamic wave)")
    print(f"Chieu dai song : {res.geom.length_m / 1000.0:.2f} km")
    extras = list(res.extra_mains or [])
    if extras:
        print(f"Song chinh them: {len(extras)} long doc lap")
        for em in extras:
            print(
                f"  {em.reach_id}  L={em.length_m / 1000.0:.2f} km  "
                f"{len(em.geom.sections)} XS  Q_ds dinh {float(np.max(em.q[:, -1])):.1f} m3/s"
            )
    print(f"Mat cat ngang  : {len(res.geom.sections)} tram, khoang TB {dx_km:.2f} km")
    n_vals = [section_n(sec, res.params.manning_n) for sec in res.geom.sections]
    n_min, n_max = min(n_vals), max(n_vals)
    if abs(n_max - n_min) < 1e-9:
        print(f"Manning n      : {n_min:.4f}  (tat ca mat cat)")
    else:
        print(f"Manning n      : {n_min:.4f} .. {n_max:.4f}  (tung mat cat)")
    q0_min, q0_max = float(np.min(res.q[0])), float(np.max(res.q[0]))
    h0_min, h0_max = float(np.min(res.h[0])), float(np.max(res.h[0]))
    if abs(q0_max - q0_min) < 1e-6:
        print(f"Q0 ban dau     : {q0_min:.2f} m3/s  (tat ca mat cat)")
    else:
        print(f"Q0 ban dau     : {q0_min:.2f} .. {q0_max:.2f} m3/s  (tung mat cat)")
    print(f"H0 ban dau     : XS1 {res.h[0, 0]:.2f} m  ..  XS cuoi {res.h[0, -1]:.2f} m  (min {h0_min:.2f}, max {h0_max:.2f})")
    print(f"dt xuat        : {res.params.dt_hours:.2f} gio  ({res.hours.size} buoc)")
    print(f"dt thuy luc    : ~{res.dt_hydro_s:.1f} s  (CFL={res.params.cfl:.2f}, ~{res.n_substep} buoc/gio)")
    for sec in res.geom.sections:
        _a, _p, b = sec.props(sec.z_bed + 3.0)
        print(
            f"  XS{sec.index+1:d}  {sec.station_m/1000:6.2f} km  "
            f"z_bed={sec.z_bed:7.2f} m  B(3m)={b:7.0f} m  "
            f"n={section_n(sec, res.params.manning_n):.4f}"
        )
    print(f"Q thuong luu   : TB {res.q_in.mean():.2f}  dinh {res.q_in.max():.2f} m3/s")
    print(f"Q ha luu       : TB {res.q[:, -1].mean():.2f}  dinh {res.q[:, -1].max():.2f} m3/s")
    print(f"Tre dinh       : {lag_h:.1f} gio")
    print(f"H ha luu       : {res.h_down.min():.2f} .. {res.h_down.max():.2f} m")
    if res.h_trib_down is not None:
        print(
            f"H ha luu nhanh thoat : {float(np.min(res.h_trib_down)):.2f} .. "
            f"{float(np.max(res.h_trib_down)):.2f} m  (cot {TRIB_H_COL})"
        )
    print(f"H thuong luu   : {res.h[:, 0].min():.2f} .. {res.h[:, 0].max():.2f} m (gio {res.hours[pk]:.0f})")
    print(f"Can bang khoi  : {res.mass_balance_m3:.3e} m3  (tich phan I-O)")
    if res.tribs:
        n_in = sum(1 for t in res.tribs if not _is_outlet(t))
        n_out = sum(1 for t in res.tribs if _is_outlet(t))
        print(f"Nhanh          : {len(res.tribs)}  (nhap luu {n_in}, thoat nuoc {n_out})")
        for t in res.tribs:
            ji = _trib_join_idx(t)
            if _is_outlet(t):
                role = "thoat nuoc, bien H ha luu"
            else:
                role = "nhap luu, bien Q"
            extra = f"z_gan={t.z_near:.2f} z_xa={t.z_far:.2f} m"
            if not _is_outlet(t):
                extra = f"Q_frac={t.q_frac:.3f}  {extra}"
            print(
                f"  {t.reach_id}  {role}  L={t.length_m/1000:.2f} km  "
                f"nut XS{t.join_xs+1} ({t.join_station_m/1000:.2f} km)  "
                f"{extra}  Q_join dinh {float(np.max(t.q[:, ji])):.1f} m3/s"
            )


def mass_balance(q_in: np.ndarray, q_out: np.ndarray, dt_s: float) -> float:
    return float((q_in.sum() - q_out.sum()) * dt_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Saint-Venant 1D mang: long chinh + nhanh nhap luu / thoat nuoc")
    p.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    p.add_argument(
        "--inflow",
        type=Path,
        default=DEFAULT_INFLOW_CSV,
        help="CSV Q vao (hour,q_m3s hoac q_m3/s). Mac dinh: rainfall_runoff_output/mike_nam_result.csv",
    )
    p.add_argument(
        "--h-csv", 
        type=Path, 
        default=DEFAULT_H_CSV, 
        help="CSV H ha luu long chinh (hour,h_m)")
    p.add_argument(
        "--trib-h-csv",
        type=Path,
        default=DEFAULT_TRIB_H_CSV,
        help="CSV H ha luu nhanh thoat (cot h_na1_m)",
    )
    p.add_argument("--h-down", type=float, default=None, help="H ha luu hang (m); uu tien hon --h-csv")
    p.add_argument("--n", type=float, default=0.030, dest="manning_n", help="Manning n mac dinh (moi mat cat)")
    p.add_argument(
        "--n-csv",
        type=Path,
        default=None,
        dest="n_csv",
        help="CSV n theo mat cat (xs_id,manning_n[,station_km]). Thieu id thi noi suy theo ly trinh. "
        "Mac dinh: saint_venant_output/demo_manning_n.csv (tim ca theo ten file neu duong dan lech).",
    )
    p.add_argument(
        "--q0",
        type=float,
        default=None,
        dest="q0",
        help="Luu luong ban dau Q0 (m3/s), dong nhat moi mat cat. Mac dinh: Q vao luc t=0",
    )
    p.add_argument(
        "--h0",
        type=float,
        default=None,
        dest="h0",
        help="Muc nuoc ban dau H0 (m) tai ha luu de tinh backwater luc t=0. Mac dinh: H ha luu luc t=0",
    )
    p.add_argument(
        "--ic-csv",
        type=Path,
        default=None,
        dest="ic_csv",
        help="CSV dieu kien ban dau theo mat cat (xs_id,q0,h0). Cot thieu dung --q0/--h0",
    )
    p.add_argument(
        "--xs-spacing",
        type=float,
        default=XS_SPACING_M,
        help="Khoang cach mat cat (m), mac dinh 1500. Vi du: 500, 900, 1500. Gia tri < 50 duoc hieu la km (CLI cu).",
    )
    p.add_argument("--xs-half", type=float, default=1500.0, help="Nua be rong lay mat cat (m)")
    p.add_argument(
        "--dt",
        type=float,
        default=1.0,
        help="Buoc thoi gian xuat ket qua (gio). Mac dinh 1. Vi du: 0.25, 0.5, 1, 2",
    )
    p.add_argument("--cfl", type=float, default=0.45, help="So CFL buoc thuy luc")
    p.add_argument("--dt-hydro", type=float, default=60.0, dest="dt_hydro_max_s", help="dt thuy luc toi da (s)")
    p.add_argument(
        "--allow-reverse",
        action="store_true",
        help="Cho phep Q am (dong nguoc). Mac dinh chan Q >= q_min.",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--no-plot", action="store_true")
    p.add_argument(
        "--no-network",
        action="store_true",
        help="Chi 1 long chinh (khong trich nhanh).",
    )
    p.add_argument(
        "--max-mains",
        type=int,
        default=DEFAULT_MAX_MAINS,
        help=f"So song chinh toi da (1 long thu nhat + cac long doc lap). Mac dinh {DEFAULT_MAX_MAINS}",
    )
    p.add_argument(
        "--max-tribs",
        type=int,
        default=DEFAULT_MAX_TRIBS,
        help=f"So song nhanh toi da noi vao long chinh thu nhat. Mac dinh {DEFAULT_MAX_TRIBS}",
    )
    p.add_argument("--min-trib-km", type=float, default=2.0, help="Chieu dai nhanh toi thieu (km)")
    p.add_argument("--trib-half", type=float, default=600.0, help="Nua be rong mat cat nhanh (m)")
    return p.parse_args(list(argv) if argv is not None else None)


def run_model(args: argparse.Namespace) -> RouteResult:
    par = SaintVenantParams(
        manning_n=args.manning_n,
        xs_spacing_m=parse_xs_spacing_m(args.xs_spacing),
        xs_half_width_m=float(args.xs_half),
        dt_hours=parse_dt_hours(args.dt),
        cfl=float(args.cfl),
        dt_hydro_max_s=float(args.dt_hydro_max_s),
        unidirectional=not bool(getattr(args, "allow_reverse", False)),
        q0=getattr(args, "q0", None),
        h0=getattr(args, "h0", None),
    )
    hours0, q0 = load_inflow_q(args.inflow)
    hours, q_in = resample_series(hours0, q0, par.dt_hours)
    q_in = np.maximum(q_in, par.q_min)
    print(
        f"dt xuat        : {par.dt_hours:g} gio  ({hours.size} buoc, "
        f"{float(hours[0]):.2f} .. {float(hours[-1]):.2f} h)",
        flush=True,
    )

    n_csv = resolve_n_csv(getattr(args, "n_csv", None))
    n_by_id: dict[int, float] = {}
    n_station = np.array([], dtype=float)
    n_along = np.array([], dtype=float)
    if n_csv is not None:
        n_by_id, n_station, n_along = load_manning_n_table(n_csv, reach_id="main")
        print(
            f"Manning n CSV  : {n_csv}  ({len(n_by_id)} mat cat long chinh, "
            f"{int(n_station.size)} tram ly trinh)",
            flush=True,
        )
    else:
        raw = getattr(args, "n_csv", None)
        if raw:
            raise FileNotFoundError(f"Khong thay file Manning n: {raw}")
        print(
            f"Manning n CSV  : khong co {DEFAULT_N_CSV.name} — dung --n {par.manning_n:g}",
            flush=True,
        )

    max_mains = parse_max_mains(getattr(args, "max_mains", DEFAULT_MAX_MAINS))
    max_tribs = (
        0
        if getattr(args, "no_network", False)
        else parse_max_tribs(getattr(args, "max_tribs", DEFAULT_MAX_TRIBS))
    )
    print(
        f"Trich mang 1D  : toi da {max_mains} song chinh, {max_tribs} song nhanh...",
        flush=True,
    )
    geom, extra_specs, trib_specs = extract_model_network(
        args.dem,
        par,
        max_mains=max_mains,
        max_tribs=max_tribs,
        water_source="saint-venant",
        no_network=bool(getattr(args, "no_network", False)),
        n_default=par.manning_n,
        n_csv=n_csv,
        min_trib_m=max(1500.0, float(getattr(args, "min_trib_km", 2.0)) * 1000.0),
        trib_half_m=float(getattr(args, "trib_half", 600.0)),
    )
    dx_m = float(np.mean(geom.dx_m)) if geom.dx_m.size else par.xs_spacing_m
    print(
        f"Mat cat        : {len(geom.sections)} tram long chinh, dx TB {dx_m:.0f} m "
        f"(yeu cau {par.xs_spacing_m:.0f} m)",
        flush=True,
    )
    nn = [section_n(sec, par.manning_n) for sec in geom.sections]
    if nn:
        print(
            f"Manning n ap dung: {min(nn):.4f} .. {max(nn):.4f}  "
            f"({len(nn)} mat cat long chinh)",
            flush=True,
        )
    if extra_specs:
        print(f"Song chinh them: {len(extra_specs)} long doc lap", flush=True)
        for spec in extra_specs:
            print(
                f"  {spec['reach_id']}: {float(spec['length_m'])/1000.0:.2f} km, "
                f"{len(spec['geom'].sections)} mat cat",
                flush=True,
            )
    else:
        print("Song chinh them: khong tim thay long doc lap du dieu kien.", flush=True)
    if trib_specs:
        for t in trib_specs:
            if _is_outlet(t):
                role = (
                    f"thoat nuoc (bien H ha luu), z_gan={t['z_near']:.2f} z_xa={t['z_far']:.2f} m"
                )
            else:
                role = (
                    f"nhap luu (bien Q), Q_frac={t['q_frac']:.3f}, "
                    f"z_gan={t['z_near']:.2f} z_xa={t['z_far']:.2f} m"
                )
            print(
                f"  {t['reach_id']}: {t['length_m']/1000:.2f} km, "
                f"nut XS{t['join_xs']+1} ({t['join_station_m']/1000:.2f} km), {role}",
                flush=True,
            )
    elif not getattr(args, "no_network", False):
        print("  Khong tim thay nhanh du dieu kien — chay khong song nhanh.", flush=True)

    q0_by_id: dict[int, float] = {}
    h0_by_id: dict[int, float] = {}
    ic_csv = getattr(args, "ic_csv", None)
    if ic_csv is not None:
        ic_path = Path(ic_csv)
        if not csv_available(ic_path):
            raise FileNotFoundError(f"Khong thay file dieu kien ban dau Q0/H0: {ic_path}")
        q0_by_id, h0_by_id = load_initial_qh_csv(ic_path)
        print(
            f"IC CSV         : {ic_path}  (Q0 {len(q0_by_id)} mat cat, H0 {len(h0_by_id)} mat cat)",
            flush=True,
        )
    h_down = load_downstream_stage(
        hours,
        getattr(args, "h_csv", None),
        getattr(args, "h_down", None),
        float(geom.z_bed[-1]),
        q_in,
    )
    trib_h_path = Path(getattr(args, "trib_h_csv", None) or DEFAULT_TRIB_H_CSV)
    h_trib_down = load_trib_outlet_stage(hours, trib_h_path)
    if h_trib_down is not None:
        print(
            f"H ha luu nhanh thoat : cot {TRIB_H_COL} trong {trib_h_path}  "
            f"({float(np.min(h_trib_down)):.2f} .. {float(np.max(h_trib_down)):.2f} m)",
            flush=True,
        )
    else:
        print(
            f"H ha luu nhanh thoat : khong doc duoc {TRIB_H_COL} tu {trib_h_path} — dung do sau H ha luu Hong",
            flush=True,
        )
    h_need = max(float(np.max(h_down)), float(np.max(geom.z_bed))) + 12.0
    if h_trib_down is not None:
        h_need = max(h_need, float(np.max(h_trib_down)) + 12.0)
    if par.h0 is not None:
        h_need = max(h_need, float(par.h0) + 12.0)
    if h0_by_id:
        h_need = max(h_need, max(h0_by_id.values()) + 12.0)
    for spec in extra_specs:
        zb = np.asarray(spec["geom"].z_bed, dtype=float)
        if zb.size:
            h_need = max(h_need, float(np.max(zb)) + 12.0)
    extend_section_tables(geom, h_need)
    for spec in extra_specs:
        extend_section_tables(spec["geom"], h_need)
    for t in trib_specs:
        extend_section_tables(t["geom"], h_need)

    trib_res: list[TribResult] = []
    q_main_up = q_in
    if trib_specs:
        print("Dang giai mang 1D (long chinh + nhanh nhap luu / thoat nuoc)...", flush=True)
        q, h, y, dt_h, n_sub, q_main_up, trib_res = route_network(
            hours, q_in, h_down, geom, trib_specs, par, q0_by_id, h0_by_id, h_trib_down
        )
        q_in_total = np.array(q_main_up, dtype=float)
        q_out_total = np.array(q[:, -1], dtype=float)
        for tr in trib_res:
            if _is_outlet(tr):
                q_out_total = q_out_total + tr.q[:, _trib_outer_idx(tr)]
            else:
                q_in_total = q_in_total + tr.q_in
        mb = mass_balance(q_in_total, q_out_total, par.dt_hours * 3600.0)
    else:
        print("Dang giai Saint-Venant 1D...", flush=True)
        q, h, y, dt_h, n_sub = route_saint_venant(
            hours, q_in, h_down, geom, par, q0_by_id, h0_by_id
        )
        mb = mass_balance(q_in, q[:, -1], par.dt_hours * 3600.0)
    extra_mains = route_extra_main_reaches(
        hours, q_in, h_down, extra_specs, par, route_saint_venant, q0_by_id, h0_by_id
    )
    pk = int(np.argmax(q[:, 0]))
    return RouteResult(
        hours=hours,
        q_in=q_in,
        h_down=h_down,
        q=q,
        h=h,
        y=y,
        geom=geom,
        params=par,
        station_km=geom.distance_m / 1000.0,
        peak_time_index=pk,
        mass_balance_m3=mb,
        dt_hydro_s=dt_h,
        n_substep=n_sub,
        q_main_up=q_main_up,
        tribs=trib_res,
        extra_mains=extra_mains,
        h_trib_down=h_trib_down,
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    print(f"DEM long song : {args.dem}")
    q_src = resolve_inflow_csv(args.inflow)
    print(f"Q vao          : {q_src}  (cot q_m3s / q_m3/s)")
    if args.h_down is not None:
        print(f"H ha luu       : hang {args.h_down:.3f} m")
    else:
        h_path = args.h_csv if args.h_csv is not None else DEFAULT_H_CSV
        print(f"H ha luu       : {h_path}")
    if args.q0 is not None:
        print(f"Q0 ban dau     : {args.q0:.3f} m3/s")
    if args.h0 is not None:
        print(f"H0 ban dau     : {args.h0:.3f} m  (ha luu, tinh backwater)")
    if getattr(args, "ic_csv", None) is not None:
        print(f"IC CSV         : {args.ic_csv}")
    print(f"Lay mat cat ngang moi {parse_xs_spacing_m(args.xs_spacing):.0f} m doc long song...")
    res = run_model(args)
    print_summary(res)

    out = args.out_dir
    write_inflow_csv(res.hours, res.q_in, out / "demo_inflow_q_m3s.csv")
    h_out = out / "demo_downstream_stage.csv"
    trib_src = Path(getattr(args, "trib_h_csv", None) or DEFAULT_TRIB_H_CSV)
    h_csv = getattr(args, "h_csv", None)
    h_src = Path(str(h_csv)) if h_csv is not None else Path(DEFAULT_H_CSV)
    keep_input_h = False
    if args.h_down is None:
        for src in (trib_src, h_src):
            if src.resolve() == h_out.resolve():
                keep_input_h = True
                break
    if keep_input_h:
        print(f"Giu bien H     : {h_out}  (khong ghi de file dau vao)")
    else:
        write_downstream_csv(res.hours, res.h_down, h_out, res.h_trib_down)
    items = reach_geom_items(res)
    write_geometries_csv(items, res.params, out / "demo_river_geometry.csv")
    write_manning_network_csv(items, out / "demo_manning_n_applied.csv")
    n_keep = out / "demo_manning_n.csv"
    if not csv_available(n_keep):
        write_manning_network_csv(items, n_keep)
        print(f"Tao Manning n  : {n_keep}  (sua roi chay lai --n-csv)")
    else:
        print(f"Giu Manning n  : {n_keep}  (khong ghi de file dau vao)")
    write_initial_csv(res.geom, res.q[0], res.h[0], out / "demo_initial_qh.csv")
    write_cross_sections_csv(res.geom, out / "demo_cross_sections.csv")
    write_result_csv(res, out / "saint_venant_result.csv")
    write_network_reaches_csv(res, out / "demo_network_reaches.csv")
    if res.tribs:
        write_tributary_geometry_csv(res, out / "demo_tributary_geometry.csv")
        write_tributary_result_csv(res, out / "saint_venant_tributary_result.csv")
    if res.extra_mains:
        write_extra_main_result_csv(res, out / "demo_extra_main_result.csv")
    print(f"Mat cat ngang  : {out / 'demo_cross_sections.csv'}  (offset_m, z_m)")
    print(f"Tram XS        : {out / 'demo_river_geometry.csv'}")
    print(f"Mang 1D        : {out / 'demo_network_reaches.csv'}")
    if res.tribs:
        print(f"Hinh hoc nhanh : {out / 'demo_tributary_geometry.csv'}")
        print(f"Q/H nhanh      : {out / 'saint_venant_tributary_result.csv'}")
    if res.extra_mains:
        print(f"Q/H song chinh them: {out / 'demo_extra_main_result.csv'}")
    print(f"Manning n XS   : {out / 'demo_manning_n.csv'}  (dau vao; sua roi chay --n-csv)")
    print(f"Manning n ap dung: {out / 'demo_manning_n_applied.csv'}")
    print(f"Q0 H0 XS       : {out / 'demo_initial_qh.csv'}  (sua roi chay --ic-csv)")
    print(f"Demo H ha luu  : {out / 'demo_downstream_stage.csv'}")
    print(f"Ket qua        : {out / 'saint_venant_result.csv'}")
    if not args.no_plot:
        plot_result(res, out)
        print(f"Bieu do        : {out / 'saint_venant_hydrograph.png'}")
        print(f"Mat cat doc    : {out / 'saint_venant_profile.png'}")
        print(f"Mat cat ngang  : {out / 'saint_venant_cross_sections.png'}")
        if res.tribs:
            print(f"Mang 1D        : {out / 'saint_venant_network.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

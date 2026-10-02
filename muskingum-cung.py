"""
Dien toan 1D Muskingum-Cunge doc long song.

Hinh hoc: mat cat ngang that lay tu DEM, cach deu ~1.5 km doc long song.

Muskingum-Cunge (K, X tinh moi buoc; dt hieu chinh):
  K = k_scale * max(dt, dx / c)     # thoi gian truyen song (s)
  X = 0.5 * (1 - Q / (B * S0 * c * dx))
  C0,C1,C2 tu K, X, dt  (sclaw/muskingum-cunge, solver WRF)

Dau vao:
  - Q(t): chay rainfall-runoff.py (thong so TANK trong file do), doc q_m3s tu tank_result.csv
  - DEM long song: projects/data/dem-song-hong.tif
  - Bien muc nuoc ha luu H_ds(t) tu muskingum_output/demo_downstream_stage.csv

Chay:
  .\\venv\\Scripts\\python.exe muskingum-cung.py
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, write_csv_rows
from flood_model.boundary import (
    MK_DOWN_H_CSV,
    DEFAULT_TANK_SCRIPT,
    TANK_RESULT_CSV as DEFAULT_TANK_CSV,
    _read_series_csv,
    demo_downstream_wse,
    load_downstream_stage,
    load_tank_inflow,
    resample_series,
    run_rainfall_runoff,
    write_downstream_csv,
    write_inflow_csv,
)
from flood_model import cross_section as _cross_section

ChannelParams = getattr(_cross_section, "ChannelParams")
CrossSection = getattr(_cross_section, "CrossSection")
RiverGeom = getattr(_cross_section, "RiverGeom")
XS_SPACING_M = getattr(_cross_section, "XS_SPACING_M")
extract_reach_from_lonlat = getattr(_cross_section, "extract_reach_from_lonlat")
extract_river = getattr(_cross_section, "extract_river")
geom_from_polyline = getattr(_cross_section, "geom_from_polyline")
sample_cross_section = getattr(_cross_section, "sample_cross_section")
section_tables = getattr(_cross_section, "section_tables")
write_cross_sections_csv = getattr(_cross_section, "write_cross_sections_csv")

DEFAULT_DEM = ROOT / "projects" / "data" / "dem-song-hong.tif"
DEFAULT_OUT_DIR = ROOT / "muskingum_output"

_section_tables = section_tables
_sample_cross_section = sample_cross_section


@dataclass
class RouteResult:
    hours: np.ndarray
    q_in: np.ndarray
    h_down: np.ndarray
    q: np.ndarray
    h: np.ndarray
    y: np.ndarray
    geom: Any
    params: Any
    station_km: np.ndarray
    peak_time_index: int
    mass_balance_m3: float
    k_s: np.ndarray = field(default_factory=lambda: np.zeros(0))
    x_cunge: np.ndarray = field(default_factory=lambda: np.zeros(0))


# Dieu kien bien Q/H: flood_model/boundary.py
# Mat cat ngang: flood_model/cross-section.py

# ---------------------------------------------------------------------------
# Muskingum-Cunge (K, X, dt) — sclaw/muskingum-cunge WRF solver
# https://github.com/sclaw/muskingum-cunge
# ---------------------------------------------------------------------------

def cunge_kx(
    dx: float,
    celerity: float,
    q_ref: float,
    top_width: float,
    slope: float,
    dt_s: float,
    k_scale: float = 1.0,
) -> tuple[float, float]:
    """K (giay) va X (-) theo Cunge/WRF: K = k_scale * max(dt, dx/c)."""
    ck = max(float(celerity), 1e-6)
    dx = max(float(dx), 1.0)
    dt_s = max(float(dt_s), 1.0)
    k = float(k_scale) * max(dt_s, dx / ck)
    b = max(float(top_width), 1.0)
    s0 = max(float(slope), 1e-8)
    # X = 1/2 [1 - Q / (B S0 c dx)]  (USACE / Cunge; sclaw USACERectangle)
    x = 0.5 * (1.0 - float(q_ref) / max(b * s0 * ck * dx, 1e-12))
    x = min(max(x, 0.0), 0.5)
    return k, x


def muskingum_coefs(k: float, x: float, dt: float) -> tuple[float, float, float]:
    """C0 (I_t), C1 (I_{t-1}), C2 (O_{t-1}). Giong sclaw route_hydrograph_wrf."""
    x = min(max(x, 0.0), 0.5)
    den = k * (1.0 - x) + 0.5 * dt
    if abs(den) < 1e-12:
        return 0.0, 0.0, 1.0
    c0 = (0.5 * dt - k * x) / den
    c1 = (k * x + 0.5 * dt) / den
    c2 = (k * (1.0 - x) - 0.5 * dt) / den
    return c0, c1, c2


def route_reach(
    q_in: np.ndarray,
    xs: Any,
    dx: float,
    slope: float,
    n: float,
    dt_s: float,
    q_min: float,
    k_scale: float = 1.0,
) -> np.ndarray:
    n_t = int(q_in.size)
    q_out = np.zeros(n_t, dtype=float)
    q_out[0] = max(float(q_in[0]), q_min)
    i_prev = max(float(q_in[0]), q_min)
    o_prev = q_out[0]
    s0 = max(slope, 1e-8)
    for t in range(1, n_t):
        i_now = max(float(q_in[t]), q_min)
        q_ref = max(0.5 * (i_prev + o_prev), q_min)
        h_ref = xs.stage_for_q(q_ref, n, s0)
        b = xs.top_width(h_ref)
        ck = xs.celerity(q_ref, n, s0)
        k, x = cunge_kx(dx, ck, q_ref, b, s0, dt_s, k_scale)
        c0, c1, c2 = muskingum_coefs(k, x, dt_s)
        o_now = max(c0 * i_now + c1 * i_prev + c2 * o_prev, q_min * 0.5)
        q_out[t] = o_now
        i_prev, o_prev = i_now, o_now
    return q_out


def reach_kx_tables(geom: Any, par: Any, q_ref: float) -> tuple[np.ndarray, np.ndarray]:
    dt_s = par.dt_hours * 3600.0
    n_r = int(geom.dx_m.size)
    k_s = np.zeros(n_r)
    x_c = np.zeros(n_r)
    q_ref = max(float(q_ref), par.q_min)
    for r in range(n_r):
        xs = geom.sections[r + 1]
        dx = float(geom.dx_m[r])
        s0 = max(float(geom.slope[r]), par.min_slope)
        h_ref = xs.stage_for_q(q_ref, par.manning_n, s0)
        b = xs.top_width(h_ref)
        ck = xs.celerity(q_ref, par.manning_n, s0)
        k_s[r], x_c[r] = cunge_kx(dx, ck, q_ref, b, s0, dt_s, par.k_scale)
    return k_s, x_c


def route_river(q_up: np.ndarray, geom: Any, par: Any) -> np.ndarray:
    n_t = int(q_up.size)
    n_node = int(geom.distance_m.size)
    q = np.zeros((n_t, n_node), dtype=float)
    q[:, 0] = np.maximum(q_up, par.q_min)
    dt_s = par.dt_hours * 3600.0
    inflow = q[:, 0].copy()
    for r in range(n_node - 1):
        xs = geom.sections[r + 1]
        outflow = route_reach(
            inflow,
            xs=xs,
            dx=float(geom.dx_m[r]),
            slope=max(float(geom.slope[r]), par.min_slope),
            n=par.manning_n,
            dt_s=dt_s,
            q_min=par.q_min,
            k_scale=par.k_scale,
        )
        q[:, r + 1] = outflow
        inflow = outflow
    return q


def stages_backwater(
    q: np.ndarray,
    h_down: np.ndarray,
    geom: Any,
    par: Any,
) -> tuple[np.ndarray, np.ndarray]:
    n_t, n_x = q.shape
    h = np.zeros((n_t, n_x), dtype=float)
    y = np.zeros((n_t, n_x), dtype=float)
    z = geom.z_bed
    n = par.manning_n
    for t in range(n_t):
        h[t, -1] = max(float(h_down[t]), z[-1] + 0.05)
        y[t, -1] = h[t, -1] - z[-1]
        for i in range(n_x - 2, -1, -1):
            dx = float(geom.dx_m[i])
            s0 = max(float(geom.slope[i]), par.min_slope)
            xs = geom.sections[i]
            xs_next = geom.sections[i + 1]
            h_n = xs.stage_for_q(float(q[t, i]), n, s0)
            a, p, _b = xs_next.props(h[t, i + 1])
            r = a / p
            sf = (n * max(float(q[t, i + 1]), par.q_min) / max(a * r ** (2.0 / 3.0), 1e-6)) ** 2
            h_bw = h[t, i + 1] + sf * dx
            h[t, i] = max(h_n, h_bw, z[i] + 0.05)
            y[t, i] = h[t, i] - z[i]
    return h, y


# ---------------------------------------------------------------------------
# Xuat file
# ---------------------------------------------------------------------------

def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    write_csv_rows(path, fieldnames, rows)


def write_geometry_csv(
    geom: Any,
    par: Any,
    path: Path,
    k_s: Optional[np.ndarray] = None,
    x_cunge: Optional[np.ndarray] = None,
) -> None:
    rows = []
    for i, sec in enumerate(geom.sections):
        h_ref = sec.z_bed + 3.0
        _a, _p, b = sec.props(h_ref)
        k_val = k_s[i] if k_s is not None and i < k_s.size else float("nan")
        x_val = x_cunge[i] if x_cunge is not None and i < x_cunge.size else float("nan")
        rows.append(
            {
                "xs_id": i + 1,
                "station_km": f"{sec.station_m / 1000.0:.4f}",
                "z_bed_m": f"{sec.z_bed:.4f}",
                "dx_m": f"{geom.dx_m[i]:.2f}" if i < geom.dx_m.size else "",
                "slope": f"{geom.slope[i]:.8f}" if i < geom.slope.size else "",
                "top_width_at_3m": f"{b:.1f}",
                "manning_n": f"{par.manning_n:.3f}",
                "k_s": f"{k_val:.2f}" if math.isfinite(k_val) else "",
                "x": f"{x_val:.4f}" if math.isfinite(x_val) else "",
                "dt_s": f"{par.dt_hours * 3600.0:.1f}",
                "k_scale": f"{par.k_scale:.3f}",
                "lon": f"{sec.lon:.6f}",
                "lat": f"{sec.lat:.6f}",
            }
        )
    write_csv(
        path,
        [
            "xs_id",
            "station_km",
            "z_bed_m",
            "dx_m",
            "slope",
            "top_width_at_3m",
            "manning_n",
            "k_s",
            "x",
            "dt_s",
            "k_scale",
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


def plot_result(res: RouteResult, out_dir: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("Thieu matplotlib - bo qua bieu do.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    t = res.hours
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    ax = axes[0]
    ax.plot(t, res.q_in, color="#c0392b", lw=1.8, label="Q thuong luu (TANK)")
    ax.plot(t, res.q[:, -1], color="#1d4ed8", lw=1.8, label="Q ha luu (sau Cunge)")
    mid = res.q.shape[1] // 2
    ax.plot(
        t,
        res.q[:, mid],
        color="#16a34a",
        lw=1.2,
        ls="--",
        label=f"Q XS{mid+1} ({res.station_km[mid]:.1f} km)",
    )
    ax.set_ylabel("Q (m3/s)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    ax.set_title(f"Muskingum-Cunge 1D - mat cat DEM moi {res.params.xs_spacing_m/1000:.1f} km")

    ax = axes[1]
    ax.plot(t, res.h_down, color="#7c3aed", lw=1.6, label="Bien muc nuoc ha luu")
    ax.plot(t, res.h[:, 0], color="#b45309", lw=1.4, label="H thuong luu (XS1)")
    ax.plot(t, res.h[:, -1], color="#0369a1", lw=1.2, ls="--", label=f"H ha luu (XS{res.q.shape[1]})")
    ax.set_ylabel("Muc nuoc H (m)")
    ax.set_xlabel("Gio")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(out_dir / "muskingum_hydrograph.png", dpi=140)
    plt.close(fig)

    pk = res.peak_time_index
    fig, ax = plt.subplots(figsize=(11, 4.4))
    km = res.station_km
    ax.fill_between(km, res.geom.z_bed, res.geom.z_bed.min() - 2, color="#c4a574", alpha=0.9, label="Day song (DEM)")
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
    fig.savefig(out_dir / "muskingum_profile.png", dpi=140)
    plt.close(fig)

    n_xs = len(res.geom.sections)
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
    fig.savefig(out_dir / "muskingum_cross_sections.png", dpi=140, bbox_inches="tight")
    plt.close(fig)


def print_summary(res: RouteResult) -> None:
    pk = res.peak_time_index
    i_pk = int(np.argmax(res.q_in))
    o_pk = int(np.argmax(res.q[:, -1]))
    lag_h = float(res.hours[o_pk] - res.hours[i_pk])
    dx_km = float(np.mean(res.geom.dx_m) / 1000.0) if res.geom.dx_m.size else 0.0
    print(f"Chieu dai song : {res.geom.length_m / 1000.0:.2f} km")
    print(f"Mat cat ngang  : {len(res.geom.sections)} tram, khoang TB {dx_km:.2f} km")
    print(f"Manning n      : {res.params.manning_n:.3f}")
    print(f"dt             : {res.params.dt_hours:.2f} gio  ({res.params.dt_hours * 3600.0:.0f} s, {res.hours.size} buoc)")
    print(f"k_scale        : {res.params.k_scale:.3f}")
    if res.k_s.size:
        print(f"K (TB)         : {float(np.nanmean(res.k_s)):.1f} s  (K = k_scale * max(dt, dx/c))")
        print(f"X (TB)         : {float(np.nanmean(res.x_cunge)):.3f}")
    for sec in res.geom.sections:
        _a, _p, b = sec.props(sec.z_bed + 3.0)
        print(
            f"  XS{sec.index+1:d}  {sec.station_m/1000:6.2f} km  "
            f"z_bed={sec.z_bed:7.2f} m  B(3m)={b:7.0f} m"
        )
    print(f"Q thuong luu   : TB {res.q_in.mean():.2f}  dinh {res.q_in.max():.2f} m3/s")
    print(f"Q ha luu       : TB {res.q[:, -1].mean():.2f}  dinh {res.q[:, -1].max():.2f} m3/s")
    print(f"Tre dinh       : {lag_h:.1f} gio")
    print(f"H ha luu       : {res.h_down.min():.2f} .. {res.h_down.max():.2f} m")
    print(f"H thuong luu   : {res.h[:, 0].min():.2f} .. {res.h[:, 0].max():.2f} m (gio {res.hours[pk]:.0f})")
    print(f"Can bang khoi  : {res.mass_balance_m3:.3e} m3  (tich phan I-O)")


def mass_balance(q_in: np.ndarray, q_out: np.ndarray, dt_s: float) -> float:
    return float((q_in.sum() - q_out.sum()) * dt_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Muskingum-Cunge 1D - mat cat DEM moi 1.5 km")
    p.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    p.add_argument(
        "--inflow",
        type=Path,
        default=DEFAULT_TANK_CSV,
        help="CSV Q vao (hour,q_m3s). Mac dinh: chay rainfall-runoff.py roi doc tank_result.csv",
    )
    p.add_argument("--h-csv", type=Path, default=MK_DOWN_H_CSV, help="CSV H ha luu (hour,h_m)")
    p.add_argument("--h-down", type=float, default=None, help="H ha luu hang (m); uu tien hon --h-csv")
    p.add_argument("--n", type=float, default=0.030, dest="manning_n")
    p.add_argument("--xs-spacing", type=float, default=XS_SPACING_M / 1000.0, help="Khoang cach mat cat (km), mac dinh 1.5")
    p.add_argument("--xs-half", type=float, default=1500.0, help="Nua be rong lay mat cat (m)")
    p.add_argument("--dt", type=float, default=1.0, help="Buoc thoi gian Muskingum-Cunge (gio)")
    p.add_argument(
        "--k-scale",
        type=float,
        default=1.0,
        dest="k_scale",
        help="He so hieu chinh K (K = k_scale * max(dt, dx/c))",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def run_model(args: argparse.Namespace) -> RouteResult:
    par = ChannelParams(
        manning_n=args.manning_n,
        xs_spacing_m=max(50.0, float(args.xs_spacing) * 1000.0),
        xs_half_width_m=float(args.xs_half),
        dt_hours=float(args.dt),
        k_scale=float(getattr(args, "k_scale", 1.0)),
    )
    hours0, q0 = load_tank_inflow(args.inflow)
    hours, q_in = resample_series(hours0, q0, par.dt_hours)
    q_in = np.maximum(q_in, par.q_min)

    geom = extract_river(args.dem, par)

    h_down = load_downstream_stage(
        hours,
        getattr(args, "h_csv", None),
        getattr(args, "h_down", None),
        float(geom.z_bed[-1]),
        q_in,
    )

    q = route_river(q_in, geom, par)
    h, y = stages_backwater(q, h_down, geom, par)
    k_s, x_cunge = reach_kx_tables(geom, par, float(np.mean(q_in)))
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
        mass_balance_m3=mass_balance(q_in, q[:, -1], par.dt_hours * 3600.0),
        k_s=k_s,
        x_cunge=x_cunge,
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    print(f"DEM long song : {args.dem}")
    q_src = args.inflow if args.inflow is not None else DEFAULT_TANK_CSV
    if Path(q_src).resolve() == DEFAULT_TANK_CSV.resolve():
        print(f"Q vao          : chay {DEFAULT_TANK_SCRIPT} -> {DEFAULT_TANK_CSV}")
    else:
        print(f"Q vao          : {q_src}")
    if args.h_down is not None:
        print(f"H ha luu       : hang {args.h_down:.3f} m")
    else:
        h_path = args.h_csv if args.h_csv is not None else MK_DOWN_H_CSV
        print(f"H ha luu       : {h_path}")
    print(f"Lay mat cat ngang moi {args.xs_spacing:.2f} km doc long song...")
    res = run_model(args)
    print_summary(res)

    out = args.out_dir
    write_inflow_csv(res.hours, res.q_in, out / "demo_inflow_q_m3s.csv")
    h_out = out / "demo_downstream_stage.csv"
    h_src = args.h_csv if args.h_csv is not None else MK_DOWN_H_CSV
    same_h_file = (
        args.h_down is None
        and h_src is not None
        and Path(h_src).resolve() == h_out.resolve()
    )
    if not same_h_file:
        write_downstream_csv(res.hours, res.h_down, h_out)
    write_geometry_csv(
        res.geom,
        res.params,
        out / "demo_river_geometry.csv",
        k_s=res.k_s,
        x_cunge=res.x_cunge,
    )
    write_cross_sections_csv(res.geom, out / "demo_cross_sections.csv")
    write_result_csv(res, out / "muskingum_result.csv")
    print(f"Mat cat ngang  : {out / 'demo_cross_sections.csv'}  (offset_m, z_m)")
    print(f"Tram XS        : {out / 'demo_river_geometry.csv'}")
    print(f"Demo H ha luu  : {h_out}")
    print(f"Ket qua        : {out / 'muskingum_result.csv'}")
    if not args.no_plot:
        plot_result(res, out)
        print(f"Bieu do        : {out / 'muskingum_hydrograph.png'}")
        print(f"Mat cat doc    : {out / 'muskingum_profile.png'}")
        print(f"Mat cat ngang  : {out / 'muskingum_cross_sections.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

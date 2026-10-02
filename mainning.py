"""
Uoc Manning n so bo tu do go DEM quanh tung mat cat 1D.

- Doc toa do XS: saint_venant_output/demo_river_geometry.csv
- Doc DEM: projects/data/dem-song-hong.tif
- Trong cua so quanh XS: khử mặt phẳng (dốc) rồi lấy σ phần dư
- Ánh xạ σ → n trong [n_min, n_max] (Chow, giá trị khởi tạo)
- Không thay thế hiệu chỉnh Q–H. Mặc định ghi file mới, không đè demo_manning_n.csv

Chay:
  .\\venv\\Scripts\\python.exe flood_model\\mainning.py
  .\\venv\\Scripts\\python.exe flood_model\\mainning.py --radius 250 --write-input
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
from rasterio.windows import from_bounds, Window

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.gis import lonlat_to_dem_xy, open_dem, resolve_dem_path
from flood_model.paths import DEFAULT_DEM, PACKAGE_DIR as FM_DIR

DEFAULT_GEOM = FM_DIR / "saint_venant_output" / "demo_river_geometry.csv"
DEFAULT_OUT = FM_DIR / "saint_venant_output" / "demo_manning_n_dem_init.csv"
DEFAULT_INPUT_N = FM_DIR / "saint_venant_output" / "demo_manning_n.csv"

# Bang Chow so bo (long tu nhien / bai song)
N_MIN = 0.025
N_MAX = 0.080
# Bien do go (m) sau khi khu doc — 10 m DEM dong bang
SIGMA_LO = 0.25
SIGMA_HI = 2.80
RADIUS_MIN_M = 80.0
RADIUS_MAX_M = 400.0


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_geometry(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"CSV mat cat rong: {path}")
        fields = {n.strip().lower(): n for n in reader.fieldnames}
        need = ("xs_id", "station_km", "lon", "lat")
        for key in need:
            if key not in fields:
                raise ValueError(f"CSV can cot {key}: {path}")
        wcol = fields.get("top_width_at_3m") or fields.get("width_m")
        for i, raw in enumerate(reader):
            try:
                xs_id = int(float(raw[fields["xs_id"]]))
                st = float(raw[fields["station_km"]])
                lon = float(raw[fields["lon"]])
                lat = float(raw[fields["lat"]])
            except (TypeError, ValueError, KeyError):
                continue
            width = float("nan")
            if wcol:
                try:
                    width = float(raw[wcol])
                except (TypeError, ValueError):
                    width = float("nan")
            rows.append({
                "xs_id": xs_id,
                "station_km": st,
                "lon": lon,
                "lat": lat,
                "width_m": width,
                "index": i,
            })
    if not rows:
        raise ValueError(f"Khong doc duoc mat cat tu {path}")
    return rows


def xs_radius_m(width_m: float, override: Optional[float]) -> float:
    if override is not None and override > 0:
        return float(override)
    if math.isfinite(width_m) and width_m > 0:
        half = 0.45 * width_m
        return float(min(max(half, RADIUS_MIN_M), RADIUS_MAX_M))
    return 200.0


def _plane_residual_std(z: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> float:
    """σ phần dư sau khi khử mặt phẳng z = a + b x + c y."""
    n = int(z.size)
    if n < 6:
        return float(np.nanstd(z)) if n else float("nan")
    a = np.column_stack([np.ones(n), xs, ys])
    try:
        coef, *_ = np.linalg.lstsq(a, z, rcond=None)
        resid = z - a @ coef
    except np.linalg.LinAlgError:
        resid = z - float(np.mean(z))
    return float(np.std(resid, ddof=1)) if resid.size > 1 else 0.0


def sample_roughness(src, x: float, y: float, radius_m: float) -> tuple[float, int]:
    cell = max(abs(float(src.res[0])), abs(float(src.res[1])), 1.0)
    pad = max(radius_m, cell)
    win: Window = from_bounds(x - pad, y - pad, x + pad, y + pad, transform=src.transform)
    win = win.round_offsets().round_lengths()
    if win.width < 2 or win.height < 2:
        return float("nan"), 0
    z = src.read(1, window=win, boundless=True).astype(np.float64)
    nodata = src.nodata
    mask = np.isfinite(z)
    if nodata is not None:
        mask &= z != nodata
    if not np.any(mask):
        return float("nan"), 0
    rows, cols = np.where(mask)
    transform = src.window_transform(win)
    xx = transform.c + (cols + 0.5) * transform.a + (rows + 0.5) * transform.b
    yy = transform.f + (cols + 0.5) * transform.d + (rows + 0.5) * transform.e
    inside = (xx - x) ** 2 + (yy - y) ** 2 <= radius_m * radius_m
    if int(np.count_nonzero(inside)) < 6:
        zz = z[mask]
        return _plane_residual_std(zz, xx, yy), int(zz.size)
    zz = z[mask][inside]
    return _plane_residual_std(zz, xx[inside], yy[inside]), int(zz.size)


def sigma_to_n(sigma_m: float, n_min: float, n_max: float, s_lo: float, s_hi: float) -> float:
    if not math.isfinite(sigma_m):
        return 0.5 * (n_min + n_max)
    t = (sigma_m - s_lo) / max(s_hi - s_lo, 1e-6)
    t = min(max(t, 0.0), 1.0)
    # Can bac 2: σ vừa phải đã tăng n rõ, tránh nhảy quá mạnh
    return float(n_min + (n_max - n_min) * math.sqrt(t))


def smooth_along(values: np.ndarray, station_km: np.ndarray, sigma_km: float = 0.75) -> np.ndarray:
    if values.size < 3 or sigma_km <= 0:
        return values.copy()
    out = np.empty_like(values)
    for i, s0 in enumerate(station_km):
        w = np.exp(-0.5 * ((station_km - s0) / sigma_km) ** 2)
        w[~np.isfinite(values)] = 0.0
        sw = float(w.sum())
        out[i] = float(np.dot(w, np.nan_to_num(values, nan=0.0)) / sw) if sw > 0 else values[i]
    return out


def estimate(
    geom: list[dict[str, Any]],
    dem_path: Path,
    *,
    radius_m: Optional[float],
    n_min: float,
    n_max: float,
    sigma_lo: float,
    sigma_hi: float,
    smooth_km: float,
) -> list[dict[str, Any]]:
    from rasterio.transform import rowcol

    with open_dem(dem_path) as src:
        lons = [r["lon"] for r in geom]
        lats = [r["lat"] for r in geom]
        xs, ys = lonlat_to_dem_xy(src, lons, lats)
        out: list[dict[str, Any]] = []
        for i, rec in enumerate(geom):
            rad = xs_radius_m(float(rec["width_m"]), radius_m)
            # Bo XS nam ngoai raster
            try:
                rr, cc = rowcol(src.transform, float(xs[i]), float(ys[i]))
                inside = 0 <= int(rr) < src.height and 0 <= int(cc) < src.width
            except Exception:
                inside = False
            if not inside:
                sig, ncell = float("nan"), 0
            else:
                sig, ncell = sample_roughness(src, float(xs[i]), float(ys[i]), rad)
            n_raw = sigma_to_n(sig, n_min, n_max, sigma_lo, sigma_hi)
            rec2 = dict(rec)
            rec2["radius_m"] = rad
            rec2["sigma_m"] = sig
            rec2["n_cells"] = ncell
            rec2["n_raw"] = n_raw
            out.append(rec2)
    st = np.array([r["station_km"] for r in out], dtype=float)
    n_raw = np.array([r["n_raw"] for r in out], dtype=float)
    n_s = smooth_along(n_raw, st, smooth_km)
    n_s = np.clip(n_s, n_min, n_max)
    for rec, n in zip(out, n_s):
        rec["manning_n"] = float(n)
    return out


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    fields = [
        "xs_id",
        "station_km",
        "manning_n",
        "n_raw",
        "sigma_m",
        "radius_m",
        "n_cells",
        "lon",
        "lat",
    ]
    write_csv_rows(
        path,
        fields,
        [
            {
                "xs_id": r["xs_id"],
                "station_km": f"{float(r['station_km']):.4f}",
                "manning_n": f"{float(r['manning_n']):.4f}",
                "n_raw": f"{float(r['n_raw']):.4f}",
                "sigma_m": f"{float(r['sigma_m']):.4f}" if math.isfinite(float(r["sigma_m"])) else "",
                "radius_m": f"{float(r['radius_m']):.1f}",
                "n_cells": int(r["n_cells"]),
                "lon": f"{float(r['lon']):.6f}",
                "lat": f"{float(r['lat']):.6f}",
            }
            for r in rows
        ],
        rebuild_hydro=False,
    )


def write_input_n(rows: list[dict[str, Any]], path: Path) -> None:
    write_csv_rows(
        path,
        ["xs_id", "station_km", "manning_n"],
        [
            {
                "xs_id": r["xs_id"],
                "station_km": f"{float(r['station_km']):.4f}",
                "manning_n": f"{float(r['manning_n']):.4f}",
            }
            for r in rows
        ],
        rebuild_hydro=False,
    )


def plot_n(rows: list[dict[str, Any]], path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    st = [r["station_km"] for r in rows]
    n = [r["manning_n"] for r in rows]
    sig = [r["sigma_m"] if math.isfinite(float(r["sigma_m"])) else float("nan") for r in rows]
    fig, axes = plt.subplots(2, 1, figsize=(10.5, 6.2), sharex=True)
    axes[0].plot(st, n, color="#b45309", lw=1.6)
    axes[0].set_ylabel("Manning n (khởi tạo)")
    axes[0].grid(True, alpha=0.3)
    axes[0].set_title("n sơ bộ từ độ gồ DEM quanh mặt cắt")
    axes[1].plot(st, sig, color="#1d4ed8", lw=1.4)
    axes[1].set_ylabel("σ dư DEM (m)")
    axes[1].set_xlabel("Lý trình (km)")
    axes[1].grid(True, alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Uoc Manning n so bo tu do go DEM quanh mat cat")
    p.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    p.add_argument("--geom", type=Path, default=DEFAULT_GEOM)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--radius", type=float, default=None, help="Ban kinh lay mau (m). Mac dinh: 0.45*be rong, 80–400 m")
    p.add_argument("--n-min", type=float, default=N_MIN)
    p.add_argument("--n-max", type=float, default=N_MAX)
    p.add_argument("--sigma-lo", type=float, default=SIGMA_LO)
    p.add_argument("--sigma-hi", type=float, default=SIGMA_HI)
    p.add_argument("--smooth-km", type=float, default=0.75, help="Lam muot n theo ly trinh (km)")
    p.add_argument(
        "--write-input",
        action="store_true",
        help="Ghi de saint_venant_output/demo_manning_n.csv (dau vao 1D)",
    )
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    geom_path = Path(args.geom)
    if not csv_available(geom_path):
        raise FileNotFoundError(f"Khong thay file mat cat: {geom_path}")
    dem_path = resolve_dem_path(None, str(args.dem) if args.dem else None)
    geom = load_geometry(geom_path)
    print(f"Mat cat : {len(geom)}  tu {geom_path.name}", flush=True)
    print(f"DEM     : {dem_path}", flush=True)
    rows = estimate(
        geom,
        dem_path,
        radius_m=args.radius,
        n_min=float(args.n_min),
        n_max=float(args.n_max),
        sigma_lo=float(args.sigma_lo),
        sigma_hi=float(args.sigma_hi),
        smooth_km=float(args.smooth_km),
    )
    nn = np.array([r["manning_n"] for r in rows], dtype=float)
    ss = np.array([r["sigma_m"] for r in rows], dtype=float)
    ok = np.isfinite(ss)
    print(
        f"σ dư    : {float(np.nanmin(ss[ok])) if ok.any() else 0:.2f} … "
        f"{float(np.nanmax(ss[ok])) if ok.any() else 0:.2f} m",
        flush=True,
    )
    print(f"n khởi tạo: {float(nn.min()):.4f} … {float(nn.max()):.4f}  (TB {float(nn.mean()):.4f})", flush=True)
    out = Path(args.out)
    write_csv(rows, out)
    print(f"CSV     : {out}", flush=True)
    if args.write_input:
        write_input_n(rows, DEFAULT_INPUT_N)
        print(f"Ghi de  : {DEFAULT_INPUT_N}  (chi khoi tao — hay hieu chinh lai)", flush=True)
    if not args.no_plot:
        fig = out.with_suffix(".png")
        if plot_n(rows, fig):
            print(f"Bieu do : {fig}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

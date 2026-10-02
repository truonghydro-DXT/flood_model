"""
Mo hinh be chua TANK (Sugawara) — chuyen mua rao thanh dong chay.

Bon be xep chong:
  Be 1 (mat)      : 2 cua ben + 1 cua day  -> dong chay mat + tham
  Be 2 (trung gian): 1 cua ben + 1 cua day  -> dong chay trung gian
  Be 3 (ngam nong): 1 cua ben + 1 cua day  -> dong chay co so nong
  Be 4 (ngam sau) : 1 cua ben              -> dong chay co so sau

Tai moi buoc thoi gian:
  mua vao be 1 -> bot ET (tu tren xuong) -> cua ben (neu vuot nguong)
  -> tham xuong be duoi -> tong Q = tong cua ben.

Chay demo:
  .\\venv\\Scripts\\python.exe rainfall-runoff.py
  .\\venv\\Scripts\\python.exe rainfall-runoff.py --no-plot
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import numpy.typing as npt

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
DEFAULT_OUT_DIR = PROJECT_ROOT / "rainfall_runoff_output"


from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.boundary import (
    demo_series,
    load_forcing_csv,
    load_rainfall_csv,
    write_demo_rainfall_csv,
)


# ---------------------------------------------------------------------------
# Tham so mo hinh
# ---------------------------------------------------------------------------

@dataclass
class TankParams:
    """He so TANK. Don vi luu tru / nguong: mm. He so cua: 1/buoc thoi gian."""

    # Be 1 — mat
    a0: float = 0.20   # tham xuong be 2
    a1: float = 0.68   # cua ben thap (chay mat)
    a2: float = 0.12   # cua ben cao (chay mat khi mua lon)
    h1a: float = 15.0  # nguong cua thap (mm)
    h1b: float = 40.0  # nguong cua cao (mm)

    # Be 2 — trung gian
    b0: float = 0.08
    b1: float = 0.10
    h2: float = 15.0

    # Be 3 — ngam nong
    c0: float = 0.02
    c1: float = 0.04
    h3: float = 15.0

    # Be 4 — ngam sau (khong cua day)
    d1: float = 0.005
    h4: float = 0.0

    # Luu tru ban dau (mm)
    s1_0: float = 8.0
    s2_0: float = 15.0
    s3_0: float = 22.0
    s4_0: float = 35.0


TANK_PARAM_NAMES = [f.name for f in fields(TankParams)]
DEFAULT_PARAMS_CSV = DEFAULT_OUT_DIR / "demo_tank_params.csv"


@dataclass
class Basin:
    """Luu vuc: dien tich (km2) va buoc thoi gian (gio).

    Doi area_km2 o day (hoac --area khi chay CLI). TANK tinh q_mm (lop nuoc),
    Q (m3/s) = q_mm * area_km2 / (3.6 * dt_hours).
    """

    area_km2: float = 3250.0
    dt_hours: float = 1.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"Luu vuc ({self.area_km2:g} km2)"


def params_to_dict(basin: Basin, params: TankParams) -> dict[str, float]:
    out = {"area_km2": float(basin.area_km2), "dt_hours": float(basin.dt_hours)}
    for name in TANK_PARAM_NAMES:
        out[name] = float(getattr(params, name))
    return out


def apply_params_dict(data: dict[str, Any]) -> tuple[Basin, TankParams]:
    basin = Basin()
    params = TankParams()
    if data.get("area_km2") is not None and str(data.get("area_km2")).strip() != "":
        basin.area_km2 = float(data["area_km2"])
    if data.get("dt_hours") is not None and str(data.get("dt_hours")).strip() != "":
        basin.dt_hours = float(data["dt_hours"])
    for name in TANK_PARAM_NAMES:
        raw = data.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        setattr(params, name, float(raw))
    basin.name = f"Luu vuc ({basin.area_km2:g} km2)"
    return basin, params


def load_params_csv(path: Path) -> tuple[Basin, TankParams]:
    data: dict[str, Any] = {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return Basin(), TankParams()
        names = {n.strip().lower(): n for n in reader.fieldnames}
        if "param" in names and "value" in names:
            for row in reader:
                key = str(row.get(names["param"], "")).strip()
                if key:
                    data[key] = row.get(names["value"])
        else:
            rows = list(reader)
            if rows:
                data = {str(k).strip(): v for k, v in rows[0].items()}
    return apply_params_dict(data)


def write_params_csv(basin: Basin, params: TankParams, path: Path) -> None:
    rows = [
        {"param": "area_km2", "value": f"{basin.area_km2:.4f}"},
        {"param": "dt_hours", "value": f"{basin.dt_hours:.4f}"},
    ]
    for name in TANK_PARAM_NAMES:
        rows.append({"param": name, "value": f"{getattr(params, name):.6g}"})
    write_csv_rows(path, ["param", "value"], rows, rebuild_hydro=False)


@dataclass
class TankResult:
    rainfall_mm: np.ndarray
    pet_mm: np.ndarray
    et_mm: np.ndarray
    q_mm: np.ndarray
    q_m3s: np.ndarray
    q_surface_mm: np.ndarray
    q_inter_mm: np.ndarray
    q_base_shallow_mm: np.ndarray
    q_base_deep_mm: np.ndarray
    s1: np.ndarray
    s2: np.ndarray
    s3: np.ndarray
    s4: np.ndarray
    hours: np.ndarray
    params: TankParams
    basin: Basin
    mass_balance_mm: float = 0.0

    @property
    def n(self) -> int:
        return int(self.rainfall_mm.size)


# ---------------------------------------------------------------------------
# Mo hinh TANK
# ---------------------------------------------------------------------------

class TankModel:
    """Mo hinh 4 be chua Sugawara, buoc tinh roi rac."""

    def __init__(self, params: Optional[TankParams] = None, basin: Optional[Basin] = None):
        self.params = params or TankParams()
        self.basin = basin or Basin()

    def run(
        self,
        rainfall_mm: npt.ArrayLike,
        pet_mm: Optional[npt.ArrayLike] = None,
    ) -> TankResult:
        p = np.asarray(rainfall_mm, dtype=float)
        n = p.size
        if pet_mm is None:
            pet = np.zeros(n, dtype=float)
        else:
            pet = np.asarray(pet_mm, dtype=float)
            if pet.size != n:
                raise ValueError("pet_mm phai cung do dai voi rainfall_mm")

        par = self.params
        s1 = float(par.s1_0)
        s2 = float(par.s2_0)
        s3 = float(par.s3_0)
        s4 = float(par.s4_0)
        s0 = s1 + s2 + s3 + s4

        out_et = np.zeros(n)
        q_s = np.zeros(n)
        q_i = np.zeros(n)
        q_bs = np.zeros(n)
        q_bd = np.zeros(n)
        st1 = np.zeros(n)
        st2 = np.zeros(n)
        st3 = np.zeros(n)
        st4 = np.zeros(n)

        print(f"Tinh TANK {n} buoc (dt={self.basin.dt_hours:g} h, A={self.basin.area_km2:g} km2)...", flush=True)
        for t in range(n):
            s1 += p[t]
            et = _take_et(pet[t], [s1, s2, s3, s4])
            s1, s2, s3, s4 = et.storages
            out_et[t] = et.actual

            q1a = par.a1 * max(s1 - par.h1a, 0.0)
            q1b = par.a2 * max(s1 - par.h1b, 0.0)
            q10 = par.a0 * s1
            q1a, q1b, q10 = _cap_outflows(s1, q1a, q1b, q10)
            s1 -= q1a + q1b + q10

            s2 += q10
            q2 = par.b1 * max(s2 - par.h2, 0.0)
            q20 = par.b0 * s2
            q2, q20 = _cap_outflows(s2, q2, q20)
            s2 -= q2 + q20

            s3 += q20
            q3 = par.c1 * max(s3 - par.h3, 0.0)
            q30 = par.c0 * s3
            q3, q30 = _cap_outflows(s3, q3, q30)
            s3 -= q3 + q30

            s4 += q30
            q4 = par.d1 * max(s4 - par.h4, 0.0)
            q4 = min(q4, s4)
            s4 -= q4

            s1 = max(s1, 0.0)
            s2 = max(s2, 0.0)
            s3 = max(s3, 0.0)
            s4 = max(s4, 0.0)

            q_s[t] = q1a + q1b
            q_i[t] = q2
            q_bs[t] = q3
            q_bd[t] = q4
            st1[t] = s1
            st2[t] = s2
            st3[t] = s3
            st4[t] = s4
            if n >= 24 and (t + 1) % 24 == 0:
                print(f"  da tinh {t + 1}/{n} gio", flush=True)

        q_mm = q_s + q_i + q_bs + q_bd
        dt = self.basin.dt_hours
        q_m3s = q_mm * self.basin.area_km2 / (3.6 * dt)

        s_end = st1[-1] + st2[-1] + st3[-1] + st4[-1]
        mass = float(p.sum() - out_et.sum() - q_mm.sum() - (s_end - s0))

        hours = np.arange(n, dtype=float) * dt
        return TankResult(
            rainfall_mm=p,
            pet_mm=pet,
            et_mm=out_et,
            q_mm=q_mm,
            q_m3s=q_m3s,
            q_surface_mm=q_s,
            q_inter_mm=q_i,
            q_base_shallow_mm=q_bs,
            q_base_deep_mm=q_bd,
            s1=st1,
            s2=st2,
            s3=st3,
            s4=st4,
            hours=hours,
            params=par,
            basin=self.basin,
            mass_balance_mm=mass,
        )


@dataclass
class _EtStep:
    actual: float
    storages: list[float]


def _take_et(demand: float, storages: list[float]) -> _EtStep:
    """Tru boc hoi tho tu be tren xuong duoi."""
    remain = max(float(demand), 0.0)
    out = list(storages)
    used = 0.0
    for i, s in enumerate(out):
        if remain <= 0:
            break
        take = min(s, remain)
        out[i] = s - take
        remain -= take
        used += take
    return _EtStep(actual=used, storages=out)


def _cap_outflows(storage: float, *flows: float) -> tuple[float, ...]:
    """Giu tong cua khong vuot luu tru (tranh be am)."""
    total = sum(flows)
    if total <= 0 or storage <= 0:
        return tuple(0.0 for _ in flows)
    if total <= storage:
        return flows
    scale = storage / total
    return tuple(f * scale for f in flows)


# ---------------------------------------------------------------------------
# Xuat ket qua
# ---------------------------------------------------------------------------

def write_csv(result: TankResult, path: Path) -> None:
    fields = [
        "hour",
        "rainfall_mm",
        "pet_mm",
        "et_mm",
        "q_total_mm",
        "q_m3s",
        "q_surface_mm",
        "q_inter_mm",
        "q_base_shallow_mm",
        "q_base_deep_mm",
        "s1_mm",
        "s2_mm",
        "s3_mm",
        "s4_mm",
    ]
    rows = [
        {
            "hour": f"{result.hours[i]:.1f}",
            "rainfall_mm": f"{result.rainfall_mm[i]:.4f}",
            "pet_mm": f"{result.pet_mm[i]:.4f}",
            "et_mm": f"{result.et_mm[i]:.4f}",
            "q_total_mm": f"{result.q_mm[i]:.4f}",
            "q_m3s": f"{result.q_m3s[i]:.4f}",
            "q_surface_mm": f"{result.q_surface_mm[i]:.4f}",
            "q_inter_mm": f"{result.q_inter_mm[i]:.4f}",
            "q_base_shallow_mm": f"{result.q_base_shallow_mm[i]:.4f}",
            "q_base_deep_mm": f"{result.q_base_deep_mm[i]:.4f}",
            "s1_mm": f"{result.s1[i]:.4f}",
            "s2_mm": f"{result.s2[i]:.4f}",
            "s3_mm": f"{result.s3[i]:.4f}",
            "s4_mm": f"{result.s4[i]:.4f}",
        }
        for i in range(result.n)
    ]
    write_csv_rows(path, fields, rows)


def plot_result(result: TankResult, path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    t = result.hours
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)

    ax = axes[0]
    ax.bar(t, result.rainfall_mm, width=result.basin.dt_hours * 0.9, color="#4a90d9", label="Mưa (mm)")
    ax.set_ylabel("Mưa (mm)")
    ax.invert_yaxis()
    ax.legend(loc="upper right")
    ax.set_title(f"TANK — {result.basin.name}")

    ax = axes[1]
    ax.plot(t, result.q_m3s, color="#c0392b", lw=1.8, label="Lưu lượng dòng chảy (m3/s)")
    ax.fill_between(t, 0, result.q_surface_mm * result.basin.area_km2 / (3.6 * result.basin.dt_hours),
                    color="#e67e22", alpha=0.35, label="Dòng chảy mặt")
    ax.plot(t, result.q_inter_mm * result.basin.area_km2 / (3.6 * result.basin.dt_hours),
            color="#27ae60", lw=1.2, label="Dòng chảy ngầm")
    ax.plot(
        t,
        (result.q_base_shallow_mm + result.q_base_deep_mm)
        * result.basin.area_km2
        / (3.6 * result.basin.dt_hours),
        color="#8e44ad",
        lw=1.2,
        label="Dòng chảy sát mặt",
    )
    ax.set_ylabel("Q (m3/s)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(t, result.s1, label="Bể chứa mặt")
    ax.plot(t, result.s2, label="Bể trung gian")
    ax.plot(t, result.s3, label="Bể ngầm nông")
    ax.plot(t, result.s4, label="Bể ngầm sâu")
    ax.set_ylabel("Lớp nước dòng chảy (mm)")
    ax.set_xlabel("Giờ")
    ax.legend(loc="upper right", ncol=2)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def _configure_stdio() -> None:
    """Windows cp1252 khong in duoc tieng Viet; ep UTF-8 khi pipe/log."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def print_summary(result: TankResult) -> None:
    peak_i = int(np.argmax(result.q_m3s))
    print(f"Lưu vực      : {result.basin.name}", flush=True)
    print(f"Diện tích    : {result.basin.area_km2:.2f} km2", flush=True)
    print(f"Bước thời gian: {result.basin.dt_hours:.2f} giờ", flush=True)
    print(f"Số bước      : {result.n}", flush=True)
    print(f"Tổng mưa     : {result.rainfall_mm.sum():.2f} mm", flush=True)
    print(f"Tổng ET      : {result.et_mm.sum():.2f} mm", flush=True)
    print(f"Tổng dòng chảy: {result.q_mm.sum():.2f} mm  ({result.q_m3s.mean():.3f} m3/s TB)", flush=True)
    print(f"  - mặt      : {result.q_surface_mm.sum():.2f} mm", flush=True)
    print(f"  - trung gian: {result.q_inter_mm.sum():.2f} mm", flush=True)
    print(f"  - cơ sở nông: {result.q_base_shallow_mm.sum():.2f} mm", flush=True)
    print(f"  - cơ sở sâu : {result.q_base_deep_mm.sum():.2f} mm", flush=True)
    print(f"Đỉnh dòng chảy: {result.q_m3s[peak_i]:.3f} m3/s tại giờ {result.hours[peak_i]:.1f}", flush=True)
    print(f"Hệ số chảy   : {result.q_mm.sum() / max(result.rainfall_mm.sum(), 1e-9):.3f}", flush=True)
    print(f"Cân bằng khối: {result.mass_balance_mm:.4e} mm (gần 0 = OK)", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mo hinh TANK mua-rao / dong-chay")
    p.add_argument("--csv", type=Path, default=None, help="CSV co cot rainfall_mm (bo qua thi dung mua demo)")
    p.add_argument(
        "--area",
        type=float,
        default=None,
        help="Dien tich luu vuc km2 (mac dinh: Basin.area_km2 hoac file --params-csv)",
    )
    p.add_argument(
        "--dt",
        type=float,
        default=None,
        help="Buoc thoi gian (gio)",
    )
    p.add_argument(
        "--params-csv",
        type=Path,
        default=None,
        help="CSV thong so TANK (cot param,value). Mac dinh: rainfall_runoff_output/demo_tank_params.csv neu co",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    basin = Basin()
    params = TankParams()
    params_path = args.params_csv
    if params_path is None:
        candidate = args.out_dir / "demo_tank_params.csv"
        if csv_available(candidate):
            params_path = candidate
    if params_path is not None:
        if not csv_available(params_path):
            raise FileNotFoundError(f"Khong thay file thong so: {params_path}")
        basin, params = load_params_csv(params_path)
        print(f"Doc thong so tu {params_path}", flush=True)
    if args.area is not None:
        basin.area_km2 = float(args.area)
        basin.name = f"Luu vuc ({basin.area_km2:g} km2)"
    if args.dt is not None:
        basin.dt_hours = float(args.dt)
    model = TankModel(params=params, basin=basin)

    if args.csv is not None:
        rain, pet = load_forcing_csv(args.csv)
        print(f"Doc mua tu {args.csv}", flush=True)
    else:
        demo_path = args.out_dir / "demo_rainfall.csv"
        if csv_available(demo_path):
            rain, pet = load_forcing_csv(demo_path)
            print(f"Doc mua tu {demo_path} (giu file hien co, khong ghi de)", flush=True)
        else:
            rain, pet = demo_series(n_hours=168, dt_hours=basin.dt_hours)
            write_demo_rainfall_csv(rain, pet, demo_path, basin.dt_hours)
            print(f"Dung mua demo 7 ngay. Ghi: {demo_path}", flush=True)

    result = model.run(rain, pet)
    print_summary(result)

    out_csv = args.out_dir / "tank_result.csv"
    write_csv(result, out_csv)
    print(f"Ket qua CSV : {out_csv}", flush=True)

    out_params = args.out_dir / "demo_tank_params.csv"
    write_params_csv(result.basin, result.params, out_params)
    print(f"Thong so CSV: {out_params}", flush=True)

    if not args.no_plot:
        fig_path = args.out_dir / "tank_hydrograph.png"
        if plot_result(result, fig_path):
            print(f"Bieu do     : {fig_path}", flush=True)
        else:
            print("Khong ve duoc (thieu matplotlib). Van co CSV.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

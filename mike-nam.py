"""
Mo hinh mua-rao / dong-chay MIKE NAM (Nedbor-Afstromnings-Model, DHI).

Ba be chua + ba nhanh dong chay (tuyen tinh):
  U  be mat (Umax)     : mua, ET, xa lien luu
  L  be re / dat (Lmax): ET dat, chia tham / nap nuoc ngam
  G  be nuoc ngam      : dong chay co so

Tai moi buoc:
  mua -> U -> ET (U roi L) -> vuot Umax = Pn
  QOF = CQOF * f(L/Lmax, TOF) * Pn     -> 2 be tuyen tinh CK1,2
  QIF = U/CKIF * f(L/Lmax, TIF)        -> 1 be tuyen tinh CKIF
  nap G = f(L/Lmax, TG) * (Pn - QOF)
  QBF = G / CKBF

Dieu kien bien mac dinh: rainfall_runoff_output/demo_rainfall.csv
  (cot hour, rainfall_mm, pet_mm) — cung file voi mo hinh TANK.

Chay:
  .\\venv\\Scripts\\python.exe flood_model\\mike-nam.py
  .\\venv\\Scripts\\python.exe flood_model\\mike-nam.py --no-plot
  .\\venv\\Scripts\\python.exe flood_model\\mike-nam.py --csv flood_model\\rainfall_runoff_output\\demo_rainfall.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
import numpy.typing as npt

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
DEFAULT_OUT_DIR = PACKAGE_DIR / "rainfall_runoff_output"
DEFAULT_RAIN_CSV = DEFAULT_OUT_DIR / "demo_rainfall.csv"
DEFAULT_PARAMS_CSV = DEFAULT_OUT_DIR / "demo_nam_params.csv"


from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.boundary import load_forcing_csv


# ---------------------------------------------------------------------------
# Tham so NAM (DHI MIKE 11 / MIKE+ RR)
# ---------------------------------------------------------------------------

@dataclass
class NamParams:
    """He so NAM. Luu tru: mm. Hang so thoi gian: gio. Nguong: 0–1."""

    umax: float = 14.0      # suc chua be mat (mm)
    lmax: float = 160.0     # suc chua dat / vung re (mm)
    cqof: float = 0.52      # he so dong chay mat (0–1)
    ckif: float = 280.0     # hang so thoi gian lien luu (gio)
    ck12: float = 18.0      # hang so thoi gian 2 be dong chay mat (gio)
    tof: float = 0.12       # nguong L/Lmax cho dong chay mat
    tif: float = 0.00       # nguong L/Lmax cho lien luu
    tg: float = 0.00        # nguong L/Lmax cho nap nuoc ngam
    ckbf: float = 650.0     # hang so thoi gian dong chay co so (gio)
    carea: float = 1.00     # ti le dien tich nap nuoc ngam / dien tich mat

    u0: float = 8.0         # U ban dau (mm)
    l0: float = 88.0        # L ban dau (mm)
    g0: float = 410.0       # G ban dau (mm) — tao Q co so ~ TANK
    sof1_0: float = 0.0     # be dinh tuyen mat 1 (mm)
    sof2_0: float = 0.0     # be dinh tuyen mat 2 (mm)
    sif_0: float = 0.0      # be dinh tuyen lien luu (mm)


NAM_PARAM_NAMES = [f.name for f in fields(NamParams)]


@dataclass
class Basin:
    """Luu vuc: dien tich (km2) va buoc thoi gian (gio).

    Q (m3/s) = q_mm * area_km2 / (3.6 * dt_hours).
    Mac dinh trung TANK (demo_tank_params.csv).
    """

    area_km2: float = 2950.0
    dt_hours: float = 1.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"Luu vuc NAM ({self.area_km2:g} km2)"


def params_to_dict(basin: Basin, params: NamParams) -> dict[str, float]:
    out = {"area_km2": float(basin.area_km2), "dt_hours": float(basin.dt_hours)}
    for name in NAM_PARAM_NAMES:
        out[name] = float(getattr(params, name))
    return out


def apply_params_dict(data: dict[str, Any]) -> tuple[Basin, NamParams]:
    basin = Basin()
    params = NamParams()
    if data.get("area_km2") is not None and str(data.get("area_km2")).strip() != "":
        basin.area_km2 = float(data["area_km2"])
    if data.get("dt_hours") is not None and str(data.get("dt_hours")).strip() != "":
        basin.dt_hours = float(data["dt_hours"])
    for name in NAM_PARAM_NAMES:
        raw = data.get(name)
        if raw is None or str(raw).strip() == "":
            continue
        setattr(params, name, float(raw))
    basin.name = f"Luu vuc NAM ({basin.area_km2:g} km2)"
    return basin, params


def load_params_csv(path: Path) -> tuple[Basin, NamParams]:
    data: dict[str, Any] = {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return Basin(), NamParams()
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


def write_params_csv(basin: Basin, params: NamParams, path: Path) -> None:
    rows = [
        {"param": "area_km2", "value": f"{basin.area_km2:.4f}"},
        {"param": "dt_hours", "value": f"{basin.dt_hours:.4f}"},
    ]
    for name in NAM_PARAM_NAMES:
        rows.append({"param": name, "value": f"{getattr(params, name):.6g}"})
    write_csv_rows(path, ["param", "value"], rows, rebuild_hydro=False)


@dataclass
class NamResult:
    rainfall_mm: np.ndarray
    pet_mm: np.ndarray
    et_mm: np.ndarray
    q_mm: np.ndarray
    q_m3s: np.ndarray
    q_overland_mm: np.ndarray
    q_inter_mm: np.ndarray
    q_base_mm: np.ndarray
    pn_mm: np.ndarray
    recharge_mm: np.ndarray
    u: np.ndarray
    l: np.ndarray
    g: np.ndarray
    hours: np.ndarray
    params: NamParams
    basin: Basin
    mass_balance_mm: float = 0.0

    @property
    def n(self) -> int:
        return int(self.rainfall_mm.size)


# ---------------------------------------------------------------------------
# Mo hinh NAM
# ---------------------------------------------------------------------------

def _rel_above(lrel: float, thresh: float) -> float:
    """(L/Lmax - T) / (1 - T), cat [0, 1]."""
    t = min(max(float(thresh), 0.0), 0.999)
    if lrel <= t:
        return 0.0
    return min(1.0, (lrel - t) / max(1.0 - t, 1e-9))


def _linres_step(storage: float, inflow_mm: float, ck_h: float, dt_h: float) -> tuple[float, float]:
    """Mot be tuyen tinh an: Q = S/CK (mm/h). Tra ve (S_moi, xa mm trong dt)."""
    ck = max(float(ck_h), 1e-6)
    dt = max(float(dt_h), 1e-9)
    s = max(float(storage), 0.0) + max(float(inflow_mm), 0.0)
    s_new = s / (1.0 + dt / ck)
    return s_new, s - s_new


class NamModel:
    """NAM DHI: be mat / dat / ngam + dinh tuyen dong chay mat, lien luu, co so."""

    def __init__(self, params: Optional[NamParams] = None, basin: Optional[Basin] = None):
        self.params = params or NamParams()
        self.basin = basin or Basin()

    def run(
        self,
        rainfall_mm: npt.ArrayLike,
        pet_mm: Optional[npt.ArrayLike] = None,
    ) -> NamResult:
        p = np.asarray(rainfall_mm, dtype=float)
        n = p.size
        if n < 1:
            raise ValueError("rainfall_mm rong")
        if pet_mm is None:
            pet = np.zeros(n, dtype=float)
        else:
            pet = np.asarray(pet_mm, dtype=float)
            if pet.size != n:
                raise ValueError("pet_mm phai cung do dai voi rainfall_mm")

        par = self.params
        dt = float(self.basin.dt_hours)
        if dt <= 0.0:
            raise ValueError("dt_hours phai > 0")

        umax = max(float(par.umax), 1e-3)
        lmax = max(float(par.lmax), 1e-3)
        cqof = min(max(float(par.cqof), 0.0), 1.0)
        carea = min(max(float(par.carea), 0.0), 2.0)

        u = min(max(float(par.u0), 0.0), umax)
        l = min(max(float(par.l0), 0.0), lmax)
        g = max(float(par.g0), 0.0)
        sof1 = max(float(par.sof1_0), 0.0)
        sof2 = max(float(par.sof2_0), 0.0)
        sif = max(float(par.sif_0), 0.0)
        s0 = u + l + g + sof1 + sof2 + sif

        out_et = np.zeros(n)
        q_of = np.zeros(n)
        q_if = np.zeros(n)
        q_bf = np.zeros(n)
        pn_s = np.zeros(n)
        rec_s = np.zeros(n)
        st_u = np.zeros(n)
        st_l = np.zeros(n)
        st_g = np.zeros(n)
        gw_sink = 0.0

        print(
            f"Tinh NAM {n} buoc (dt={dt:g} h, A={self.basin.area_km2:g} km2)...",
            flush=True,
        )
        for t in range(n):
            u += max(p[t], 0.0)

            ep = max(pet[t], 0.0)
            e_u = min(u, ep)
            u -= e_u
            e_l = min(l, (ep - e_u) * (l / lmax))
            l -= e_l
            out_et[t] = e_u + e_l

            if u > umax:
                pn = u - umax
                u = umax
            else:
                pn = 0.0
            pn_s[t] = pn

            lrel = l / lmax
            qof_gen = min(max(cqof * _rel_above(lrel, par.tof) * pn, 0.0), pn)
            inf = pn - qof_gen

            qg = min(max(_rel_above(lrel, par.tg) * inf, 0.0), inf)
            dl = inf - qg
            if l + dl > lmax:
                qg += l + dl - lmax
                l = lmax
            else:
                l += dl
            rec_s[t] = qg

            qif_gen = min(
                max((dt / max(float(par.ckif), 1e-6)) * _rel_above(lrel, par.tif) * u, 0.0),
                u,
            )
            u -= qif_gen

            sof1, qof1 = _linres_step(sof1, qof_gen, par.ck12, dt)
            sof2, qof_out = _linres_step(sof2, qof1, par.ck12, dt)
            sif, qif_out = _linres_step(sif, qif_gen, par.ckif, dt)

            lost_gw = qg * max(0.0, 1.0 - min(carea, 1.0))
            extra_gw = qg * max(0.0, carea - 1.0)
            gw_sink += lost_gw - extra_gw
            g += qg * min(carea, 1.0) + extra_gw
            g, qbf_out = _linres_step(g, 0.0, par.ckbf, dt)

            u = max(u, 0.0)
            l = min(max(l, 0.0), lmax)
            g = max(g, 0.0)
            sof1 = max(sof1, 0.0)
            sof2 = max(sof2, 0.0)
            sif = max(sif, 0.0)

            q_of[t] = qof_out
            q_if[t] = qif_out
            q_bf[t] = qbf_out
            st_u[t] = u
            st_l[t] = l
            st_g[t] = g
            if n >= 24 and (t + 1) % 24 == 0:
                print(f"  da tinh {t + 1}/{n} gio", flush=True)

        q_mm = q_of + q_if + q_bf
        q_m3s = q_mm * self.basin.area_km2 / (3.6 * dt)
        s_end = float(st_u[-1] + st_l[-1] + st_g[-1] + sof1 + sof2 + sif)
        mass = float(p.sum() - out_et.sum() - q_mm.sum() - (s_end - s0) - gw_sink)

        hours = np.arange(n, dtype=float) * dt
        return NamResult(
            rainfall_mm=p,
            pet_mm=pet,
            et_mm=out_et,
            q_mm=q_mm,
            q_m3s=q_m3s,
            q_overland_mm=q_of,
            q_inter_mm=q_if,
            q_base_mm=q_bf,
            pn_mm=pn_s,
            recharge_mm=rec_s,
            u=st_u,
            l=st_l,
            g=st_g,
            hours=hours,
            params=par,
            basin=self.basin,
            mass_balance_mm=mass,
        )


# ---------------------------------------------------------------------------
# Xuat ket qua
# ---------------------------------------------------------------------------

def write_csv(result: NamResult, path: Path) -> None:
    cols = [
        "hour",
        "rainfall_mm",
        "pet_mm",
        "et_mm",
        "q_total_mm",
        "q_m3s",
        "q_overland_mm",
        "q_inter_mm",
        "q_base_mm",
        "pn_mm",
        "recharge_mm",
        "u_mm",
        "l_mm",
        "g_mm",
    ]
    rows = [
        {
            "hour": f"{result.hours[i]:.1f}",
            "rainfall_mm": f"{result.rainfall_mm[i]:.4f}",
            "pet_mm": f"{result.pet_mm[i]:.4f}",
            "et_mm": f"{result.et_mm[i]:.4f}",
            "q_total_mm": f"{result.q_mm[i]:.4f}",
            "q_m3s": f"{result.q_m3s[i]:.4f}",
            "q_overland_mm": f"{result.q_overland_mm[i]:.4f}",
            "q_inter_mm": f"{result.q_inter_mm[i]:.4f}",
            "q_base_mm": f"{result.q_base_mm[i]:.4f}",
            "pn_mm": f"{result.pn_mm[i]:.4f}",
            "recharge_mm": f"{result.recharge_mm[i]:.4f}",
            "u_mm": f"{result.u[i]:.4f}",
            "l_mm": f"{result.l[i]:.4f}",
            "g_mm": f"{result.g[i]:.4f}",
        }
        for i in range(result.n)
    ]
    write_csv_rows(path, cols, rows)


def plot_result(result: NamResult, path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    t = result.hours
    kq = result.basin.area_km2 / (3.6 * result.basin.dt_hours)
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)

    ax = axes[0]
    ax.bar(t, result.rainfall_mm, width=result.basin.dt_hours * 0.9, color="#4a90d9", label="Mưa (mm)")
    ax.set_ylabel("Mưa (mm)")
    ax.invert_yaxis()
    ax.legend(loc="upper right")
    ax.set_title(f"MIKE NAM — {result.basin.name}")

    ax = axes[1]
    ax.plot(t, result.q_m3s, color="#c0392b", lw=1.8, label="Lưu lượng tổng (m3/s)")
    ax.fill_between(t, 0, result.q_overland_mm * kq, color="#e67e22", alpha=0.35, label="Dòng chảy mặt")
    ax.plot(t, result.q_inter_mm * kq, color="#27ae60", lw=1.2, label="Liên lưu")
    ax.plot(t, result.q_base_mm * kq, color="#8e44ad", lw=1.2, label="Dòng chảy cơ sở")
    ax.set_ylabel("Q (m3/s)")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(t, result.u, label="U bề mặt")
    ax.plot(t, result.l, label="L đất / rễ")
    ax.plot(t, result.g, label="G nước ngầm")
    ax.set_ylabel("Lưu trữ (mm)")
    ax.set_xlabel("Giờ")
    ax.legend(loc="upper right", ncol=3)
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def print_summary(result: NamResult) -> None:
    peak_i = int(np.argmax(result.q_m3s))
    print(f"Lưu vực       : {result.basin.name}", flush=True)
    print(f"Diện tích     : {result.basin.area_km2:.2f} km2", flush=True)
    print(f"Bước thời gian: {result.basin.dt_hours:.2f} giờ", flush=True)
    print(f"Số bước       : {result.n}", flush=True)
    print(f"Tổng mưa      : {result.rainfall_mm.sum():.2f} mm", flush=True)
    print(f"Tổng ET       : {result.et_mm.sum():.2f} mm", flush=True)
    print(f"Tổng dòng chảy: {result.q_mm.sum():.2f} mm  ({result.q_m3s.mean():.3f} m3/s TB)", flush=True)
    print(f"  - mặt       : {result.q_overland_mm.sum():.2f} mm", flush=True)
    print(f"  - liên lưu  : {result.q_inter_mm.sum():.2f} mm", flush=True)
    print(f"  - cơ sở     : {result.q_base_mm.sum():.2f} mm", flush=True)
    print(f"Đỉnh dòng chảy: {result.q_m3s[peak_i]:.3f} m3/s tại giờ {result.hours[peak_i]:.1f}", flush=True)
    print(f"Hệ số chảy    : {result.q_mm.sum() / max(result.rainfall_mm.sum(), 1e-9):.3f}", flush=True)
    print(f"Cân bằng khối : {result.mass_balance_mm:.4e} mm (gần 0 = OK)", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mo hinh MIKE NAM mua-rao / dong-chay")
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="CSV bien (hour,rainfall_mm,pet_mm). Mac dinh: rainfall_runoff_output/demo_rainfall.csv",
    )
    p.add_argument("--area", type=float, default=None, help="Dien tich luu vuc km2")
    p.add_argument("--dt", type=float, default=None, help="Buoc thoi gian (gio)")
    p.add_argument(
        "--params-csv",
        type=Path,
        default=None,
        help="CSV thong so NAM (param,value). Mac dinh: rainfall_runoff_output/demo_nam_params.csv neu co",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    basin = Basin()
    params = NamParams()
    params_path = args.params_csv
    if params_path is None:
        candidate = args.out_dir / "demo_nam_params.csv"
        if csv_available(candidate):
            params_path = candidate
    if params_path is not None:
        if not csv_available(params_path):
            raise FileNotFoundError(f"Khong thay file thong so: {params_path}")
        basin, params = load_params_csv(params_path)
        print(f"Doc thong so tu {params_path}", flush=True)
    if args.area is not None:
        basin.area_km2 = float(args.area)
        basin.name = f"Luu vuc NAM ({basin.area_km2:g} km2)"
    if args.dt is not None:
        basin.dt_hours = float(args.dt)

    rain_path = args.csv if args.csv is not None else DEFAULT_RAIN_CSV
    if not csv_available(rain_path):
        raise FileNotFoundError(
            f"Khong thay file bien mua: {rain_path}. "
            "Can rainfall_mm (va pet_mm neu co) nhu demo_rainfall.csv."
        )
    rain, pet = load_forcing_csv(rain_path)
    print(f"Doc bien mua/PET tu {rain_path}  ({rain.size} buoc)", flush=True)

    result = NamModel(params=params, basin=basin).run(rain, pet)
    print_summary(result)

    out_csv = args.out_dir / "mike_nam_result.csv"
    write_csv(result, out_csv)
    print(f"Ket qua CSV : {out_csv}", flush=True)

    out_params = args.out_dir / "demo_nam_params.csv"
    write_params_csv(result.basin, result.params, out_params)
    print(f"Thong so CSV: {out_params}", flush=True)

    if not args.no_plot:
        fig_path = args.out_dir / "mike_nam_hydrograph.png"
        if plot_result(result, fig_path):
            print(f"Bieu do     : {fig_path}", flush=True)
        else:
            print("Khong ve duoc (thieu matplotlib). Van co CSV.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

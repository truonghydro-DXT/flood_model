"""Dieu kien bien: mua, boc hoi (PET), muc nuoc H, luu luong Q.

Dung chung cho TANK, NAM, Muskingum-Cunge, Saint-Venant, MIKE HD, UI 3D.
"""

from __future__ import annotations

import csv
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.routing import (
    is_main_reach_id,
    is_primary_main_reach,
    load_grouped_geom_csv,
    normalize_reach_id,
)

TANK_OUT_DIR = ROOT / "rainfall_runoff_output"
TANK_RESULT_CSV = TANK_OUT_DIR / "tank_result.csv"
TANK_RAIN_CSV = TANK_OUT_DIR / "demo_rainfall.csv"
TANK_FLOW_CSV = TANK_OUT_DIR / "demo_flow.csv"
NAM_RESULT_CSV = TANK_OUT_DIR / "mike_nam_result.csv"
DEFAULT_RAIN_CSV = TANK_RAIN_CSV
DEFAULT_TANK_SCRIPT = ROOT / "rainfall-runoff.py"

SV_OUT_DIR = ROOT / "saint_venant_output"
SV_INFLOW_CSV = SV_OUT_DIR / "demo_inflow_q_m3s.csv"
SV_DOWN_H_CSV = SV_OUT_DIR / "demo_downstream_stage.csv"
MK_DOWN_H_CSV = ROOT / "muskingum_output" / "demo_downstream_stage.csv"

DEFAULT_INFLOW_CSV = NAM_RESULT_CSV
DEFAULT_H_CSV = SV_DOWN_H_CSV
DEFAULT_TRIB_H_CSV = SV_DOWN_H_CSV
# Hieu chinh WSE: niem H ha luu / H trong song them (m) de long DEM hien nuoc.
STAGE_CALIBRATION_M = 3.0
DEFAULT_TANK_CSV = NAM_RESULT_CSV
TRIB_H_COL = "h_na1_m"
INFLOW_Q_COLS = ("q_m3s", "q_m3/s", "q_in_m3s", "q_total_m3s")


def csv_field_map(fieldnames: Optional[list[str] | tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in fieldnames or []:
        key = str(name).strip().lower().replace(" ", "")
        if key:
            out[key] = name
    return out


def pick_csv_col(fields: dict[str, str], *candidates: str) -> str | None:
    for cand in candidates:
        key = cand.strip().lower().replace(" ", "")
        if key in fields:
            return fields[key]
    return None


def input_cell(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.lower() in {"nan", "none", "null"}:
        return ""
    return s


def hour_key(hour: float) -> float:
    return round(float(hour), 6)


def hour_cell(hour: float) -> str:
    if abs(hour - round(hour)) < 1e-9:
        return str(int(round(hour)))
    return f"{hour:.4f}".rstrip("0").rstrip(".")


def read_series_csv(
    path: Path,
    value_col: str,
    hour_col: str = "hour",
) -> tuple[np.ndarray, np.ndarray]:
    hours: list[float] = []
    vals: list[float] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or value_col not in reader.fieldnames:
            return np.zeros(0), np.zeros(0)
        for i, row in enumerate(reader):
            try:
                v = float(row[value_col])
            except (TypeError, ValueError):
                continue
            t = float(row[hour_col]) if hour_col in row and str(row[hour_col]).strip() else float(i)
            hours.append(t)
            vals.append(v)
    return np.asarray(hours, dtype=float), np.asarray(vals, dtype=float)


_read_series_csv = read_series_csv


def resample_series(hours: np.ndarray, values: np.ndarray, dt_hours: float) -> tuple[np.ndarray, np.ndarray]:
    if hours.size < 2:
        return hours, values
    t0, t1 = float(hours[0]), float(hours[-1])
    n = max(2, int(round((t1 - t0) / dt_hours)) + 1)
    t = t0 + np.arange(n, dtype=float) * dt_hours
    t[-1] = t1
    return t, np.interp(t, hours, values)


def series_by_hour(path: Path, value_names: tuple[str, ...]) -> dict[float, str]:
    if not csv_available(path):
        return {}
    out: dict[float, str] = {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        fmap = csv_field_map(reader.fieldnames)
        hcol = pick_csv_col(fmap, "hour", "t", "time")
        vcol = pick_csv_col(fmap, *value_names)
        if vcol is None:
            return {}
        for i, row in enumerate(reader):
            hour = float(i)
            if hcol and str(row.get(hcol, "")).strip() != "":
                try:
                    hour = float(row[hcol])
                except (TypeError, ValueError):
                    hour = float(i)
            val = input_cell(row.get(vcol))
            if val != "":
                out[hour_key(hour)] = val
    return out


# ---------------------------------------------------------------------------
# Mua / PET
# ---------------------------------------------------------------------------

def _pulse(t: np.ndarray, center: float, width: float, peak: float) -> np.ndarray:
    """Xung Gaussian cat can (mm/gio)."""
    sigma = width / 2.355
    y = peak * np.exp(-0.5 * ((t - center) / sigma) ** 2)
    y[np.abs(t - center) > 2.5 * width] = 0.0
    return y


def demo_series(n_hours: int = 168, dt_hours: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Mua 7 ngay (gio) + PET ~ 3.5 mm/ngay, sin theo gio."""
    t = np.arange(n_hours, dtype=float) * dt_hours
    rain = np.zeros(n_hours, dtype=float)

    rain += _pulse(t, center=18.0, width=6.0, peak=2.0)
    rain += _pulse(t, center=42.0, width=10.0, peak=5.0)
    rain += _pulse(t, center=70.0, width=8.0, peak=12.0)
    rain += _pulse(t, center=82.0, width=10.0, peak=20.0)
    rain += _pulse(t, center=96.0, width=8.0, peak=6.0)
    rain += _pulse(t, center=130.0, width=6.0, peak=2.5)

    rng = np.random.default_rng(42)
    drizzle = rng.uniform(0.0, 0.3, size=n_hours)
    drizzle[(t < 12) | ((t > 105) & (t < 120)) | (t > 145)] = 0.0
    rain = np.clip(rain + drizzle, 0.0, None)

    hour_of_day = t % 24.0
    pet_daily = 3.5
    weight = np.clip(np.sin((hour_of_day - 6.0) / 24.0 * 2.0 * math.pi), 0.0, None)
    wsum = weight.reshape(-1, 24).sum(axis=1, keepdims=True) if n_hours >= 24 else weight.sum()
    if n_hours >= 24 and n_hours % 24 == 0:
        pet = weight.reshape(-1, 24) / np.maximum(wsum, 1e-9) * pet_daily
        pet = pet.ravel()
    else:
        pet = weight / max(float(weight.sum()), 1e-9) * pet_daily * (n_hours / 24.0)

    return rain, pet


def load_forcing_csv(
    path: Path,
    rainfall_col: str = "rainfall_mm",
    pet_col: str = "pet_mm",
) -> tuple[np.ndarray, np.ndarray]:
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV rong: {path}")
        fields_map = {n.strip().lower(): n for n in reader.fieldnames}
        rcol = fields_map.get(rainfall_col.lower())
        if rcol is None:
            raise ValueError(f"CSV can cot '{rainfall_col}': {path}")
        pcol = fields_map.get(pet_col.lower())
        rain: list[float] = []
        pet: list[float] = []
        for row in reader:
            rain.append(float(row[rcol]))
            raw = row.get(pcol, "") if pcol else ""
            pet.append(float(raw) if str(raw).strip() != "" else 0.0)
        return np.array(rain, dtype=float), np.array(pet, dtype=float)


def load_rainfall_csv(path: Path, rainfall_col: str = "rainfall_mm") -> np.ndarray:
    rain, _pet = load_forcing_csv(path, rainfall_col=rainfall_col)
    return rain


def load_rainfall_hours(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Chuoi gio + rainfall_mm (mo phong hat mua / align len luoi 1D)."""
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "rainfall_mm" not in reader.fieldnames:
            raise ValueError(f"CSV can cot rainfall_mm: {path}")
        hours: list[float] = []
        rain: list[float] = []
        for i, row in enumerate(reader):
            raw_h = row.get("hour", "")
            hours.append(float(raw_h) if str(raw_h).strip() != "" else float(i))
            rain.append(float(row["rainfall_mm"]))
    return np.asarray(hours, dtype=float), np.asarray(rain, dtype=float)


def align_rainfall(hours: np.ndarray, rain_hours: np.ndarray, rain_mm: np.ndarray) -> np.ndarray:
    """Noi suy mua (mm/h) len luoi gio cua dong chay 1D / TANK."""
    t = np.asarray(hours, dtype=float)
    if t.size == 0:
        return np.zeros(0, dtype=float)
    rh = np.asarray(rain_hours, dtype=float)
    rm = np.asarray(rain_mm, dtype=float)
    if rm.size == 0:
        return np.zeros(t.size, dtype=float)
    if rh.size != rm.size:
        rh = np.arange(rm.size, dtype=float)
    return np.interp(t, rh, rm, left=0.0, right=0.0)


def write_demo_rainfall_csv(rainfall: np.ndarray, pet: np.ndarray, path: Path, dt_hours: float) -> None:
    rows = [
        {
            "hour": f"{i * dt_hours:.1f}",
            "rainfall_mm": f"{r:.4f}",
            "pet_mm": f"{e:.4f}",
        }
        for i, (r, e) in enumerate(zip(rainfall, pet))
    ]
    write_csv_rows(path, ["hour", "rainfall_mm", "pet_mm"], rows)


# ---------------------------------------------------------------------------
# Luu luong Q
# ---------------------------------------------------------------------------

def run_rainfall_runoff() -> Path:
    """Chay rainfall-runoff.py dung nguyen thong so TANK trong file do."""
    if not DEFAULT_TANK_SCRIPT.is_file():
        raise FileNotFoundError(f"Khong thay TANK: {DEFAULT_TANK_SCRIPT}")
    print(f"Chay TANK     : {DEFAULT_TANK_SCRIPT}")
    proc = subprocess.run(
        [sys.executable, str(DEFAULT_TANK_SCRIPT), "--no-plot"],
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rainfall-runoff.py that bai (exit {proc.returncode})")
    if not csv_available(TANK_RESULT_CSV):
        raise FileNotFoundError(f"TANK khong ghi CSV: {TANK_RESULT_CSV}")
    return TANK_RESULT_CSV


def load_tank_inflow(csv_path: Optional[Path] = None, *, run_tank: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Mac dinh: chay TANK roi doc q_m3s. --inflow file khac thi chi doc CSV."""
    path = Path(csv_path) if csv_path is not None else TANK_RESULT_CSV
    use_default = path.resolve() == TANK_RESULT_CSV.resolve()
    if run_tank and use_default:
        path = run_rainfall_runoff()
    if not csv_available(path):
        raise FileNotFoundError(f"Chua co ket qua TANK: {path}")
    hours = np.zeros(0)
    q = np.zeros(0)
    for col in INFLOW_Q_COLS:
        hours, q = read_series_csv(path, col, hour_col="hour")
        if hours.size >= 2:
            break
    if hours.size < 2:
        raise ValueError(f"CSV TANK can cot hour va mot trong {INFLOW_Q_COLS}: {path}")
    return hours, q


def resolve_inflow_csv(csv_path: Optional[Path] = None) -> Path:
    """Mac dinh: diem Q_us trong boundary_input.csv, roi NAM."""
    default_paths = {DEFAULT_INFLOW_CSV.resolve(), NAM_RESULT_CSV.resolve()}
    explicit = Path(csv_path) if csv_path is not None else None
    use_station = explicit is None or explicit.resolve() in default_paths
    if use_station:
        st = station_data_path(sid="Q_us") or station_data_path(kind="Q", reach="main")
        if st is not None:
            return st
    if explicit is not None and csv_available(explicit):
        return explicit
    for fallback in (NAM_RESULT_CSV, SV_INFLOW_CSV, TANK_RESULT_CSV):
        if csv_available(fallback):
            return fallback
    return explicit if explicit is not None else DEFAULT_INFLOW_CSV


def load_inflow_q(csv_path: Optional[Path] = None) -> tuple[np.ndarray, np.ndarray]:
    """Doc Q bien thuong luu (hour + q_m3s hoac q_m3/s)."""
    path = resolve_inflow_csv(csv_path)
    if not csv_available(path):
        raise FileNotFoundError(
            f"Chua co Q vao: {path}. Hay nhap du lieu bien 1D hoac chay NAM/TANK truoc."
        )
    hours = np.zeros(0)
    q = np.zeros(0)
    for col in INFLOW_Q_COLS:
        hours, q = read_series_csv(path, col, hour_col="hour")
        if hours.size >= 2:
            break
    if hours.size < 2:
        raise ValueError(f"CSV Q vao can cot hour va mot trong {INFLOW_Q_COLS}: {path}")
    return hours, q


def load_tank_q(path: Path) -> dict[str, np.ndarray] | None:
    if not csv_available(path):
        return None
    hours, q = [], []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            try:
                hours.append(float(row.get("hour") or row.get("t") or 0.0))
            except (TypeError, ValueError):
                continue
            raw = row.get("q_m3s") or row.get("q_m3/s") or row.get("q")
            try:
                q.append(float(raw))
            except (TypeError, ValueError):
                q.append(float("nan"))
    if len(q) < 2:
        return None
    return {"hours": np.asarray(hours, dtype=float), "q": np.asarray(q, dtype=float)}


def write_inflow_csv(hours: np.ndarray, q: np.ndarray, path: Path) -> None:
    write_csv_rows(
        path,
        ["hour", "q_m3s"],
        ({"hour": f"{t:.2f}", "q_m3s": f"{v:.4f}"} for t, v in zip(hours, q)),
    )


# ---------------------------------------------------------------------------
# Muc nuoc H
# ---------------------------------------------------------------------------

def demo_downstream_wse(hours: np.ndarray, q_in: np.ndarray, z_ds: float) -> np.ndarray:
    qn = (q_in - float(np.min(q_in))) / max(float(np.max(q_in) - np.min(q_in)), 1e-6)
    h_base = z_ds + 3.0
    h_tide = 0.40 * np.sin(2.0 * math.pi * hours / 24.0)
    h_flood = 1.60 * qn
    return h_base + h_tide + h_flood


def load_downstream_stage(
    hours: np.ndarray,
    csv_path: Optional[Path],
    h_const: Optional[float],
    z_ds: float,
    q_in: np.ndarray,
) -> np.ndarray:
    """H ha luu: --h-down hang, --h-csv, hoac demo_downstream_stage.csv."""
    if h_const is not None:
        return np.full_like(hours, float(h_const), dtype=float)
    path = csv_path if csv_path is not None else DEFAULT_H_CSV
    col = "h_m"
    if path is None or Path(path).resolve() == DEFAULT_H_CSV.resolve():
        rec = find_boundary_station(sid="H_ds") or find_boundary_station(kind="H", reach="main")
        st = station_data_path(sid="H_ds") or station_data_path(kind="H", reach="main")
        if st is not None:
            path = st
            if rec and rec.get("value_col"):
                col = rec["value_col"]
    if path is not None and csv_available(Path(path)):
        th, hh = read_series_csv(Path(path), col)
        if th.size < 2 and col != "h_m":
            th, hh = read_series_csv(Path(path), "h_m")
        if th.size < 2:
            raise ValueError(f"CSV bien ha luu can cot hour,{col}: {path}")
        return np.interp(hours, th, hh)
    return demo_downstream_wse(hours, q_in, z_ds)


def load_trib_outlet_stage(
    hours: np.ndarray,
    csv_path: Optional[Path] = None,
    col: str = TRIB_H_COL,
) -> Optional[np.ndarray]:
    """H bien dau xa nhanh thoat: cot h_na1_m trong demo_downstream_stage.csv."""
    path = Path(csv_path) if csv_path is not None else DEFAULT_TRIB_H_CSV
    use_col = col
    if csv_path is None or Path(path).resolve() == DEFAULT_TRIB_H_CSV.resolve():
        rec = find_boundary_station(sid="H_na1") or find_boundary_station(kind="H", reach="trib")
        st = station_data_path(sid="H_na1") or station_data_path(kind="H", reach="trib")
        if st is not None:
            path = st
            if rec and rec.get("value_col"):
                use_col = rec["value_col"]
    if not csv_available(path):
        return None
    th, hh = read_series_csv(path, use_col)
    if th.size < 2:
        return None
    return np.interp(hours, th, hh)


def write_downstream_csv(
    hours: np.ndarray,
    h: np.ndarray,
    path: Path,
    h_na1: Optional[np.ndarray] = None,
) -> None:
    if h_na1 is None:
        write_csv_rows(
            path,
            ["hour", "h_m"],
            ({"hour": f"{t:.2f}", "h_m": f"{v:.4f}"} for t, v in zip(hours, h)),
        )
        return
    write_csv_rows(
        path,
        ["hour", "h_m", "h_na1_m"],
        (
            {"hour": f"{t:.2f}", "h_m": f"{hm:.4f}", "h_na1_m": f"{hn:.4f}"}
            for t, hm, hn in zip(hours, h, h_na1)
        ),
    )


def load_initial_qh_csv(path: Path) -> tuple[dict[int, float], dict[int, float]]:
    """CSV: xs_id,q0,h0  (co the them station_km; thieu q0 hoac h0 thi bo qua cot do)."""
    q0_by_id: dict[int, float] = {}
    h0_by_id: dict[int, float] = {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return q0_by_id, h0_by_id
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        id_col = fields.get("xs_id") or fields.get("id")
        q_col = fields.get("q0") or fields.get("q") or fields.get("q_m3s")
        h_col = fields.get("h0") or fields.get("h") or fields.get("h_m")
        if q_col is None and h_col is None:
            raise ValueError(f"CSV dieu kien ban dau can cot q0 va/hoac h0: {path}")
        for i, row in enumerate(reader):
            if id_col and str(row.get(id_col, "")).strip():
                try:
                    xs_id = int(float(row[id_col]))
                except (TypeError, ValueError):
                    xs_id = i + 1
            else:
                xs_id = i + 1
            if q_col is not None:
                raw_q = str(row.get(q_col, "")).strip()
                if raw_q:
                    try:
                        qv = float(raw_q)
                    except ValueError:
                        qv = float("nan")
                    if math.isfinite(qv):
                        q0_by_id[xs_id] = qv
            if h_col is not None:
                raw_h = str(row.get(h_col, "")).strip()
                if raw_h:
                    try:
                        hv = float(raw_h)
                    except ValueError:
                        hv = float("nan")
                    if math.isfinite(hv):
                        h0_by_id[xs_id] = hv
    return q0_by_id, h0_by_id


def write_initial_csv(geom, q0: np.ndarray, h0: np.ndarray, path: Path) -> None:
    write_csv_rows(
        path,
        ["xs_id", "station_km", "q0", "h0"],
        (
            {
                "xs_id": sec.index + 1,
                "station_km": f"{sec.station_m / 1000.0:.4f}",
                "q0": f"{float(q0[i]):.4f}",
                "h0": f"{float(h0[i]):.4f}",
            }
            for i, sec in enumerate(geom.sections)
        ),
    )


# ---------------------------------------------------------------------------
# Bang bien UI (mua/PET/Q va Q/H 1D)
# ---------------------------------------------------------------------------

def ensure_runoff_input_csv() -> Path:
    if not csv_available(TANK_RAIN_CSV):
        write_csv_rows(
            TANK_RAIN_CSV,
            ["hour", "timeseries", "rainfall_mm", "pet_mm", "q_m3s"],
            [],
        )
    return TANK_RAIN_CSV


def _obs_q_by_hour() -> dict[float, str]:
    by_q = series_by_hour(
        TANK_FLOW_CSV,
        ("q_m3/s", "q_m3s", "q_obs_m3s", "q_obs"),
    )
    if by_q:
        return by_q
    try:
        from flood_model.db import fetch_hydro_timeseries

        for row in fetch_hydro_timeseries():
            hour = row.get("hour")
            q = row.get("q_m3s")
            if hour is None or q is None:
                continue
            by_q[hour_key(float(hour))] = input_cell(q)
    except Exception:
        pass
    return by_q


def _ts_by_hour() -> dict[float, str]:
    from flood_model.db import ts_label

    by_ts = series_by_hour(TANK_RAIN_CSV, ("timeseries", "ts"))
    try:
        from flood_model.db import fetch_hydro_timeseries

        for row in fetch_hydro_timeseries():
            hour = row.get("hour")
            if hour is None:
                continue
            key = hour_key(float(hour))
            ts = input_cell(row.get("timeseries") or row.get("ts"))
            if ts and key not in by_ts:
                by_ts[key] = ts
    except Exception:
        pass
    if not by_ts:
        return {}
    return {k: v or ts_label(k) for k, v in by_ts.items()}


def load_runoff_input_payload() -> dict[str, Any]:
    from flood_model.db import ts_label

    ensure_runoff_input_csv()
    by_q = _obs_q_by_hour()
    rows_out: list[dict[str, Any]] = []
    with csv_open(TANK_RAIN_CSV) as f:
        reader = csv.DictReader(f)
        fmap = csv_field_map(reader.fieldnames)
        hcol = pick_csv_col(fmap, "hour", "t", "time")
        rcol = pick_csv_col(fmap, "rainfall_mm", "rain_mm", "p_mm")
        pcol = pick_csv_col(fmap, "pet_mm", "evap_mm", "e_mm")
        qcol = pick_csv_col(fmap, "q_m3s", "q_m3/s", "q_obs_m3s", "q_obs")
        tscol = pick_csv_col(fmap, "timeseries", "ts")
        for i, row in enumerate(reader):
            hour = float(i)
            if hcol and str(row.get(hcol, "")).strip() != "":
                try:
                    hour = float(row[hcol])
                except (TypeError, ValueError):
                    hour = float(i)
            ts = input_cell(row.get(tscol)) if tscol else ""
            if not ts:
                ts = ts_label(hour)
            q_val = input_cell(row.get(qcol) if qcol else "") or by_q.get(hour_key(hour), "")
            rows_out.append(
                {
                    "hour": hour,
                    "timeseries": ts,
                    "rainfall_mm": input_cell(row.get(rcol) if rcol else ""),
                    "pet_mm": input_cell(row.get(pcol) if pcol else ""),
                    "q_m3s": q_val,
                }
            )
    if not rows_out and by_q:
        for hour in sorted(by_q):
            rows_out.append(
                {
                    "hour": hour,
                    "timeseries": ts_label(hour),
                    "rainfall_mm": "",
                    "pet_mm": "",
                    "q_m3s": by_q[hour],
                }
            )
    return {
        "ok": True,
        "path": str(TANK_RAIN_CSV),
        "obs_path": str(TANK_FLOW_CSV),
        "database": "data_flood",
        "n": len(rows_out),
        "columns": ["hour", "timeseries", "rainfall_mm", "pet_mm", "q_m3s"],
        "rows": rows_out,
    }


def save_runoff_input_rows(raw_rows: Any) -> dict[str, Any]:
    from flood_model.db import ts_label

    if not isinstance(raw_rows, list):
        raise ValueError("Can JSON {rows: [{hour, timeseries, rainfall_mm, pet_mm, q_m3s}, ...]}.")
    material: list[dict[str, str]] = []
    for i, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            continue
        hour_raw = row.get("hour")
        try:
            hour = float(hour_raw) if hour_raw is not None and str(hour_raw).strip() != "" else float(i)
        except (TypeError, ValueError):
            raise ValueError(f"Dong {i + 1}: 'hour' khong hop le.")
        if not math.isfinite(hour) or hour < 0:
            raise ValueError(f"Dong {i + 1}: 'hour' phai >= 0.")

        def _num_cell(key: str, lo: float = 0.0) -> str:
            raw = row.get(key)
            if raw is None or str(raw).strip() == "":
                return ""
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"Dong {i + 1}: '{key}' khong hop le.")
            if not math.isfinite(v) or v < lo:
                raise ValueError(f"Dong {i + 1}: '{key}' phai >= {lo:g}.")
            return f"{v:.4f}"

        ts = input_cell(row.get("timeseries") or row.get("ts"))
        if not ts:
            ts = ts_label(hour)
        material.append(
            {
                "hour": (
                    str(int(round(hour)))
                    if abs(hour - round(hour)) < 1e-9
                    else f"{hour:.4f}".rstrip("0").rstrip(".")
                ),
                "timeseries": ts,
                "rainfall_mm": _num_cell("rainfall_mm") or "0.0000",
                "pet_mm": _num_cell("pet_mm") or "0.0000",
                "q_m3s": _num_cell("q_m3s"),
            }
        )
    if not material:
        raise ValueError("Khong co dong du lieu hop le.")
    write_csv_rows(
        TANK_FLOW_CSV,
        ["hour", "q_m3/s"],
        [{"hour": row["hour"], "q_m3/s": row["q_m3s"]} for row in material],
        rebuild_hydro=False,
    )
    write_csv_rows(
        TANK_RAIN_CSV,
        ["hour", "timeseries", "rainfall_mm", "pet_mm", "q_m3s"],
        material,
    )
    return load_runoff_input_payload()


def load_sv_boundary_payload() -> dict[str, Any]:
    from flood_model.db import ts_label

    by_q = series_by_hour(NAM_RESULT_CSV, INFLOW_Q_COLS)
    if not by_q:
        by_q = series_by_hour(SV_INFLOW_CSV, INFLOW_Q_COLS)
    if not by_q:
        by_q = series_by_hour(TANK_RESULT_CSV, INFLOW_Q_COLS)
    by_h = series_by_hour(SV_DOWN_H_CSV, ("h_m", "h_down_m"))
    if not by_h:
        by_h = series_by_hour(MK_DOWN_H_CSV, ("h_m", "h_down_m"))
    by_na1 = series_by_hour(SV_DOWN_H_CSV, ("h_na1_m",))
    by_ts = _ts_by_hour()
    hours = sorted(set(by_q) | set(by_h) | set(by_na1) | set(by_ts))
    rows_out: list[dict[str, Any]] = []
    for hour in hours:
        ts = by_ts.get(hour) or ts_label(hour)
        rows_out.append(
            {
                "hour": hour,
                "timeseries": ts,
                "q_m3s": by_q.get(hour, ""),
                "h_down_m": by_h.get(hour, ""),
                "h_na1_m": by_na1.get(hour, ""),
            }
        )
    return {
        "ok": True,
        "database": "data_flood",
        "q_path": str(NAM_RESULT_CSV),
        "h_path": str(SV_DOWN_H_CSV),
        "h_trib_path": str(SV_DOWN_H_CSV),
        "n": len(rows_out),
        "columns": ["hour", "timeseries", "q_m3s", "h_down_m", "h_na1_m"],
        "rows": rows_out,
    }


def save_sv_boundary_rows(raw_rows: Any) -> dict[str, Any]:
    from flood_model.db import ts_label

    if not isinstance(raw_rows, list):
        raise ValueError("Can JSON {rows: [{hour, q_m3s, h_down_m, h_na1_m}, ...]}.")
    q_rows: list[dict[str, str]] = []
    h_rows: list[dict[str, str]] = []
    trib_rows: list[dict[str, str]] = []
    have_na1 = False
    for i, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            continue
        hour_raw = row.get("hour")
        try:
            hour = float(hour_raw) if hour_raw is not None and str(hour_raw).strip() != "" else float(i)
        except (TypeError, ValueError):
            raise ValueError(f"Dong {i + 1}: 'hour' khong hop le.")
        if not math.isfinite(hour) or hour < 0:
            raise ValueError(f"Dong {i + 1}: 'hour' phai >= 0.")

        def _num(key: str, *, required: bool, lo: float | None = None) -> str:
            raw = row.get(key)
            if raw is None or str(raw).strip() == "":
                if required:
                    raise ValueError(f"Dong {i + 1}: '{key}' khong duoc de trong.")
                return ""
            try:
                v = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"Dong {i + 1}: '{key}' khong hop le.")
            if not math.isfinite(v):
                raise ValueError(f"Dong {i + 1}: '{key}' khong hop le.")
            if lo is not None and v < lo:
                raise ValueError(f"Dong {i + 1}: '{key}' phai >= {lo:g}.")
            return f"{v:.4f}"

        q_val = _num("q_m3s", required=True, lo=0.0)
        h_val = _num("h_down_m", required=True)
        na1_val = _num("h_na1_m", required=False)
        hour_s = hour_cell(hour)
        ts = input_cell(row.get("timeseries") or row.get("ts")) or ts_label(hour)
        q_rows.append({"hour": hour_s, "timeseries": ts, "q_m3s": q_val})
        h_rows.append({"hour": hour_s, "h_m": h_val})
        trib_rows.append({"hour": hour_s, "h_m": h_val, "h_na1_m": na1_val})
        if na1_val:
            have_na1 = True
    if not q_rows:
        raise ValueError("Khong co dong du lieu hop le.")
    write_csv_rows(
        SV_INFLOW_CSV,
        ["hour", "q_m3s"],
        [{"hour": r["hour"], "q_m3s": r["q_m3s"]} for r in q_rows],
        rebuild_hydro=False,
    )
    write_csv_rows(MK_DOWN_H_CSV, ["hour", "h_m"], h_rows, rebuild_hydro=False)
    if have_na1:
        write_csv_rows(
            SV_DOWN_H_CSV,
            ["hour", "h_m", "h_na1_m"],
            trib_rows,
        )
    else:
        write_csv_rows(SV_DOWN_H_CSV, ["hour", "h_m"], h_rows)
    return load_sv_boundary_payload()


# ---------------------------------------------------------------------------
# Khai bao diem bien: toa do + duong dan chuoi
# ---------------------------------------------------------------------------

BOUNDARY_CSV = ROOT / "boundary_input.csv"
BOUNDARY_STATION_COLS = [
    "id",
    "name",
    "kind",
    "reach",
    "lon",
    "lat",
    "station_km",
    "xs_id",
    "file",
    "value_col",
    "unit",
]
BOUNDARY_KINDS = ("P", "PET", "Q", "H")
BOUNDARY_REACH_BASIN = "basin"


def _boundary_reach_label(reach: Any) -> str:
    rid = str(reach or "").strip().lower()
    if rid in ("", BOUNDARY_REACH_BASIN):
        return "Lưu vực"
    key = normalize_reach_id(rid, "main")
    if key == "main" or is_primary_main_reach(key):
        return "Sông chính 1"
    if is_main_reach_id(key):
        m = re.search(r"(\d+)$", key)
        return f"Sông chính {int(m.group(1)) if m else 2}"
    m = re.search(r"(\d+)$", key)
    return f"Sông nhánh {int(m.group(1)) if m else 1}"


def _boundary_reach_kind(reach: Any) -> str:
    rid = str(reach or "").strip().lower()
    if rid in ("", BOUNDARY_REACH_BASIN):
        return "basin"
    key = normalize_reach_id(rid, "main")
    if is_main_reach_id(key):
        return "main"
    return "trib"


def _normalize_reach_value(raw: Any) -> str:
    rid = input_cell(raw).lower().replace(" ", "_").replace("-", "_")
    if not rid or rid in ("basin", "luu_vuc", "lưu_vực"):
        return BOUNDARY_REACH_BASIN
    if rid in ("main", "chinh", "chính", "song_chinh", "sông_chính"):
        return "main"
    if rid in ("trib", "nhanh", "nhánh", "song_nhanh", "sông_nhánh"):
        return "trib_1"
    return normalize_reach_id(rid, "main")


def _geom_csv_paths() -> tuple[Path, Path]:
    main_geom = ROOT / "mike_hd_output" / "mike_hd_river_geometry.csv"
    if not csv_available(main_geom):
        main_geom = SV_OUT_DIR / "demo_river_geometry.csv"
    trib_geom = ROOT / "mike_hd_output" / "mike_hd_tributary_geometry.csv"
    if not csv_available(trib_geom):
        trib_geom = SV_OUT_DIR / "demo_tributary_geometry.csv"
    return main_geom, trib_geom


def _reach_end_point(geom: dict[str, Any], *, at_end: bool) -> dict[str, str]:
    lon = geom.get("lon")
    lat = geom.get("lat")
    station_m = geom.get("station_m")
    if lon is None or lat is None or len(lon) < 1:
        return {"lon": "", "lat": "", "station_km": "", "xs_id": ""}
    idx = -1 if at_end else 0
    try:
        st_km = float(station_m[idx]) / 1000.0 if station_m is not None and len(station_m) else ""
        st_txt = f"{st_km:.3f}" if st_km != "" else ""
    except (TypeError, ValueError, IndexError):
        st_txt = ""
    return {
        "lon": f"{float(lon[idx]):.6f}",
        "lat": f"{float(lat[idx]):.6f}",
        "station_km": st_txt,
        "xs_id": "",
    }


def list_boundary_reaches() -> list[dict[str, str]]:
    """Danh sach song chinh / song nhanh tu hinh hoc 1D + luu vuc."""
    main_geom, trib_geom = _geom_csv_paths()
    reaches: list[dict[str, str]] = [
        {"id": BOUNDARY_REACH_BASIN, "kind": "basin", "label": "Lưu vực"},
    ]
    seen = {BOUNDARY_REACH_BASIN}
    for which, path in (
        ("primary", main_geom),
        ("extra_main", main_geom),
        ("trib", trib_geom),
    ):
        for geom in load_grouped_geom_csv(path, which=which):
            rid = normalize_reach_id(geom.get("id"), "main" if which != "trib" else "trib_1")
            if rid in seen:
                continue
            seen.add(rid)
            reaches.append({
                "id": rid,
                "kind": _boundary_reach_kind(rid),
                "label": _boundary_reach_label(rid),
            })
    if "main" not in seen:
        reaches.insert(1, {"id": "main", "kind": "main", "label": "Sông chính 1"})
    if not any(r["kind"] == "trib" for r in reaches):
        reaches.append({"id": "trib_1", "kind": "trib", "label": "Sông nhánh 1"})
    return reaches


def _rel_data_path(path: Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_data_path(raw: Any) -> Path:
    text = input_cell(raw)
    if not text:
        return ROOT
    path = Path(text)
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def _geom_end_point(path: Path, *, at_end: bool) -> dict[str, str]:
    empty = {"lon": "", "lat": "", "station_km": "", "xs_id": ""}
    if not csv_available(path):
        return empty
    rows: list[dict[str, str]] = []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        return empty
    rec = rows[-1] if at_end else rows[0]
    fmap = csv_field_map(rec.keys())
    lon_c = pick_csv_col(fmap, "lon", "lng", "longitude")
    lat_c = pick_csv_col(fmap, "lat", "latitude")
    st_c = pick_csv_col(fmap, "station_km", "station", "s_km")
    id_c = pick_csv_col(fmap, "xs_id", "id")
    return {
        "lon": input_cell(rec.get(lon_c) if lon_c else ""),
        "lat": input_cell(rec.get(lat_c) if lat_c else ""),
        "station_km": input_cell(rec.get(st_c) if st_c else ""),
        "xs_id": input_cell(rec.get(id_c) if id_c else ""),
    }


def _mid_lonlat(a: dict[str, str], b: dict[str, str]) -> tuple[str, str]:
    try:
        lon = 0.5 * (float(a["lon"]) + float(b["lon"]))
        lat = 0.5 * (float(a["lat"]) + float(b["lat"]))
    except (KeyError, TypeError, ValueError):
        return a.get("lon") or b.get("lon") or "", a.get("lat") or b.get("lat") or ""
    return f"{lon:.6f}", f"{lat:.6f}"


def _station(
    sid: str,
    name: str,
    kind: str,
    reach: str,
    point: dict[str, str],
    path: Path,
    value_col: str,
    unit: str,
    *,
    lon: str = "",
    lat: str = "",
) -> dict[str, str]:
    return {
        "id": sid,
        "name": name,
        "kind": kind,
        "reach": reach,
        "lon": lon or point.get("lon", ""),
        "lat": lat or point.get("lat", ""),
        "station_km": point.get("station_km", ""),
        "xs_id": point.get("xs_id", ""),
        "file": _rel_data_path(path),
        "value_col": value_col,
        "unit": unit,
    }


def default_boundary_stations() -> list[dict[str, str]]:
    """Diem bien mac dinh theo song chinh / song nhanh tu hinh hoc 1D."""
    main_geom, trib_geom = _geom_csv_paths()
    primaries = load_grouped_geom_csv(main_geom, which="primary")
    extras = load_grouped_geom_csv(main_geom, which="extra_main")
    tribs = load_grouped_geom_csv(trib_geom, which="trib")
    if not primaries:
        us = _geom_end_point(main_geom, at_end=False)
        ds = _geom_end_point(main_geom, at_end=True)
    else:
        us = _reach_end_point(primaries[0], at_end=False)
        ds = _reach_end_point(primaries[0], at_end=True)
    basin_lon, basin_lat = _mid_lonlat(us, ds)
    basin = {"lon": basin_lon, "lat": basin_lat, "station_km": "", "xs_id": ""}
    rows = [
        _station("P_basin", "Mưa lưu vực", "P", BOUNDARY_REACH_BASIN, basin, TANK_RAIN_CSV, "rainfall_mm", "mm"),
        _station("PET_basin", "Bốc hơi tiềm năng", "PET", BOUNDARY_REACH_BASIN, basin, TANK_RAIN_CSV, "pet_mm", "mm"),
        _station("Q_obs", "Q quan trắc TANK", "Q", BOUNDARY_REACH_BASIN, us, TANK_FLOW_CSV, "q_m3/s", "m3/s"),
        _station("Q_us", "Q thượng lưu sông chính 1", "Q", "main", us, NAM_RESULT_CSV, "q_m3s", "m3/s"),
        _station("H_ds", "H hạ lưu sông chính 1", "H", "main", ds, SV_DOWN_H_CSV, "h_m", "m"),
    ]
    for i, geom in enumerate(extras, start=2):
        rid = normalize_reach_id(geom.get("id"), f"main_{i}")
        label = _boundary_reach_label(rid)
        rows.append(_station(
            f"Q_{rid}", f"Q thượng lưu {label}", "Q", rid,
            _reach_end_point(geom, at_end=False), NAM_RESULT_CSV, "q_m3s", "m3/s",
        ))
        rows.append(_station(
            f"H_{rid}_ds", f"H hạ lưu {label}", "H", rid,
            _reach_end_point(geom, at_end=True), SV_DOWN_H_CSV, "h_m", "m",
        ))
    if not tribs:
        trib_pt = _geom_end_point(trib_geom, at_end=True)
        rows.append(_station(
            "H_na1", "H hạ lưu sông nhánh 1", "H", "trib_1",
            trib_pt, SV_DOWN_H_CSV, "h_na1_m", "m",
        ))
    else:
        for i, geom in enumerate(tribs, start=1):
            rid = normalize_reach_id(geom.get("id"), f"trib_{i}")
            label = _boundary_reach_label(rid)
            sid = "H_na1" if i == 1 else f"H_{rid}"
            col = "h_na1_m" if i == 1 else "h_m"
            rows.append(_station(
                sid, f"H hạ lưu {label}", "H", rid,
                _reach_end_point(geom, at_end=True), SV_DOWN_H_CSV, col, "m",
            ))
    return rows


def _normalize_station(raw: Any, index: int) -> dict[str, str]:
    row = raw if isinstance(raw, dict) else {}
    rec = {col: input_cell(row.get(col)) for col in BOUNDARY_STATION_COLS}
    if not rec["id"]:
        rec["id"] = f"bc_{index}"
    kind = rec["kind"].upper()
    rec["kind"] = kind if kind in BOUNDARY_KINDS else rec["kind"]
    rec["reach"] = _normalize_reach_value(rec.get("reach"))
    if rec["lon"]:
        try:
            lon = float(rec["lon"])
        except ValueError as exc:
            raise ValueError(f"Dong {index}: lon khong hop le.") from exc
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"Dong {index}: lon phai trong [-180, 180].")
        rec["lon"] = f"{lon:.6f}"
    if rec["lat"]:
        try:
            lat = float(rec["lat"])
        except ValueError as exc:
            raise ValueError(f"Dong {index}: lat khong hop le.") from exc
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"Dong {index}: lat phai trong [-90, 90].")
        rec["lat"] = f"{lat:.6f}"
    if rec["file"]:
        rec["file"] = rec["file"].replace("\\", "/")
    return rec


def load_boundary_stations(*, seed: bool = True) -> list[dict[str, str]]:
    if csv_available(BOUNDARY_CSV):
        rows: list[dict[str, str]] = []
        with csv_open(BOUNDARY_CSV) as f:
            for i, row in enumerate(csv.DictReader(f), start=1):
                rows.append(_normalize_station(row, i))
        if rows:
            return rows
    rows = default_boundary_stations()
    if seed:
        write_csv_rows(BOUNDARY_CSV, BOUNDARY_STATION_COLS, rows, rebuild_hydro=False)
    return rows


def save_boundary_stations(raw_rows: Any) -> list[dict[str, str]]:
    if not isinstance(raw_rows, list):
        raise ValueError("Can JSON {rows: [{id, name, kind, lon, lat, file, value_col}, ...]}.")
    rows = [_normalize_station(row, i + 1) for i, row in enumerate(raw_rows) if isinstance(row, dict)]
    if not rows:
        raise ValueError("Khong co diem bien hop le.")
    write_csv_rows(BOUNDARY_CSV, BOUNDARY_STATION_COLS, rows, rebuild_hydro=False)
    return load_boundary_stations(seed=False)


def station_file_ok(rec: dict[str, str]) -> bool:
    path = resolve_data_path(rec.get("file"))
    if not rec.get("file") or not csv_available(path):
        return False
    col = input_cell(rec.get("value_col"))
    if not col:
        return True
    try:
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            fmap = csv_field_map(reader.fieldnames)
        return pick_csv_col(fmap, col) is not None
    except Exception:
        return False


def _reach_matches(rec_reach: Any, want_reach: str) -> bool:
    if not want_reach:
        return True
    rid = _normalize_reach_value(rec_reach)
    want = _normalize_reach_value(want_reach)
    # goi cu reach="trib" -> khop moi song nhanh
    if str(want_reach).strip().lower() in ("trib", "nhanh", "nhánh"):
        return _boundary_reach_kind(rid) == "trib"
    if want == "main":
        return is_primary_main_reach(rid)
    return rid == want


def find_boundary_station(*, kind: str = "", reach: str = "", sid: str = "") -> dict[str, str] | None:
    want_kind = kind.upper()
    for rec in load_boundary_stations(seed=False):
        if sid and rec.get("id") != sid:
            continue
        if want_kind and rec.get("kind", "").upper() != want_kind:
            continue
        if reach and not _reach_matches(rec.get("reach"), reach):
            continue
        return rec
    return None


def station_data_path(*, kind: str = "", reach: str = "", sid: str = "") -> Path | None:
    rec = find_boundary_station(kind=kind, reach=reach, sid=sid)
    if not rec or not rec.get("file"):
        return None
    path = resolve_data_path(rec["file"])
    return path if csv_available(path) else None


def load_boundary_station_payload() -> dict[str, Any]:
    rows = load_boundary_stations()
    reaches = list_boundary_reaches()
    reach_ids = {r["id"] for r in reaches}
    out: list[dict[str, Any]] = []
    for rec in rows:
        item = dict(rec)
        rid = _normalize_reach_value(item.get("reach"))
        item["reach"] = rid
        item["reach_kind"] = _boundary_reach_kind(rid)
        item["reach_label"] = _boundary_reach_label(rid)
        item["file_ok"] = station_file_ok(rec)
        item["abs_file"] = str(resolve_data_path(rec.get("file"))) if rec.get("file") else ""
        out.append(item)
        if rid not in reach_ids:
            reaches.append({
                "id": rid,
                "kind": item["reach_kind"],
                "label": item["reach_label"],
            })
            reach_ids.add(rid)
    return {
        "ok": True,
        "path": str(BOUNDARY_CSV),
        "database": "data_flood",
        "n": len(out),
        "kinds": list(BOUNDARY_KINDS),
        "columns": list(BOUNDARY_STATION_COLS),
        "reaches": reaches,
        "rows": out,
    }


_BROWSE_SKIP = {
    ".git",
    "__pycache__",
    "venv",
    "node_modules",
    "static",
    "templates",
    "vendors",
}


def browse_data_dir(rel: Any = "") -> dict[str, Any]:
    """Liet ke thu muc / file CSV (dia + PostgreSQL data_flood) de Browse duong dan bien."""
    root = ROOT.resolve()
    raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if ".." in Path(raw).parts:
        raise ValueError("Duong dan khong hop le.")
    cur = (root / raw).resolve() if raw else root
    try:
        cur.relative_to(root)
    except ValueError as exc:
        raise ValueError("Chi duoc duyet trong thu muc flood_model.") from exc

    dirs_map: dict[str, dict[str, str]] = {}
    files_map: dict[str, dict[str, str]] = {}

    if cur.exists() and cur.is_dir():
        for child in sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            name = child.name
            if name.startswith(".") or name in _BROWSE_SKIP:
                continue
            try:
                rel_path = child.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            if child.is_dir():
                dirs_map[name] = {"name": name, "path": rel_path, "kind": "dir", "source": "disk"}
            elif child.suffix.lower() == ".csv":
                files_map[name] = {"name": name, "path": rel_path, "kind": "file", "source": "disk"}

    # Dataset trong PostgreSQL (nhieu CSV chi con trong data_flood)
    try:
        from flood_model.db import list_datasets

        prefix = (raw + "/") if raw else ""
        for ds in list_datasets():
            key = str(ds.get("dataset_key") or "").replace("\\", "/").lstrip("/")
            if not key.lower().endswith(".csv"):
                continue
            if prefix:
                if not key.startswith(prefix):
                    continue
                rest = key[len(prefix):]
            else:
                rest = key
            if not rest:
                continue
            parts = rest.split("/")
            if len(parts) > 1:
                folder = parts[0]
                if folder in _BROWSE_SKIP or folder.startswith("."):
                    continue
                dirs_map.setdefault(
                    folder,
                    {"name": folder, "path": f"{prefix}{folder}".rstrip("/"), "kind": "dir", "source": "db"},
                )
            else:
                name = parts[0]
                files_map.setdefault(
                    name,
                    {
                        "name": name,
                        "path": key,
                        "kind": "file",
                        "source": "db",
                        "n_rows": ds.get("n_rows"),
                    },
                )
    except Exception:
        pass

    parent = ""
    if raw:
        parent = str(Path(raw).parent).replace("\\", "/")
        if parent in (".",):
            parent = ""

    return {
        "ok": True,
        "root": str(root),
        "cwd": raw,
        "parent": parent,
        "dirs": [dirs_map[k] for k in sorted(dirs_map.keys(), key=str.lower)],
        "files": [files_map[k] for k in sorted(files_map.keys(), key=str.lower)],
    }


def csv_columns_for_path(rel: Any) -> dict[str, Any]:
    """Doc danh sach ten cot cua 1 CSV (PostgreSQL hoac file dia)."""
    raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw or ".." in Path(raw).parts:
        raise ValueError("Duong dan file khong hop le.")
    if not raw.lower().endswith(".csv"):
        raise ValueError("Chi ho tro file CSV.")
    path = resolve_data_path(raw)
    columns: list[str] = []
    source = ""
    try:
        from flood_model.db import fetch_rows_for_path

        loaded = fetch_rows_for_path(path)
        if loaded is not None:
            columns = [str(c) for c in loaded[0] if str(c or "").strip()]
            source = "db"
    except Exception:
        loaded = None
    if not columns and path.is_file():
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None) or []
        columns = [str(c).strip() for c in header if str(c or "").strip()]
        source = "disk"
    if not columns and csv_available(path):
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            columns = [str(c).strip() for c in (reader.fieldnames or []) if str(c or "").strip()]
            source = source or "csv"
    if not columns:
        raise FileNotFoundError(f"Khong doc duoc cot cua {raw}")
    return {
        "ok": True,
        "path": raw,
        "columns": columns,
        "n": len(columns),
        "source": source,
    }

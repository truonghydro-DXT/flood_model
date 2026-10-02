"""
Mat cat doc DEM 3D + muc nuoc dong chay mat (TANK q_surface_mm).

- Lay cao do dia hinh doc duong ve (polyline lon/lat).
- Chay mo hinh TANK (rainfall-runoff.py) lay q_surface_mm.
- Muc nuoc 1D: Saint-Venant (saint-venant.py).
- Ve mat cat: giu geometry polyline ve tay (cao do DEM doc duong ve);
  muc nuoc 1D van gan theo tram/XS gan nhat (khong thay profile bang XS he thong).

API chay mo hinh (flood_model/api_run_model.py, cung prefix /api/flow-3d):
  POST /api/flow-3d/simulate         Model 1D (saint-venant.py / sv-mike-by-dhi.py)
  GET  /api/flow-3d/simulate         trang thai mo phong 1D / RR / NAM
  POST /api/flow-3d/simulate-rr      Model RR TANK (rainfall-runoff.py)
  POST /api/flow-3d/simulate-nam     Model NAM (mike-nam.py)

API (blueprint):
  POST /api/flow-3d/profile          toa do ve tay
  POST /api/flow-3d/thalweg          tu dong theo long song
  GET  /api/flow-3d/runoff-params    dien tich luu vuc + he so TANK
  PUT  /api/flow-3d/runoff-params    ghi demo_tank_params.csv
  GET  /api/flow-3d/nam-params       dien tich luu vuc + he so NAM
  PUT  /api/flow-3d/nam-params       ghi demo_nam_params.csv
  GET  /api/flow-3d/manning-n        doc he so nham theo mat cat
  PUT  /api/flow-3d/manning-n        ghi demo_manning_n.csv
  POST /api/flow-3d/extract-xs       trich mat cat theo khoang XS, luu PostgreSQL
  POST /api/flow-3d/manning-n-dem    chay mainning.py (n tu DEM)
  GET  /api/flow-3d/xs-hydrograph    duong qua trinh Q/H tai 1 mat cat
  GET  /api/flow-3d/xs-profile       profile mat cat (offset/z) + Manning n
  GET  /api/flow-3d/xs-obs           Q/H thuc do (mike_hd_thucdo.csv)
  POST /api/flow-3d/xs-obs           tai file CSV thuc do
  GET  /api/flow-3d/sv-input         Q/H bien Saint-Venant
  PUT  /api/flow-3d/sv-input         ghi Q thuong luu va H bien
  GET  /api/flow-3d/reverse-geocode  lat, lon -> dia danh
  POST /api/flow-3d/reverse-geocode  JSON {lat, lon} -> dia danh
  GET  /api/flow-3d/place            alias: lat, lon -> dia danh
  POST /api/flow-3d/place            alias JSON {lat, lon} -> dia danh
  GET  /api/flow-3d/constructions    khai bao cong trinh (de, dap, ho)
  PUT  /api/flow-3d/constructions    ghi constructions.csv
  GET  /api/flow-3d/network-overlay  mat cat + cong trinh (lon/lat) ve DEM 3D
  GET  /api/flow-3d/runoff           chuoi q_surface_mm demo

DEM mac dinh: flood_model/projects/data/dem-song-hong.tif
"""

from __future__ import annotations

import argparse
import csv
import heapq
import io
import json
import math
import os
import re
import runpy
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence, cast

import numpy as np
import numpy.typing as npt
from flask import Blueprint, Response, jsonify, request
from rasterio.enums import Resampling
from rasterio.transform import Affine, rowcol, xy
from rasterio.warp import transform as rio_transform
from rasterio.windows import Window, from_bounds as window_from_bounds
from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.api_run_model import (
    _SIM_JOB,
    _SIM_LOCK,
    _append_sim_log,
    _sim_snapshot,
    _sim_subprocess_env,
)
from flood_model.boundary import (
    MK_DOWN_H_CSV,
    NAM_RESULT_CSV,
    SV_DOWN_H_CSV,
    SV_INFLOW_CSV,
    TANK_FLOW_CSV,
    TANK_OUT_DIR,
    TANK_RAIN_CSV,
    TANK_RESULT_CSV,
    browse_data_dir,
    csv_columns_for_path,
    csv_field_map as _csv_field_map,
    ensure_runoff_input_csv,
    load_boundary_station_payload,
    load_runoff_input_payload,
    load_sv_boundary_payload,
    pick_csv_col as _pick_csv_col,
    save_boundary_stations,
    save_runoff_input_rows,
    save_sv_boundary_rows,
)
from flood_model.construction import (
    default_constructions,
    load_constructions,
    load_constructions_payload,
    normalize_structure_type,
    save_constructions,
    structure_bank_side,
    structure_formula_label,
    structure_type_label,
)
from flood_model import cross_section as _cross_section

chainage_m = getattr(_cross_section, "chainage_m")
densify_xy = getattr(_cross_section, "densify_xy")
load_official_xs = getattr(_cross_section, "load_official_xs")
lower_envelope = getattr(_cross_section, "lower_envelope")
profile_from_lonlat = getattr(_cross_section, "profile_from_lonlat")
sample_cross_section = getattr(_cross_section, "sample_cross_section")
sample_z = getattr(_cross_section, "sample_z")
from flood_model.routing import (
    HYDRO1D_FORCE_MIKE,
    extra_main_routes,
    hydro1d_csv_paths,
    hydro1d_label,
    hydro1d_water_sources,
    network_sv_routes,
    normalize_reach_id,
    pick_route_by_id,
)
from flood_model.river_network import (
    DUONG_TRACK_VER,
    clear_network_cache,
    dem_river_network,
    extra_main_centerlines,
    main_centerline_lonlat,
    thalweg_lonlat,
    floodplain_banks,
    tributary_centerlines,
)


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
SCRIPT_DIR = PACKAGE_DIR
DEFAULT_DEM = ROOT / "projects" / "data" / "dem-song-hong.tif"
DEFAULT_TARGET_PEAK_DEPTH_M = 4.0
MAX_PROFILE_POINTS = 800
SAINT_VENANT_GEOM_CSV = ROOT / "saint_venant_output" / "demo_river_geometry.csv"
SAINT_VENANT_H_CSV = ROOT / "saint_venant_output" / "saint_venant_result.csv"
SAINT_VENANT_N_CSV = ROOT / "saint_venant_output" / "demo_manning_n.csv"
SAINT_VENANT_TRIB_N_CSV = ROOT / "saint_venant_output" / "demo_manning_n_trib.csv"
SAINT_VENANT_PARAMS_CSV = ROOT / "saint_venant_output" / "demo_sv_params.csv"
SAINT_VENANT_TRIB_GEOM_CSV = ROOT / "saint_venant_output" / "demo_tributary_geometry.csv"
SAINT_VENANT_XS_CSV = ROOT / "saint_venant_output" / "demo_cross_sections.csv"
XS_SNAP_RIVER_MAX_M = 2500.0
TRIB_XS_PENALTY_M = 150.0
TANK_PARAMS_CSV = TANK_OUT_DIR / "demo_tank_params.csv"
NAM_PARAMS_CSV = TANK_OUT_DIR / "demo_nam_params.csv"
XS_MATCH_TOL_M = 200.0
WATER_SOURCE_ALIASES = {
    "sv": "saint-venant",
    "saint_venant": "saint-venant",
    "saintvenant": "saint-venant",
    "saint-venant": "saint-venant",
    "saint-venant-1d": "saint-venant-1d",
    "saintvenant-1d": "saint-venant-1d",
    "saint_venant_1d": "saint-venant-1d",
    "sv-mike": "saint-venant-1d",
    "mike-hd": "saint-venant-1d",
    "mike": "saint-venant-1d",
    "tank": "tank",
    "runoff": "tank",
}
MIKE_HD_OUT_DIR = ROOT / "mike_hd_output"
MIKE_HD_OBS_CSV = MIKE_HD_OUT_DIR / "mike_hd_thucdo.csv"
SAINT_VENANT_OBS_CSV = ROOT / "saint_venant_output" / "demo_thucdo.csv"
MIKE_HD_RESULT_COPIES = (
    ("mike_hd_result.csv", "saint_venant_result.csv"),
    ("mike_hd_river_geometry.csv", "demo_river_geometry.csv"),
    ("mike_hd_cross_sections.csv", "demo_cross_sections.csv"),
    ("mike_hd_tributary_geometry.csv", "demo_tributary_geometry.csv"),
    ("mike_hd_tributary_result.csv", "saint_venant_tributary_result.csv"),
    ("mike_hd_extra_main_result.csv", "demo_extra_main_result.csv"),
    ("mike_hd_network_reaches.csv", "demo_network_reaches.csv"),
)

_TANK_MOD: dict[str, Any] | None = None
_NAM_MOD: dict[str, Any] | None = None


def _csv_ready(path: Path) -> bool:
    return csv_available(path)


def _tank_mod() -> dict[str, Any]:
    """Nap rainfall-runoff.py (ten co dau gach). run_name de @dataclass tim duoc module."""
    global _TANK_MOD
    if _TANK_MOD is None:
        path = SCRIPT_DIR / "rainfall-runoff.py"
        if not path.is_file():
            raise FileNotFoundError(f"Khong tim thay {path}")
        _TANK_MOD = runpy.run_path(str(path), run_name="rainfall_runoff")
    return _TANK_MOD


def _nam_mod() -> dict[str, Any]:
    """Nap mike-nam.py. run_name de @dataclass tim duoc module."""
    global _NAM_MOD
    if _NAM_MOD is None:
        path = SCRIPT_DIR / "mike-nam.py"
        if not path.is_file():
            raise FileNotFoundError(f"Khong tim thay {path}")
        _NAM_MOD = runpy.run_path(str(path), run_name="mike_nam")
    return _NAM_MOD


RR_PARAM_GROUPS: list[dict[str, Any]] = [
    {
        "title": "Lưu vực",
        "fields": [
            {"key": "area_km2", "label": "Diện tích (km²)", "min": 1.0, "max": 1_000_000.0, "step": 1},
            {"key": "dt_hours", "label": "Bước thời gian (giờ)", "min": 0.25, "max": 24.0, "step": 0.25},
        ],
    },
    {
        "title": "Bể 1 — mặt",
        "fields": [
            {"key": "a0", "label": "a0 thấm xuống bể 2", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "a1", "label": "a1 cửa bên thấp", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "a2", "label": "a2 cửa bên cao", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "h1a", "label": "h1a ngưỡng thấp (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "h1b", "label": "h1b ngưỡng cao (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "s1_0", "label": "s1_0 lưu trữ ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
        ],
    },
    {
        "title": "Bể 2 — trung gian",
        "fields": [
            {"key": "b0", "label": "b0 thấm xuống bể 3", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "b1", "label": "b1 cửa bên", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "h2", "label": "h2 ngưỡng (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "s2_0", "label": "s2_0 lưu trữ ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
        ],
    },
    {
        "title": "Bể 3 — ngầm nông",
        "fields": [
            {"key": "c0", "label": "c0 thấm xuống bể 4", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "c1", "label": "c1 cửa bên", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "h3", "label": "h3 ngưỡng (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "s3_0", "label": "s3_0 lưu trữ ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
        ],
    },
    {
        "title": "Bể 4 — ngầm sâu",
        "fields": [
            {"key": "d1", "label": "d1 cửa bên", "min": 0.0, "max": 1.0, "step": 0.001},
            {"key": "h4", "label": "h4 ngưỡng (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "s4_0", "label": "s4_0 lưu trữ ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
        ],
    },
]
RR_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    str(field["key"]): (float(field["min"]), float(field["max"]))
    for group in RR_PARAM_GROUPS
    for field in group["fields"]
}

NAM_PARAM_GROUPS: list[dict[str, Any]] = [
    {
        "title": "Lưu vực",
        "fields": [
            {"key": "area_km2", "label": "Diện tích (km²)", "min": 1.0, "max": 1_000_000.0, "step": 1},
            {"key": "dt_hours", "label": "Bước thời gian (giờ)", "min": 0.25, "max": 24.0, "step": 0.25},
            {"key": "carea", "label": "Carea tỉ lệ nước ngầm", "min": 0.0, "max": 2.0, "step": 0.01},
        ],
    },
    {
        "title": "Bể mặt U",
        "fields": [
            {"key": "umax", "label": "Umax sức chứa (mm)", "min": 1.0, "max": 80.0, "step": 0.1},
            {"key": "u0", "label": "U0 ban đầu (mm)", "min": 0.0, "max": 80.0, "step": 0.1},
        ],
    },
    {
        "title": "Bể đất / rễ L",
        "fields": [
            {"key": "lmax", "label": "Lmax sức chứa (mm)", "min": 10.0, "max": 500.0, "step": 1},
            {"key": "l0", "label": "L0 ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 1},
            {"key": "tof", "label": "TOF ngưỡng chảy mặt", "min": 0.0, "max": 0.99, "step": 0.01},
            {"key": "tif", "label": "TIF ngưỡng liên lưu", "min": 0.0, "max": 0.99, "step": 0.01},
            {"key": "tg", "label": "TG ngưỡng nạp ngầm", "min": 0.0, "max": 0.99, "step": 0.01},
        ],
    },
    {
        "title": "Hệ số chảy / thời gian",
        "fields": [
            {"key": "cqof", "label": "CQOF hệ số chảy mặt", "min": 0.0, "max": 1.0, "step": 0.01},
            {"key": "ck12", "label": "CK1,2 chảy mặt (giờ)", "min": 1.0, "max": 200.0, "step": 0.5},
            {"key": "ckif", "label": "CKIF liên lưu (giờ)", "min": 10.0, "max": 5000.0, "step": 10},
            {"key": "ckbf", "label": "CKBF cơ sở (giờ)", "min": 50.0, "max": 10000.0, "step": 10},
        ],
    },
    {
        "title": "Nước ngầm / định tuyến",
        "fields": [
            {"key": "g0", "label": "G0 nước ngầm (mm)", "min": 0.0, "max": 2000.0, "step": 1},
            {"key": "sof1_0", "label": "SOF1 ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "sof2_0", "label": "SOF2 ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
            {"key": "sif_0", "label": "SIF ban đầu (mm)", "min": 0.0, "max": 500.0, "step": 0.1},
        ],
    },
]
NAM_PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    str(field["key"]): (float(field["min"]), float(field["max"]))
    for group in NAM_PARAM_GROUPS
    for field in group["fields"]
}


def load_runoff_params() -> tuple[Any, Any]:
    mod = _tank_mod()
    if _csv_ready(TANK_PARAMS_CSV):
        return mod["load_params_csv"](TANK_PARAMS_CSV)
    return mod["Basin"](), mod["TankParams"]()


def runoff_params_payload() -> dict[str, Any]:
    basin, params = load_runoff_params()
    values = _tank_mod()["params_to_dict"](basin, params)
    return {
        "ok": True,
        "path": str(TANK_PARAMS_CSV),
        "values": {k: round(float(v), 6) if isinstance(v, (int, float)) else v for k, v in values.items()},
        "groups": RR_PARAM_GROUPS,
        "hydrograph": runoff_hydrograph_payload(),
    }


def _load_xy_csv(
    path: Path,
    hour_names: tuple[str, ...],
    q_names: tuple[str, ...],
) -> tuple[list[float], list[float]]:
    if not csv_available(path):
        return [], []
    hours: list[float] = []
    qs: list[float] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        fields = _csv_field_map(list(reader.fieldnames) if reader.fieldnames is not None else None)
        hcol = _pick_csv_col(fields, *hour_names)
        qcol = _pick_csv_col(fields, *q_names)
        if qcol is None:
            return [], []
        for i, row in enumerate(reader):
            try:
                q = float(row[qcol])
            except (TypeError, ValueError, KeyError):
                continue
            if not math.isfinite(q):
                continue
            hour = float(i)
            if hcol is not None and str(row.get(hcol, "")).strip() != "":
                try:
                    hour = float(row[hcol])
                except (TypeError, ValueError):
                    hour = float(i)
            hours.append(hour)
            qs.append(q)
    return hours, qs


def _round_series(vals: list[float], ndigits: int) -> list[float]:
    return [round(float(v), ndigits) for v in vals]


def _pair_obs_sim(
    hours_obs: list[float],
    q_obs: list[float],
    hours_sim: list[float],
    q_sim: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    o = np.asarray(q_obs, dtype=float)
    s = np.asarray(q_sim, dtype=float)
    if o.size == 0 or s.size == 0:
        return np.array([]), np.array([])
    ho = np.asarray(hours_obs, dtype=float)
    hs = np.asarray(hours_sim, dtype=float)
    if ho.size == o.size and hs.size == s.size and o.size and s.size:
        order = np.argsort(hs)
        s_at_o = np.interp(ho, hs[order], s[order])
        mask = np.isfinite(o) & np.isfinite(s_at_o)
        return o[mask], s_at_o[mask]
    n = min(o.size, s.size)
    mask = np.isfinite(o[:n]) & np.isfinite(s[:n])
    return o[:n][mask], s[:n][mask]


def nash_sutcliffe(q_obs: np.ndarray, q_sim: np.ndarray) -> float | None:
    if q_obs.size < 2 or q_sim.size != q_obs.size:
        return None
    denom = float(np.sum((q_obs - float(q_obs.mean())) ** 2))
    if denom <= 1e-12:
        return None
    nse = 1.0 - float(np.sum((q_obs - q_sim) ** 2) / denom)
    if not math.isfinite(nse):
        return None
    return nse


def _timeseries_by_hour() -> dict[float, str]:
    from flood_model.db import fetch_hydro_timeseries

    out: dict[float, str] = {}
    try:
        for row in fetch_hydro_timeseries() or []:
            hour = row.get("hour")
            label = row.get("timeseries") or row.get("ts")
            if hour is None or label is None or str(label).strip() == "":
                continue
            out[round(float(hour), 6)] = str(label).strip()
    except Exception:
        pass
    return out


def _ts_series(hours: npt.ArrayLike, by_hour: dict[float, str] | None = None) -> list[str]:
    from flood_model.db import ts_label

    mapping = by_hour if by_hour is not None else _timeseries_by_hour()
    labels: list[str] = []
    for hour in np.asarray(hours, dtype=float).tolist():
        key = round(float(hour), 6)
        labels.append(mapping.get(key) or ts_label(float(hour)))
    return labels


def runoff_hydrograph_payload() -> dict[str, Any]:
    obs_csv = TANK_FLOW_CSV if csv_available(TANK_FLOW_CSV) else TANK_RAIN_CSV
    h_obs, q_obs = _load_xy_csv(
        obs_csv,
        ("hour", "t", "time"),
        ("q_m3/s", "q_m3s", "q_obs_m3s", "q_obs"),
    )
    h_sim, q_sim = _load_xy_csv(
        TANK_RESULT_CSV,
        ("hour", "t", "time"),
        ("q_m3s", "q_m3/s", "q_total_m3s"),
    )
    o_pair, s_pair = _pair_obs_sim(h_obs, q_obs, h_sim, q_sim)
    nse = nash_sutcliffe(o_pair, s_pair)
    ts_map = _timeseries_by_hour()
    return {
        "hours_obs": _round_series(h_obs, 3),
        "ts_obs": _ts_series(h_obs, ts_map),
        "q_obs_m3s": _round_series(q_obs, 4),
        "hours_sim": _round_series(h_sim, 3),
        "ts_sim": _ts_series(h_sim, ts_map),
        "q_sim_m3s": _round_series(q_sim, 4),
        "nse": None if nse is None else round(nse, 4),
        "nse_n": int(o_pair.size),
        "obs_path": str(obs_csv) if q_obs else None,
        "sim_path": str(TANK_RESULT_CSV) if q_sim else None,
    }


def _fmt_param_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not math.isfinite(float(v)):
            return ""
        return f"{float(v):.6g}"
    return str(v)


def _param_rows(values: dict[str, Any], order: Sequence[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in order:
        if key not in values:
            continue
        rows.append({"param": key, "value": _fmt_param_value(values[key])})
        seen.add(key)
    for key, val in values.items():
        if key in seen:
            continue
        rows.append({"param": str(key), "value": _fmt_param_value(val)})
    return rows


def write_param_table(path: Path, values: dict[str, Any], order: Sequence[str]) -> None:
    write_csv_rows(path, ["param", "value"], _param_rows(values, order), rebuild_hydro=False)


def dataset_write_meta(path: Path) -> dict[str, str]:
    from flood_model.db import dataset_key, table_name_for

    return {
        "database": "data_flood",
        "dataset_key": dataset_key(path),
        "table": table_name_for(path),
        "path": str(path),
    }


def save_runoff_params(raw: dict[str, Any]) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Can JSON {values: {area_km2, a0, ...}}.")
    basin, params = load_runoff_params()
    merged = _tank_mod()["params_to_dict"](basin, params)
    for key, (lo, hi) in RR_PARAM_BOUNDS.items():
        if key not in raw:
            continue
        try:
            val = float(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"Gia tri '{key}' khong hop le.")
        if not math.isfinite(val) or val < lo or val > hi:
            raise ValueError(f"{key} phai trong khoang {lo:g}–{hi:g}.")
        merged[key] = val
    basin, params = _tank_mod()["apply_params_dict"](merged)
    saved = _tank_mod()["params_to_dict"](basin, params)
    write_param_table(
        TANK_PARAMS_CSV,
        saved,
        ["area_km2", "dt_hours", *_tank_mod()["TANK_PARAM_NAMES"]],
    )
    return saved


def load_nam_params() -> tuple[Any, Any]:
    mod = _nam_mod()
    if _csv_ready(NAM_PARAMS_CSV):
        return mod["load_params_csv"](NAM_PARAMS_CSV)
    return mod["Basin"](), mod["NamParams"]()


def nam_hydrograph_payload() -> dict[str, Any]:
    obs_csv = TANK_FLOW_CSV if csv_available(TANK_FLOW_CSV) else TANK_RAIN_CSV
    h_obs, q_obs = _load_xy_csv(
        obs_csv,
        ("hour", "t", "time"),
        ("q_m3/s", "q_m3s", "q_obs_m3s", "q_obs"),
    )
    h_sim, q_sim = _load_xy_csv(
        NAM_RESULT_CSV,
        ("hour", "t", "time"),
        ("q_m3s", "q_m3/s", "q_total_m3s"),
    )
    rain_csv = NAM_RESULT_CSV if csv_available(NAM_RESULT_CSV) else TANK_RAIN_CSV
    h_rain, rain = _load_xy_csv(
        rain_csv,
        ("hour", "t", "time"),
        ("rainfall_mm", "rain_mm", "p_mm"),
    )
    if not rain and csv_available(TANK_RAIN_CSV) and rain_csv != TANK_RAIN_CSV:
        h_rain, rain = _load_xy_csv(
            TANK_RAIN_CSV,
            ("hour", "t", "time"),
            ("rainfall_mm", "rain_mm", "p_mm"),
        )
    o_pair, s_pair = _pair_obs_sim(h_obs, q_obs, h_sim, q_sim)
    nse = nash_sutcliffe(o_pair, s_pair)
    ts_map = _timeseries_by_hour()
    return {
        "hours_obs": _round_series(h_obs, 3),
        "ts_obs": _ts_series(h_obs, ts_map),
        "q_obs_m3s": _round_series(q_obs, 4),
        "hours_sim": _round_series(h_sim, 3),
        "ts_sim": _ts_series(h_sim, ts_map),
        "q_sim_m3s": _round_series(q_sim, 4),
        "hours_rain": _round_series(h_rain, 3),
        "ts_rain": _ts_series(h_rain, ts_map),
        "rainfall_mm": _round_series(rain, 4),
        "nse": None if nse is None else round(nse, 4),
        "nse_n": int(o_pair.size),
        "obs_path": str(obs_csv) if q_obs else None,
        "sim_path": str(NAM_RESULT_CSV) if q_sim else None,
        "rain_path": str(rain_csv) if rain else None,
    }


def nam_params_payload() -> dict[str, Any]:
    basin, params = load_nam_params()
    values = _nam_mod()["params_to_dict"](basin, params)
    return {
        "ok": True,
        "path": str(NAM_PARAMS_CSV),
        "values": {k: round(float(v), 6) if isinstance(v, (int, float)) else v for k, v in values.items()},
        "groups": NAM_PARAM_GROUPS,
        "hydrograph": nam_hydrograph_payload(),
    }


def save_nam_params(raw: dict[str, Any]) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Can JSON {values: {area_km2, umax, ...}}.")
    basin, params = load_nam_params()
    merged = _nam_mod()["params_to_dict"](basin, params)
    for key, (lo, hi) in NAM_PARAM_BOUNDS.items():
        if key not in raw:
            continue
        try:
            val = float(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"Gia tri '{key}' khong hop le.")
        if not math.isfinite(val) or val < lo or val > hi:
            raise ValueError(f"{key} phai trong khoang {lo:g}–{hi:g}.")
        merged[key] = val
    basin, params = _nam_mod()["apply_params_dict"](merged)
    saved = _nam_mod()["params_to_dict"](basin, params)
    write_param_table(
        NAM_PARAMS_CSV,
        saved,
        ["area_km2", "dt_hours", *_nam_mod()["NAM_PARAM_NAMES"]],
    )
    return saved


# Dieu kien bien mua/PET/Q/H: flood_model/boundary.py


def open_path_in_os(path: Path) -> bool:
    resolved = path.resolve()
    if not resolved.is_file():
        return False
    if sys.platform.startswith("win"):
        os.startfile(str(resolved))  # type: ignore[attr-defined]
        return True
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(resolved)], close_fds=True)
    return True


def _runoff_from_arrays(
    hours: np.ndarray,
    rainfall_mm: np.ndarray,
    q_s: np.ndarray,
    q_m3s: np.ndarray,
    area_km2: float,
    dt_hours: float,
) -> dict[str, Any]:
    peak = float(np.nanmax(q_s)) if q_s.size else 0.0
    scale = DEFAULT_TARGET_PEAK_DEPTH_M / max(peak / 1000.0, 1e-9)
    h_display = (q_s / 1000.0) * scale
    return {
        "hours": np.asarray(hours, dtype=float),
        "rainfall_mm": np.asarray(rainfall_mm, dtype=float),
        "q_surface_mm": q_s,
        "q_m3s": np.asarray(q_m3s, dtype=float),
        "h_physical_m": q_s / 1000.0,
        "h_display_m": h_display,
        "depth_scale": float(scale),
        "target_peak_depth_m": DEFAULT_TARGET_PEAK_DEPTH_M,
        "area_km2": float(area_km2),
        "dt_hours": float(dt_hours),
    }


def _load_tank_result_csv(path: Path) -> dict[str, Any] | None:
    if not csv_available(path):
        return None
    hours: list[float] = []
    rain: list[float] = []
    qs: list[float] = []
    qm: list[float] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return None
        fields = {n.strip().lower(): n for n in reader.fieldnames}
        hcol = fields.get("hour")
        rcol = fields.get("rainfall_mm")
        scol = fields.get("q_surface_mm")
        qcol = fields.get("q_m3s")
        if not all((hcol, rcol, scol, qcol)):
            return None
        for row in reader:
            try:
                hours.append(float(row[hcol]))
                rain.append(float(row[rcol]))
                qs.append(float(row[scol]))
                qm.append(float(row[qcol]))
            except (TypeError, ValueError, KeyError):
                continue
    if not hours:
        return None
    hours_a = np.asarray(hours, dtype=float)
    dt = float(hours_a[1] - hours_a[0]) if hours_a.size > 1 else 1.0
    area = 3250.0
    try:
        basin, _params = load_runoff_params()
        area = float(basin.area_km2)
        if float(basin.dt_hours) > 0:
            dt = float(basin.dt_hours)
    except Exception:
        traceback.print_exc()
    return _runoff_from_arrays(
        hours_a,
        np.asarray(rain, dtype=float),
        np.asarray(qs, dtype=float),
        np.asarray(qm, dtype=float),
        area,
        dt,
    )


@lru_cache(maxsize=1)
def _demo_runoff() -> dict[str, Any]:
    loaded = _load_tank_result_csv(TANK_RESULT_CSV)
    if loaded:
        return loaded
    mod = _tank_mod()
    basin, params = load_runoff_params()
    rain, pet = mod["demo_series"](n_hours=168, dt_hours=float(basin.dt_hours))
    result = mod["TankModel"](params=params, basin=basin).run(rain, pet)
    q_s = np.asarray(result.q_surface_mm, dtype=float)
    return _runoff_from_arrays(
        np.asarray(result.hours, dtype=float),
        np.asarray(result.rainfall_mm, dtype=float),
        q_s,
        np.asarray(result.q_m3s, dtype=float),
        float(result.basin.area_km2),
        float(result.basin.dt_hours),
    )


def resolve_dem_path(file_id: str | None = None, dem_path: str | None = None) -> Path:
    raw = (dem_path or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        else:
            p = p.resolve()
        try:
            p.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError("DEM phai nam trong thu muc flood_model.") from exc
        if not p.is_file():
            raise FileNotFoundError(f"Khong tim thay DEM: {p}")
        return p

    fid = (file_id or "").strip()
    if fid:
        candidate = (ROOT / fid).resolve() if not Path(fid).is_absolute() else Path(fid).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            return candidate

    if DEFAULT_DEM.is_file():
        return DEFAULT_DEM
    raise FileNotFoundError(f"Khong tim thay DEM mac dinh: {DEFAULT_DEM}")


def _open_dem(path: Path):
    import rasterio

    return rasterio.open(path)


def _is_web_mercator(src) -> bool:
    crs = src.crs
    if crs is None:
        return False
    s = str(crs).upper()
    if "3857" in s or "900913" in s or "PSEUDO-MERCATOR" in s or "WEB MERCATOR" in s:
        return True
    b = src.bounds
    return max(abs(b.left), abs(b.right), abs(b.bottom), abs(b.top)) > 180


WEB_MERCATOR_MAX = 20037508.342789244


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
        xy = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lons, lats)]
        return np.array([p[0] for p in xy]), np.array([p[1] for p in xy])
    try:
        transformed = cast(
            tuple[list[float], list[float]],
            rio_transform("EPSG:4326", src.crs, lons.tolist(), lats.tolist()),
        )
        xs, ys = transformed[0], transformed[1]
        return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    except Exception:
        xy = [lonlat_to_mercator(float(lo), float(la)) for lo, la in zip(lons, lats)]
        return np.array([p[0] for p in xy]), np.array([p[1] for p in xy])


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




def _json_num_list(a: np.ndarray | Sequence[float], nd: int = 4) -> list[float | None]:
    out: list[float | None] = []
    for v in np.asarray(a, dtype=float).tolist():
        if v is None or not math.isfinite(float(v)):
            out.append(None)
        else:
            out.append(round(float(v), nd))
    return out


def _tributary_sv_routes(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    try:
        from flood_model.routing import tributary_routes

        recs = list(tributary_routes(parse_hydro1d_source(water_source)))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for rec in recs:
        h = np.asarray(rec.get("h"), dtype=float)
        if h.ndim != 2 or h.shape[1] < 2:
            continue
        out.append(
            {
                "id": normalize_reach_id(rec.get("id") or "trib_1", "trib_1"),
                "station_m": np.asarray(rec["station_m"], dtype=float),
                "lon": np.asarray(rec["lon"], dtype=float),
                "lat": np.asarray(rec["lat"], dtype=float),
                "z_bed": np.asarray(rec["z_bed"], dtype=float),
                "h": h,
                "q": None,
            }
        )
    return out


def _extra_main_sv_routes(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        recs = list(extra_main_routes(parse_hydro1d_source(water_source)))
    except Exception:
        return []
    for rec in recs:
        h = np.asarray(rec.get("h"), dtype=float)
        if h.ndim != 2 or h.shape[1] < 2:
            continue
        item = dict(rec)
        item["id"] = normalize_reach_id(rec.get("id"), "main_2")
        item["h"] = h
        item["q"] = rec.get("q")
        out.append(item)
    return out


def _saint_venant_network_routes(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    try:
        return network_sv_routes(water_source)
    except Exception:
        kind = parse_hydro1d_source(water_source)
        main = saint_venant_route(kind)
        return [dict(main, id="main")]


def _overlay_item_from_track(
    src,
    tr: dict[str, Any],
    rec: dict[str, Any] | None,
    hours: np.ndarray | None,
    rid: str,
) -> dict[str, Any] | None:
    lon = [float(v) for v in tr["lon"]]
    lat = [float(v) for v in tr["lat"]]
    if len(lon) < 2:
        return None
    prof = profile_from_lonlat(src, lon, lat)
    wse_grid = None
    if rec is not None and hours is not None and int(rec["h"].shape[0]) == int(hours.size):
        route = dict(rec, hours=hours)
        pack = attach_route_wse(prof, route, horizontal=False)
        if pack is not None:
            wse_grid, _meta = pack
    length_m = float(prof["distance_m"][-1]) if prof["distance_m"].size else 0.0
    banks = floodplain_banks(src, prof["lon"], prof["lat"])
    item: dict[str, Any] = {
        "id": rid,
        "lon": _json_num_list(prof["lon"], 6),
        "lat": _json_num_list(prof["lat"], 6),
        "z_dem": _json_num_list(prof["z_dem"]),
        "length_m": round(length_m, 2) if math.isfinite(length_m) else 0.0,
        "wse_by_time": None,
        "bank_left_lon": _json_num_list(banks["left_lon"], 6),
        "bank_left_lat": _json_num_list(banks["left_lat"], 6),
        "bank_left_z": _json_num_list(banks["left_z"]),
        "bank_right_lon": _json_num_list(banks["right_lon"], 6),
        "bank_right_lat": _json_num_list(banks["right_lat"], 6),
        "bank_right_z": _json_num_list(banks["right_z"]),
    }
    if wse_grid is not None:
        item["wse_by_time"] = [_json_num_list(wse_grid[t]) for t in range(int(wse_grid.shape[0]))]
    return item


def _branch_overlay_payloads(src, water_source: str = "saint-venant") -> list[dict[str, Any]]:
    kind = parse_hydro1d_source(water_source)
    tracks = tributary_centerlines(src, kind)
    extra_tracks = extra_main_centerlines(kind)
    trib_sv = _tributary_sv_routes(kind)
    extra_sv = _extra_main_sv_routes(kind)
    hours = None
    try:
        hours = np.asarray(saint_venant_route(kind)["hours"], dtype=float)
    except Exception:
        hours = None
    out: list[dict[str, Any]] = []
    used_trib: set[str] = set()
    for i, tr in enumerate(tracks or []):
        rid = normalize_reach_id(tr.get("id") or f"trib_{i + 1}", "trib_1")
        rec = pick_route_by_id(trib_sv, rid, "trib_1")
        item = _overlay_item_from_track(src, tr, rec, hours, rid)
        if item is None:
            continue
        out.append(item)
        used_trib.add(rid)
    for rec in trib_sv:
        rid = normalize_reach_id(rec.get("id"), "trib_1")
        if rid in used_trib:
            continue
        item = _overlay_item_from_track(
            src,
            {"lon": rec["lon"], "lat": rec["lat"]},
            rec,
            hours,
            rid,
        )
        if item is not None:
            out.append(item)
            used_trib.add(rid)
    used_extra: set[str] = set()
    for tr in extra_tracks or []:
        rid = normalize_reach_id(tr.get("id"), "main_2")
        rec = pick_route_by_id(extra_sv, rid, "main_2")
        item = _overlay_item_from_track(src, tr, rec, hours, rid)
        if item is None:
            continue
        out.append(item)
        used_extra.add(rid)
    for rec in extra_sv:
        rid = normalize_reach_id(rec.get("id"), "main_2")
        if rid in used_extra:
            continue
        item = _overlay_item_from_track(
            src,
            {"lon": rec["lon"], "lat": rec["lat"]},
            rec,
            hours,
            rid,
        )
        if item is not None:
            out.append(item)
            used_extra.add(rid)
    return out


@lru_cache(maxsize=1)
def _saint_venant_mod() -> dict[str, Any]:
    path = SCRIPT_DIR / "saint-venant.py"
    if not path.is_file():
        raise FileNotFoundError("Khong tim thay saint-venant.py")
    return runpy.run_path(str(path), run_name="saint_venant")


def _xs_value_cols(fields: Sequence[str], kind: str) -> list[tuple[int, str]]:
    cols: list[tuple[int, str]] = []
    for name in fields:
        m = re.match(rf"^{kind}_xs(\d+)_", name or "")
        if m:
            cols.append((int(m.group(1)), name))
    cols.sort(key=lambda x: x[0])
    return cols


def _row_xs_vals(row: dict[str, str], cols: Sequence[tuple[int, str]], n_x: int) -> list[float]:
    vals: list[float] = []
    for _, col in list(cols)[:n_x]:
        try:
            vals.append(float(row[col]))
        except (KeyError, TypeError, ValueError):
            vals.append(float("nan"))
    return vals


def _load_route_csv(geom_csv: Path, h_csv: Path) -> dict[str, Any] | None:
    if not csv_available(geom_csv) or not csv_available(h_csv):
        return None
    stations: list[float] = []
    lons: list[float] = []
    lats: list[float] = []
    z_bed: list[float] = []
    with csv_open(geom_csv) as f:
        for row in csv.DictReader(f):
            try:
                stations.append(float(row["station_km"]) * 1000.0)
                lons.append(float(row["lon"]))
                lats.append(float(row["lat"]))
                z_bed.append(float(row["z_bed_m"]))
            except (KeyError, TypeError, ValueError):
                continue
    if len(stations) < 2:
        return None
    hours: list[float] = []
    h_rows: list[list[float]] = []
    q_rows: list[list[float]] = []
    have_q = False
    with csv_open(h_csv) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        h_cols = _xs_value_cols(fields, "h")
        q_cols = _xs_value_cols(fields, "q")
        if len(h_cols) < 2:
            return None
        n_x = min(len(h_cols), len(stations))
        q_by_id = {xs_id: name for xs_id, name in q_cols}
        q_aligned = [(xs_id, q_by_id[xs_id]) for xs_id, _ in h_cols[:n_x] if xs_id in q_by_id]
        have_q = len(q_aligned) == n_x
        for row in reader:
            try:
                hours.append(float(row["hour"]))
            except (KeyError, TypeError, ValueError):
                continue
            h_rows.append(_row_xs_vals(row, h_cols, n_x))
            if have_q:
                q_rows.append(_row_xs_vals(row, q_aligned, n_x))
    if not hours:
        return None
    h = np.asarray(h_rows, dtype=float)
    n_x = h.shape[1]
    out = {
        "hours": np.asarray(hours, dtype=float),
        "station_m": np.asarray(stations[:n_x], dtype=float),
        "lon": np.asarray(lons[:n_x], dtype=float),
        "lat": np.asarray(lats[:n_x], dtype=float),
        "z_bed": np.asarray(z_bed[:n_x], dtype=float),
        "h": h,
        "q": np.asarray(q_rows, dtype=float) if have_q else None,
        "source": "csv",
    }
    return out


def _route_from_result(res: Any, source: str) -> dict[str, Any]:
    q = getattr(res, "q", None)
    return {
        "hours": np.asarray(res.hours, dtype=float),
        "station_m": np.asarray(res.geom.distance_m, dtype=float),
        "lon": np.asarray(res.geom.lon, dtype=float),
        "lat": np.asarray(res.geom.lat, dtype=float),
        "z_bed": np.asarray(res.geom.z_bed, dtype=float),
        "h": np.asarray(res.h, dtype=float),
        "q": np.asarray(q, dtype=float) if q is not None else None,
        "source": source,
    }


def _run_saint_venant_model() -> dict[str, Any]:
    mod = _saint_venant_mod()
    args = argparse.Namespace(
        dem=mod["DEFAULT_DEM"],
        inflow=mod.get("DEFAULT_INFLOW_CSV", mod["DEFAULT_TANK_CSV"]),
        h_csv=mod["DEFAULT_H_CSV"],
        h_down=None,
        manning_n=0.030,
        n_csv=mod.get("DEFAULT_N_CSV"),
        q0=None,
        h0=None,
        ic_csv=None,
        xs_spacing=mod["XS_SPACING_M"],
        xs_half=1500.0,
        dt=1.0,
        cfl=0.45,
        dt_hydro_max_s=60.0,
        allow_reverse=False,
        out_dir=mod["DEFAULT_OUT_DIR"],
        no_plot=True,
    )
    return _route_from_result(mod["run_model"](args), "model")


@lru_cache(maxsize=4)
def saint_venant_route(water_source: str = "saint-venant") -> dict[str, Any]:
    src = parse_hydro1d_source(water_source)
    paths = _hydro1d_files(src)
    loaded = _load_route_csv(paths["geom"], paths["h"])
    if loaded is not None:
        loaded = dict(loaded)
        loaded["water_source"] = src
        return loaded
    if src == "saint-venant-1d":
        raise FileNotFoundError(
            "Thieu mike_hd_output/mike_hd_result.csv. Chay Model 1D Saint-venant-1D truoc."
        )
    return _run_saint_venant_model()


def _xs_col_series(grid: Any, col: int) -> list[float | None]:
    if grid is None:
        return []
    arr = np.asarray(grid, dtype=float)
    if arr.ndim != 2 or col < 0 or col >= arr.shape[1]:
        return []
    out: list[float | None] = []
    for v in arr[:, col]:
        fv = float(v)
        out.append(round(fv, 4) if math.isfinite(fv) else None)
    return out


def xs_hydrograph_payload(
    xs_id: Any = None, water_source: Any = None, reach_id: Any = None
) -> dict[str, Any]:
    src = parse_hydro1d_source(water_source)
    try:
        routes = network_sv_routes(src)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "Chưa có kết quả Model 1D. Bấm Hiệu chỉnh để chạy Saint-venant-1D trước."
        ) from exc
    if not routes:
        raise FileNotFoundError("Kết quả Model 1D không có mặt cắt.")
    hours_main = np.asarray(routes[0].get("hours"), dtype=float)
    reaches: list[dict[str, Any]] = []
    for rec in routes:
        rid = normalize_reach_id(rec.get("id") or rec.get("reach_id"), "main")
        h_arr = np.asarray(rec.get("h"), dtype=float)
        n_x = int(h_arr.shape[1]) if h_arr.ndim == 2 else 0
        if n_x < 1:
            continue
        reaches.append(
            {
                "id": rid,
                "kind": _reach_kind(rid),
                "label": _reach_label(rid),
                "n_sections": n_x,
            }
        )
    if not reaches:
        raise FileNotFoundError("Kết quả Model 1D không có mặt cắt.")
    want = normalize_reach_id(reach_id, "main") if str(reach_id or "").strip() else "main"
    rec = pick_route_by_id(routes, want, "main")
    if rec is None:
        rec = routes[0]
    rid = normalize_reach_id(rec.get("id") or rec.get("reach_id"), "main")
    hours = np.asarray(rec.get("hours") if rec.get("hours") is not None else hours_main, dtype=float)
    station = np.asarray(rec["station_m"], dtype=float)
    z_bed = np.asarray(rec["z_bed"], dtype=float) if rec.get("z_bed") is not None else np.array([])
    h = np.asarray(rec["h"], dtype=float)
    q = rec.get("q")
    q = np.asarray(q, dtype=float) if q is not None else None
    if q is not None and (q.ndim != 2 or q.shape[1] < 1 or not np.isfinite(q).any()):
        q = None
    n_x = int(h.shape[1]) if h.ndim == 2 else 0
    if n_x < 1:
        raise FileNotFoundError("Kết quả Model 1D không có mặt cắt trên sông này.")
    sections: list[dict[str, Any]] = []
    for i in range(n_x):
        km = round(float(station[i]) / 1000.0, 3) if i < station.size else None
        zb = None
        if i < z_bed.size:
            zv = float(z_bed[i])
            zb = round(zv, 3) if math.isfinite(zv) else None
        sections.append({"xs_id": i + 1, "station_km": km, "z_bed_m": zb, "reach": rid})
    try:
        k = int(float(xs_id)) if xs_id is not None and str(xs_id).strip() != "" else 1
    except (TypeError, ValueError):
        k = 1
    k = max(1, min(k, n_x))
    i = k - 1
    ts_map = _timeseries_by_hour()
    return {
        "ok": True,
        "water_source": src,
        "water_source_label": hydro1d_label(src),
        "reach": rid,
        "reach_kind": _reach_kind(rid),
        "reach_label": _reach_label(rid),
        "reaches": reaches,
        "xs_id": k,
        "station_km": sections[i]["station_km"],
        "z_bed_m": sections[i]["z_bed_m"],
        "hours_sim": _round_series([float(v) for v in hours], 3),
        "ts_sim": _ts_series(hours, ts_map),
        "q_m3s": _xs_col_series(q, i),
        "h_m": _xs_col_series(h, i),
        "sections": sections,
        "n_sections": n_x,
        "has_q": q is not None,
    }


def _parse_obs_cell(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


_OBS_TIME_KEYS = {"gio", "giờ", "hour", "t", "time", "timeseries", "ts"}


def _obs_norm_name(name: Any) -> str:
    return str(name or "").strip().lower().replace(" ", "")


def _obs_col_kind(name: str) -> str:
    key = _obs_norm_name(name)
    if (
        key.startswith("q")
        or "q_td" in key
        or "qthucdo" in key
        or "m3/s" in key
        or "m³/s" in key
        or "m3s" in key
    ):
        return "q"
    return "h"


def _obs_value_names(fieldnames: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for raw in fieldnames or []:
        name = str(raw or "").strip()
        if not name or _obs_norm_name(name) in _OBS_TIME_KEYS:
            continue
        out.append(name)
    return out


def _match_obs_column(names: Sequence[str], wanted: Any) -> str | None:
    if not names:
        return None
    raw = str(wanted or "").strip()
    if not raw:
        return names[0]
    if raw in names:
        return raw
    key = _obs_norm_name(raw)
    for name in names:
        if _obs_norm_name(name) == key:
            return name
    try:
        idx = int(raw)
        if 1 <= idx <= len(names):
            return names[idx - 1]
    except (TypeError, ValueError):
        pass
    return None


def _round_obs_series(vals: Sequence[float | None], ndigits: int = 4) -> list[float | None]:
    out: list[float | None] = []
    for v in vals:
        if v is None:
            out.append(None)
        else:
            fv = float(v)
            out.append(round(fv, ndigits) if math.isfinite(fv) else None)
    return out


def resolve_xs_obs_csv() -> Path | None:
    for path in (
        MIKE_HD_OBS_CSV,
        SAINT_VENANT_OBS_CSV,
        ROOT / "saint_venant_output" / "mike_hd_thucdo.csv",
        ROOT / "mike_hd_output" / "thucdo.csv",
    ):
        if csv_available(path):
            return path
    return None


def _decode_obs_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1258", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_obs_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    sample = str(text or "").lstrip("\ufeff")
    if not sample.strip():
        raise ValueError("File thực đo trống.")
    first = next((ln for ln in sample.splitlines() if ln.strip()), "")
    n_semi, n_comma, n_tab = first.count(";"), first.count(","), first.count("\t")
    if n_tab > n_comma and n_tab > n_semi:
        delim = "\t"
    elif n_semi > n_comma:
        delim = ";"
    else:
        delim = ","
    reader = csv.DictReader(io.StringIO(sample), delimiter=delim)
    if not reader.fieldnames:
        raise ValueError("File CSV không có dòng tiêu đề.")
    names = [str(n or "").strip() for n in reader.fieldnames if str(n or "").strip()]
    if not names:
        raise ValueError("File CSV không có tên cột.")
    fields = _csv_field_map(names)
    hour_col = _pick_csv_col(fields, "gio", "giờ", "hour", "t")
    time_col = _pick_csv_col(fields, "time", "timeseries", "ts")
    if hour_col is None and time_col is None:
        raise ValueError("File thực đo cần cột Gio hoặc Time.")
    if not _obs_value_names(names):
        raise ValueError("File thực đo cần cột Gio/Time và ít nhất một cột giá trị (Q hoặc H).")
    value_names = _obs_value_names(names)
    rows: list[dict[str, str]] = []
    for row in reader:
        rec: dict[str, str] = {}
        for raw_name in reader.fieldnames:
            key = str(raw_name or "").strip()
            if not key:
                continue
            val = row.get(raw_name)
            rec[key] = "" if val is None else str(val).strip()
        if not any(rec.get(name) for name in value_names):
            continue
        rows.append(rec)
    if not rows:
        raise ValueError("File thực đo không có hàng dữ liệu.")
    return names, rows


def save_xs_obs_upload(raw: bytes, filename: str = "") -> dict[str, Any]:
    names, rows = _parse_obs_table(_decode_obs_bytes(raw))
    for dest in (MIKE_HD_OBS_CSV, SAINT_VENANT_OBS_CSV):
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_csv_rows(dest, names, rows, rebuild_hydro=False)
    payload = load_xs_obs_payload()
    payload["uploaded"] = str(filename or MIKE_HD_OBS_CSV.name)
    return payload


def load_xs_obs_payload(column: Any = None, path: Path | None = None) -> dict[str, Any]:
    csv_path = Path(path) if path else resolve_xs_obs_csv()
    if csv_path is None or not csv_available(csv_path):
        raise FileNotFoundError(
            "Chưa có file thực đo. Bấm Load file... rồi chọn CSV "
            "(cột Gio hoặc Time và ít nhất một cột Q hoặc H)."
        )
    hours: list[float] = []
    ts: list[str] = []
    series: dict[str, list[float | None]] = {}
    value_names: list[str] = []
    with csv_open(csv_path) as f:
        reader = csv.DictReader(f)
        fields = _csv_field_map(list(reader.fieldnames) if reader.fieldnames is not None else None)
        hour_col = _pick_csv_col(fields, "gio", "giờ", "hour", "t")
        time_col = _pick_csv_col(fields, "time", "timeseries", "ts")
        value_names = _obs_value_names(reader.fieldnames)
        if not value_names:
            raise ValueError("File thực đo không có cột giá trị (ngoài Gio, Time).")
        series = {name: [] for name in value_names}
        for i, row in enumerate(reader):
            hour = float(i)
            if hour_col is not None and str(row.get(hour_col, "")).strip() != "":
                parsed = _parse_obs_cell(row.get(hour_col))
                if parsed is not None:
                    hour = parsed
            label = str(row.get(time_col) or "").strip() if time_col else ""
            any_val = False
            row_vals: dict[str, float | None] = {}
            for name in value_names:
                val = _parse_obs_cell(row.get(name))
                row_vals[name] = val
                if val is not None:
                    any_val = True
            if not any_val and not label:
                continue
            hours.append(hour)
            ts.append(label)
            for name in value_names:
                series[name].append(row_vals[name])
    if not hours:
        raise ValueError("File thực đo không có hàng giá trị hợp lệ.")
    selected = _match_obs_column(value_names, column)
    if selected is None:
        raise ValueError(f"Không tìm thấy cột thực đo: {column}")
    columns: list[dict[str, Any]] = []
    for name in value_names:
        kind = _obs_col_kind(name)
        values = _round_obs_series(series[name])
        columns.append({
            "name": name,
            "kind": kind,
            "values": values,
            "n": sum(1 for v in values if v is not None),
        })
    chosen = next(c for c in columns if c["name"] == selected)
    kind = chosen["kind"]
    values = chosen["values"]
    return {
        "ok": True,
        "path": str(csv_path),
        "hours": _round_series(hours, 3),
        "ts": ts,
        "columns": columns,
        "column": selected,
        "kind": kind,
        "q_m3s": values if kind == "q" else [],
        "h_m": values if kind == "h" else [],
        "has_q": kind == "q",
        "has_h": kind == "h",
        "n": len(hours),
    }


def parse_water_source(raw: Any) -> str:
    key = str(raw or "saint-venant").strip().lower().replace(" ", "-").replace("_", "-")
    if key in ("mc", "muskingum", "muskingum-cunge"):
        return "saint-venant"
    return WATER_SOURCE_ALIASES.get(key, "saint-venant")


def parse_hydro1d_source(raw: Any) -> str:
    if HYDRO1D_FORCE_MIKE:
        return "saint-venant-1d"
    src = parse_water_source(raw)
    if src == "saint-venant-1d":
        return "saint-venant-1d"
    return "saint-venant"


def _hydro1d_files(src: Any = None) -> dict[str, Path]:
    raw = hydro1d_csv_paths(parse_hydro1d_source(src))
    return {key: Path(val) for key, val in raw.items() if key not in ("kind",)}


def _river_xy_m(mus: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for lo, la in zip(mus["lon"], mus["lat"]):
        x, y = lonlat_to_mercator(float(lo), float(la))
        xs.append(x)
        ys.append(y)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def project_to_river_s(lon: float, lat: float, mus: dict[str, Any]) -> tuple[float, float]:
    """Chieu diem len truc long song 1D. Tra ve (station_m, khoang_cach_m)."""
    px, py = lonlat_to_mercator(float(lon), float(lat))
    xs, ys = _river_xy_m(mus)
    s = np.asarray(mus["station_m"], dtype=float)
    best_d2 = float("inf")
    best_s = float(s[0])
    for i in range(int(xs.size) - 1):
        x0, y0, x1, y1 = float(xs[i]), float(ys[i]), float(xs[i + 1]), float(ys[i + 1])
        dx, dy = x1 - x0, y1 - y0
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 < 1e-6 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / l2))
        qx, qy = x0 + t * dx, y0 + t * dy
        d2 = (px - qx) ** 2 + (py - qy) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best_s = float(s[i] + t * (s[i + 1] - s[i]))
    return best_s, math.sqrt(best_d2)


def _closest_s_on_route(profile: dict[str, np.ndarray], route: dict[str, Any]) -> tuple[float, float] | None:
    lon = np.asarray(profile["lon"], dtype=float)
    lat = np.asarray(profile["lat"], dtype=float)
    n = int(lon.size)
    if n < 1:
        return None
    # Mat cat ngang: chi dung diem gan offset=0 (tim long), tranh dau bo chieu len tram khac.
    idxs: list[int]
    dist = profile.get("distance_m")
    if dist is not None:
        d = np.asarray(dist, dtype=float)
        if d.size == n and np.isfinite(d).any():
            idxs = [int(np.nanargmin(np.abs(d)))]
        else:
            idxs = [n // 2]
    else:
        idxs = list(range(n))
    best_s, best_d = None, float("inf")
    for i in idxs:
        lo, la = float(lon[i]), float(lat[i])
        if not (math.isfinite(lo) and math.isfinite(la)):
            continue
        si, di = project_to_river_s(lo, la, route)
        if di < best_d:
            best_d = di
            best_s = si
    if best_s is None:
        return None
    return float(best_s), float(best_d)




def _route_normal_dem_xy(src, route: dict[str, Any], index: int) -> tuple[float, float, float, float]:
    lon = np.asarray(route["lon"], dtype=float)
    lat = np.asarray(route["lat"], dtype=float)
    xs, ys = lonlat_to_dem_xy(src, lon, lat)
    n = int(xs.size)
    i = max(0, min(int(index), max(n - 1, 0)))
    if n < 2:
        return 0.0, 1.0, float(xs[i]) if n else 0.0, float(ys[i]) if n else 0.0
    if i <= 0:
        tx, ty = float(xs[1] - xs[0]), float(ys[1] - ys[0])
    elif i >= n - 1:
        tx, ty = float(xs[-1] - xs[-2]), float(ys[-1] - ys[-2])
    else:
        tx, ty = float(xs[i + 1] - xs[i - 1]), float(ys[i + 1] - ys[i - 1])
    length = math.hypot(tx, ty) or 1.0
    return -ty / length, tx / length, float(xs[i]), float(ys[i])


def official_xs_profile(
    src, route: dict[str, Any], xs_id: int, water_source: str = "saint-venant"
) -> dict[str, np.ndarray] | None:
    table = load_official_xs(parse_hydro1d_source(water_source))
    rec = table.get(int(xs_id))
    if rec is None:
        return None
    off = np.asarray(rec["offset_m"], dtype=float)
    z = np.asarray(rec["z_m"], dtype=float)
    order = np.argsort(off)
    off, z = off[order], z[order]
    st = np.asarray(route["station_m"], dtype=float)
    st_csv = rec["station_km"]
    s_m = float(st_csv[0]) * 1000.0 if st_csv.size and math.isfinite(float(st_csv[0])) else float(st[int(xs_id) - 1])
    index = int(np.argmin(np.abs(st - s_m)))
    lon_c = np.asarray(rec["lon"], dtype=float)[order]
    lat_c = np.asarray(rec["lat"], dtype=float)[order]
    n_unique = len({
        (round(float(a), 5), round(float(b), 5))
        for a, b in zip(lon_c, lat_c)
        if math.isfinite(float(a)) and math.isfinite(float(b))
    })
    if n_unique >= 3:
        lon, lat = lon_c, lat_c
        xs, ys = lonlat_to_dem_xy(src, lon, lat)
    else:
        nx, ny, x0, y0 = _route_normal_dem_xy(src, route, index)
        xs = x0 + nx * off
        ys = y0 + ny * off
        lon, lat = dem_xy_to_lonlat(src, xs, ys)
    zmin = float(np.nanmin(z)) if z.size else 0.0
    return {
        "x": np.asarray(xs, dtype=float),
        "y": np.asarray(ys, dtype=float),
        "lon": np.asarray(lon, dtype=float),
        "lat": np.asarray(lat, dtype=float),
        "distance_m": off,
        "z_dem": z,
        "z_bed": np.full(z.shape, zmin if math.isfinite(zmin) else 0.0, dtype=float),
        "official_xs": np.array([1.0]),
        "xs_id": np.array([float(xs_id)]),
    }


def snap_profile_to_official_xs(
    src, profile: dict[str, np.ndarray], water_source: str = "saint-venant"
) -> dict[str, np.ndarray]:
    """Cat ve tay -> mat cat 1D gan nhat (offset, z) de H nam dung nhu PNG Mat cat."""
    kind = parse_hydro1d_source(water_source)
    try:
        routes = _saint_venant_network_routes(kind)
    except Exception:
        return profile
    best: tuple[float, float, float, dict[str, Any]] | None = None
    for route in routes:
        pack = _closest_s_on_route(profile, route)
        if pack is None:
            continue
        s_m, dist = pack
        rid = str(route.get("id") or "main")
        score = dist + (0.0 if rid == "main" else TRIB_XS_PENALTY_M)
        if best is None or score < best[0]:
            best = (score, dist, s_m, route)
    if best is None or best[1] > XS_SNAP_RIVER_MAX_M:
        return profile
    _score, _dist, s_m, route = best
    if str(route.get("id") or "main") != "main":
        return profile
    st = np.asarray(route["station_m"], dtype=float)
    k = int(np.argmin(np.abs(st - float(s_m))))
    official = official_xs_profile(src, route, k + 1, kind)
    return official if official is not None else profile


def _lookup_manning_row(xs_id: int, reach: str) -> dict[str, Any] | None:
    rid = _reach_key(reach or "main")
    rows, _src = load_manning_n_rows()
    hit = None
    for row in rows:
        try:
            xid = int(row["xs_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if xid != int(xs_id):
            continue
        r = _reach_key(row.get("reach") or "main")
        if r == rid:
            return row
        if hit is None and _reach_kind(r) == _reach_kind(rid):
            hit = row
    return hit


def _profile_from_route_sample(
    src,
    route: dict[str, Any],
    *,
    xs_id: int,
    station_km: float | None,
    lon_c: float | None,
    lat_c: float | None,
    half_w: float,
) -> dict[str, np.ndarray] | None:
    try:
        st = np.asarray(route["station_m"], dtype=float)
        lon = np.asarray(route["lon"], dtype=float)
        lat = np.asarray(route["lat"], dtype=float)
    except Exception:
        return None
    if st.size < 1 or lon.size < 1:
        return None
    if lon_c is None or lat_c is None or not (math.isfinite(lon_c) and math.isfinite(lat_c)):
        if station_km is not None and math.isfinite(float(station_km)):
            idx = int(np.argmin(np.abs(st - float(station_km) * 1000.0)))
        else:
            idx = max(0, min(int(xs_id) - 1, int(st.size) - 1))
        lon_c = float(lon[idx])
        lat_c = float(lat[idx])
        index = idx
    else:
        index = _route_index_at_station(st, lon, lat, station_km, float(lon_c), float(lat_c))
    nx, ny, x0, y0 = _route_normal_dem_xy(src, route, index)
    step = max(float(getattr(src, "res", (10.0, 10.0))[0]), 5.0)
    off, z = sample_cross_section(src, x0, y0, nx, ny, float(half_w), step)
    xs = x0 + nx * off
    ys = y0 + ny * off
    lon_arr, lat_arr = dem_xy_to_lonlat(src, xs, ys)
    zmin = float(np.nanmin(z)) if z.size else 0.0
    return {
        "x": np.asarray(xs, dtype=float),
        "y": np.asarray(ys, dtype=float),
        "lon": np.asarray(lon_arr, dtype=float),
        "lat": np.asarray(lat_arr, dtype=float),
        "distance_m": np.asarray(off, dtype=float),
        "z_dem": np.asarray(z, dtype=float),
        "z_bed": np.full(z.shape, zmin if math.isfinite(zmin) else 0.0, dtype=float),
        "official_xs": np.array([1.0]),
        "xs_id": np.array([float(xs_id)]),
    }


def xs_station_profile_payload(
    xs_id: Any,
    *,
    reach_id: Any = None,
    water_source: Any = None,
    file_id: Any = None,
    dem_path: Any = None,
) -> dict[str, Any]:
    """Profile mat cat ngang (offset/z) + Manning n khi click XS tren DEM 3D."""
    kind = parse_hydro1d_source(water_source)
    try:
        xid = int(float(xs_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("Can xs_id hop le.") from exc
    if xid < 1:
        raise ValueError("xs_id phai >= 1.")
    rid = normalize_reach_id(reach_id, "main") if str(reach_id or "").strip() else "main"
    manning_row = _lookup_manning_row(xid, rid)
    manning_n = None
    station_km = None
    lon_c = lat_c = None
    if manning_row:
        try:
            raw_manning_n = manning_row.get("manning_n")
            if raw_manning_n is not None:
                manning_n = float(raw_manning_n)
        except (TypeError, ValueError):
            manning_n = None
        try:
            if manning_row.get("station_km") is not None and str(manning_row.get("station_km")).strip() != "":
                station_km = float(manning_row["station_km"])
        except (TypeError, ValueError):
            station_km = None
        try:
            if manning_row.get("x") is not None and manning_row.get("y") is not None:
                lon_c = float(manning_row["x"])
                lat_c = float(manning_row["y"])
        except (TypeError, ValueError):
            lon_c = lat_c = None
        rid = _reach_key(manning_row.get("reach") or rid)

    width_z = _geom_width_z_by_xs(kind)
    wz = width_z.get((rid, xid))
    if wz is None and _reach_kind(rid) == "main":
        wz = width_z.get(("main", xid))
    half_w = 400.0
    if wz is not None and math.isfinite(wz[0]) and wz[0] > 2.0:
        half_w = max(0.5 * float(wz[0]), 40.0)

    dem = resolve_dem_path(file_id, dem_path)
    with _open_dem(dem) as src:
        routes = _saint_venant_network_routes(kind)
        route = pick_route_by_id(routes, rid, "main")
        if route is None and routes:
            route = routes[0]
            rid = normalize_reach_id(route.get("id"), "main")
        if route is None:
            raise FileNotFoundError("Chưa có hình học sông. Chạy cắt mặt cắt / Model 1D trước.")
        prof = None
        if _reach_kind(rid) == "main":
            try:
                prof = official_xs_profile(src, route, xid, kind)
            except Exception:
                prof = None
        if prof is None:
            prof = _profile_from_route_sample(
                src,
                route,
                xs_id=xid,
                station_km=station_km,
                lon_c=lon_c,
                lat_c=lat_c,
                half_w=half_w,
            )
        if prof is None:
            raise FileNotFoundError(f"Không lấy được profile XS{xid} trên {rid}.")
        payload = attach_water(
            prof,
            prefer_cross_section=True,
            water_source=kind,
            force_xs_id=xid,
            force_reach=rid,
        )

    payload["ok"] = True
    payload["source"] = "xs_click"
    payload["xs_id"] = xid
    payload["route_xs"] = xid
    payload["reach"] = rid
    payload["route_reach"] = rid
    payload["reach_label"] = _reach_label(rid)
    payload["station_km"] = (
        round(float(station_km), 4) if station_km is not None and math.isfinite(station_km) else payload.get("station_km")
    )
    payload["manning_n"] = (
        round(float(manning_n), 4) if manning_n is not None and math.isfinite(float(manning_n)) else None
    )
    payload["dem_path"] = str(dem)
    return payload


def route_field_at_s(route: dict[str, Any], field: str, s_vals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """H/Q(t,s): trung tram (node) thi giu nguyen, khong thi noi suy giua 2 mat cat."""
    st = np.asarray(route["station_m"], dtype=float)
    grid = np.asarray(route[field], dtype=float)
    s_vals = np.asarray(s_vals, dtype=float)
    n_t = int(grid.shape[0])
    n_s = int(s_vals.size)
    out = np.empty((n_t, n_s), dtype=float)
    snapped = np.full(n_s, -1, dtype=int)
    spacing = float(np.median(np.diff(st))) if st.size > 1 else 1500.0
    tol = min(XS_MATCH_TOL_M, 0.2 * max(spacing, 1.0))
    for i, s in enumerate(s_vals.tolist()):
        s = float(s)
        k = int(np.argmin(np.abs(st - s)))
        if abs(float(st[k]) - s) <= tol:
            out[:, i] = grid[:, k]
            snapped[i] = k
        else:
            for t in range(n_t):
                out[t, i] = float(np.interp(s, st, grid[t]))
    return out, snapped


def wse_along_stations(
    station_m: Sequence[float],
    route: dict[str, Any] | None = None,
) -> np.ndarray | None:
    """Mat thoang H(t,s) cho khoi nuoc 3D: trung tram XS thi giu H node, khong thi noi suy — giong Mat cat."""
    try:
        sv = route or saint_venant_route()
    except Exception:
        traceback.print_exc()
        return None
    st = np.asarray(station_m, dtype=float)
    if st.size < 1 or "h" not in sv:
        return None
    wse, _snapped = route_field_at_s(sv, "h", st)
    return wse


def attach_route_wse(
    profile: dict[str, np.ndarray],
    route: dict[str, Any],
    *,
    horizontal: bool,
    force_xs_id: int | None = None,
) -> tuple[np.ndarray, dict[str, Any]] | None:
    lon = np.asarray(profile["lon"], dtype=float)
    lat = np.asarray(profile["lat"], dtype=float)
    n_s = int(lon.size)
    if n_s < 1:
        return None
    if horizontal:
        st = np.asarray(route["station_m"], dtype=float)
        n_x = int(st.size)
        if n_x < 1:
            return None
        if force_xs_id is not None:
            k = max(0, min(int(force_xs_id) - 1, n_x - 1))
            best_s = float(st[k])
            best_d = 0.0
        else:
            pack = _closest_s_on_route(profile, route)
            if pack is None:
                return None
            best_s, best_d = pack
            k = int(np.argmin(np.abs(st - float(best_s))))
            best_s = float(st[k])
        h_col = np.asarray(route["h"], dtype=float)[:, k]
        wse = np.repeat(h_col[:, None], n_s, axis=1)
        q_cut = None
        q_grid = route.get("q")
        if q_grid is not None:
            q_cut = np.asarray(q_grid, dtype=float)[:, k]
        meta = {
            "route_s_m": round(float(best_s), 2),
            "route_dist_m": round(float(best_d), 2),
            "route_xs": k + 1,
            "hours": route["hours"],
            "q_cut": q_cut,
        }
        return wse, meta
    s_pts = np.empty(n_s, dtype=float)
    for i in range(n_s):
        if math.isfinite(float(lon[i])) and math.isfinite(float(lat[i])):
            s_pts[i], _ = project_to_river_s(float(lon[i]), float(lat[i]), route)
        else:
            s_pts[i] = np.nan
    good = np.isfinite(s_pts)
    if not good.any():
        return None
    if (~good).any():
        idx = np.arange(n_s)
        s_pts[~good] = np.interp(idx[~good], idx[good], s_pts[good])
    wse, snapped = route_field_at_s(route, "h", s_pts)
    meta = {
        "route_s_m": None,
        "route_dist_m": None,
        "route_xs": None,
        "hours": route["hours"],
        "n_snapped": int(np.sum(snapped >= 0)),
        "route_station_m": s_pts,
    }
    return wse, meta


def attach_saint_venant_wse(
    profile: dict[str, np.ndarray],
    *,
    horizontal: bool,
    water_source: str | None = None,
    force_xs_id: int | None = None,
    force_reach: str | None = None,
) -> tuple[np.ndarray, dict[str, Any]] | None:
    kind = parse_hydro1d_source(water_source)
    try:
        routes = _saint_venant_network_routes(kind)
    except FileNotFoundError:
        if kind == "saint-venant-1d":
            raise
        return None
    except Exception:
        traceback.print_exc()
        return None
    if not routes:
        return None
    if horizontal:
        if force_xs_id is not None:
            want = normalize_reach_id(force_reach, "main") if force_reach else "main"
            route = pick_route_by_id(routes, want, "main") or routes[0]
            pack = attach_route_wse(
                profile, route, horizontal=True, force_xs_id=int(force_xs_id)
            )
            if pack is None:
                return None
            wse, meta = pack
            meta = dict(meta)
            meta["route_id"] = normalize_reach_id(route.get("id"), "main")
            return wse, meta
        best: tuple[float, np.ndarray, dict[str, Any]] | None = None
        for route in routes:
            pack = attach_route_wse(profile, route, horizontal=True)
            if pack is None:
                continue
            wse, meta = pack
            dist = meta.get("route_dist_m")
            if dist is None:
                continue
            rid = str(route.get("id") or "main")
            score = float(dist) + (0.0 if rid == "main" else TRIB_XS_PENALTY_M)
            if best is None or score < best[0]:
                meta = dict(meta)
                meta["route_id"] = rid
                best = (score, wse, meta)
        if best is None:
            return None
        return best[1], best[2]
    lon = np.asarray(profile["lon"], dtype=float)
    lat = np.asarray(profile["lat"], dtype=float)
    n_s = int(lon.size)
    if n_s < 1:
        return None
    main_only = [r for r in routes if str(r.get("id") or "main") == "main"] or list(routes[:1])
    n_t = int(np.asarray(main_only[0]["h"]).shape[0])
    wse = np.full((n_t, n_s), np.nan, dtype=float)
    s_pts = np.full(n_s, np.nan, dtype=float)
    n_snapped = 0
    for i in range(n_s):
        lo, la = float(lon[i]), float(lat[i])
        if not (math.isfinite(lo) and math.isfinite(la)):
            continue
        best_d = float("inf")
        best_route: dict[str, Any] | None = None
        best_s = 0.0
        for route in main_only:
            si, di = project_to_river_s(lo, la, route)
            if di < best_d:
                best_d = di
                best_route = route
                best_s = si
        if best_route is None:
            continue
        h_grid, snapped = route_field_at_s(best_route, "h", np.array([best_s]))
        if int(h_grid.shape[0]) != n_t:
            continue
        wse[:, i] = h_grid[:, 0]
        s_pts[i] = best_s
        if int(snapped[0]) >= 0:
            n_snapped += 1
    if not np.isfinite(s_pts).any():
        return None
    meta = {
        "route_s_m": None,
        "route_dist_m": None,
        "route_xs": None,
        "route_id": None,
        "hours": main_only[0]["hours"],
        "n_snapped": n_snapped,
        "route_station_m": s_pts,
    }
    return wse, meta


def excess_depth_m(h: np.ndarray, time_index: int | None = None) -> np.ndarray | float:
    """Do sau cong them so voi Q0 tai t=0. h0 tuong ung dong chuan, depth(0)=0."""
    h = np.asarray(h, dtype=float)
    if h.size == 0:
        return 0.0 if time_index is not None else h
    h0 = float(h[0]) if np.isfinite(h[0]) else 0.0
    dh = np.maximum(h - h0, 0.0)
    if time_index is None:
        return dh
    t = max(0, min(int(time_index), dh.size - 1))
    return float(dh[t])


def water_surface(
    z_dem: np.ndarray,
    z_bed: np.ndarray,
    depth: float,
    wse0: float | None = None,
) -> np.ndarray:
    """Muc nuoc long chinh.

    Mat cat ngang: H mat = dh + (zmax+zmin)*0.5, duong ngang, chi ngap noi WSE > z_dem.
    Thalweg: bam day, max(z_dem, z_bed + depth).
    """
    z_dem = np.asarray(z_dem, dtype=float)
    extra = float(depth)
    if wse0 is not None:
        return np.full(z_dem.shape, float(wse0) + extra, dtype=float)
    z_bed = np.asarray(z_bed, dtype=float)
    return np.maximum(z_dem, z_bed + extra)


def attach_water(
    profile: dict[str, np.ndarray],
    runoff: dict[str, Any] | None = None,
    *,
    prefer_cross_section: bool | None = None,
    water_source: str | None = None,
    force_xs_id: int | None = None,
    force_reach: str | None = None,
) -> dict[str, Any]:
    runoff = runoff or _demo_runoff()
    z_dem = np.asarray(profile["z_dem"], dtype=float)
    z_bed = np.asarray(profile["z_bed"], dtype=float)
    h = np.asarray(runoff["h_display_m"], dtype=float)
    n_t = int(h.size)
    n_s = int(z_dem.size)
    zmin = float(np.nanmin(z_dem)) if n_s else 0.0
    zmax = float(np.nanmax(z_dem)) if n_s else 0.0
    if not math.isfinite(zmin):
        zmin = 0.0
    if not math.isfinite(zmax):
        zmax = 0.0
    mid = 0.5 * (zmax + zmin)
    horizontal = prefer_cross_section is not False
    wanted = parse_water_source(water_source)
    hydro1d = parse_hydro1d_source(wanted) if wanted != "tank" else None
    route_pack = None
    if hydro1d is not None:
        route_pack = attach_saint_venant_wse(
            profile,
            horizontal=horizontal,
            water_source=hydro1d,
            force_xs_id=force_xs_id,
            force_reach=force_reach,
        )
    runoff_hours = np.asarray(runoff["hours"], dtype=float)
    rain = np.asarray(runoff["rainfall_mm"], dtype=float)
    qs = np.asarray(runoff["q_surface_mm"], dtype=float)
    used_source = "tank"
    route_meta: dict[str, Any] = {}
    if route_pack is not None:
        wse, route_meta = route_pack
        hours = np.asarray(route_meta["hours"], dtype=float)
        n_t = int(wse.shape[0])
        used_source = hydro1d or "saint-venant"
        if rain.size and hours.size:
            rain = np.interp(hours, runoff_hours, rain)
            qs = np.interp(hours, runoff_hours, qs)
        h_wse = wse[:, wse.shape[1] // 2] if wse.size else np.zeros(n_t)
        h = h_wse
        wse_elev0 = float(h_wse[0]) if h_wse.size else mid
        wse0 = wse_elev0
        h0_depth = max(wse_elev0 - zmin, 0.0)
    else:
        hours = runoff_hours
        h = np.asarray(runoff["h_display_m"], dtype=float)
        n_t = int(h.size)
        wse_elev0 = mid if horizontal else None
        wse0 = mid if horizontal else None
        h0_depth = max(mid - zmin, 0.0) if horizontal else 0.0
        dh = np.asarray(excess_depth_m(h), dtype=float)
        if n_t == 0:
            wse = water_surface(z_dem, z_bed, 0.0, wse_elev0)[None, :]
        else:
            wse = np.vstack([water_surface(z_dem, z_bed, float(d), wse_elev0) for d in dh])
        h_wse = wse[:, 0] if wse.size else h
    depth = np.maximum(wse - z_dem[None, :], 0.0)
    wet_frac = np.mean(depth > 0.02, axis=1) if n_s and n_t else np.zeros(max(n_t, 1))

    def _f(a: np.ndarray) -> list[float]:
        out = []
        for v in np.asarray(a, dtype=float).tolist():
            out.append(None if v is None or not math.isfinite(float(v)) else round(float(v), 4))
        return out

    peak_i = int(np.nanargmax(h_wse)) if n_t and np.asarray(h_wse).size else 0
    dist = np.asarray(profile["distance_m"], dtype=float)
    if n_s and np.isfinite(dist).any():
        length_m = float(np.nanmax(dist) - np.nanmin(dist))
    else:
        length_m = 0.0
    if not math.isfinite(length_m):
        length_m = 0.0
    official_xs = bool(profile.get("official_xs") is not None)
    q_m3s = np.asarray(runoff["q_m3s"], dtype=float)
    h_phys = np.asarray(runoff["h_physical_m"], dtype=float)
    if used_source in ("saint-venant", "saint-venant-1d") and hours.size and runoff_hours.size:
        q_m3s = np.interp(hours, runoff_hours, q_m3s)
        h_phys = np.interp(hours, runoff_hours, h_phys)
    q_cut = route_meta.get("q_cut")
    q_cut_arr = np.asarray(q_cut, dtype=float) if q_cut is not None else None
    xs_id = route_meta.get("route_xs")
    s_m = route_meta.get("route_s_m")
    reach_id = route_meta.get("route_id")
    if used_source in ("saint-venant", "saint-venant-1d"):
        if horizontal:
            if xs_id and official_xs:
                hq_note = f"Mat cat XS{xs_id} (offset, z DEM mo hinh) + H, Q dung tai node."
            elif xs_id:
                hq_note = f"Mat cat trung tram XS{xs_id}, giu H va Q tai node."
            else:
                hq_note = f"Noi suy H, Q giua 2 mat cat (s={s_m} m)."
        else:
            hq_note = "Doc long song: H(s) noi suy theo tram XS, trung tram thi giu nguyen."
        if reach_id and reach_id != "main":
            from flood_model.routing import is_main_reach_id as _is_main

            role = "Song chinh" if _is_main(reach_id) else "Nhanh"
            hq_note = f"{role} {reach_id}. " + hq_note
        note = f"Muc nuoc {hydro1d_label(used_source)}. {hq_note}"
    else:
        note = "H mat = dh + (zmax+zmin)*0.5 (fallback, chua co ket qua Model 1D)."
    return {
        "ok": True,
        "n_station": n_s,
        "n_time": n_t,
        "distance_m": _f(profile["distance_m"]),
        "lon": _f(profile["lon"]),
        "lat": _f(profile["lat"]),
        "z_dem": _f(z_dem),
        "z_bed": _f(z_bed),
        "hours": _f(hours),
        "rainfall_mm": _f(rain),
        "q_surface_mm": _f(qs),
        "q_m3s": _f(q_m3s),
        "q_cut_m3s": _f(q_cut_arr) if q_cut_arr is not None else None,
        "h_display_m": _f(h_wse),
        "h_physical_m": _f(h_phys),
        "h_wse_m": _f(h_wse),
        "wse_by_time": [_f(wse[t]) for t in range(n_t)],
        "wet_fraction": _f(wet_frac),
        "wse_peak": _f(wse[peak_i]),
        "wse_t0": _f(wse[0]),
        "wse0": round(float(wse0), 4) if wse0 is not None else None,
        "h0_depth_m": round(float(h0_depth), 4) if horizontal else 0.0,
        "wse_elev0": round(float(wse_elev0), 4) if wse_elev0 is not None else None,
        "water_mode": "horizontal" if horizontal else "along_bed",
        "water_source": used_source,
        "route_s_m": s_m,
        "route_xs": xs_id,
        "route_reach": reach_id,
        "official_xs": official_xs,
        "offset_m": _f(dist) if official_xs else None,
        "river_station_m": _f(np.asarray(route_meta["route_station_m"], dtype=float))
        if route_meta.get("route_station_m") is not None
        else None,
        "chart_x": "along_river" if not horizontal else "cross_section",
        "initial_time_index": 0,
        "depth_scale": float(runoff["depth_scale"]),
        "target_peak_depth_m": float(runoff["target_peak_depth_m"]),
        "peak_time_index": peak_i,
        "length_m": length_m,
        "zmin": zmin,
        "zmax": zmax,
        "note": note,
    }


def wse_at_time(payload: dict[str, Any], time_index: int) -> list[float | None]:
    grid = payload.get("wse_by_time")
    if grid:
        t = max(0, min(int(time_index), len(grid) - 1))
        return list(grid[t])
    z_dem = np.asarray(payload.get("z_dem") or payload["z_bed"], dtype=float)
    z_bed = np.asarray(payload["z_bed"], dtype=float)
    h = np.asarray(payload.get("h_wse_m") or payload["h_display_m"], dtype=float)
    n = int(z_bed.size)
    if h.size == 0:
        return [None] * n
    t = max(0, min(int(time_index), h.size - 1))
    if payload.get("water_mode") == "horizontal" or payload.get("h_wse_m"):
        val = h[t]
        return [None if not math.isfinite(float(z)) else round(float(val), 4) for z in z_dem.tolist()]
    depth = float(excess_depth_m(h, time_index))
    wse_elev0 = payload.get("wse_elev0")
    wse = water_surface(z_dem, z_bed, depth, wse_elev0)
    out: list[float | None] = []
    for zg, we in zip(z_dem.tolist(), wse.tolist()):
        if zg is None or not math.isfinite(float(we)):
            out.append(None)
        else:
            out.append(round(float(we), 4))
    return out


def _parse_coords(data: dict[str, Any]) -> tuple[list[float], list[float]]:
    coords = data.get("coordinates") or data.get("latlngs") or []
    lons: list[float] = []
    lats: list[float] = []
    for pt in coords:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        a, b = float(pt[0]), float(pt[1])
        # Chap nhan [lon,lat] hoac {lat,lng}; neu |a|<=90 va |b|>90 coi la [lat,lon]
        if abs(a) <= 90 and abs(b) > 90:
            lat, lon = a, b
        else:
            lon, lat = a, b
        lons.append(lon)
        lats.append(lat)
    if len(lons) < 2:
        raise ValueError("Can it nhat 2 diem de ve mat cat doc.")
    return lons, lats


def _json_runoff_only() -> dict[str, Any]:
    r = _demo_runoff()

    def _f(a: np.ndarray) -> list[float]:
        return [round(float(v), 4) for v in np.asarray(a).tolist()]

    return {
        "ok": True,
        "hours": _f(r["hours"]),
        "rainfall_mm": _f(r["rainfall_mm"]),
        "q_surface_mm": _f(r["q_surface_mm"]),
        "q_m3s": _f(r["q_m3s"]),
        "h_display_m": _f(r["h_display_m"]),
        "h_physical_m": _f(r["h_physical_m"]),
        "depth_scale": r["depth_scale"],
        "target_peak_depth_m": r["target_peak_depth_m"],
        "area_km2": r["area_km2"],
        "dt_hours": r["dt_hours"],
    }


def _clear_hydro_caches() -> None:
    _demo_runoff.cache_clear()
    saint_venant_route.cache_clear()
    load_official_xs.cache_clear()
    clear_network_cache()
    try:
        from flood_model.routing import clear_route_cache

        clear_route_cache()
    except Exception:
        traceback.print_exc()
    try:
        from flood_model.flood_3d import clear_flood_result_caches

        clear_flood_result_caches()
    except Exception:
        traceback.print_exc()
    try:
        from flood_model.flow_run import clear_flow_run_cache

        clear_flow_run_cache()
    except Exception:
        traceback.print_exc()


def _clear_runoff_cache() -> None:
    _demo_runoff.cache_clear()


MANNING_N_MIN = 0.001
MANNING_N_MAX = 0.20


def _parse_manning_rows_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        n_col = fields.get("manning_n") or fields.get("n")
        id_col = fields.get("xs_id") or fields.get("id")
        st_col = fields.get("station_km") or fields.get("station") or fields.get("s_km")
        if n_col is None:
            return rows
        for i, row in enumerate(reader):
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
            station_km = None
            if st_col and str(row.get(st_col, "")).strip():
                try:
                    station_km = float(row[st_col])
                except (TypeError, ValueError):
                    station_km = None
            item: dict[str, Any] = {
                "xs_id": xs_id,
                "manning_n": round(n, 4),
            }
            if station_km is not None and math.isfinite(station_km):
                item["station_km"] = round(station_km, 4)
            r_col = fields.get("reach_id") or fields.get("reach")
            n_col_name = fields.get("name") or fields.get("reach_name") or fields.get("river")
            if r_col and str(row.get(r_col, "")).strip():
                item["reach"] = _reach_key(row.get(r_col))
            if n_col_name and str(row.get(n_col_name, "")).strip():
                item["name"] = str(row.get(n_col_name)).strip()
            x_col = fields.get("lon") or fields.get("x") or fields.get("x_m")
            y_col = fields.get("lat") or fields.get("y") or fields.get("y_m")
            if x_col and y_col:
                try:
                    x = float(row[x_col])
                    y = float(row[y_col])
                except (TypeError, ValueError, KeyError):
                    x = y = None
                if x is not None and y is not None and math.isfinite(x) and math.isfinite(y):
                    item["x"] = round(x, 6)
                    item["y"] = round(y, 6)
            rows.append(item)
    rows.sort(key=lambda r: (_reach_sort_key(r.get("reach")), int(r["xs_id"])))
    return rows


_REACH_NAME_HINTS = {
    "hong": "Sông Hồng",
    "song-hong": "Sông Hồng",
    "sông-hồng": "Sông Hồng",
    "duong": "Sông Đuống",
    "song-duong": "Sông Đuống",
    "sông-đuống": "Sông Đuống",
}


def _reach_key(raw: Any) -> str:
    return normalize_reach_id(raw, "main")


def _reach_kind(reach: Any) -> str:
    k = _reach_key(reach).lower().replace("_", "-")
    if k == "main" or k.startswith("main-") or k.startswith("main_"):
        return "main"
    if k.startswith("chinh") or k.startswith("chính"):
        return "main"
    if k in ("hong", "song-hong", "sông-hồng"):
        return "main"
    return "trib"


def _reach_index(reach: Any) -> int:
    k = _reach_key(reach)
    m = re.search(r"(\d+)$", k)
    if m:
        return int(m.group(1))
    return 1


def _reach_sort_key(reach: Any) -> tuple[int, int, str]:
    rid = _reach_key(reach)
    kind_ord = 0 if _reach_kind(rid) == "main" else 1
    return (kind_ord, _reach_index(rid), rid)


def _reach_label(reach: str, name: str | None = None) -> str:
    custom = str(name or "").strip()
    if custom:
        return custom
    rid = _reach_key(reach)
    hint = _REACH_NAME_HINTS.get(rid.lower().replace("_", "-"))
    if hint:
        return hint
    idx = _reach_index(rid)
    if _reach_kind(rid) == "main":
        return f"Sông chính {idx}"
    return f"Sông nhánh {idx}"


def _annotate_reach(row: dict[str, Any]) -> dict[str, Any]:
    rid = _reach_key(row.get("reach") or row.get("reach_id") or "main")
    row["reach"] = rid
    row["kind"] = _reach_kind(rid)
    row["reach_label"] = _reach_label(rid, row.get("name"))
    return row


def _reach_stats(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    mains = {_reach_key(r.get("reach")) for r in rows if _reach_kind(r.get("reach")) == "main"}
    tribs = {_reach_key(r.get("reach")) for r in rows if _reach_kind(r.get("reach")) != "main"}
    return {
        "n_main_reaches": len(mains),
        "n_trib_reaches": len(tribs),
        "n_main_xs": sum(1 for r in rows if _reach_kind(r.get("reach")) == "main"),
        "n_trib_xs": sum(1 for r in rows if _reach_kind(r.get("reach")) != "main"),
        "n_xs": len(rows),
        "n_rows": len(rows),
    }


def _trib_geom_paths(water_source: Any = None) -> list[Path]:
    paths: list[Path] = []
    try:
        paths.append(Path(hydro1d_csv_paths(water_source)["trib_geom"]))
    except Exception:
        pass
    paths.append(SAINT_VENANT_TRIB_GEOM_CSV)
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen or not csv_available(path):
            continue
        seen.add(key)
        out.append(path)
    return out


def _load_trib_n_map() -> dict[tuple[str, int], float]:
    out: dict[tuple[str, int], float] = {}
    if not csv_available(SAINT_VENANT_TRIB_N_CSV):
        return out
    with csv_open(SAINT_VENANT_TRIB_N_CSV) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return out
        fields = {name.strip().lower(): name for name in reader.fieldnames}
        n_col = fields.get("manning_n") or fields.get("n")
        id_col = fields.get("xs_id") or fields.get("id")
        r_col = fields.get("reach_id") or fields.get("reach")
        if n_col is None:
            return out
        for i, row in enumerate(reader):
            try:
                n = float(row[n_col])
                xs_id = int(float(row[id_col])) if id_col and str(row.get(id_col, "")).strip() else i + 1
            except (TypeError, ValueError, KeyError):
                continue
            if not math.isfinite(n) or n <= 0:
                continue
            reach = _reach_key(row.get(r_col) if r_col else "trib_1")
            if reach == "main":
                reach = "trib_1"
            out[(reach, xs_id)] = round(n, 4)
    return out


def _write_trib_manning_rows(rows: Sequence[dict[str, Any]]) -> None:
    payload = []
    for row in rows:
        payload.append(
            {
                "reach_id": _reach_key(row.get("reach") or row.get("reach_id") or "trib_1"),
                "xs_id": int(row["xs_id"]),
                "station_km": "" if row.get("station_km") is None else f"{float(row['station_km']):.4f}",
                "manning_n": f"{float(row['manning_n']):.4f}",
            }
        )
    write_csv_rows(
        SAINT_VENANT_TRIB_N_CSV,
        ["reach_id", "xs_id", "station_km", "manning_n"],
        payload,
        rebuild_hydro=False,
    )


def _parse_trib_xs_rows(water_source: Any = None) -> list[dict[str, Any]]:
    n_map = _load_trib_n_map()
    rows: list[dict[str, Any]] = []
    for path in _trib_geom_paths(water_source):
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            fields = {name.strip().lower(): name for name in reader.fieldnames}
            id_col = fields.get("xs_id") or fields.get("id")
            r_col = fields.get("reach_id") or fields.get("reach")
            n_col_name = fields.get("name") or fields.get("reach_name") or fields.get("river")
            st_col = fields.get("station_km") or fields.get("station")
            x_col = fields.get("lon") or fields.get("x")
            y_col = fields.get("lat") or fields.get("y")
            n_col = fields.get("manning_n") or fields.get("n")
            if not id_col or not x_col or not y_col:
                continue
            for i, row in enumerate(reader):
                try:
                    xs_id = int(float(row[id_col])) if str(row.get(id_col, "")).strip() else i + 1
                    x = float(row[x_col])
                    y = float(row[y_col])
                except (TypeError, ValueError, KeyError):
                    continue
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                reach = _reach_key(row.get(r_col) if r_col else "trib_1")
                if reach == "main":
                    reach = "trib_1"
                station_km = None
                if st_col and str(row.get(st_col, "")).strip():
                    try:
                        station_km = float(row[st_col])
                    except (TypeError, ValueError):
                        station_km = None
                n = n_map.get((reach, xs_id))
                if n is None and n_col:
                    try:
                        n = float(row[n_col])
                    except (TypeError, ValueError, KeyError):
                        n = None
                if n is None or not math.isfinite(float(n)):
                    n = 0.03
                item: dict[str, Any] = {
                    "reach": reach,
                    "xs_id": xs_id,
                    "manning_n": round(float(n), 4),
                    "x": round(x, 6),
                    "y": round(y, 6),
                }
                if n_col_name and str(row.get(n_col_name, "")).strip():
                    item["name"] = str(row.get(n_col_name)).strip()
                if station_km is not None and math.isfinite(station_km):
                    item["station_km"] = round(station_km, 4)
                rows.append(_annotate_reach(item))
        if rows:
            break
    return rows


def load_manning_n_rows() -> tuple[list[dict[str, Any]], str]:
    if csv_available(SAINT_VENANT_N_CSV):
        rows, src = _parse_manning_rows_csv(SAINT_VENANT_N_CSV), str(SAINT_VENANT_N_CSV)
    elif csv_available(SAINT_VENANT_GEOM_CSV):
        rows, src = _parse_manning_rows_csv(SAINT_VENANT_GEOM_CSV), str(SAINT_VENANT_GEOM_CSV)
    else:
        rows, src = [], str(SAINT_VENANT_N_CSV)
    rows = _attach_xs_xy(rows)
    for row in rows:
        if not row.get("reach"):
            row["reach"] = "main"
        _annotate_reach(row)
    seen: set[tuple[str, int]] = set()
    for row in rows:
        try:
            seen.add((_reach_key(row.get("reach")), int(row["xs_id"])))
        except (KeyError, TypeError, ValueError):
            continue
    for row in _parse_trib_xs_rows():
        _annotate_reach(row)
        try:
            key = (_reach_key(row.get("reach")), int(row["xs_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if key in seen:
            continue
        rows.append(row)
        seen.add(key)
    rows.sort(key=lambda r: (_reach_sort_key(r.get("reach")), int(r["xs_id"])))
    return rows, src


def _geom_xy_by_xs(water_source: Any = None) -> dict[tuple[str, int], tuple[float, float]]:
    """Toa do tam mat cat (lon=X, lat=Y) theo (reach_id, xs_id)."""
    paths: list[tuple[str, Path]] = []
    try:
        kind = parse_hydro1d_source(water_source)
        files = hydro1d_csv_paths(kind)
        paths.append(("main", Path(files["geom"])))
        paths.append(("trib", Path(files["trib_geom"])))
    except Exception:
        pass
    paths.append(("main", SAINT_VENANT_GEOM_CSV))
    for trib_path in _trib_geom_paths(water_source):
        paths.append(("trib", trib_path))
    seen: set[str] = set()
    out: dict[tuple[str, int], tuple[float, float]] = {}
    for default_kind, path in paths:
        key = str(path)
        if key in seen or not csv_available(path):
            continue
        seen.add(key)
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            fields = {name.strip().lower(): name for name in reader.fieldnames}
            id_col = fields.get("xs_id") or fields.get("id")
            r_col = fields.get("reach_id") or fields.get("reach")
            x_col = fields.get("lon") or fields.get("x") or fields.get("x_m")
            y_col = fields.get("lat") or fields.get("y") or fields.get("y_m")
            if not id_col or not x_col or not y_col:
                continue
            for i, row in enumerate(reader):
                try:
                    xs_id = int(float(row[id_col])) if str(row.get(id_col, "")).strip() else i + 1
                    x = float(row[x_col])
                    y = float(row[y_col])
                except (TypeError, ValueError, KeyError):
                    continue
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                if r_col and str(row.get(r_col, "")).strip():
                    reach = _reach_key(row.get(r_col))
                else:
                    reach = "trib_1" if default_kind == "trib" else "main"
                if default_kind == "trib" and reach == "main":
                    reach = "trib_1"
                out[(reach, xs_id)] = (round(x, 6), round(y, 6))
    return out


def _attach_xs_xy(rows: list[dict[str, Any]], water_source: Any = None) -> list[dict[str, Any]]:
    xy = _geom_xy_by_xs(water_source)
    if not xy:
        return rows
    for row in rows:
        if row.get("x") is not None and row.get("y") is not None:
            continue
        try:
            xs_id = int(row["xs_id"])
        except (KeyError, TypeError, ValueError):
            continue
        reach = _reach_key(row.get("reach") or "main")
        pair = xy.get((reach, xs_id))
        if pair is None and _reach_kind(reach) == "main":
            pair = xy.get(("main", xs_id))
        if pair is not None:
            row["x"], row["y"] = pair
    return rows


def _geom_width_z_by_xs(water_source: Any = None) -> dict[tuple[str, int], tuple[float, float]]:
    """(reach, xs_id) -> (top_width_at_3m, z_bed_m) tu geom CSV."""
    paths: list[tuple[str, Path]] = []
    try:
        kind = parse_hydro1d_source(water_source)
        files = hydro1d_csv_paths(kind)
        paths.append(("main", Path(files["geom"])))
        paths.append(("trib", Path(files["trib_geom"])))
    except Exception:
        pass
    paths.append(("main", SAINT_VENANT_GEOM_CSV))
    for trib_path in _trib_geom_paths(water_source):
        paths.append(("trib", trib_path))
    seen: set[str] = set()
    out: dict[tuple[str, int], tuple[float, float]] = {}
    for default_kind, path in paths:
        key = str(path)
        if key in seen or not csv_available(path):
            continue
        seen.add(key)
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            fields = {name.strip().lower(): name for name in reader.fieldnames}
            id_col = fields.get("xs_id") or fields.get("id")
            r_col = fields.get("reach_id") or fields.get("reach")
            w_col = fields.get("top_width_at_3m") or fields.get("top_width_m") or fields.get("width_m")
            z_col = fields.get("z_bed_m") or fields.get("z_bed") or fields.get("zb")
            if not id_col:
                continue
            for i, row in enumerate(reader):
                try:
                    xs_id = int(float(row[id_col])) if str(row.get(id_col, "")).strip() else i + 1
                except (TypeError, ValueError, KeyError):
                    continue
                if r_col and str(row.get(r_col, "")).strip():
                    reach = _reach_key(row.get(r_col))
                else:
                    reach = "trib_1" if default_kind == "trib" else "main"
                if default_kind == "trib" and reach == "main":
                    reach = "trib_1"
                width = float("nan")
                z_bed = float("nan")
                if w_col:
                    try:
                        width = float(row[w_col])
                    except (TypeError, ValueError, KeyError):
                        width = float("nan")
                if z_col:
                    try:
                        z_bed = float(row[z_col])
                    except (TypeError, ValueError, KeyError):
                        z_bed = float("nan")
                out[(reach, xs_id)] = (width, z_bed)
    return out


def _en_normal_at(lon: np.ndarray, lat: np.ndarray, index: int) -> tuple[float, float]:
    """Phap tuyen trai (east, north) don vi met tai index tren polyline."""
    te, tn = _en_tangent_at(lon, lat, index)
    return -tn, te


def _en_tangent_at(lon: np.ndarray, lat: np.ndarray, index: int) -> tuple[float, float]:
    """Huong tiep tuyen theo dong chay (east, north) don vi tai index tren polyline."""
    n = int(lon.size)
    i = max(0, min(int(index), max(n - 1, 0)))
    if n < 2:
        return 0.0, 1.0
    if i <= 0:
        dlon = float(lon[1] - lon[0])
        dlat = float(lat[1] - lat[0])
    elif i >= n - 1:
        dlon = float(lon[-1] - lon[-2])
        dlat = float(lat[-1] - lat[-2])
    else:
        dlon = float(lon[i + 1] - lon[i - 1])
        dlat = float(lat[i + 1] - lat[i - 1])
    lat0 = float(lat[i])
    me = dlon * (111320.0 * math.cos(math.radians(lat0)))
    mn = dlat * 110540.0
    length = math.hypot(me, mn) or 1.0
    return me / length, mn / length


def _offset_lonlat(lon: float, lat: float, nx: float, ny: float, offset_m: float) -> tuple[float, float]:
    lat0 = float(lat)
    denom = max(111320.0 * math.cos(math.radians(lat0)), 1e-6)
    return float(lon) + (nx * offset_m) / denom, float(lat) + (ny * offset_m) / 110540.0


def _subsample_indices(n: int, max_pts: int = 24) -> list[int]:
    if n <= 0:
        return []
    if n <= max_pts:
        return list(range(n))
    step = (n - 1) / float(max_pts - 1)
    out: list[int] = []
    seen: set[int] = set()
    for k in range(max_pts):
        i = int(round(k * step))
        i = max(0, min(n - 1, i))
        if i not in seen:
            seen.add(i)
            out.append(i)
    if (n - 1) not in seen:
        out.append(n - 1)
    return out


def _route_index_at_station(station_m: np.ndarray, lon: np.ndarray, lat: np.ndarray,
                           station_km: float | None, lon_c: float, lat_c: float) -> int:
    if station_m.size < 1:
        return 0
    if station_km is not None and math.isfinite(float(station_km)):
        return int(np.argmin(np.abs(station_m - float(station_km) * 1000.0)))
    if lon.size != lat.size or lon.size < 1:
        return 0
    d2 = (lon - float(lon_c)) ** 2 + (lat - float(lat_c)) ** 2
    return int(np.argmin(d2))


def _channel_bank_offsets_from_xs(
    rec: dict[str, Any] | None,
    *,
    rise_m: float = 2.0,
) -> tuple[float, float] | None:
    """Tim mep bo song (gan long) tu profile offset/z: (off_trai, off_phai).

    Di tu tim (offset~0) ra hai ben, lay moc dau tien z >= z_bed + rise_m.
    Quy uoc CSV: offset am = trai (ta), duong = phai (huu).
    """
    if not rec:
        return None
    off = np.asarray(rec.get("offset_m"), dtype=float)
    z = np.asarray(rec.get("z_m"), dtype=float)
    if off.size < 5 or z.size != off.size:
        return None
    order = np.argsort(off)
    off = off[order]
    z = z[order]
    if not np.isfinite(off).any() or not np.isfinite(z).any():
        return None
    z0 = float(np.nanmin(z))
    if not math.isfinite(z0):
        return None
    thr = z0 + float(rise_m)
    i0 = int(np.nanargmin(np.abs(off)))
    left = float(off[0])
    right = float(off[-1])
    for i in range(i0, -1, -1):
        zi = float(z[i])
        if math.isfinite(zi) and zi >= thr:
            left = float(off[i])
            break
    for i in range(i0, int(off.size)):
        zi = float(z[i])
        if math.isfinite(zi) and zi >= thr:
            right = float(off[i])
            break
    if not (math.isfinite(left) and math.isfinite(right)):
        return None
    if left > right:
        left, right = right, left
    # Dam bao dung dau: trai <= 0 <= phai (neu bo lech het mot ben van giu).
    if left > 0 and right > 0:
        left = -abs(left)
    if left < 0 and right < 0:
        right = abs(right)
    return float(left), float(right)


def _bank_offsets_m_at_station(
    *,
    official: dict[int, dict[str, Any]],
    width_z: dict[tuple[str, int], tuple[float, float]],
    rows: list[dict[str, Any]],
    reach: str,
    st_km: float | None,
) -> tuple[float, float]:
    """(offset_trai_m, offset_phai_m): mep bo song; am = ta, duong = huu."""
    left_off = -120.0
    right_off = 120.0
    best_ds = float("inf")
    found = False
    if st_km is None or not math.isfinite(float(st_km)):
        return left_off, right_off
    st_ref = float(st_km)
    for row in rows:
        try:
            rid = _reach_key(row.get("reach") or "main")
            if rid != reach and not (_reach_kind(reach) == "main" and rid == "main"):
                continue
            xid = int(row["xs_id"])
            sk = float(row["station_km"]) if row.get("station_km") not in (None, "") else float("nan")
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(sk):
            continue
        ds = abs(sk - st_ref)
        if ds >= best_ds:
            continue
        rec = official.get(xid) if (_reach_kind(reach) == "main" or rid == "main") else None
        banks = _channel_bank_offsets_from_xs(rec, rise_m=2.0)
        if banks is None and rec is not None:
            banks = _channel_bank_offsets_from_xs(rec, rise_m=1.0)
        if banks is not None:
            best_ds = ds
            left_off, right_off = banks
            found = True
            continue
        wz = width_z.get((rid, xid)) or width_z.get((reach, xid))
        if wz is None and _reach_kind(reach) == "main":
            wz = width_z.get(("main", xid))
        if wz is None:
            continue
        w_raw = float(wz[0])
        if math.isfinite(w_raw) and w_raw > 2.0:
            best_ds = ds
            left_off, right_off = -0.5 * w_raw, 0.5 * w_raw
            found = True
    if not found:
        return left_off, right_off
    if left_off > right_off:
        left_off, right_off = right_off, left_off
    # Gioi han hop ly cho mep bo (tranh tip bai ngap hang km).
    left_off = max(-800.0, min(float(left_off), -25.0))
    right_off = min(800.0, max(float(right_off), 25.0))
    return float(left_off), float(right_off)


def _dem_bank_offsets_m(
    src,
    lon0: float,
    lat0: float,
    nx: float,
    ny: float,
    *,
    search_m: float = 560.0,
    step_m: float = 2.0,
) -> tuple[float, float] | None:
    """Mep duong bo song (vai dinh de) tu DEM doc phap tuyen: (off_trai, off_phai), am = ta.

    Khong lay diem doc doc lon nhat (giua mas) — di tu long ra, bat mas doc, roi len
    dinh/vai phia song (mép đường bờ).
    """
    if not (math.isfinite(lon0) and math.isfinite(lat0)):
        return None
    step = max(float(step_m), 1.0)
    search = max(float(search_m), 120.0)
    offs = np.arange(-search, search + 0.5 * step, step, dtype=float)
    lons: list[float] = []
    lats: list[float] = []
    for o in offs:
        lo, la = _offset_lonlat(lon0, lat0, nx, ny, float(o))
        lons.append(lo)
        lats.append(la)
    try:
        xs, ys = lonlat_to_dem_xy(src, lons, lats)
        zs = sample_z(src, xs, ys)
    except Exception:
        return None
    z = np.asarray(zs, dtype=float)
    if not np.isfinite(z).any():
        return None
    i0 = int(np.nanargmin(np.abs(offs)))
    near = np.abs(offs) <= 40.0
    if np.isfinite(z[near]).any():
        z0 = float(np.nanmin(z[near]))
    else:
        z0 = float(np.nanmin(z))
    if not math.isfinite(z0):
        return None
    g = np.gradient(z, offs)
    # Lam muot doc doc de bot nhieu DEM.
    k = 5
    kernel = np.ones(k, dtype=float) / float(k)
    g_s = np.convolve(np.nan_to_num(g, nan=0.0), kernel, mode="same")

    def _find(side: str) -> float | None:
        indices = list(range(i0, -1, -1) if side == "left" else range(i0, int(offs.size)))
        # 1) Diem doc doc lon nhat tren mas bo (z >= z0+1).
        best_i: int | None = None
        best_score = -1.0
        for i in indices:
            zi = float(z[i])
            gi = float(g_s[i])
            if not (math.isfinite(zi) and math.isfinite(gi)):
                continue
            if zi < z0 + 1.0:
                continue
            # Bo qua doi xa: chi xet mas dau tien (len < ~20 m tu day long).
            if zi > z0 + 22.0:
                break
            score = (-gi) if side == "left" else gi
            if score > best_score:
                best_score = score
                best_i = i
        if best_i is None or best_score < 0.03:
            thr = z0 + 3.0
            for i in indices:
                zi = float(z[i])
                if math.isfinite(zi) and zi >= thr:
                    return float(offs[i])
            return None

        # 2) Tu mas doc di ra ngoai toi dinh (local max).
        peak_i = best_i
        peak_z = float(z[best_i])
        if side == "left":
            for j in range(best_i, -1, -1):
                zj = float(z[j])
                if not math.isfinite(zj):
                    break
                if zj >= peak_z:
                    peak_z = zj
                    peak_i = j
                elif zj < peak_z - 0.35:
                    break
                if float(offs[best_i]) - float(offs[j]) > 140.0:
                    break
        else:
            for j in range(best_i, int(offs.size)):
                zj = float(z[j])
                if not math.isfinite(zj):
                    break
                if zj >= peak_z:
                    peak_z = zj
                    peak_i = j
                elif zj < peak_z - 0.35:
                    break
                if float(offs[j]) - float(offs[best_i]) > 140.0:
                    break

        # 3) Vai phia song: tu dinh di vao long toi khi ha ~0.3 m (mép đường).
        sh_i = peak_i
        if side == "left":
            for j in range(peak_i, best_i + 1):
                zj = float(z[j])
                if math.isfinite(zj) and zj < peak_z - 0.3:
                    sh_i = max(j - 1, peak_i)
                    break
                sh_i = j
        else:
            for j in range(peak_i, best_i - 1, -1):
                zj = float(z[j])
                if math.isfinite(zj) and zj < peak_z - 0.3:
                    sh_i = min(j + 1, peak_i)
                    break
                sh_i = j
        return float(offs[sh_i])

    left = _find("left")
    right = _find("right")
    if left is None and right is None:
        return None
    if left is None and right is not None:
        left = -abs(float(right))
    if right is None and left is not None:
        right = abs(float(left))
    if left is None or right is None:
        return None
    if left > 0:
        left = -abs(left)
    if right < 0:
        right = abs(right)
    left = max(-900.0, min(float(left), -20.0))
    right = min(900.0, max(float(right), 20.0))
    return float(left), float(right)


def _dike_half_thickness_m(width_m: float | None, length_m: float | None) -> float:
    """Nua be day de de dich tam ra ngoai, mat trong bam mep bo."""
    w = float(width_m) if width_m is not None and math.isfinite(float(width_m)) else float("nan")
    L = float(length_m) if length_m is not None and math.isfinite(float(length_m)) else float("nan")
    along = L if math.isfinite(L) and L > 0 else (w if math.isfinite(w) and w > 0 else 200.0)
    if math.isfinite(w) and w > 0 and math.isfinite(L) and L > 0 and min(w, L) / max(w, L) > 0.7:
        across = min(30.0, max(8.0, along * 0.008))
    elif math.isfinite(w) and w > 0 and w < along * 0.35:
        across = w
    else:
        across = 12.0
    return 0.5 * max(float(across), 4.0)


def _dike_along_m(width_m: float | None, length_m: float | None) -> float:
    """Chieu dai de theo song (m) de ve polyline uon theo bo."""
    w = float(width_m) if width_m is not None and math.isfinite(float(width_m)) else float("nan")
    L = float(length_m) if length_m is not None and math.isfinite(float(length_m)) else float("nan")
    if math.isfinite(L) and L > 0 and math.isfinite(w) and w > 0 and min(w, L) / max(w, L) > 0.7:
        along = max(w, L)
    elif math.isfinite(L) and L > 0:
        along = L
    elif math.isfinite(w) and w > 0:
        along = w
    else:
        along = 200.0
    return float(min(max(along, 40.0), 6000.0))


def _weir_bed_path_lonlat(
    *,
    lon0: float,
    lat0: float,
    nx: float,
    ny: float,
    left_off: float,
    right_off: float,
    width_m: float | None,
    dem_src,
    official_rec: dict[str, Any] | None = None,
    step_m: float = 5.0,
    full_floodplain: bool = True,
) -> tuple[list[float], list[float], list[float | None]]:
    """Polyline dap/tran ngang long: bam toan bo mat cat / bai (neu co XS).

    Mac dinh lay het tip trai..tip phai cua mat cat chinh thuc (het bai).
    width_m chi thu hep khi nho hon khoang tip XS.
    """
    lo = float(left_off)
    ro = float(right_off)
    off_s: np.ndarray | None = None
    z_s: np.ndarray | None = None
    if official_rec is not None:
        off_raw = np.asarray(official_rec.get("offset_m"), dtype=float)
        z_raw = np.asarray(official_rec.get("z_m"), dtype=float)
        if off_raw.size >= 5 and z_raw.size == off_raw.size and np.isfinite(off_raw).any():
            order = np.argsort(off_raw)
            off_s = off_raw[order]
            z_s = z_raw[order]
            tip_l = float(np.nanmin(off_s))
            tip_r = float(np.nanmax(off_s))
            if full_floodplain and math.isfinite(tip_l) and math.isfinite(tip_r) and tip_r - tip_l > 20.0:
                lo, ro = tip_l, tip_r

    if lo > ro:
        lo, ro = ro, lo
    if not (math.isfinite(lo) and math.isfinite(ro)):
        lo, ro = -40.0, 40.0
    if lo > 0 and ro > 0:
        lo = -abs(lo)
    if lo < 0 and ro < 0:
        ro = abs(ro)
    span = max(ro - lo, 8.0)
    w = float(width_m) if width_m is not None and math.isfinite(float(width_m)) and float(width_m) > 2.0 else span
    # Het bai tren mat cat: luon dung tip XS khi full_floodplain.
    # width_m chi thu hep khi KHONG ve full bai.
    if full_floodplain and official_rec is not None and span > 50.0:
        a, b = lo, ro
    elif w + 1.0 < span:
        mid = 0.0 if (lo <= 0.0 <= ro) else 0.5 * (lo + ro)
        a = max(lo, mid - 0.5 * w)
        b = min(ro, mid + 0.5 * w)
        if b - a < 6.0:
            a, b = lo, ro
    else:
        a, b = lo, ro

    offs: np.ndarray
    zs_xs: np.ndarray | None = None
    if off_s is not None and z_s is not None:
        mask = (off_s >= a - 1e-6) & (off_s <= b + 1e-6) & np.isfinite(off_s)
        if int(np.count_nonzero(mask)) >= 3:
            offs = off_s[mask]
            zs_xs = z_s[mask]
        else:
            offs = np.asarray([], dtype=float)
    else:
        offs = np.asarray([], dtype=float)

    if offs.size < 3:
        step = max(float(step_m), 2.0)
        n = max(int(math.ceil((b - a) / step)), 2)
        offs = np.linspace(a, b, n + 1)
        zs_xs = None
    # Gioi han / lam day diem (mat cat dai ~3 km van can du moc).
    max_pts = 200
    if offs.size > max_pts:
        idx = np.linspace(0, offs.size - 1, max_pts).astype(int)
        offs = offs[idx]
        if zs_xs is not None:
            zs_xs = zs_xs[idx]
    elif offs.size < 36 and (b - a) > 40.0:
        dense_n = max(36, int((b - a) / max(float(step_m), 3.0)) + 1)
        dense_n = min(dense_n, max_pts)
        dense = np.linspace(a, b, dense_n)
        if zs_xs is not None and offs.size >= 2:
            zs_xs = np.interp(dense, offs, zs_xs.astype(float))
        offs = dense

    path_lon: list[float] = []
    path_lat: list[float] = []
    path_z: list[float | None] = []
    lons: list[float] = []
    lats: list[float] = []
    for o in offs:
        lo_ll, la_ll = _offset_lonlat(float(lon0), float(lat0), nx, ny, float(o))
        lons.append(lo_ll)
        lats.append(la_ll)
        path_lon.append(round(lo_ll, 6))
        path_lat.append(round(la_ll, 6))

    dem_zs: list[float | None] = [None] * len(path_lon)
    if dem_src is not None and lons:
        try:
            xs, ys = lonlat_to_dem_xy(dem_src, lons, lats)
            zs = sample_z(dem_src, xs, ys)
            dem_zs = []
            for zv in zs:
                zf = float(zv)
                dem_zs.append(round(zf, 3) if math.isfinite(zf) else None)
        except Exception:
            dem_zs = [None] * len(path_lon)

    # Chi giu diem nam trong DEM (co z hop le). Khong fallback XS ngoai DEM
    # — tranh canh de/tran bay ra ngoai vung dia hinh.
    keep_lon: list[float] = []
    keep_lat: list[float] = []
    keep_z: list[float | None] = []
    for i in range(len(path_lon)):
        z_dem = dem_zs[i] if i < len(dem_zs) else None
        if z_dem is None:
            continue
        keep_lon.append(path_lon[i])
        keep_lat.append(path_lat[i])
        keep_z.append(z_dem)

    if len(keep_lon) < 2 and zs_xs is not None:
        # Khong co DEM: moi dung mat cat, nhung van can >=2 diem.
        for i in range(len(path_lon)):
            zf = float(zs_xs[i]) if i < int(zs_xs.size) else float("nan")
            if not math.isfinite(zf):
                continue
            keep_lon.append(path_lon[i])
            keep_lat.append(path_lat[i])
            keep_z.append(round(zf, 3))

    return keep_lon, keep_lat, keep_z


def _nearest_official_xs(
    *,
    official: dict[int, dict[str, Any]],
    rows: list[dict[str, Any]],
    reach: str,
    st_km: float | None,
) -> dict[str, Any] | None:
    if st_km is None or not math.isfinite(float(st_km)) or not official:
        return None
    st_ref = float(st_km)
    best_ds = float("inf")
    best_rec: dict[str, Any] | None = None
    for row in rows:
        try:
            rid = _reach_key(row.get("reach") or "main")
            if rid != reach and not (_reach_kind(reach) == "main" and rid == "main"):
                continue
            xid = int(row["xs_id"])
            sk = float(row["station_km"]) if row.get("station_km") not in (None, "") else float("nan")
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(sk):
            continue
        rec = official.get(xid) if (_reach_kind(reach) == "main" or rid == "main") else None
        if rec is None:
            continue
        ds = abs(sk - st_ref)
        if ds < best_ds:
            best_ds = ds
            best_rec = rec
    return best_rec


def _dike_bank_path_lonlat(
    *,
    pts: dict[str, np.ndarray],
    st_km: float,
    bank: str,
    length_m: float,
    dem_src,
    half_thickness_m: float = 0.0,
    fallback_offset_m: float | None = None,
    spacing_m: float = 50.0,
) -> tuple[list[float], list[float]]:
    """Polyline de bam mep bo (uon theo song): (path_lon, path_lat).

    Offset ngang lay o moc thua (hoac 1 moc giua) roi noi suy — tranh zig-zag khi
    quet DEM moi buoc. Phap tuyen lay theo tim song tai tung diem de de uon theo bo.
    """
    st_arr = np.asarray(pts.get("station_m"), dtype=float)
    lon_arr = np.asarray(pts.get("lon"), dtype=float)
    lat_arr = np.asarray(pts.get("lat"), dtype=float)
    if st_arr.size < 2 or lon_arr.size != st_arr.size or lat_arr.size != st_arr.size:
        return [], []
    if not (math.isfinite(float(st_km)) and math.isfinite(float(length_m)) and float(length_m) > 1.0):
        return [], []
    half = 0.5 * float(length_m)
    s_mid = float(st_km) * 1000.0
    s0 = max(float(st_arr[0]), s_mid - half)
    s1 = min(float(st_arr[-1]), s_mid + half)
    if s1 - s0 < 20.0:
        return [], []
    step = max(float(spacing_m), 25.0)
    n_seg = max(int(math.ceil((s1 - s0) / step)), 2)
    stations = np.linspace(s0, s1, n_seg + 1)
    if stations.size > 90:
        stations = np.linspace(s0, s1, 90)

    fb = float(fallback_offset_m) if fallback_offset_m is not None and math.isfinite(float(fallback_offset_m)) else (
        -120.0 if bank == "left" else 120.0
    )

    # Offset neo thua dung lam fallback khi mot diem quet DEM bi nodata.
    anchor_step = max(350.0, step * 6.0)
    anchors = [s0, s_mid, s1]
    a = s0 + anchor_step
    while a < s1 - 0.5 * anchor_step:
        anchors.append(a)
        a += anchor_step
    anchors = sorted(set(round(float(x), 1) for x in anchors if s0 - 1e-6 <= x <= s1 + 1e-6))
    anchor_s: list[float] = []
    anchor_off: list[float] = []
    for s in anchors:
        lon_c = float(np.interp(s, st_arr, lon_arr))
        lat_c = float(np.interp(s, st_arr, lat_arr))
        idx = int(np.argmin(np.abs(st_arr - float(s))))
        nx, ny = _en_normal_at(lon_arr, lat_arr, idx)
        off = fb
        if dem_src is not None:
            dem_banks = _dem_bank_offsets_m(
                dem_src, lon_c, lat_c, nx, ny, search_m=560.0, step_m=4.0
            )
            if dem_banks is not None:
                off = float(dem_banks[0] if bank == "left" else dem_banks[1])
        anchor_s.append(float(s))
        anchor_off.append(float(off))
    if not anchor_s:
        return [], []
    # Chan nhay giua cac moc neo.
    for i in range(1, len(anchor_off)):
        prev = anchor_off[i - 1]
        cur = anchor_off[i]
        if abs(cur - prev) > 120.0:
            anchor_off[i] = prev + math.copysign(120.0, cur - prev)

    sign = -1.0 if bank == "left" else 1.0
    ht = max(float(half_thickness_m), 0.0)

    # Dùng trực tiếp ranh floodplain (z <= z_cap) làm mép trong của đê. Đây là
    # đúng đường ranh đang thấy trên DEM; quét vai/đỉnh dốc có thể đưa đê lên
    # sâu trong bãi, đặc biệt ở tả ngạn.
    if dem_src is not None and stations.size >= 2:
        dense_lon = np.interp(stations, st_arr, lon_arr)
        dense_lat = np.interp(stations, st_arr, lat_arr)
        try:
            banks = floodplain_banks(
                dem_src,
                dense_lon,
                dense_lat,
                max_m=560.0,
                step_m=4.0,
            )
            edge_lon = np.asarray(
                banks["left_lon" if bank == "left" else "right_lon"], dtype=float
            )
            edge_lat = np.asarray(
                banks["left_lat" if bank == "left" else "right_lat"], dtype=float
            )
            if (
                edge_lon.size == stations.size
                and edge_lat.size == stations.size
                and np.isfinite(edge_lon).all()
                and np.isfinite(edge_lat).all()
            ):
                clean_lon: list[float] = []
                clean_lat: list[float] = []
                for lo, la in zip(edge_lon.tolist(), edge_lat.tolist()):
                    if len(clean_lon) >= 2:
                        lat_ref = math.radians(float(clean_lat[-1]))
                        scale_x = 111320.0 * max(math.cos(lat_ref), 1e-6)
                        v0x = (clean_lon[-1] - clean_lon[-2]) * scale_x
                        v0y = (clean_lat[-1] - clean_lat[-2]) * 110540.0
                        v1x = (lo - clean_lon[-1]) * scale_x
                        v1y = (la - clean_lat[-1]) * 110540.0
                        n0 = math.hypot(v0x, v0y)
                        n1 = math.hypot(v1x, v1y)
                        if n0 > 1e-6 and n1 > 1e-6:
                            cos_turn = (v0x * v1x + v0y * v1y) / (n0 * n1)
                            if cos_turn < -0.25:
                                continue
                    clean_lon.append(float(lo))
                    clean_lat.append(float(la))

                for _ in range(5):
                    if len(clean_lon) < 3:
                        break
                    prev_lon = clean_lon.copy()
                    prev_lat = clean_lat.copy()
                    for i in range(1, len(clean_lon) - 1):
                        clean_lon[i] = (
                            0.25 * prev_lon[i - 1]
                            + 0.5 * prev_lon[i]
                            + 0.25 * prev_lon[i + 1]
                        )
                        clean_lat[i] = (
                            0.25 * prev_lat[i - 1]
                            + 0.5 * prev_lat[i]
                            + 0.25 * prev_lat[i + 1]
                        )
                if len(clean_lon) >= 4:
                    clean_lon[0] = 2.0 * clean_lon[1] - clean_lon[2]
                    clean_lat[0] = 2.0 * clean_lat[1] - clean_lat[2]
                    clean_lon[-1] = 2.0 * clean_lon[-2] - clean_lon[-3]
                    clean_lat[-1] = 2.0 * clean_lat[-2] - clean_lat[-3]

                if len(clean_lon) >= 2:
                    lo_arr = np.asarray(clean_lon, dtype=float)
                    la_arr = np.asarray(clean_lat, dtype=float)
                    path_lon: list[float] = []
                    path_lat: list[float] = []
                    for i in range(len(clean_lon)):
                        nx, ny = _en_normal_at(lo_arr, la_arr, i)
                        lo, la = _offset_lonlat(
                            clean_lon[i], clean_lat[i], nx, ny, sign * ht
                        )
                        path_lon.append(round(lo, 6))
                        path_lat.append(round(la, 6))
                    return path_lon, path_lat
        except Exception:
            pass

    path_lon: list[float] = []
    path_lat: list[float] = []
    sampled_offsets: list[float] = []
    for s in stations:
        lon_c = float(np.interp(s, st_arr, lon_arr))
        lat_c = float(np.interp(s, st_arr, lat_arr))
        idx = int(np.argmin(np.abs(st_arr - float(s))))
        nx, ny = _en_normal_at(lon_arr, lat_arr, idx)
        off = float(np.interp(float(s), np.asarray(anchor_s, dtype=float), np.asarray(anchor_off, dtype=float)))
        if dem_src is not None:
            dem_banks = _dem_bank_offsets_m(
                dem_src, lon_c, lat_c, nx, ny, search_m=560.0, step_m=4.0
            )
            if dem_banks is not None:
                candidate = float(dem_banks[0] if bank == "left" else dem_banks[1])
                if math.isfinite(candidate):
                    off = candidate
        sampled_offsets.append(off)

    # DEM có thể trả về xen kẽ mép trong/mép ngoài tại các mặt cắt liên tiếp,
    # tạo đường đê zíc-zắc. Lọc median để bỏ điểm nhảy, giữ gần các offset neo,
    # rồi giới hạn độ đổi ngang và low-pass trước khi dựng polygon.
    if sampled_offsets:
        raw = np.asarray(sampled_offsets, dtype=float)
        anchor_base = np.interp(
            stations,
            np.asarray(anchor_s, dtype=float),
            np.asarray(anchor_off, dtype=float),
        )
        med = raw.copy()
        for i in range(raw.size):
            lo = max(0, i - 2)
            hi = min(raw.size, i + 3)
            med[i] = float(np.median(raw[lo:hi]))

        # Không cho một lựa chọn mép DEM cục bộ lệch quá xa đường neo ổn định.
        vals = np.clip(med, anchor_base - 70.0, anchor_base + 70.0)
        max_delta = max(8.0, min(18.0, step * 0.22))
        for _ in range(2):
            for i in range(1, vals.size):
                vals[i] = np.clip(vals[i], vals[i - 1] - max_delta, vals[i - 1] + max_delta)
            for i in range(vals.size - 2, -1, -1):
                vals[i] = np.clip(vals[i], vals[i + 1] - max_delta, vals[i + 1] + max_delta)

        kernel = np.asarray([1.0, 2.0, 3.0, 2.0, 1.0], dtype=float)
        kernel /= float(kernel.sum())
        for _ in range(4):
            vals = np.convolve(np.pad(vals, 2, mode="edge"), kernel, mode="valid")

        # Giữ đê đúng phía của tim sông.
        if bank == "left":
            vals = np.minimum(vals, -max(4.0, ht))
        else:
            vals = np.maximum(vals, max(4.0, ht))
        sampled_offsets = vals.tolist()

    for s, off in zip(stations, sampled_offsets):
        lon_c = float(np.interp(s, st_arr, lon_arr))
        lat_c = float(np.interp(s, st_arr, lat_arr))
        idx = int(np.argmin(np.abs(st_arr - float(s))))
        nx, ny = _en_normal_at(lon_arr, lat_arr, idx)
        lo, la = _offset_lonlat(lon_c, lat_c, nx, ny, off + sign * ht)
        path_lon.append(round(lo, 6))
        path_lat.append(round(la, 6))

    # Một vài đoạn tim sông nguồn có điểm quay ngược cục bộ. Bỏ điểm gây góc
    # gần 180 độ để polygon không gập lại thành chữ Z rồi quay về hướng cũ.
    clean_lon: list[float] = []
    clean_lat: list[float] = []
    for lo, la in zip(path_lon, path_lat):
        if len(clean_lon) >= 2:
            lat_ref = math.radians(float(clean_lat[-1]))
            scale_x = 111320.0 * max(math.cos(lat_ref), 1e-6)
            v0x = (clean_lon[-1] - clean_lon[-2]) * scale_x
            v0y = (clean_lat[-1] - clean_lat[-2]) * 110540.0
            v1x = (lo - clean_lon[-1]) * scale_x
            v1y = (la - clean_lat[-1]) * 110540.0
            n0 = math.hypot(v0x, v0y)
            n1 = math.hypot(v1x, v1y)
            if n0 > 1e-6 and n1 > 1e-6:
                cos_turn = (v0x * v1x + v0y * v1y) / (n0 * n1)
                if cos_turn < -0.25:
                    continue
        clean_lon.append(lo)
        clean_lat.append(la)

    # Làm trơn hình học lần cuối (giữ nguyên hai đầu) để hai cạnh polygon có
    # độ cong liên tục thay vì gãy tại từng station 50–60 m.
    for _ in range(3):
        if len(clean_lon) < 3:
            break
        prev_lon = clean_lon.copy()
        prev_lat = clean_lat.copy()
        for i in range(1, len(clean_lon) - 1):
            clean_lon[i] = 0.25 * prev_lon[i - 1] + 0.5 * prev_lon[i] + 0.25 * prev_lon[i + 1]
            clean_lat[i] = 0.25 * prev_lat[i - 1] + 0.5 * prev_lat[i] + 0.25 * prev_lat[i + 1]

    # Hai điểm đầu cũng phải tiếp tục theo tiếp tuyến của đường đã làm trơn.
    # Nếu giữ điểm DEM thô ban đầu, hai đầu đê sẽ chĩa ra ngoài mép bờ.
    if len(clean_lon) >= 4:
        clean_lon[0] = 2.0 * clean_lon[1] - clean_lon[2]
        clean_lat[0] = 2.0 * clean_lat[1] - clean_lat[2]
        clean_lon[-1] = 2.0 * clean_lon[-2] - clean_lon[-3]
        clean_lat[-1] = 2.0 * clean_lat[-2] - clean_lat[-3]
    return clean_lon, clean_lat


def _reservoir_upstream_limit_m(storage_area_m2: float | None) -> float:
    """Chieu dai toi da cua long ho ve phia thuong luu (m).

    Dap nam o mep ha luu. Ho tron cung dien tich A co ban kinh
    R = sqrt(2A/pi); khong keo mat nuoc theo dai song phang.
    """
    if storage_area_m2 is not None and math.isfinite(float(storage_area_m2)) and float(storage_area_m2) > 0:
        radius = math.sqrt(2.0 * float(storage_area_m2) / math.pi)
        return float(max(400.0, min(8000.0, radius)))
    return 1800.0


def _reservoir_search_radius_m(storage_area_m2: float | None) -> float:
    """Ban kinh cua so DEM de flood-fill mat ho (m)."""
    upstream = _reservoir_upstream_limit_m(storage_area_m2)
    if storage_area_m2 is not None and math.isfinite(float(storage_area_m2)) and float(storage_area_m2) > 0:
        r_eq = math.sqrt(float(storage_area_m2) / math.pi)
        lateral = max(r_eq * 1.6, 500.0)
        return float(max(upstream * 1.05, min(12000.0, max(upstream, lateral))))
    return 2500.0


def _reservoir_water_level_m(
    *,
    initial_level: float | None,
    crest: float | None,
    invert: float | None,
) -> float | None:
    for v in (initial_level, crest):
        if v is not None and math.isfinite(float(v)):
            return float(v)
    if invert is not None and math.isfinite(float(invert)):
        return float(invert) + 2.0
    return None


def _paint_line_barrier(
    barrier: np.ndarray,
    transform: Affine,
    xs: npt.ArrayLike,
    ys: npt.ArrayLike,
    *,
    thick: int = 1,
) -> None:
    """Ve duong chan (dap) len mask barrier theo toa do DEM."""
    nr, nc = barrier.shape
    pts: list[tuple[int, int]] = []
    for x, y in zip(np.asarray(xs).ravel().tolist(), np.asarray(ys).ravel().tolist()):
        try:
            r, c = rowcol(transform, float(x), float(y))
        except Exception:
            continue
        if 0 <= r < nr and 0 <= c < nc:
            pts.append((int(r), int(c)))
    if len(pts) < 1:
        return
    for i in range(len(pts)):
        r0, c0 = pts[i]
        if i + 1 < len(pts):
            r1, c1 = pts[i + 1]
        else:
            r1, c1 = r0, c0
        n = max(abs(r1 - r0), abs(c1 - c0), 1)
        for k in range(n + 1):
            t = k / float(n)
            rr = int(round(r0 + (r1 - r0) * t))
            cc = int(round(c0 + (c1 - c0) * t))
            for dr in range(-thick, thick + 1):
                for dc in range(-thick, thick + 1):
                    r2, c2 = rr + dr, cc + dc
                    if 0 <= r2 < nr and 0 <= c2 < nc:
                        barrier[r2, c2] = True


def _reservoir_water_surface_mesh(
    dem_src,
    *,
    lon: float,
    lat: float,
    level_m: float,
    flow_e: float = 0.0,
    flow_n: float = 1.0,
    path_lon: Sequence[float] | None = None,
    path_lat: Sequence[float] | None = None,
    storage_area_m2: float | None = None,
    width_m: float | None = None,
    max_out: int = 96,
    max_side: int = 640,
    include_rings: bool = True,
) -> dict[str, Any] | None:
    """
    Flood-fill thuong luu dap tren DEM: o z <= level, khong vuot qua than dap.
    Tra mesh lon/lat/wse/z/depth de ve mat nuoc tren DEM 3D.
    max_side/max_out nho + include_rings=False = nhanh (overlay Cong trinh).
    """
    if dem_src is None:
        return None
    if not (math.isfinite(float(lon)) and math.isfinite(float(lat)) and math.isfinite(float(level_m))):
        return None

    radius_m = _reservoir_search_radius_m(storage_area_m2)
    dlat = radius_m / 110540.0
    dlon = radius_m / max(111320.0 * math.cos(math.radians(float(lat))), 1e-6)
    west, east = float(lon) - dlon, float(lon) + dlon
    south, north = float(lat) - dlat, float(lat) + dlat
    try:
        xs_b, ys_b = lonlat_to_dem_xy(
            dem_src,
            [west, east, east, west],
            [south, south, north, north],
        )
        win = window_from_bounds(
            float(np.min(xs_b)),
            float(np.min(ys_b)),
            float(np.max(xs_b)),
            float(np.max(ys_b)),
            transform=dem_src.transform,
        )
        win = win.round_offsets().round_lengths()
        clip = Window.from_slices(
            (0, int(dem_src.height)), (0, int(dem_src.width))
        )
        col_start = max(float(win.col_off), float(clip.col_off))
        row_start = max(float(win.row_off), float(clip.row_off))
        col_end = min(float(win.col_off + win.width), float(clip.col_off + clip.width))
        row_end = min(float(win.row_off + win.height), float(clip.row_off + clip.height))
        win = Window.from_slices(
            (int(row_start), int(row_end)),
            (int(col_start), int(col_end)),
        )
    except Exception:
        return None
    if win is None or win.width < 3 or win.height < 3:
        return None

    # Gioi han kich thuoc cua so doc (downsample neu can).
    max_side_i = max(48, int(max_side))
    scale = max(float(win.width), float(win.height)) / float(max_side_i)
    out_w = max(8, int(round(float(win.width) / max(scale, 1.0))))
    out_h = max(8, int(round(float(win.height) / max(scale, 1.0))))
    try:
        z = np.empty((out_h, out_w), dtype=np.float32)
        dem_src.read(
            1,
            window=win,
            out=z,
            resampling=Resampling.average if scale > 1.05 else Resampling.nearest,
        )
    except Exception:
        return None
    nodata = dem_src.nodata
    if nodata is not None:
        z = np.where(z == nodata, np.nan, z)
    z[~np.isfinite(z)] = np.nan

    transform = dem_src.window_transform(win) * Affine.scale(
        float(win.width) / float(out_w),
        float(win.height) / float(out_h),
    )
    # Kich thuoc o (m) gan tam.
    x0, y0 = xy(transform, out_h // 2, out_w // 2, offset="center")
    x1, y1 = xy(transform, out_h // 2, out_w // 2 + 1, offset="center")
    lo0, la0 = dem_xy_to_lonlat(dem_src, [x0], [y0])
    lo1, la1 = dem_xy_to_lonlat(dem_src, [x1], [y1])
    mx0, my0 = lonlat_to_mercator(float(lo0[0]), float(la0[0]))
    mx1, my1 = lonlat_to_mercator(float(lo1[0]), float(la1[0]))
    cell_m = max(math.hypot(mx1 - mx0, my1 - my0), 1.0)
    cell_m2 = cell_m * cell_m

    fl = math.hypot(float(flow_e), float(flow_n)) or 1.0
    fe, fn = float(flow_e) / fl, float(flow_n) / fl
    # Phap tuyen ngang long (vuong goc huong chay).
    nx, ny = -fn, fe
    level = float(level_m)

    barrier = np.zeros(z.shape, dtype=bool)
    # Barrier mong cat ngang long, dich nhe xuong ha luu — mat nuoc sat than dap.
    # Khong dung path cong trinh: path thuong dai / uon theo dong chay, ve thanh barrier
    # se chan ca dai thuong luu (meo nuoc cach than hang tram met).
    # thick=0 -> chi dung 1 o (range 0..0); thick=1 -> 3x3 (~3*cell_m) qua day o luoi tho.
    barrier_thick = 0 if cell_m >= 25.0 else 1
    barrier_shift_m = max(0.35 * cell_m, 3.0)
    b_lon0, b_lat0 = _offset_lonlat(float(lon), float(lat), fe, fn, barrier_shift_m)
    half_guess = 200.0
    if width_m is not None and math.isfinite(float(width_m)) and float(width_m) > 2.0:
        half_guess = max(0.6 * float(width_m), 80.0)
    half_guess = min(half_guess, radius_m * 0.9)
    step_scan = max(cell_m, 8.0)

    def _extent_along_normal(sign: float) -> float:
        last = half_guess
        dist = 0.0
        while dist <= radius_m * 0.95:
            lo, la = _offset_lonlat(float(b_lon0), float(b_lat0), nx, ny, sign * dist)
            try:
                xs_p, ys_p = lonlat_to_dem_xy(dem_src, [lo], [la])
                rr, cc = rowcol(transform, float(xs_p[0]), float(ys_p[0]))
            except Exception:
                break
            if rr < 0 or cc < 0 or rr >= out_h or cc >= out_w:
                break
            zv = float(z[int(rr), int(cc)])
            if math.isfinite(zv) and zv > level + 0.25:
                return min(dist + 1.5 * cell_m, radius_m * 0.95)
            last = dist
            dist += step_scan
        return max(last, half_guess)

    half_l = _extent_along_normal(-1.0)
    half_r = _extent_along_normal(1.0)
    a_lon, a_lat = _offset_lonlat(float(b_lon0), float(b_lat0), nx, ny, -half_l)
    b_lon, b_lat = _offset_lonlat(float(b_lon0), float(b_lat0), nx, ny, half_r)
    pxs, pys = lonlat_to_dem_xy(dem_src, [a_lon, b_lon], [a_lat, b_lat])
    _paint_line_barrier(barrier, transform, pxs, pys, thick=barrier_thick)

    # Chan nua ha luu — vector hoa; moc theo barrier (dich ha luu), khong theo tam dap.
    rr_g = np.arange(out_h, dtype=np.float64)[:, None]
    cc_g = np.arange(out_w, dtype=np.float64)[None, :]
    xs_g = transform.c + (cc_g + 0.5) * transform.a + (rr_g + 0.5) * transform.b
    ys_g = transform.f + (cc_g + 0.5) * transform.d + (rr_g + 0.5) * transform.e
    lo_g, la_g = dem_xy_to_lonlat(dem_src, xs_g.ravel(), ys_g.ravel())
    lo_g = np.asarray(lo_g, dtype=np.float64).reshape(out_h, out_w)
    la_g = np.asarray(la_g, dtype=np.float64).reshape(out_h, out_w)
    cos_lat = max(math.cos(math.radians(float(lat))), 1e-6)
    me_g = (lo_g - float(b_lon0)) * (111320.0 * cos_lat)
    mn_g = (la_g - float(b_lat0)) * 110540.0
    # Chi chan phia ha luu cua barrier (mat nuoc duoc len den than dap).
    downstream_block = (me_g * fe + mn_g * fn) > (0.15 * cell_m)
    # Cat phan keo dai thuong luu: song phang van thap hon muc nuoc rat xa.
    me_dam = (lo_g - float(lon)) * (111320.0 * cos_lat)
    mn_dam = (la_g - float(lat)) * 110540.0
    along_dam = me_dam * fe + mn_dam * fn
    upstream_limit = _reservoir_upstream_limit_m(storage_area_m2)
    too_far_upstream = along_dam < -float(upstream_limit)

    # Hat giong: sat dap ve thuong luu (khong lui xa).
    seed_lon, seed_lat = _offset_lonlat(float(lon), float(lat), -fe, -fn, max(0.8 * cell_m, 8.0))
    try:
        sxs, sys_ = lonlat_to_dem_xy(dem_src, [seed_lon, lon], [seed_lat, lat])
        sr0, sc0 = rowcol(transform, float(sxs[0]), float(sys_[0]))
    except Exception:
        return None
    nr, nc = z.shape

    def _is_upstream(r: int, c: int) -> bool:
        # So voi tam dap (khong phai barrier) — uu tien o thuong luu.
        me = (lo_g[r, c] - float(lon)) * (111320.0 * cos_lat)
        mn = (la_g[r, c] - float(lat)) * 110540.0
        return bool((me * fe + mn * fn) < 0.25 * cell_m)

    def _can_wet(r: int, c: int) -> bool:
        if r < 0 or c < 0 or r >= nr or c >= nc:
            return False
        if barrier[r, c] or downstream_block[r, c] or too_far_upstream[r, c]:
            return False
        zv = float(z[r, c])
        return math.isfinite(zv) and zv <= level + 1e-3

    seed: tuple[int, int] | None = None
    # Spiral search quanh hat giong (gioi han ban kinh).
    max_seed_rad = min(max(nr, nc), 64)
    for rad in range(0, max_seed_rad):
        found: list[tuple[int, int]] = []
        for dr in range(-rad, rad + 1):
            for dc in range(-rad, rad + 1):
                if rad > 0 and abs(dr) != rad and abs(dc) != rad:
                    continue
                r, c = int(sr0) + dr, int(sc0) + dc
                if not _can_wet(r, c):
                    continue
                if _is_upstream(r, c) or rad <= 3:
                    found.append((r, c))
        if found:
            # Gan hat giong nhat.
            found.sort(key=lambda rc: (rc[0] - sr0) ** 2 + (rc[1] - sc0) ** 2)
            seed = found[0]
            break
    if seed is None:
        # Fallback: o thap nhat thuong luu (subsample).
        best = None
        best_z = float("inf")
        step_s = max(1, min(nr, nc) // 80)
        for r in range(0, nr, step_s):
            for c in range(0, nc, step_s):
                if not _can_wet(r, c) or not _is_upstream(r, c):
                    continue
                zv = float(z[r, c])
                if zv < best_z:
                    best_z = zv
                    best = (r, c)
        seed = best
    if seed is None:
        return None

    wet = np.zeros(z.shape, dtype=bool)
    # Ve theo muc nuoc (initial_level) phia thuong luu, nhung khong vuot
    # chieu dai ho tron tuong duong. Dien tich A khong cat tung o mat nuoc.
    target_area = None
    if storage_area_m2 is not None and math.isfinite(float(storage_area_m2)) and float(storage_area_m2) > 0:
        target_area = float(storage_area_m2)

    wet[seed[0], seed[1]] = True
    n_wet = 1
    q: deque[tuple[int, int]] = deque([seed])
    while q:
        r, c = q.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if rr < 0 or cc < 0 or rr >= nr or cc >= nc:
                continue
            if wet[rr, cc] or not _can_wet(rr, cc):
                continue
            wet[rr, cc] = True
            n_wet += 1
            q.append((rr, cc))

    if n_wet < 2:
        return None

    area_m2 = float(n_wet) * cell_m2
    rows_i = np.where(wet.any(axis=1))[0]
    cols_i = np.where(wet.any(axis=0))[0]
    r0, r1 = int(rows_i[0]), int(rows_i[-1])
    c0, c1 = int(cols_i[0]), int(cols_i[-1])
    bh = r1 - r0 + 1
    bw = c1 - c0 + 1
    step = max(1, int(math.ceil(max(bh, bw) / float(max(8, max_out)))))
    rr = np.arange(r0, r1 + 1, step)
    cc = np.arange(c0, c1 + 1, step)
    # Dam bao mep ha luu (sat dap) khong bi subsample bo mat.
    try:
        sxs_d, sys_d = lonlat_to_dem_xy(dem_src, [float(lon)], [float(lat)])
        dr_d, dc_d = rowcol(transform, float(sxs_d[0]), float(sys_d[0]))
        near = []
        rad_edge = max(2, int(math.ceil(120.0 / max(cell_m, 1.0))))
        for r in range(max(0, int(dr_d) - rad_edge), min(nr, int(dr_d) + rad_edge + 1)):
            for c in range(max(0, int(dc_d) - rad_edge), min(nc, int(dc_d) + rad_edge + 1)):
                if wet[r, c]:
                    near.append((r, c))
        if near:
            # Them hang/cot cua cac o uot gan dap + o uot xa nhat theo ha luu.
            rr = np.unique(np.concatenate([rr, np.array([r for r, _ in near], dtype=int)]))
            cc = np.unique(np.concatenate([cc, np.array([c for _, c in near], dtype=int)]))
            # O uot "xa nhat" ve phia dap (along lon nhat).
            along_wet = (lo_g[wet] - float(lon)) * (111320.0 * cos_lat) * fe + (
                la_g[wet] - float(lat)
            ) * 110540.0 * fn
            # Giu ~toi da 24 o mep ha luu.
            wet_rc = np.argwhere(wet)
            order = np.argsort(-along_wet)[:24]
            for k in order:
                r_k, c_k = int(wet_rc[k, 0]), int(wet_rc[k, 1])
                if r_k not in rr:
                    rr = np.sort(np.append(rr, r_k))
                if c_k not in cc:
                    cc = np.sort(np.append(cc, c_k))
    except Exception:
        pass
    if rr.size < 2 or cc.size < 2:
        # Dam bao it nhat 2x2 de tao mesh.
        rr = np.unique(np.clip(np.array([r0, r1], dtype=int), 0, nr - 1))
        cc = np.unique(np.clip(np.array([c0, c1], dtype=int), 0, nc - 1))
        if rr.size < 2:
            rr = np.array([max(0, r0 - 1), min(nr - 1, r0 + 1)], dtype=int)
        if cc.size < 2:
            cc = np.array([max(0, c0 - 1), min(nc - 1, c0 + 1)], dtype=int)

    lons = np.full((rr.size, cc.size), np.nan, dtype=float)
    lats = np.full((rr.size, cc.size), np.nan, dtype=float)
    wse = np.full((rr.size, cc.size), np.nan, dtype=np.float32)
    zz = np.full((rr.size, cc.size), np.nan, dtype=np.float32)
    depth = np.full((rr.size, cc.size), np.nan, dtype=np.float32)
    for i, r in enumerate(rr):
        for j, c in enumerate(cc):
            if not wet[int(r), int(c)]:
                continue
            zv = float(z[int(r), int(c)])
            lons[i, j] = float(lo_g[int(r), int(c)])
            lats[i, j] = float(la_g[int(r), int(c)])
            wse[i, j] = float(level)  # luon dung muc nuoc khai bao
            zz[i, j] = zv
            depth[i, j] = max(float(level) - zv, 0.0)

    if not np.isfinite(wse).any():
        return None

    # Keo mep ha luu cua mesh sat mat thuong luu dap (tranh khoang trong do o DEM tho).
    face_along = -max(0.12 * cell_m, 2.0)
    snap_from = face_along - 1.35 * cell_m
    for i in range(lons.shape[0]):
        for j in range(lons.shape[1]):
            if not math.isfinite(float(lons[i, j])):
                continue
            me_p = (float(lons[i, j]) - float(lon)) * (111320.0 * cos_lat)
            mn_p = (float(lats[i, j]) - float(lat)) * 110540.0
            along_p = me_p * fe + mn_p * fn
            if along_p < snap_from or along_p > face_along + 0.35 * cell_m:
                continue
            across_p = me_p * (-fn) + mn_p * fe
            # Di chuyen diem den mat thuong luu dap, giu toa do ngang long.
            lo_s, la_s = _offset_lonlat(float(lon), float(lat), fe, fn, face_along)
            lo_s, la_s = _offset_lonlat(lo_s, la_s, nx, ny, across_p)
            lons[i, j] = float(lo_s)
            lats[i, j] = float(la_s)
            # Dam bao co do sau toi thieu de nhin thay khoi nuoc sat than.
            if not math.isfinite(float(depth[i, j])) or float(depth[i, j]) < 0.05:
                depth[i, j] = max(float(depth[i, j]) if math.isfinite(float(depth[i, j])) else 0.0, 0.15)
                zz[i, j] = float(level) - float(depth[i, j])

    def _grid_out(a: np.ndarray, nd: int) -> list[list[float | None]]:
        out: list[list[float | None]] = []
        for row in np.asarray(a):
            out.append([
                (round(float(v), nd) if v is not None and math.isfinite(float(v)) else None)
                for v in row.tolist()
            ])
        return out

    # Polygon ranh mat ho (lon/lat) — bo qua o che do nhanh (overlay).
    rings_out: list[list[list[float]]] = []
    if include_rings:
        try:
            from rasterio import features as rio_features

            for geom, val in rio_features.shapes(
                wet.astype(np.uint8),
                mask=wet,
                transform=transform,
            ):
                if int(val) != 1:
                    continue
                coords = geom.get("coordinates") or []
                if not coords:
                    continue
                # Chi lay vong ngoai; subsample neu qua dai.
                exterior = coords[0]
                if len(exterior) < 4:
                    continue
                step_pt = max(1, len(exterior) // 180)
                xs_r = [float(p[0]) for p in exterior[::step_pt]]
                ys_r = [float(p[1]) for p in exterior[::step_pt]]
                if exterior[-1] != exterior[::step_pt][-1]:
                    xs_r.append(float(exterior[-1][0]))
                    ys_r.append(float(exterior[-1][1]))
                lo_r, la_r = dem_xy_to_lonlat(dem_src, xs_r, ys_r)
                ring = [
                    [round(float(lo_r[i]), 6), round(float(la_r[i]), 6)]
                    for i in range(len(lo_r))
                    if math.isfinite(float(lo_r[i])) and math.isfinite(float(la_r[i]))
                ]
                if len(ring) >= 4:
                    rings_out.append(ring)
                if len(rings_out) >= 4:
                    break
        except Exception:
            rings_out = []

    return {
        "level_m": round(level, 3),
        "area_m2": round(area_m2, 1),
        "area_declared_m2": round(float(storage_area_m2), 1)
        if storage_area_m2 is not None and math.isfinite(float(storage_area_m2))
        else None,
        "area_target_m2": round(float(target_area), 1) if target_area is not None else None,
        "n_cells": int(n_wet),
        "cell_m": round(cell_m, 2),
        "rings": rings_out,
        "lons": _grid_out(lons, 6),
        "lats": _grid_out(lats, 6),
        "wse": _grid_out(wse, 3),
        "z": _grid_out(zz, 3),
        "depth": _grid_out(depth, 3),
    }


def _structure_section_context(water_source: Any = None):
    """Tim song, mat cat va DEM de lay cao trinh day theo mat cat."""
    kind = parse_hydro1d_source(water_source)
    manning_rows, _src = load_manning_n_rows()
    width_z = _geom_width_z_by_xs(kind)
    official = load_official_xs(kind)
    routes = {
        normalize_reach_id(r.get("id"), "main"): r
        for r in _saint_venant_network_routes(kind)
    }
    reach_pts: dict[str, dict[str, np.ndarray]] = {}
    for rid, route in routes.items():
        try:
            reach_pts[rid] = {
                "lon": np.asarray(route["lon"], dtype=float),
                "lat": np.asarray(route["lat"], dtype=float),
                "station_m": np.asarray(route["station_m"], dtype=float),
            }
        except Exception:
            continue
    return manning_rows, width_z, official, reach_pts


def weir_section_min_dem_m(
    rec: dict[str, Any],
    *,
    dem_src,
    official,
    manning_rows,
    reach_pts,
    width_z,
) -> float | None:
    """Cao trinh thap nhat tren mat cat DEM ngang long tai vi tri dap/tran."""
    if dem_src is None:
        return None
    reach = _reach_key(rec.get("reach") or "main")
    st_km = None
    try:
        if str(rec.get("station_km") or "").strip():
            st_km = float(rec["station_km"])
    except (TypeError, ValueError):
        st_km = None
    pts = reach_pts.get(reach)
    if pts is None and _reach_kind(reach) == "main":
        pts = reach_pts.get("main")
    lon_c = None
    lat_c = None
    try:
        if str(rec.get("lon") or "").strip() and str(rec.get("lat") or "").strip():
            lon_c = float(rec["lon"])
            lat_c = float(rec["lat"])
            if not (math.isfinite(lon_c) and math.isfinite(lat_c)):
                lon_c = lat_c = None
    except (TypeError, ValueError):
        lon_c = lat_c = None
    if lon_c is None or lat_c is None:
        if st_km is None or pts is None or not pts["station_m"].size:
            return None
        s = float(st_km) * 1000.0
        lon_c = float(np.interp(s, pts["station_m"], pts["lon"]))
        lat_c = float(np.interp(s, pts["station_m"], pts["lat"]))
    nx, ny = 1.0, 0.0
    if pts is not None and pts["station_m"].size:
        idx = _route_index_at_station(
            pts["station_m"], pts["lon"], pts["lat"], st_km, float(lon_c), float(lat_c)
        )
        nx, ny = _en_normal_at(pts["lon"], pts["lat"], idx)
    width_m = None
    try:
        if str(rec.get("width_m") or "").strip():
            width_m = float(rec["width_m"])
            if not math.isfinite(width_m):
                width_m = None
    except (TypeError, ValueError):
        width_m = None
    left_off, right_off = _bank_offsets_m_at_station(
        official=official,
        width_z=width_z,
        rows=manning_rows,
        reach=reach,
        st_km=st_km,
    )
    lo_b, ro_b = float(left_off), float(right_off)
    dem_banks = _dem_bank_offsets_m(dem_src, float(lon_c), float(lat_c), nx, ny)
    if dem_banks is not None:
        lo_b = min(lo_b, float(dem_banks[0]))
        ro_b = max(ro_b, float(dem_banks[1]))
    xs_rec = _nearest_official_xs(
        official=official,
        rows=manning_rows,
        reach=reach,
        st_km=st_km,
    )
    _path_lon, _path_lat, path_z = _weir_bed_path_lonlat(
        lon0=float(lon_c),
        lat0=float(lat_c),
        nx=nx,
        ny=ny,
        left_off=lo_b,
        right_off=ro_b,
        width_m=width_m,
        dem_src=dem_src,
        official_rec=xs_rec,
        step_m=5.0,
        full_floodplain=True,
    )
    vals: list[float] = []
    for z in path_z:
        try:
            v = float(z)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            vals.append(v)
    return min(vals) if vals else None


def _fmt_bed_m(value: float) -> str:
    rounded = round(float(value), 2)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.2f}"


def _reservoir_has_bed_m() -> float | None:
    """Cao trinh day ho = H thap nhat cua H-A-S, giong trang ho chua."""
    try:
        from flood_model.reservoir import has_bed_m
        bed = has_bed_m()
    except Exception:
        return None
    if bed is None or not math.isfinite(float(bed)):
        return None
    return float(bed)


def apply_weir_dem_beds(rows: list[dict[str, Any]], water_source: Any = None) -> None:
    """Tran: diem thap nhat mat cat DEM. Ho chua: H thap nhat cua H-A-S."""
    targets = [
        row for row in rows
        if normalize_structure_type(row.get("type")) in ("weir", "reservoir")
    ]
    if not targets:
        return
    has_bed = _reservoir_has_bed_m()
    for row in targets:
        if normalize_structure_type(row.get("type")) == "reservoir" and has_bed is not None:
            row["invert_m"] = _fmt_bed_m(has_bed)
    weirs = [
        row for row in targets
        if normalize_structure_type(row.get("type")) == "weir"
    ]
    if not weirs:
        return
    dem_cm = None
    try:
        dem_cm = _open_dem(resolve_dem_path("dem", None))
        dem_src = dem_cm.__enter__()
        manning_rows, width_z, official, reach_pts = _structure_section_context(water_source)
        for row in weirs:
            bed = weir_section_min_dem_m(
                row,
                dem_src=dem_src,
                official=official,
                manning_rows=manning_rows,
                reach_pts=reach_pts,
                width_z=width_z,
            )
            if bed is not None:
                row["invert_m"] = _fmt_bed_m(bed)
    except Exception:
        return
    finally:
        if dem_cm is not None:
            try:
                dem_cm.__exit__(None, None, None)
            except Exception:
                pass


def map_network_overlay_payload(water_source: Any = None) -> dict[str, Any]:
    """Mat cat + cong trinh (lon/lat) de ve len DEM 3D."""
    kind = parse_hydro1d_source(water_source)
    rows, src_path = load_manning_n_rows()
    width_z = _geom_width_z_by_xs(kind)
    official = load_official_xs(kind)
    routes = {
        normalize_reach_id(r.get("id"), "main"): r
        for r in _saint_venant_network_routes(kind)
    }
    reach_pts: dict[str, dict[str, np.ndarray]] = {}
    for rid, route in routes.items():
        try:
            reach_pts[rid] = {
                "lon": np.asarray(route["lon"], dtype=float),
                "lat": np.asarray(route["lat"], dtype=float),
                "station_m": np.asarray(route["station_m"], dtype=float),
            }
        except Exception:
            continue

    sections: list[dict[str, Any]] = []
    for row in rows:
        reach = _reach_key(row.get("reach") or "main")
        try:
            xs_id = int(row["xs_id"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            lon_c = float(row["x"]) if row.get("x") is not None else float("nan")
            lat_c = float(row["y"]) if row.get("y") is not None else float("nan")
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lon_c) and math.isfinite(lat_c)):
            continue
        st_km = None
        try:
            if row.get("station_km") is not None and str(row.get("station_km")).strip() != "":
                st_km = float(row["station_km"])
        except (TypeError, ValueError):
            st_km = None
        wz = width_z.get((reach, xs_id))
        if wz is None and _reach_kind(reach) == "main":
            wz = width_z.get(("main", xs_id))
        half_w = 150.0
        z_bed = float("nan")
        if wz is not None:
            w_raw, z_raw = wz
            if math.isfinite(w_raw) and w_raw > 2.0:
                half_w = 0.5 * float(w_raw)
            if math.isfinite(z_raw):
                z_bed = float(z_raw)

        pts = reach_pts.get(reach)
        if pts is None and _reach_kind(reach) == "main":
            pts = reach_pts.get("main")
        if pts is not None:
            idx = _route_index_at_station(
                pts["station_m"], pts["lon"], pts["lat"], st_km, lon_c, lat_c
            )
            nx, ny = _en_normal_at(pts["lon"], pts["lat"], idx)
        else:
            nx, ny = 1.0, 0.0

        lon_line: list[float] = []
        lat_line: list[float] = []
        z_line: list[float | None] = []
        rec = official.get(xs_id) if _reach_kind(reach) == "main" else None
        if rec is not None:
            off = np.asarray(rec["offset_m"], dtype=float)
            z_arr = np.asarray(rec["z_m"], dtype=float)
            order = np.argsort(off)
            for j in _subsample_indices(int(order.size), 24):
                o = float(off[order[j]])
                lo, la = _offset_lonlat(lon_c, lat_c, nx, ny, o)
                lon_line.append(round(lo, 6))
                lat_line.append(round(la, 6))
                zv = float(z_arr[order[j]])
                z_line.append(round(zv, 3) if math.isfinite(zv) else None)
        if len(lon_line) < 2:
            lon_line, lat_line, z_line = [], [], []
            for o in (-half_w, 0.0, half_w):
                lo, la = _offset_lonlat(lon_c, lat_c, nx, ny, o)
                lon_line.append(round(lo, 6))
                lat_line.append(round(la, 6))
                z_line.append(round(z_bed, 3) if math.isfinite(z_bed) else None)

        sections.append({
            "xs_id": xs_id,
            "reach": reach,
            "reach_label": row.get("reach_label") or reach,
            "kind": _reach_kind(reach),
            "station_km": round(float(st_km), 4) if st_km is not None and math.isfinite(st_km) else None,
            "manning_n": row.get("manning_n"),
            "center_lon": round(lon_c, 6),
            "center_lat": round(lat_c, 6),
            "z_bed": round(z_bed, 3) if math.isfinite(z_bed) else None,
            "lon": lon_line,
            "lat": lat_line,
            "z": z_line,
        })

    structures: list[dict[str, Any]] = []
    try:
        cons = load_constructions(seed=False)
    except Exception:
        cons = []

    dem_src = None
    dem_cm = None
    try:
        dem_cm = _open_dem(resolve_dem_path("dem", None))
        dem_src = dem_cm.__enter__()
    except Exception:
        dem_src = None
        dem_cm = None

    def _num_field(rec: dict[str, Any], key: str) -> float | None:
        try:
            raw = rec.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            v = float(raw)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    try:
        for rec in cons:
            reach = _reach_key(rec.get("reach") or "main")
            stype = normalize_structure_type(rec.get("type"))
            lon_s = None
            lat_s = None
            had_xy = False
            try:
                if str(rec.get("lon") or "").strip() and str(rec.get("lat") or "").strip():
                    lon_s = float(rec["lon"])
                    lat_s = float(rec["lat"])
                    had_xy = math.isfinite(lon_s) and math.isfinite(lat_s)
            except (TypeError, ValueError):
                lon_s, lat_s = None, None
                had_xy = False
            st_km = None
            try:
                if str(rec.get("station_km") or "").strip():
                    st_km = float(rec["station_km"])
            except (TypeError, ValueError):
                st_km = None
            pts = reach_pts.get(reach)
            if pts is None and _reach_kind(reach) == "main":
                pts = reach_pts.get("main")
            lon_c = lon_s
            lat_c = lat_s
            if (lon_s is None or lat_s is None) and st_km is not None and math.isfinite(st_km):
                if pts is not None and pts["station_m"].size:
                    s = float(st_km) * 1000.0
                    lon_s = float(np.interp(s, pts["station_m"], pts["lon"]))
                    lat_s = float(np.interp(s, pts["station_m"], pts["lat"]))
                    lon_c, lat_c = lon_s, lat_s
            if lon_s is None or lat_s is None or not (math.isfinite(lon_s) and math.isfinite(lat_s)):
                continue
            if (
                lon_c is None
                or lat_c is None
                or not math.isfinite(float(lon_c))
                or not math.isfinite(float(lat_c))
            ):
                lon_c, lat_c = lon_s, lat_s
            flow_e, flow_n = 0.0, 1.0
            nx, ny = 1.0, 0.0
            if pts is not None and pts["station_m"].size:
                idx = _route_index_at_station(
                    pts["station_m"], pts["lon"], pts["lat"], st_km, float(lon_c), float(lat_c)
                )
                flow_e, flow_n = _en_tangent_at(pts["lon"], pts["lat"], idx)
                nx, ny = _en_normal_at(pts["lon"], pts["lat"], idx)

            # De ta/huu: mep bo tu doc doc DEM; dich nua be day ra bai de mat trong bam mep.
            bank = structure_bank_side(rec)
            if bank and pts is not None and st_km is not None and math.isfinite(float(st_km)):
                # Station la moc tin cay hon toa do cu trong CSV: tim lai tam song
                # truoc khi quet phap tuyen DEM, tranh snap sai bo khi toa do da cu.
                s = float(st_km) * 1000.0
                lon_c = float(np.interp(s, pts["station_m"], pts["lon"]))
                lat_c = float(np.interp(s, pts["station_m"], pts["lat"]))
            width_m = _num_field(rec, "width_m")
            length_m = _num_field(rec, "length_m")
            left_off, right_off = _bank_offsets_m_at_station(
                official=official,
                width_z=width_z,
                rows=rows,
                reach=reach,
                st_km=st_km,
            )
            if dem_src is not None and bank:
                dem_banks = _dem_bank_offsets_m(dem_src, float(lon_c), float(lat_c), nx, ny)
                if dem_banks is not None:
                    left_off, right_off = dem_banks
            bank_offset_m = 0.0
            path_lon: list[float] = []
            path_lat: list[float] = []
            path_z: list[float | None] = []
            path_kind: str | None = None
            if bank:
                if bank == "left":
                    bank_offset_m = float(left_off)
                elif bank == "right":
                    bank_offset_m = float(right_off)
                half_t = 0.0
                if stype == "dike":
                    half_t = _dike_half_thickness_m(width_m, length_m)
                    if bank == "left":
                        bank_offset_m -= half_t
                    else:
                        bank_offset_m += half_t
                lon_s, lat_s = _offset_lonlat(float(lon_c), float(lat_c), nx, ny, bank_offset_m)
                if stype == "dike" and pts is not None and st_km is not None and math.isfinite(float(st_km)):
                    along_m = _dike_along_m(width_m, length_m)
                    # Offset truoc khi cong half_t (path tu tinh half_t).
                    fb = float(left_off if bank == "left" else right_off)
                    path_lon, path_lat = _dike_bank_path_lonlat(
                        pts=pts,
                        st_km=float(st_km),
                        bank=bank,
                        length_m=along_m,
                        dem_src=dem_src,
                        half_thickness_m=half_t,
                        fallback_offset_m=fb,
                        spacing_m=60.0,
                    )
                    path_kind = "along_bank" if len(path_lon) >= 2 else None
                    if len(path_lon) >= 2:
                        mid = len(path_lon) // 2
                        lon_s, lat_s = float(path_lon[mid]), float(path_lat[mid])
            # Dap/tran, cong, ho chua: polyline ngang long theo mat cat, day bam DEM.
            if stype in ("weir", "gate", "reservoir") and not had_xy and not path_lon:
                lo_b, ro_b = float(left_off), float(right_off)
                if dem_src is not None:
                    dem_banks = _dem_bank_offsets_m(
                        dem_src, float(lon_c), float(lat_c), nx, ny
                    )
                    if dem_banks is not None:
                        lo_b = min(lo_b, float(dem_banks[0]))
                        ro_b = max(ro_b, float(dem_banks[1]))
                xs_rec = _nearest_official_xs(
                    official=official,
                    rows=rows,
                    reach=reach,
                    st_km=st_km,
                )
                # Ve het bai tren mat cat chinh thuc (tip..tip).
                path_lon, path_lat, path_z = _weir_bed_path_lonlat(
                    lon0=float(lon_c),
                    lat0=float(lat_c),
                    nx=nx,
                    ny=ny,
                    left_off=lo_b,
                    right_off=ro_b,
                    width_m=width_m,
                    dem_src=dem_src,
                    official_rec=xs_rec,
                    step_m=5.0,
                    full_floodplain=True,
                )
                if len(path_lon) >= 2:
                    path_kind = "across_bed"
                    mid = len(path_lon) // 2
                    lon_s, lat_s = float(path_lon[mid]), float(path_lat[mid])
            crest = _num_field(rec, "crest_m")
            invert = _num_field(rec, "invert_m")
            if stype == "reservoir":
                bed = _reservoir_has_bed_m()
                if bed is not None:
                    invert = bed
            elif stype == "weir" and dem_src is not None:
                bed = weir_section_min_dem_m(
                    rec,
                    dem_src=dem_src,
                    official=official,
                    manning_rows=rows,
                    reach_pts=reach_pts,
                    width_z=width_z,
                )
                if bed is not None:
                    invert = bed
            height_m = _num_field(rec, "height_m")
            gate_opening = _num_field(rec, "gate_opening_m")
            cd = _num_field(rec, "cd")
            submerged_exp = _num_field(rec, "submerged_exp")
            q_max = _num_field(rec, "q_max_m3s")
            q_min = _num_field(rec, "q_min_m3s")
            storage_area = _num_field(rec, "storage_area_m2")
            initial_level = _num_field(rec, "initial_level_m")
            dam_crest = _num_field(rec, "dam_crest_m")
            outlet_sill = _num_field(rec, "outlet_sill_m")
            spillway_crest = _num_field(rec, "spillway_crest_m")
            gate_left_offset = _num_field(rec, "gate_left_offset_m")
            gate_spacing = _num_field(rec, "gate_spacing_m")
            formula = str(rec.get("formula") or "").strip()
            placement = str(rec.get("placement") or "").strip()
            valve = str(rec.get("valve") or "").strip()
            control = str(rec.get("control") or "").strip()
            water_surface = None
            if stype == "reservoir" and dem_src is not None:
                lvl = _reservoir_water_level_m(
                    initial_level=initial_level,
                    crest=crest,
                    invert=invert,
                )
                if lvl is not None:
                    try:
                        water_surface = _reservoir_water_surface_mesh(
                            dem_src,
                            lon=float(lon_s),
                            lat=float(lat_s),
                            level_m=float(lvl),
                            flow_e=float(flow_e),
                            flow_n=float(flow_n),
                            path_lon=path_lon,
                            path_lat=path_lat,
                            storage_area_m2=storage_area,
                            width_m=width_m,
                            max_out=56,
                            max_side=280,
                            include_rings=False,
                        )
                    except Exception:
                        water_surface = None
            structures.append({
                "id": rec.get("id") or "",
                "name": rec.get("name") or "",
                "type": stype,
                "type_label": structure_type_label(stype),
                "reach": reach,
                "station_km": round(float(st_km), 4) if st_km is not None and math.isfinite(st_km) else None,
                "xs_id": str(rec.get("xs_id") or "").strip() or None,
                "lon": round(float(lon_s), 6),
                "lat": round(float(lat_s), 6),
                "path_lon": path_lon,
                "path_lat": path_lat,
                "path_z": path_z,
                "path_kind": path_kind,
                "crest_m": round(crest, 3) if crest is not None else None,
                "invert_m": round(invert, 3) if invert is not None else None,
                "width_m": round(width_m, 3) if width_m is not None else None,
                "length_m": round(length_m, 3) if length_m is not None else None,
                "height_m": round(height_m, 3) if height_m is not None else None,
                "gate_opening_m": round(gate_opening, 3) if gate_opening is not None else None,
                "cd": round(cd, 4) if cd is not None else None,
                "submerged_exp": round(submerged_exp, 3) if submerged_exp is not None else None,
                "formula": formula,
                "formula_label": structure_formula_label(formula) if formula else "",
                "placement": placement,
                "bank_side": bank,
                "bank_offset_m": round(bank_offset_m, 1) if bank else None,
                "valve": valve,
                "control": control,
                "q_max_m3s": round(q_max, 3) if q_max is not None else None,
                "q_min_m3s": round(q_min, 3) if q_min is not None else None,
                "storage_area_m2": round(storage_area, 1) if storage_area is not None else None,
                "initial_level_m": round(initial_level, 3) if initial_level is not None else None,
                "dam_crest_m": round(dam_crest, 3) if dam_crest is not None else None,
                "outlet_sill_m": round(outlet_sill, 3) if outlet_sill is not None else None,
                "spillway_crest_m": round(spillway_crest, 3) if spillway_crest is not None else None,
                "gate_left_offset_m": round(gate_left_offset, 3) if gate_left_offset is not None else None,
                "gate_spacing_m": round(gate_spacing, 3) if gate_spacing is not None else None,
                "outlet_gate_count": str(rec.get("outlet_gate_count") or "").strip() or None,
                "outlet_gate_widths_m": str(rec.get("outlet_gate_widths_m") or "").strip() or None,
                "water_surface": water_surface,
                "upstream_reach": str(rec.get("upstream_reach") or "").strip(),
                "downstream_reach": str(rec.get("downstream_reach") or "").strip(),
                "file": str(rec.get("file") or "").strip(),
                "value_col": str(rec.get("value_col") or "").strip(),
                "unit": str(rec.get("unit") or "").strip(),
                "note": str(rec.get("note") or "").strip(),
                "flow_east": round(float(flow_e), 6),
                "flow_north": round(float(flow_n), 6),
            })
    finally:
        if dem_cm is not None:
            try:
                dem_cm.__exit__(None, None, None)
            except Exception:
                pass

    return {
        "ok": True,
        "water_source": kind,
        "geom_source": src_path,
        "n_sections": len(sections),
        "n_structures": len(structures),
        "sections": sections,
        "structures": structures,
    }


def current_xs_spacing_m() -> float:
    saved = load_sv_ui_params()
    if saved.get("xs_spacing_m") is not None:
        return float(saved["xs_spacing_m"])
    try:
        return float(_saint_venant_mod()["XS_SPACING_M"])
    except Exception:
        return 1500.0


_SV_UI_FLOAT_KEYS = {
    "xs_spacing_m": (50.0, 20000.0),
    "dt_hours": (1.0 / 60.0, 24.0),
    "cfl": (0.05, 10.0),
    "dt_hydro_max_s": (1.0, 3600.0),
    "theta": (0.5, 1.0),
    "n_picard": (1.0, 10.0),
    "convective": (0.0, 1.0),
    "manning_blend": (0.0, 1.0),
    "q_relax": (0.1, 1.0),
    "n_mains": (1.0, 12.0),
    "n_tribs": (0.0, 24.0),
}


def _finite_sv_ui(raw: Any, default: float, lo: float, hi: float) -> float:
    if raw is None or str(raw).strip() == "":
        return float(default)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(v):
        return float(default)
    return float(min(hi, max(lo, v)))


def _fmt_sv_ui(key: str, value: Any) -> str:
    if key == "water_source":
        return parse_hydro1d_source(value)
    if key in ("n_picard", "n_mains", "n_tribs"):
        return str(int(round(float(value))))
    v = float(value)
    if key in ("xs_spacing_m", "dt_hydro_max_s"):
        return f"{v:.4f}"
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def load_sv_ui_params() -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not csv_available(SAINT_VENANT_PARAMS_CSV):
        return out
    with csv_open(SAINT_VENANT_PARAMS_CSV) as f:
        for row in csv.DictReader(f):
            key = str(row.get("param") or "").strip()
            raw = row.get("value")
            if not key or raw is None or str(raw).strip() == "":
                continue
            if key == "water_source":
                out[key] = parse_hydro1d_source(raw)
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v):
                continue
            if key == "xs_spacing_m" and v >= 50.0:
                out[key] = v
            elif key == "dt_hours" and v > 0.0:
                out[key] = min(24.0, max(1.0 / 60.0, v))
            elif key in _SV_UI_FLOAT_KEYS and key not in ("xs_spacing_m", "dt_hours"):
                lo, hi = _SV_UI_FLOAT_KEYS[key]
                out[key] = min(hi, max(lo, v))
    return out


def parse_sv_hd_params(data: dict[str, Any] | None, src: Any) -> dict[str, Any]:
    kind = parse_hydro1d_source(src)
    saved = load_sv_ui_params()
    data = data or {}

    def raw(key: str, *alts: str) -> Any:
        for name in (key,) + alts:
            if name in data and data[name] is not None and str(data[name]).strip() != "":
                return data[name]
        return saved.get(key)

    cfl_default = 1.0 if kind == "saint-venant-1d" else 0.45
    cfl_raw = raw("cfl")
    # 0.45 la mac dinh Saint-venant hien; sv-mike-by-dhi.py khong --cfl dung 1.0.
    if kind == "saint-venant-1d" and cfl_raw is not None:
        try:
            if abs(float(cfl_raw) - 0.45) < 1e-6:
                cfl_raw = None
        except (TypeError, ValueError):
            cfl_raw = None
    return {
        "water_source": kind,
        "cfl": _finite_sv_ui(cfl_raw, cfl_default, 0.05, 10.0),
        "dt_hydro_max_s": _finite_sv_ui(
            raw("dt_hydro_max_s", "dt_hydro"), 60.0, 1.0, 3600.0
        ),
        "theta": _finite_sv_ui(raw("theta"), 1.0, 0.5, 1.0),
        "n_picard": int(round(_finite_sv_ui(raw("n_picard", "picard"), 3.0, 1.0, 10.0))),
        "convective": _finite_sv_ui(raw("convective"), 0.15, 0.0, 1.0),
        "manning_blend": _finite_sv_ui(raw("manning_blend"), 0.25, 0.0, 1.0),
        "q_relax": _finite_sv_ui(raw("q_relax"), 0.35, 0.1, 1.0),
        "n_mains": int(round(_finite_sv_ui(raw("n_mains", "n_main"), 1.0, 1.0, 12.0))),
        "n_tribs": int(round(_finite_sv_ui(raw("n_tribs", "n_trib", "max_tribs"), 1.0, 0.0, 24.0))),
    }


def current_sv_hd_params(src: Any = None) -> dict[str, Any]:
    saved = load_sv_ui_params()
    kind = parse_hydro1d_source(src if src is not None else saved.get("water_source"))
    return parse_sv_hd_params(saved, kind)


def save_sv_ui_params(
    xs_spacing_m: float,
    dt_hours: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    xs_m = parse_request_xs_spacing_m(xs_spacing_m)
    dt_h = parse_request_dt_hours(dt_hours)
    merged: dict[str, Any] = dict(load_sv_ui_params())
    merged["xs_spacing_m"] = xs_m
    merged["dt_hours"] = dt_h
    if extra:
        hd = parse_sv_hd_params(extra, extra.get("water_source", merged.get("water_source")))
        merged.update(hd)
    order = [
        "xs_spacing_m",
        "dt_hours",
        "n_mains",
        "n_tribs",
        "cfl",
        "dt_hydro_max_s",
        "theta",
        "n_picard",
        "convective",
        "manning_blend",
        "q_relax",
        "water_source",
    ]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for key in order:
        if key not in merged:
            continue
        seen.add(key)
        rows.append({"param": key, "value": _fmt_sv_ui(key, merged[key])})
    for key, value in merged.items():
        if key in seen:
            continue
        rows.append({"param": key, "value": str(value)})
    write_csv_rows(
        SAINT_VENANT_PARAMS_CSV,
        ["param", "value"],
        rows,
        rebuild_hydro=False,
    )
    out: dict[str, Any] = {"xs_spacing_m": xs_m, "dt_hours": dt_h}
    out.update(current_sv_hd_params(merged.get("water_source")))
    out["xs_spacing_m"] = xs_m
    out["dt_hours"] = dt_h
    return out


def current_dt_hours() -> float:
    saved = load_sv_ui_params()
    if saved.get("dt_hours") is not None:
        return float(saved["dt_hours"])
    hours: list[float] = []
    if csv_available(SAINT_VENANT_H_CSV):
        with csv_open(SAINT_VENANT_H_CSV) as f:
            for row in csv.DictReader(f):
                try:
                    hours.append(float(row["hour"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if len(hours) >= 24:
                    break
    diffs = [hours[i + 1] - hours[i] for i in range(len(hours) - 1) if hours[i + 1] > hours[i]]
    if diffs:
        return parse_request_dt_hours(float(np.median(np.asarray(diffs, dtype=float))))
    return 1.0


def parse_request_dt_hours(raw: Any) -> float:
    sv = _saint_venant_mod()
    parse = sv.get("parse_dt_hours")
    default = 1.0
    if raw is None or raw == "":
        return current_dt_hours()
    if parse is None:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v) or v <= 0:
            return default
        return min(24.0, max(1.0 / 60.0, v))
    return float(parse(raw, default))


def parse_request_xs_spacing_m(raw: Any) -> float:
    sv = _saint_venant_mod()
    parse = sv.get("parse_xs_spacing_m")
    default = float(sv.get("XS_SPACING_M") or 1500.0)
    if raw is None or raw == "":
        return current_xs_spacing_m()
    if parse is None:
        v = float(raw)
        return max(50.0, v * 1000.0 if v < 50.0 else v)
    return float(parse(raw, default))


def parse_n_mains(raw: Any, default: int = 1) -> int:
    if raw is None or str(raw).strip() == "":
        saved = load_sv_ui_params().get("n_mains")
        raw = saved if saved is not None else default
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        v = int(default)
    return max(1, min(12, v))


def parse_n_tribs(raw: Any, default: int = 1) -> int:
    if raw is None or str(raw).strip() == "":
        saved = load_sv_ui_params().get("n_tribs")
        raw = saved if saved is not None else default
    try:
        v = int(round(float(raw)))
    except (TypeError, ValueError):
        v = int(default)
    return max(0, min(24, v))


def save_manning_n_rows(raw_rows: Sequence[Any]) -> list[dict[str, Any]]:
    if not raw_rows:
        raise ValueError("Can it nhat 1 mat cat he so nham n.")
    existing, _src = load_manning_n_rows()
    station_by_key = {
        (_reach_key(r.get("reach")), int(r["xs_id"])): r.get("station_km")
        for r in existing
        if r.get("station_km") is not None
    }
    main_out: list[dict[str, Any]] = []
    trib_out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        try:
            raw_xs_id = item.get("xs_id")
            raw_n = item.get("manning_n")
            if raw_xs_id is None or raw_n is None:
                continue
            xs_id = int(float(raw_xs_id))
            n = float(raw_n)
        except (TypeError, ValueError):
            raise ValueError("Hang Manning n can xs_id va manning_n so.")
        if xs_id <= 0:
            raise ValueError(f"xs_id khong hop le: {xs_id}")
        reach = _reach_key(item.get("reach"))
        key = (reach, xs_id)
        if key in seen:
            raise ValueError(f"Trung mat cat {reach} XS{xs_id}.")
        if not math.isfinite(n) or n < MANNING_N_MIN or n > MANNING_N_MAX:
            raise ValueError(
                f"n tai {reach} XS{xs_id} phai trong khoang {MANNING_N_MIN:g}–{MANNING_N_MAX:g}."
            )
        station_km = item.get("station_km", station_by_key.get(key))
        try:
            st = float(station_km) if station_km is not None and str(station_km).strip() != "" else None
        except (TypeError, ValueError):
            st = station_by_key.get(key)
        row: dict[str, Any] = {
            "reach": reach,
            "xs_id": xs_id,
            "manning_n": round(n, 4),
        }
        if item.get("name"):
            row["name"] = str(item.get("name")).strip()
        _annotate_reach(row)
        if st is not None and math.isfinite(float(st)):
            row["station_km"] = round(float(st), 4)
        if item.get("x") is not None:
            try:
                row["x"] = round(float(item["x"]), 6)
            except (TypeError, ValueError):
                pass
        if item.get("y") is not None:
            try:
                row["y"] = round(float(item["y"]), 6)
            except (TypeError, ValueError):
                pass
        if _reach_kind(reach) == "main":
            main_out.append(row)
        else:
            trib_out.append(row)
        seen.add(key)
    if not main_out and not trib_out:
        raise ValueError("Khong co hang Manning n hop le.")
    main_out.sort(key=lambda r: (_reach_sort_key(r.get("reach")), int(r["xs_id"])))
    trib_out.sort(key=lambda r: (_reach_sort_key(r.get("reach")), int(r["xs_id"])))
    if main_out:
        write_csv_rows(
            SAINT_VENANT_N_CSV,
            ["reach_id", "xs_id", "station_km", "manning_n"],
            [
                {
                    "reach_id": _reach_key(row.get("reach")),
                    "xs_id": row["xs_id"],
                    "station_km": "" if row.get("station_km") is None else f"{row['station_km']:.4f}",
                    "manning_n": f"{row['manning_n']:.4f}",
                }
                for row in main_out
            ],
            rebuild_hydro=False,
        )
    if trib_out:
        _write_trib_manning_rows(trib_out)
    out = _attach_xs_xy(main_out) + trib_out
    out.sort(key=lambda r: (_reach_sort_key(r.get("reach")), int(r["xs_id"])))
    return out


def extract_and_save_cross_sections(
    xs_spacing_m: float,
    water_source: Any = None,
    file_id: str | None = None,
    dem_path: str | None = None,
    n_mains: Any = None,
    n_tribs: Any = None,
) -> dict[str, Any]:
    """Trich mat cat DEM theo khoang XS, ghi geometry + n vao data_flood."""
    from types import SimpleNamespace

    import numpy as np

    sv = _saint_venant_mod()
    kind = parse_hydro1d_source(water_source)
    xs_m = parse_request_xs_spacing_m(xs_spacing_m)
    n_main_default = int(sv.get("DEFAULT_MAX_MAINS", 8))
    n_trib_default = int(sv.get("DEFAULT_MAX_TRIBS", 12))
    if n_mains is None or str(n_mains).strip() == "":
        n_main_want = n_main_default
    else:
        n_main_want = parse_n_mains(n_mains, default=n_main_default)
    if n_tribs is None or str(n_tribs).strip() == "":
        n_trib_want = n_trib_default
    else:
        n_trib_want = parse_n_tribs(n_tribs, default=n_trib_default)
    par = sv["SaintVenantParams"](xs_spacing_m=xs_m)
    dem = resolve_dem_path(file_id, dem_path)
    clear_network_cache()
    n_csv = SAINT_VENANT_N_CSV if csv_available(SAINT_VENANT_N_CSV) else None
    geom, extra_mains, trib_specs = sv["extract_model_network"](
        dem,
        par,
        max_mains=n_main_want,
        max_tribs=n_trib_want,
        water_source=kind,
        n_default=float(par.manning_n),
        n_csv=n_csv,
    )
    if geom is None or not getattr(geom, "sections", None):
        raise ValueError("DEM khong cho duoc mat cat.")

    trib_ns = [
        SimpleNamespace(
            reach_id=str(spec["reach_id"]),
            geom=spec["geom"],
            join_station_m=float(spec["join_station_m"]),
            join_xs=int(spec["join_xs"]),
            kind=str(spec.get("kind") or "outlet"),
            bc=str(spec.get("bc") or "H"),
        )
        for spec in trib_specs
    ]
    res_like = SimpleNamespace(tribs=trib_ns)
    main_items = [("main", geom)] + [
        (str(spec["reach_id"]), spec["geom"]) for spec in extra_mains
    ]
    for src in ("saint-venant", "saint-venant-1d"):
        paths = hydro1d_csv_paths(src)
        sv["write_geometries_csv"](main_items, par, Path(paths["geom"]))
        sv["write_cross_sections_csv"](geom, Path(paths["xs"]))
        sv["write_tributary_geometry_csv"](res_like, Path(paths["trib_geom"]))

    sv["write_manning_network_csv"](main_items, SAINT_VENANT_N_CSV)
    trib_n_rows = []
    for item in trib_ns:
        for i, sec in enumerate(item.geom.sections):
            n_val = getattr(sec, "manning_n", None)
            try:
                n_num = float(n_val) if n_val is not None else float(par.manning_n)
            except (TypeError, ValueError):
                n_num = float(par.manning_n)
            trib_n_rows.append(
                {
                    "reach": item.reach_id,
                    "xs_id": i + 1,
                    "station_km": float(sec.station_m) / 1000.0,
                    "manning_n": n_num,
                }
            )
    _write_trib_manning_rows(trib_n_rows)
    ui = save_sv_ui_params(
        xs_m,
        current_dt_hours(),
        extra={"water_source": kind, "n_mains": n_main_want, "n_tribs": n_trib_want},
    )
    rows, n_src = load_manning_n_rows()
    geom_path = Path(hydro1d_csv_paths(kind)["geom"])
    stats = _reach_stats(rows)
    return {
        "ok": True,
        **stats,
        "length_km": round(float(geom.length_m) / 1000.0, 3),
        "xs_spacing_m": float(ui["xs_spacing_m"]),
        "dt_hours": float(ui["dt_hours"]),
        "water_source": kind,
        "rows": rows,
        "n_path": str(n_src),
        **dataset_write_meta(geom_path),
        "n_table": dataset_write_meta(SAINT_VENANT_N_CSV)["table"],
        "cfl": ui.get("cfl"),
        "dt_hydro_max_s": ui.get("dt_hydro_max_s"),
        "theta": ui.get("theta"),
        "n_picard": ui.get("n_picard"),
        "convective": ui.get("convective"),
        "manning_blend": ui.get("manning_blend"),
        "q_relax": ui.get("q_relax"),
        "n_mains": int(ui.get("n_mains") or n_main_want),
        "n_tribs": int(ui.get("n_tribs") or n_trib_want),
    }


MANNING_N_DEM_CSV = ROOT / "saint_venant_output" / "demo_manning_n_dem_init.csv"


def _manning_dem_script() -> Path:
    script = SCRIPT_DIR / "mainning.py"
    if not script.is_file():
        raise FileNotFoundError("Khong thay mainning.py")
    return script


def _manning_geom_csv(water_source: Any = None) -> Path:
    geom = Path(hydro1d_csv_paths(water_source)["geom"])
    if csv_available(geom):
        return geom
    if csv_available(SAINT_VENANT_GEOM_CSV):
        return SAINT_VENANT_GEOM_CSV
    raise FileNotFoundError(
        "Chua co toa do mat cat (mike_hd_river_geometry.csv / demo_river_geometry.csv). "
        "Hay chay Model 1D mot lan de trich mat cat, roi lay he so nham tu DEM."
    )


def _estimate_trib_manning_from_dem(dem: Path, water_source: Any = None) -> str:
    paths = _trib_geom_paths(water_source)
    if not paths:
        return ""
    import runpy

    mn = runpy.run_path(str(_manning_dem_script()), run_name="mainning_trib")
    geom_rows: list[dict[str, Any]] = []
    for path in paths:
        with csv_open(path) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            fields = {name.strip().lower(): name for name in reader.fieldnames}
            id_col = fields.get("xs_id") or fields.get("id")
            r_col = fields.get("reach_id") or fields.get("reach")
            st_col = fields.get("station_km")
            x_col = fields.get("lon") or fields.get("x")
            y_col = fields.get("lat") or fields.get("y")
            w_col = fields.get("top_width_at_3m") or fields.get("width_m")
            if not id_col or not x_col or not y_col:
                continue
            for i, row in enumerate(reader):
                try:
                    xs_id = int(float(row[id_col])) if str(row.get(id_col, "")).strip() else i + 1
                    lon = float(row[x_col])
                    lat = float(row[y_col])
                    st = float(row[st_col]) if st_col and str(row.get(st_col, "")).strip() else float(i)
                except (TypeError, ValueError, KeyError):
                    continue
                width = float("nan")
                if w_col:
                    try:
                        width = float(row[w_col])
                    except (TypeError, ValueError, KeyError):
                        width = float("nan")
                reach = _reach_key(row.get(r_col) if r_col else "trib_1")
                if reach == "main":
                    reach = "trib_1"
                geom_rows.append(
                    {
                        "reach_id": reach,
                        "xs_id": xs_id,
                        "station_km": st,
                        "lon": lon,
                        "lat": lat,
                        "width_m": width,
                        "index": len(geom_rows),
                    }
                )
        if geom_rows:
            break
    if not geom_rows:
        return ""
    _append_sim_log(f"Uoc n song nhanh: {len(geom_rows)} mat cat")
    est = mn["estimate"](
        geom_rows,
        dem,
        radius_m=None,
        n_min=float(mn.get("N_MIN", 0.025)),
        n_max=float(mn.get("N_MAX", 0.080)),
        sigma_lo=float(mn.get("SIGMA_LO", 0.25)),
        sigma_hi=float(mn.get("SIGMA_HI", 2.80)),
        smooth_km=0.75,
    )
    trib_n = []
    for rec in est:
        trib_n.append(
            {
                "reach": rec.get("reach_id") or "trib_1",
                "xs_id": rec["xs_id"],
                "station_km": rec.get("station_km"),
                "manning_n": rec["manning_n"],
            }
        )
    _write_trib_manning_rows(trib_n)
    return f"Nhanh: {len(trib_n)} mat cat ({SAINT_VENANT_TRIB_N_CSV.name})."


def _run_manning_dem_job(dem: Path, water_source: Any = None) -> None:
    t0 = time.time()
    label = "He so nham tu DEM"
    try:
        script = _manning_dem_script()
        geom = _manning_geom_csv(water_source)
        cmd = [
            sys.executable,
            "-u",
            str(script),
            "--no-plot",
            "--write-input",
            "--dem",
            str(dem),
            "--geom",
            str(geom),
            "--out",
            str(MANNING_N_DEM_CSV),
        ]
        with _SIM_LOCK:
            _SIM_JOB["message"] = f"Dang chay {label}..."
        _append_sim_log(f"Chay {label}: {script.name}  --geom {geom.name}  --write-input")
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=_sim_subprocess_env(),
        )
        lines: list[str] = []
        if proc.stdout is not None:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                _append_sim_log(line)
        rc = proc.wait()
        tail = "\n".join([ln for ln in lines if ln.strip()][-8:])
        if rc != 0:
            raise RuntimeError(tail or f"Thoat ma {rc}")
        if not csv_available(SAINT_VENANT_N_CSV):
            raise FileNotFoundError(f"Khong ghi duoc {SAINT_VENANT_N_CSV.name}")
        trib_note = _estimate_trib_manning_from_dem(dem, water_source)
        with _SIM_LOCK:
            _SIM_JOB["status"] = "ok"
            _SIM_JOB["error"] = None
            _SIM_JOB["result_csv"] = str(SAINT_VENANT_N_CSV)
            _SIM_JOB["elapsed_s"] = time.time() - t0
            done = f"Xong {label} ({_SIM_JOB['elapsed_s']:.0f} s). Da luu {SAINT_VENANT_N_CSV.name}."
            if trib_note:
                done += " " + trib_note
            _SIM_JOB["message"] = done
            _SIM_JOB["log_tail"] = tail or None
        _append_sim_log(done)
    except Exception as exc:
        traceback.print_exc()
        with _SIM_LOCK:
            _SIM_JOB["status"] = "error"
            _SIM_JOB["error"] = str(exc)
            _SIM_JOB["elapsed_s"] = time.time() - t0
            _SIM_JOB["message"] = f"Loi he so nham DEM: {exc}"
        _append_sim_log(_SIM_JOB["message"])


def start_manning_dem(dem: Path, water_source: Any = None) -> tuple[bool, dict[str, Any]]:
    geom = _manning_geom_csv(water_source)
    _manning_dem_script()
    with _SIM_LOCK:
        already = _SIM_JOB.get("status") == "running"
        if not already:
            _SIM_JOB.update(
                {
                    "status": "running",
                    "kind": "manning-dem",
                    "water_source": parse_hydro1d_source(water_source),
                    "label": "He so nham tu DEM",
                    "message": f"Dang uoc n tu DEM quanh mat cat ({geom.name})...",
                    "error": None,
                    "result_csv": None,
                    "started_at": time.time(),
                    "elapsed_s": 0.0,
                    "log": [],
                    "log_tail": None,
                }
            )
    if already:
        snap = _sim_snapshot()
        running = snap.get("label") or "mo phong khac"
        snap["error"] = (
            f"Dang chay {running}. Cho xong roi lay he so nham tu DEM."
        )
        return False, snap
    threading.Thread(
        target=_run_manning_dem_job,
        args=(dem, water_source),
        daemon=True,
    ).start()
    return True, _sim_snapshot()


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def _place_from_nominatim(data: dict[str, Any]) -> str:
    addr = cast(
        dict[str, Any],
        data.get("address") if isinstance(data.get("address"), dict) else {},
    )
    parts: list[str] = []
    name = str(data.get("name") or "").strip()
    if name:
        parts.append(name)
    for key in (
        "road",
        "neighbourhood",
        "suburb",
        "quarter",
        "village",
        "town",
        "city_district",
        "city",
        "county",
        "state",
    ):
        val = str(addr.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)
    if parts:
        return ", ".join(parts)
    return str(data.get("display_name") or "").strip()


def _nearest_boundary_station(lon: float, lat: float, max_m: float = 800.0) -> dict[str, Any] | None:
    try:
        payload = load_boundary_station_payload()
        rows = payload.get("rows") if isinstance(payload, dict) else None
    except Exception:
        rows = None
    if not isinstance(rows, list):
        return None
    best: dict[str, Any] | None = None
    best_d = max_m
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        try:
            raw_lon = rec.get("lon")
            raw_lat = rec.get("lat")
            if raw_lon is None or raw_lat is None:
                continue
            slon = float(raw_lon)
            slat = float(raw_lat)
        except (TypeError, ValueError):
            continue
        dist = _haversine_m(lon, lat, slon, slat)
        if dist <= best_d:
            best_d = dist
            label = str(rec.get("name") or rec.get("id") or "").strip()
            best = {
                "id": rec.get("id") or "",
                "name": label,
                "kind": rec.get("kind") or "",
                "distance_m": round(dist, 1),
            }
    return best


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_json(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "flood_model/1.0 (local flood modeling; reverse geocode)",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _place_from_photon(data: dict[str, Any]) -> str:
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list) or not feats:
        return ""
    props = feats[0].get("properties") if isinstance(feats[0], dict) else None
    if not isinstance(props, dict):
        return ""
    parts: list[str] = []
    for key in ("name", "street", "district", "city", "county", "state", "country"):
        val = str(props.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)
    return ", ".join(parts)


def _place_from_bigdatacloud(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for key in (
        "locality",
        "localityInfo",
        "city",
        "principalSubdivision",
        "countryName",
    ):
        val = data.get(key)
        if key == "localityInfo" and isinstance(val, dict):
            admin = val.get("administrative")
            if isinstance(admin, list):
                for item in admin[:4]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if name and name not in parts:
                        parts.append(name)
            continue
        text = str(val or "").strip()
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts)


def reverse_geocode_point(lon: float, lat: float) -> dict[str, Any]:
    """Tra dia danh tu lon/lat: Photon / BigDataCloud / Nominatim + tram bien gan."""
    out: dict[str, Any] = {
        "ok": True,
        "lon": round(float(lon), 6),
        "lat": round(float(lat), 6),
        "place": "",
        "display_name": "",
        "station": None,
        "source": "",
    }
    near = _nearest_boundary_station(out["lon"], out["lat"])
    if near:
        out["station"] = near
    lon_s = f"{out['lon']:.6f}"
    lat_s = f"{out['lat']:.6f}"
    lookups = (
        (
            "photon",
            f"https://photon.komoot.io/reverse?lon={lon_s}&lat={lat_s}",
            _place_from_photon,
        ),
        (
            "bigdatacloud",
            (
                "https://api.bigdatacloud.net/data/reverse-geocode-client"
                f"?latitude={lat_s}&longitude={lon_s}&localityLanguage=vi"
            ),
            _place_from_bigdatacloud,
        ),
        (
            "nominatim",
            "https://nominatim.openstreetmap.org/reverse?"
            + urllib.parse.urlencode(
                {
                    "lat": lat_s,
                    "lon": lon_s,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "accept-language": "vi",
                    "zoom": 16,
                }
            ),
            _place_from_nominatim,
        ),
    )
    for source, url, parse in lookups:
        try:
            raw = _http_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        place = parse(raw) if isinstance(raw, dict) else ""
        if place:
            out["place"] = place
            out["display_name"] = place
            out["source"] = source
            break
    if near and near.get("name"):
        dist = near.get("distance_m")
        extra = f"{near['name']} (cách {dist:.0f} m)"
        if not out["place"]:
            out["place"] = extra
            out["source"] = out["source"] or "station"
        elif extra not in out["place"]:
            out["place"] = f"{out['place']} · gần {extra}"
    if not out["place"]:
        out["place"] = "Không tra được địa danh"
        out["source"] = out["source"] or "offline"
    return out


def create_flow3d_blueprint(
    name: str = "flow3d",
    url_prefix: str = "/api/flow-3d",
) -> Blueprint:
    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    def _fail(message: object, status: int = 500):
        return jsonify({"ok": False, "error": str(message)}), status

    @bp.route("/", methods=["GET"])
    @bp.route("", methods=["GET"])
    def api_ping():
        return jsonify({
            "ok": True,
            "service": "flow-3d",
            "routes": [
                "/api/flow-3d/runoff",
                "/api/flow-3d/profile",
                "/api/flow-3d/thalweg",
                "/api/flow-3d/simulate",
                "/api/flow-3d/simulate-rr",
                "/api/flow-3d/runoff-params",
                "/api/flow-3d/runoff-input",
                "/api/flow-3d/simulate-nam",
                "/api/flow-3d/nam-params",
                "/api/flow-3d/nam-input",
                "/api/flow-3d/manning-n",
                "/api/flow-3d/extract-xs",
                "/api/flow-3d/manning-n-dem",
                "/api/flow-3d/xs-hydrograph",
                "/api/flow-3d/xs-profile",
                "/api/flow-3d/xs-obs",
                "/api/flow-3d/sv-input",
                "/api/flow-3d/reverse-geocode",
                "/api/flow-3d/place",
                "/api/flow-3d/boundary-stations",
                "/api/flow-3d/constructions",
                "/api/flow-3d/network-overlay",
                "/api/flow-3d/browse-data",
                "/api/flow-3d/csv-columns",
                "/api/flow-3d/db",
                "/api/flow-3d/timeseries",
                "/api/flow-3d/dataset",
            ],
            "water_sources": hydro1d_water_sources(),
        })

    @bp.errorhandler(404)
    def _bp_404(_err):
        return _fail("Khong tim thay API mat cat.", 404)

    @bp.errorhandler(405)
    def _bp_405(_err):
        return _fail("Phuong thuc khong hop le. Can POST JSON.", 405)

    @bp.errorhandler(Exception)
    def _bp_exc(err):
        traceback.print_exc()
        return _fail(f"Loi mat cat: {err}", 500)

    @bp.route("/runoff", methods=["GET"])
    def api_runoff():
        try:
            return jsonify(_json_runoff_only())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/extract-xs", methods=["POST"])
    def api_extract_xs():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong cat mat cat luc nay.", 409)
            data = request.get_json(silent=True) or {}
            spacing_raw = data.get("xs_spacing_m", data.get("xs_spacing"))
            payload = extract_and_save_cross_sections(
                cast(float, spacing_raw if spacing_raw is not None else current_xs_spacing_m()),
                data.get("water_source"),
                data.get("file_id"),
                data.get("dem_path"),
                data.get("n_mains", data.get("n_main")),
                data.get("n_tribs", data.get("n_trib", data.get("max_tribs"))),
            )
            return jsonify(payload)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi trich mat cat: {exc}", 500)

    @bp.route("/manning-n-dem", methods=["POST"])
    def api_manning_n_dem_start():
        try:
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            started, job = start_manning_dem(dem, data.get("water_source"))
            code = 202 if started else 409
            job["started"] = started
            return jsonify(job), code
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi he so nham DEM: {exc}", 500)

    @bp.route("/runoff-params", methods=["GET"])
    def api_runoff_params_get():
        try:
            return jsonify(runoff_params_payload())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/runoff-params", methods=["PUT", "POST"])
    def api_runoff_params_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua thong so luc nay.", 409)
            data = request.get_json(silent=True) or {}
            values = data.get("values") if isinstance(data.get("values"), dict) else data
            if not isinstance(values, dict):
                values = {}
            saved = save_runoff_params(cast(dict[str, Any], values))
            return jsonify({
                "ok": True,
                **dataset_write_meta(TANK_PARAMS_CSV),
                "values": saved,
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/runoff-input", methods=["GET"])
    @bp.route("/nam-input", methods=["GET"])
    def api_runoff_input_get():
        try:
            return jsonify(load_runoff_input_payload())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/runoff-input", methods=["PUT", "POST"])
    @bp.route("/nam-input", methods=["PUT", "POST"])
    def api_runoff_input_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua du lieu luc nay.", 409)
            data = request.get_json(silent=True) or {}
            rows = data.get("rows")
            if not isinstance(rows, list):
                return jsonify(load_runoff_input_payload())
            saved = save_runoff_input_rows(rows)
            return jsonify(saved)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/sv-input", methods=["GET"])
    def api_sv_input_get():
        try:
            return jsonify(load_sv_boundary_payload())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/sv-input", methods=["PUT", "POST"])
    def api_sv_input_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua du lieu luc nay.", 409)
            data = request.get_json(silent=True) or {}
            rows = data.get("rows")
            if not isinstance(rows, list):
                return jsonify(load_sv_boundary_payload())
            saved = save_sv_boundary_rows(rows)
            return jsonify(saved)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/boundary-stations", methods=["GET"])
    def api_boundary_stations_get():
        try:
            return jsonify(load_boundary_station_payload())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/boundary-stations", methods=["PUT", "POST"])
    def api_boundary_stations_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua du lieu luc nay.", 409)
            data = request.get_json(silent=True) or {}
            if data.get("reset"):
                from flood_model.boundary import default_boundary_stations

                save_boundary_stations(default_boundary_stations())
                return jsonify(load_boundary_station_payload())
            rows = data.get("rows")
            if not isinstance(rows, list):
                return jsonify(load_boundary_station_payload())
            save_boundary_stations(rows)
            return jsonify(load_boundary_station_payload())
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/constructions", methods=["GET"])
    def api_constructions_get():
        try:
            payload = load_constructions_payload()
            apply_weir_dem_beds(payload.get("rows") or [])
            return jsonify(payload)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/weir-bed", methods=["GET"])
    def api_weir_bed():
        try:
            station = request.args.get("station_km")
            reach = request.args.get("reach") or "main"
            bed = None
            dem_cm = None
            try:
                dem_cm = _open_dem(resolve_dem_path("dem", None))
                dem_src = dem_cm.__enter__()
                manning_rows, width_z, official, reach_pts = _structure_section_context(
                    request.args.get("water_source")
                )
                bed = weir_section_min_dem_m(
                    {"type": "weir", "reach": reach, "station_km": station, "width_m": request.args.get("width_m")},
                    dem_src=dem_src,
                    official=official,
                    manning_rows=manning_rows,
                    reach_pts=reach_pts,
                    width_z=width_z,
                )
            finally:
                if dem_cm is not None:
                    dem_cm.__exit__(None, None, None)
            return jsonify({
                "ok": True,
                "invert_m": _fmt_bed_m(bed) if bed is not None else None,
            })
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/network-overlay", methods=["GET"])
    def api_network_overlay():
        try:
            return jsonify(map_network_overlay_payload(
                request.args.get("water_source") or request.args.get("src")
            ))
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/constructions", methods=["PUT", "POST"])
    def api_constructions_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua cong trinh luc nay.", 409)
            data = request.get_json(silent=True) or {}
            if data.get("reset"):
                save_constructions(default_constructions())
                payload = load_constructions_payload()
                apply_weir_dem_beds(payload.get("rows") or [])
                return jsonify(payload)
            if data.get("clear"):
                save_constructions([])
                return jsonify(load_constructions_payload())
            rows = data.get("rows")
            if not isinstance(rows, list):
                payload = load_constructions_payload()
                apply_weir_dem_beds(payload.get("rows") or [])
                return jsonify(payload)
            apply_weir_dem_beds(rows)
            save_constructions(rows)
            payload = load_constructions_payload()
            apply_weir_dem_beds(payload.get("rows") or [])
            return jsonify(payload)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/browse-data", methods=["GET"])
    def api_browse_data():
        try:
            return jsonify(browse_data_dir(request.args.get("path") or request.args.get("dir") or ""))
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/csv-columns", methods=["GET"])
    def api_csv_columns():
        try:
            rel = request.args.get("path") or request.args.get("file") or ""
            return jsonify(csv_columns_for_path(rel))
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    def _parse_lat_lon() -> tuple[float, float]:
        data = request.get_json(silent=True) if request.method == "POST" else None
        if not isinstance(data, dict):
            data = {}
        raw_lat = cast(str, request.args.get("lat", data.get("lat", data.get("latitude"))) or "")
        raw_lon = cast(str, request.args.get("lon", data.get("lon", data.get("longitude", data.get("lng")))) or "")
        try:
            lat = float(raw_lat)
            lon = float(raw_lon)
        except (TypeError, ValueError):
            raise ValueError("Cần lat và lon hợp lệ.")
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError("lat/lon nằm ngoài phạm vi.")
        return lat, lon

    @bp.route("/reverse-geocode", methods=["GET", "POST"])
    @bp.route("/place", methods=["GET", "POST"])
    def api_reverse_geocode():
        try:
            lat, lon = _parse_lat_lon()
            payload = reverse_geocode_point(lon, lat)
            body = {
                "ok": True,
                "lat": payload.get("lat"),
                "lon": payload.get("lon"),
                "place": payload.get("place") or "",
                "display_name": payload.get("display_name") or payload.get("place") or "",
                "source": payload.get("source") or "",
                "station": payload.get("station"),
            }
            text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
            return Response(
                text.encode("utf-8"),
                mimetype="application/json; charset=utf-8",
            )
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/nam-params", methods=["GET"])
    def api_nam_params_get():
        try:
            return jsonify(nam_params_payload())
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/nam-params", methods=["PUT", "POST"])
    def api_nam_params_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua thong so luc nay.", 409)
            data = request.get_json(silent=True) or {}
            values = data.get("values") if isinstance(data.get("values"), dict) else data
            if not isinstance(values, dict):
                values = {}
            saved = save_nam_params(cast(dict[str, Any], values))
            return jsonify({
                "ok": True,
                **dataset_write_meta(NAM_PARAMS_CSV),
                "values": saved,
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/manning-n", methods=["GET"])
    def api_manning_n_get():
        try:
            rows, src = load_manning_n_rows()
            hd = current_sv_hd_params()
            return jsonify({
                "ok": True,
                "path": src,
                "exists": csv_available(Path(src)),
                "n_min": MANNING_N_MIN,
                "n_max": MANNING_N_MAX,
                "xs_spacing_m": current_xs_spacing_m(),
                "dt_hours": current_dt_hours(),
                "dt_min": 1.0 / 60.0,
                "dt_max": 24.0,
                "cfl": hd["cfl"],
                "dt_hydro_max_s": hd["dt_hydro_max_s"],
                "theta": hd["theta"],
                "n_picard": hd["n_picard"],
                "convective": hd["convective"],
                "manning_blend": hd["manning_blend"],
                "q_relax": hd["q_relax"],
                "n_mains": hd.get("n_mains", 1),
                "n_tribs": hd.get("n_tribs", 1),
                "water_source": hd["water_source"],
                "rows": rows,
                **_reach_stats(rows),
            })
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/xs-hydrograph", methods=["GET"])
    def api_xs_hydrograph():
        try:
            return jsonify(xs_hydrograph_payload(
                request.args.get("xs_id"),
                request.args.get("water_source"),
                request.args.get("reach") or request.args.get("reach_id"),
            ))
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/xs-profile", methods=["GET"])
    def api_xs_profile():
        try:
            return jsonify(xs_station_profile_payload(
                request.args.get("xs_id"),
                reach_id=request.args.get("reach") or request.args.get("reach_id"),
                water_source=request.args.get("water_source") or request.args.get("src"),
                file_id=request.args.get("file_id"),
                dem_path=request.args.get("dem_path"),
            ))
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/xs-obs", methods=["GET", "POST"])
    def api_xs_obs():
        try:
            if request.method == "POST":
                uploaded = request.files.get("file") or request.files.get("obs")
                if uploaded is None or not str(uploaded.filename or "").strip():
                    raise ValueError(
                        "Chọn file CSV thực đo (cột Gio/Time và ít nhất một cột Q hoặc H)."
                    )
                raw = uploaded.read()
                if not raw:
                    raise ValueError("File thực đo trống.")
                return jsonify(save_xs_obs_upload(raw, str(uploaded.filename)))
            return jsonify(load_xs_obs_payload(request.args.get("col") or request.args.get("column")))
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/manning-n", methods=["PUT", "POST"])
    def api_manning_n_put():
        try:
            with _SIM_LOCK:
                if _SIM_JOB.get("status") == "running":
                    return _fail("Dang mo phong, khong sua n luc nay.", 409)
            data = request.get_json(silent=True) or {}
            rows = data.get("rows")
            if not isinstance(rows, list):
                raise ValueError("Can JSON {rows: [{xs_id, manning_n}, ...]}.")
            saved = save_manning_n_rows(rows)
            xs_raw = data.get("xs_spacing_m", data.get("xs_spacing"))
            dt_raw = data.get("dt_hours", data.get("dt"))
            ui = save_sv_ui_params(
                parse_request_xs_spacing_m(xs_raw),
                parse_request_dt_hours(dt_raw),
                extra=data,
            )
            return jsonify({
                "ok": True,
                **dataset_write_meta(SAINT_VENANT_N_CSV),
                "params_path": str(SAINT_VENANT_PARAMS_CSV),
                "params_table": dataset_write_meta(SAINT_VENANT_PARAMS_CSV)["table"],
                "xs_spacing_m": ui["xs_spacing_m"],
                "dt_hours": ui["dt_hours"],
                "cfl": ui.get("cfl"),
                "dt_hydro_max_s": ui.get("dt_hydro_max_s"),
                "theta": ui.get("theta"),
                "n_picard": ui.get("n_picard"),
                "convective": ui.get("convective"),
                "manning_blend": ui.get("manning_blend"),
                "q_relax": ui.get("q_relax"),
                "water_source": ui.get("water_source"),
                "rows": saved,
                **_reach_stats(saved),
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/db", methods=["GET"])
    def api_flood_db_status():
        try:
            from flood_model.db import db_status, list_datasets

            status = db_status()
            status["datasets"] = list_datasets() if status.get("ok") else []
            return jsonify(status)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/timeseries", methods=["GET"])
    def api_hydro_timeseries_get():
        try:
            from flood_model.db import fetch_hydro_timeseries

            rows = fetch_hydro_timeseries()
            return jsonify({
                "ok": True,
                "n": len(rows),
                "rows": rows,
                "timeseries": [r.get("timeseries") for r in rows],
            })
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/timeseries", methods=["PUT", "POST"])
    def api_hydro_timeseries_put():
        try:
            from flood_model.db import fetch_hydro_timeseries, write_hydro_timeseries

            data = request.get_json(silent=True) or {}
            points = data.get("rows") or data.get("timeseries") or data.get("points")
            if not isinstance(points, list):
                raise ValueError("Can JSON {rows: [{hour, ts, rainfall_mm, pet_mm, et_mm, q_m3s, h_m}, ...]}.")
            n = write_hydro_timeseries(points)
            return jsonify({
                "ok": True,
                "n": n,
                "rows": fetch_hydro_timeseries(),
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/dataset", methods=["GET"])
    def api_dataset_list():
        try:
            from flood_model.db import list_datasets

            return jsonify({"ok": True, "datasets": list_datasets()})
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/dataset/<table>", methods=["GET"])
    def api_dataset_get(table: str):
        try:
            from flood_model.db import fetch_table_rows

            limit_raw = request.args.get("limit")
            limit = int(limit_raw) if limit_raw not in (None, "") else None
            columns, rows = fetch_table_rows(table, limit=limit)
            return jsonify({
                "ok": True,
                "table": table,
                "columns": columns,
                "n": len(rows),
                "rows": rows,
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/dataset/<table>", methods=["PUT", "POST"])
    def api_dataset_put(table: str):
        try:
            from flood_model.db import fetch_table_rows, write_dataset_rows

            data = request.get_json(silent=True) or {}
            rows = data.get("rows")
            if not isinstance(rows, list):
                raise ValueError("Can JSON {rows: [...]} trung cot CSV goc.")
            n = write_dataset_rows(table, rows)
            columns, saved = fetch_table_rows(table)
            return jsonify({
                "ok": True,
                "table": table,
                "n": n,
                "columns": columns,
                "rows": saved,
            })
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    @bp.route("/profile", methods=["POST"])
    def api_profile():
        try:
            data = request.get_json(silent=True) or {}
            lons, lats = _parse_coords(data)
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            kind = parse_hydro1d_source(data.get("water_source"))
            with _open_dem(dem) as src:
                # Giu dung mat cat ve tay (DEM); khong snap sang XS mac dinh he thong.
                prof = profile_from_lonlat(src, lons, lats)
            payload = attach_water(
                prof,
                prefer_cross_section=True,
                water_source=kind,
            )
            payload["dem_path"] = str(dem)
            payload["source"] = "drawn"
            payload["official_xs"] = False
            return jsonify(payload)
        except ValueError as exc:
            return _fail(exc, 400)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mat cat: {exc}", 500)

    @bp.route("/thalweg", methods=["POST"])
    def api_thalweg():
        try:
            clear_network_cache()
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            kind = parse_hydro1d_source(data.get("water_source"))
            with _open_dem(dem) as src:
                lons, lats, thalweg_src = main_centerline_lonlat(src, kind)
                prof = profile_from_lonlat(src, lons, lats)
                branches = _branch_overlay_payloads(src, kind)
            payload = attach_water(
                prof,
                prefer_cross_section=False,
                water_source=kind,
            )
            payload["dem_path"] = str(dem)
            payload["source"] = "thalweg"
            payload["thalweg_source"] = thalweg_src
            payload["branches"] = branches
            payload["n_branches"] = len(branches)
            payload["duong_track_ver"] = DUONG_TRACK_VER
            payload["draw_coordinates"] = [[round(lo, 6), round(la, 6)] for lo, la in zip(lons, lats)]
            return jsonify(payload)
        except ValueError as exc:
            return _fail(exc, 400)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi long song: {exc}", 500)

    @bp.route("/wse", methods=["POST"])
    def api_wse():
        try:
            data = request.get_json(silent=True) or {}
            lons, lats = _parse_coords(data)
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            t = int(data.get("time_index") or 0)
            kind = parse_hydro1d_source(data.get("water_source"))
            with _open_dem(dem) as src:
                prof = profile_from_lonlat(src, lons, lats)
                prof = snap_profile_to_official_xs(src, prof, kind)
            payload = attach_water(
                prof,
                prefer_cross_section=True,
                water_source=kind,
            )
            return jsonify(
                {
                    "ok": True,
                    "time_index": t,
                    "hour": payload["hours"][t] if payload["hours"] else 0,
                    "q_surface_mm": payload["q_surface_mm"][t] if payload["q_surface_mm"] else 0,
                    "h_display_m": payload["h_display_m"][t] if payload["h_display_m"] else 0,
                    "wse": wse_at_time(payload, t),
                    "z_dem": payload["z_dem"],
                    "distance_m": payload["distance_m"],
                }
            )
        except ValueError as exc:
            return _fail(exc, 400)
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except Exception as exc:
            traceback.print_exc()
            return _fail(exc, 500)

    return bp


def plot_profile_png(payload: dict[str, Any], path: Path, time_index: int | None = None) -> None:
    import matplotlib.pyplot as plt

    t = payload.get("initial_time_index", 0) if time_index is None else time_index
    t = max(0, min(int(t), len(payload["h_display_m"]) - 1))
    dist_km = np.array(payload["distance_m"], dtype=float) / 1000.0
    z = np.array(payload["z_dem"], dtype=float)
    wse = np.array(wse_at_time(payload, t), dtype=float)
    fig, ax = plt.subplots(figsize=(11, 4.2))
    ax.fill_between(dist_km, z, np.nanmin(z) - 2, color="#c4a574", alpha=0.85, label="Dia hinh DEM")
    ax.plot(dist_km, z, color="#6b4f2a", lw=1.2)
    wet = np.isfinite(wse) & np.isfinite(z) & (wse > z + 0.01)
    ax.plot(dist_km, np.where(wet, wse, np.nan), color="#1d4ed8", lw=1.6, label="Muc nuoc long chinh")
    ax.fill_between(dist_km, z, wse, where=wet, color="#3b82f6", alpha=0.5, interpolate=True)
    hour = payload["hours"][t]
    qs = payload["q_surface_mm"][t]
    ax.set_xlabel("Khoang cach (km)")
    ax.set_ylabel("Cao do (m)")
    ax.set_title(f"Mat cat doc — gio {hour:.0f}h, q_surface = {qs:.2f} mm")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def main() -> int:
    dem = resolve_dem_path()
    print(f"DEM: {dem}")
    with _open_dem(dem) as src:
        net = dem_river_network(src)
        lons, lats = net["main_lon"], net["main_lat"]
        prof = profile_from_lonlat(src, lons, lats)
        n_br = len(net["branches"])
    payload = attach_water(prof, prefer_cross_section=False)
    out = ROOT / "rainfall_runoff_output" / "flow3d_profile.png"
    plot_profile_png(payload, out)
    print(f"Long song {payload['length_m']/1000:.2f} km, {payload['n_station']} diem")
    print(f"Song nhanh   : {n_br}")
    print(f"z DEM {payload['zmin']:.2f} .. {payload['zmax']:.2f} m")
    print(f"Dinh q_surface gio {payload['hours'][payload['peak_time_index']]:.0f}")
    print(f"Bieu do: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

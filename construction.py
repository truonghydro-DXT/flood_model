"""Khai bao cong trinh thuy luc (MIKE 11 HD / MIKE Hydro River — DHI).

Loai + cach gan luoi Abbott (H/Q):
  - dike / levee     : de — lateral overbank (Villemonte), q_lat
  - weir / barrage   : dap/tran — inline tren Q-point (Villemonte / Honma)
  - reservoir / dam  : ho + tran xa — inline Q-point + kho chua
  - gate / sluice    : cong — inline Q-point (free/drowned)
  - culvert          : cong hop — inline Q-point
  - pump             : tram bom — lateral q_lat

Du lieu luu PostgreSQL data_flood qua csv_io (constructions.csv).
"""

from __future__ import annotations

import csv
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.boundary import (  # noqa: E402
    input_cell,
    read_series_csv,
)
from flood_model.csv_io import csv_available, csv_open, write_csv_rows  # noqa: E402
from flood_model.routing import normalize_reach_id  # noqa: E402

G = 9.81
# He so weir Villemonte mac dinh DHI (MIKE 11 / MIKE+): Q = W C H^k * factor.
WEIR_C_SI = 1.838
WEIR_EXP_K = 1.5
HONMA_C1 = 1.70
GATE_CD = 0.63  # Cc mac dinh sluice MIKE 11
CULVERT_CD = 0.60
VILLEMONTE_SUB_EXP = 0.385

# Bat buoc dung cong trinh trong solver 1D (co the tat bang STRUCTURES_IN_SOLVER=0).
STRUCTURES_IN_SOLVER = os.environ.get("STRUCTURES_IN_SOLVER", "1") != "0"

STRUCTURE_CSV = ROOT / "constructions.csv"
STRUCTURE_TYPES = (
    "dike",
    "weir",
    "reservoir",
    "gate",
    "culvert",
    "pump",
)
STRUCTURE_FORMULAS = ("villemonte", "honma", "honma_ext", "broad_crested")
STRUCTURE_PLACEMENTS = ("auto", "inline", "lateral")
STRUCTURE_VALVES = ("both", "positive", "negative")
STRUCTURE_TYPE_ALIASES = {
    "de": "dike",
    "đê": "dike",
    "levee": "dike",
    "dyke": "dike",
    "dap": "weir",
    "đập": "weir",
    "dap_dang": "weir",
    "đập_dâng": "weir",
    "barrage": "weir",
    "spillway": "weir",
    "tran": "weir",
    "tràn": "weir",
    "ho": "reservoir",
    "hồ": "reservoir",
    "ho_chua": "reservoir",
    "hồ_chứa": "reservoir",
    "dam": "reservoir",
    "cong": "gate",
    "cống": "gate",
    "sluice": "gate",
    "ong": "culvert",
    "ống": "culvert",
    "bom": "pump",
    "bơm": "pump",
}
STRUCTURE_COLS = [
    "id",
    "name",
    "type",
    "reach",
    "station_km",
    "xs_id",
    "lon",
    "lat",
    # Hinh hoc / thong so MIKE 11 HD
    "crest_m",          # cao trinh dinh de / nguong weir / tran xa (Hw)
    "width_m",          # be rong hieu dung W (m)
    "length_m",         # chieu dai de / ong (m)
    "height_m",         # chieu cao cua / duong kinh ong / weir height
    "gate_opening_m",   # do mo cua (m); trong neu mo theo file
    "invert_m",         # cao trinh day ong / nguong cong / weir invert
    "cd",               # he so xa C (trong = mac dinh theo loai)
    "submerged_exp",    # mu k Villemonte (mac dinh 1.5)
    "formula",          # villemonte | honma | honma_ext | broad_crested
    "placement",        # auto | inline | lateral  (MIKE: structure tren Q-point / lateral)
    "valve",            # both | positive | negative  (MIKE valve regulation)
    "q_max_m3s",        # chan Q max / Q bom
    "q_min_m3s",        # chan Q min
    "storage_area_m2",  # dien tich mat ho (xap xi kho chua tuyen tinh)
    "initial_level_m",  # muc nuoc ban dau (ho)
    "dam_crest_m",      # cao trinh dinh dap (ve 3D)
    "outlet_sill_m",    # cao trinh day cua xa (ho)
    "spillway_crest_m", # cao trinh dinh tran (ho)
    "gate_left_offset_m",  # khoang cach tu mep trai den cua xa dau (m)
    "gate_spacing_m",   # khoang cach giua cac cua xa (m)
    "outlet_gate_count",  # so cua xa (ho)
    "outlet_gate_widths_m",  # do rong tung cua xa, phan tach bang ';'
    "control",          # free | controlled | closed
    "upstream_reach",   # reach thuong luu (tuy chon)
    "downstream_reach", # reach ha luu (tuy chon)
    "file",             # CSV dieu khien (muc cua / Q bom / H ho)
    "value_col",        # cot gia tri trong file
    "unit",
    "note",
]


def normalize_structure_type(raw: Any) -> str:
    key = input_cell(raw).lower().replace(" ", "_").replace("-", "_")
    if key in STRUCTURE_TYPES:
        return key
    return STRUCTURE_TYPE_ALIASES.get(key, key or "weir")


def structure_type_label(stype: Any) -> str:
    labels = {
        "dike": "Đê / Tràn bãi",
        "weir": "Đập / Tràn",
        "reservoir": "Đập / Hồ chứa",
        "gate": "Cống điều tiết",
        "culvert": "Cống hộp",
        "pump": "Trạm bơm",
    }
    return labels.get(normalize_structure_type(stype), str(stype or ""))


def structure_formula_label(formula: Any) -> str:
    labels = {
        "villemonte": "Villemonte (Công thức 1)",
        "honma": "Honma (Công thức 2)",
        "honma_ext": "Honma mở rộng (Công thức 3)",
        "broad_crested": "Đỉnh rộng (Broad crested)",
        "free": "Đỉnh rộng (Broad crested)",
    }
    return labels.get(normalize_structure_formula(formula), str(formula or ""))


def normalize_structure_valve(raw: Any) -> str:
    key = input_cell(raw).lower().replace(" ", "_").replace("-", "_")
    if key in ("positive", "pos", "only_positive", "forward", "+"):
        return "positive"
    if key in ("negative", "neg", "only_negative", "reverse", "-"):
        return "negative"
    return "both"


def _parse_float(raw: Any, default: float | None = None) -> float | None:
    s = input_cell(raw)
    if s == "":
        return default
    try:
        v = float(s)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _cell_num(raw: Any) -> str:
    v = _parse_float(raw, None)
    if v is None:
        return ""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.6g}"


def resolve_data_path(raw: Any) -> Path:
    text = input_cell(raw)
    if not text:
        return ROOT
    path = Path(text)
    if path.is_absolute():
        return path
    return (ROOT / path).resolve()


def normalize_structure_formula(raw: Any) -> str:
    key = input_cell(raw).lower().replace(" ", "_").replace("-", "_")
    if key in ("villemonte", "weir1", "weir_formula_1", "formula1", "1"):
        return "villemonte"
    if key in ("honma", "weir2", "weir_formula_2", "formula2", "2"):
        return "honma"
    if key in ("honma_ext", "extended_honma", "weir3", "weir_formula_3", "formula3", "3"):
        return "honma_ext"
    if key in ("free", "broad", "broad_crested", "overflow", "broadcrested"):
        return "broad_crested"
    return "villemonte"


def structure_placement(rec: dict[str, Any] | None, stype: Any = None) -> str:
    """MIKE 11 HD: weir/gate/culvert/dam tren Q-point (inline); de/bom = lateral."""
    row = rec or {}
    raw = input_cell(row.get("placement")).lower()
    if raw in ("inline", "in_line", "qh", "structure"):
        return "inline"
    if raw in ("lateral", "side", "overbank", "levee"):
        return "lateral"
    t = normalize_structure_type(stype if stype is not None else row.get("type"))
    if t in ("dike", "pump"):
        return "lateral"
    return "inline"


def structure_bank_side(rec: dict[str, Any] | None) -> str | None:
    """Bo trai/phai (nhin theo chieu dong): left = ta ngan, right = huu ngan.

    Uu tien cot bank_side/side; sau do id (_L/_R), ten (ta/huu).
    """
    row = rec or {}
    raw = input_cell(row.get("bank_side") or row.get("side")).lower().replace(" ", "")
    if raw in ("left", "l", "lb", "ta", "tangan"):
        return "left"
    if raw in ("right", "r", "rb", "huu", "huungan"):
        return "right"
    # Unicode names
    raw_nm = input_cell(row.get("bank_side") or row.get("side")).lower()
    if "tả" in raw_nm or "ta ngan" in raw_nm:
        return "left"
    if "hữu" in raw_nm or "huu ngan" in raw_nm:
        return "right"
    sid = input_cell(row.get("id")).upper().replace("-", "_")
    if sid.endswith("_L") or sid.endswith(".L") or "_L_" in sid:
        return "left"
    if sid.endswith("_R") or sid.endswith(".R") or "_R_" in sid:
        return "right"
    name = input_cell(row.get("name")).lower()
    if "tả" in name or "ta ngan" in name or "tangan" in name or "left bank" in name:
        return "left"
    if "hữu" in name or "huu ngan" in name or "huungan" in name or "right bank" in name:
        return "right"
    return None


def weir_crest_level(rec: dict[str, Any]) -> float:
    """Hw MIKE: crest, hoac invert + height."""
    crest = _parse_float(rec.get("crest_m"), None)
    if crest is not None:
        return float(crest)
    invert = _parse_float(rec.get("invert_m"), 0.0) or 0.0
    height = _parse_float(rec.get("height_m"), 0.0) or 0.0
    return float(invert + height)


def _clamp_q(q: float, q_min: float, q_max: float | None) -> float:
    q = max(float(q), float(q_min))
    if q_max is not None:
        q = min(q, float(q_max))
    return float(q)


# ---------------------------------------------------------------------------
# Cong thuc xa — MIKE 11 HD / MIKE Hydro River (DHI)
# ---------------------------------------------------------------------------

def weir_free_q(
    h_up: float,
    crest_m: float,
    width_m: float,
    *,
    cd: float = WEIR_C_SI,
    exponent: float = WEIR_EXP_K,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """Weir tu do: Q = C * W * (H - Hw)^k."""
    y = max(float(h_up) - float(crest_m), 0.0)
    if y < 1e-4 or width_m <= 0.0:
        return float(q_min)
    q = float(cd) * max(float(width_m), 0.0) * (y ** float(exponent))
    return _clamp_q(q, q_min, q_max)


def weir_villemonte_q(
    h_up: float,
    h_down: float,
    crest_m: float,
    width_m: float,
    *,
    cd: float = WEIR_C_SI,
    exponent: float = WEIR_EXP_K,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """MIKE 11 Weir Formula 1 (Villemonte), Ref. Manual §1.40.1:

    Q = W * C * (Hus-Hw)^k * [1 - ((Hds-Hw)/(Hus-Hw))^k ]^0.385
    """
    if width_m <= 0.0:
        return float(q_min)
    k = float(exponent) if exponent and exponent > 0.0 else WEIR_EXP_K
    hus = max(float(h_up) - float(crest_m), 0.0)
    hds = max(float(h_down) - float(crest_m), 0.0)
    if hus < 1e-4:
        return float(q_min)
    ratio = min(max(hds / hus, 0.0), 1.0)
    factor = max(1.0 - (ratio ** k), 0.0) ** VILLEMONTE_SUB_EXP
    q = float(cd) * float(width_m) * (hus ** k) * factor
    return _clamp_q(q, q_min, q_max)


def weir_honma_q(
    h_up: float,
    h_down: float,
    crest_m: float,
    width_m: float,
    *,
    c1: float = HONMA_C1,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """MIKE 11 Weir Formula 2 (Honma), Ref. Manual §1.40.2:

    free (hds/hus < 2/3):  Q = C1 W hus^1.5
    submerged:             Q = C2 W hds sqrt(hus - hds),  C2 = (3√3/2) C1
    """
    if width_m <= 0.0:
        return float(q_min)
    hus = max(float(h_up) - float(crest_m), 0.0)
    hds = max(float(h_down) - float(crest_m), 0.0)
    if hus < 1e-4:
        return float(q_min)
    c1 = float(c1)
    if hds / hus < (2.0 / 3.0):
        q = c1 * float(width_m) * (hus ** 1.5)
    else:
        c2 = c1 * 1.5 * math.sqrt(3.0)
        q = c2 * float(width_m) * hds * math.sqrt(max(hus - hds, 0.0))
    return _clamp_q(q, q_min, q_max)


def weir_honma_ext_q(
    h_up: float,
    h_down: float,
    crest_m: float,
    width_m: float,
    *,
    c1: float = HONMA_C1,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """MIKE 11 Weir Formula 3 (Extended Honma) — 3 che do overflow.

    perfect   (hds/hus < 0.45): Q = C1 W hus^1.5
    imperfect (0.45..0.90):     Q = C1 W hus sqrt(hus - hds)
    submerged (>= 0.90):        Q = C2 W hds sqrt(hus - hds), C2=(3√3/2)C1
    """
    if width_m <= 0.0:
        return float(q_min)
    hus = max(float(h_up) - float(crest_m), 0.0)
    hds = max(float(h_down) - float(crest_m), 0.0)
    if hus < 1e-4:
        return float(q_min)
    c1 = float(c1)
    ratio = hds / hus
    if ratio < 0.45:
        q = c1 * float(width_m) * (hus ** 1.5)
    elif ratio < 0.90:
        q = c1 * float(width_m) * hus * math.sqrt(max(hus - hds, 0.0))
    else:
        c2 = c1 * 1.5 * math.sqrt(3.0)
        q = c2 * float(width_m) * hds * math.sqrt(max(hus - hds, 0.0))
    return _clamp_q(q, q_min, q_max)


def weir_mike_q(
    h_up: float,
    h_down: float,
    crest_m: float,
    width_m: float,
    *,
    formula: str = "villemonte",
    cd: float = WEIR_C_SI,
    exponent: float = WEIR_EXP_K,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """Chon cong thuc weir MIKE 11 (Weir Formula 1/2/3 / broad crested)."""
    key = normalize_structure_formula(formula)
    if key == "honma":
        return weir_honma_q(
            h_up, h_down, crest_m, width_m, c1=cd if cd else HONMA_C1,
            q_min=q_min, q_max=q_max,
        )
    if key == "honma_ext":
        return weir_honma_ext_q(
            h_up, h_down, crest_m, width_m, c1=cd if cd else HONMA_C1,
            q_min=q_min, q_max=q_max,
        )
    if key == "broad_crested":
        return weir_free_q(
            h_up, crest_m, width_m, cd=cd, exponent=exponent,
            q_min=q_min, q_max=q_max,
        )
    return weir_villemonte_q(
        h_up, h_down, crest_m, width_m, cd=cd, exponent=exponent,
        q_min=q_min, q_max=q_max,
    )


def structure_face_q(
    h_left: float,
    h_right: float,
    rec: dict[str, Any],
    *,
    hour: float = 0.0,
) -> float:
    """Q tren mat (Q-point) giua 2 nut H: + ve ha luu (left->right).

    MIKE HD: thay dong luong bang Q = f(Hus, Hds); valve chi cho 1 chieu.
    """
    hl = float(h_left)
    hr = float(h_right)
    if hl >= hr:
        q = float(structure_discharge(rec, hl, hr, hour=hour))
    else:
        q = -float(structure_discharge(rec, hr, hl, hour=hour))
    valve = normalize_structure_valve(rec.get("valve"))
    if valve == "positive" and q < 0.0:
        return 0.0
    if valve == "negative" and q > 0.0:
        return 0.0
    return float(q)


def dike_overflow_q(
    h_up: float,
    h_down: float,
    crest_m: float,
    length_m: float,
    *,
    cd: float = WEIR_C_SI,
    exponent: float = WEIR_EXP_K,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """Tran qua de (overbank) — Villemonte, W = chieu dai de."""
    return weir_villemonte_q(
        h_up,
        h_down,
        crest_m,
        length_m,
        cd=cd,
        exponent=exponent,
        q_min=q_min,
        q_max=q_max,
    )


def gate_underflow_q(
    h_up: float,
    h_down: float,
    invert_m: float,
    opening_m: float,
    width_m: float,
    *,
    cd: float = GATE_CD,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """Sluice / underflow gan MIKE 11 (free + drowned, Cc ~ 0.63).

    Free:  Q = Cd * b * w * sqrt(2 g H1)
    Drown: Q = Cd * b * w * sqrt(2 g (H1 - H2))
    Chuyen doi muot khi H2/H1 ~ 2/3 (psi).
    """
    w = max(float(opening_m), 0.0)
    b = max(float(width_m), 0.0)
    if w <= 0.0 or b <= 0.0:
        return float(q_min)
    h1 = max(float(h_up) - float(invert_m), 0.0)
    h2 = max(float(h_down) - float(invert_m), 0.0)
    if h1 <= 1e-4:
        return float(q_min)
    # Neu H1 < mo cua: xu ly nhu weir dinh rong (nuoc khong day duoi cua).
    if h1 <= w:
        return weir_villemonte_q(
            h_up, h_down, invert_m, b, cd=WEIR_C_SI, q_min=q_min, q_max=q_max
        )
    q_free = float(cd) * b * w * math.sqrt(max(2.0 * G * h1, 0.0))
    q_sub = float(cd) * b * w * math.sqrt(max(2.0 * G * max(h1 - h2, 0.0), 0.0))
    ratio = h2 / h1
    # Chuyen doi muot quanh 2/3 (MIKE dung psi de tranh nhay).
    if ratio <= 0.55:
        q = q_free
    elif ratio >= 0.80:
        q = q_sub
    else:
        t = (ratio - 0.55) / 0.25
        q = (1.0 - t) * q_free + t * q_sub
    return _clamp_q(q, q_min, q_max)


def culvert_q(
    h_up: float,
    h_down: float,
    invert_m: float,
    height_m: float,
    width_m: float,
    *,
    cd: float = CULVERT_CD,
    q_min: float = 0.0,
    q_max: float | None = None,
) -> float:
    """Culvert MIKE-like: inlet weir khi thap, orifice khi ngap dinh."""
    crown = float(invert_m) + max(float(height_m), 0.0)
    if float(h_up) <= float(invert_m):
        return float(q_min)
    if float(h_up) < crown:
        return weir_villemonte_q(
            h_up,
            h_down,
            invert_m,
            width_m,
            cd=WEIR_C_SI * 0.9,
            q_min=q_min,
            q_max=q_max,
        )
    opening = max(float(height_m), 0.0)
    return gate_underflow_q(
        h_up,
        h_down,
        invert_m,
        opening,
        width_m,
        cd=cd,
        q_min=q_min,
        q_max=q_max,
    )


def reservoir_storage_m3(level_m: float, area_m2: float, bed_m: float) -> float:
    """Kho chua xap xi: V = A * (H - bed) (mat ho khong doi)."""
    return max(float(area_m2), 0.0) * max(float(level_m) - float(bed_m), 0.0)


def reservoir_level_from_storage(volume_m3: float, area_m2: float, bed_m: float) -> float:
    a = max(float(area_m2), 1e-6)
    return float(bed_m) + max(float(volume_m3), 0.0) / a


# ---------------------------------------------------------------------------
# Ban ghi / CSV
# ---------------------------------------------------------------------------

def _structure(
    sid: str,
    name: str,
    stype: str,
    *,
    reach: str = "main",
    station_km: float | str = "",
    xs_id: str = "",
    lon: float | str = "",
    lat: float | str = "",
    crest_m: float | str = "",
    width_m: float | str = "",
    length_m: float | str = "",
    height_m: float | str = "",
    gate_opening_m: float | str = "",
    invert_m: float | str = "",
    cd: float | str = "",
    submerged_exp: float | str = "1.5",
    formula: str = "villemonte",
    placement: str = "auto",
    valve: str = "both",
    q_max_m3s: float | str = "",
    q_min_m3s: float | str = "0",
    storage_area_m2: float | str = "",
    initial_level_m: float | str = "",
    dam_crest_m: float | str = "",
    outlet_sill_m: float | str = "",
    spillway_crest_m: float | str = "",
    gate_left_offset_m: float | str = "",
    gate_spacing_m: float | str = "",
    outlet_gate_count: float | str = "",
    outlet_gate_widths_m: str = "",
    control: str = "free",
    upstream_reach: str = "",
    downstream_reach: str = "",
    file: str = "",
    value_col: str = "",
    unit: str = "",
    note: str = "",
) -> dict[str, str]:
    return {
        "id": sid,
        "name": name,
        "type": normalize_structure_type(stype),
        "reach": normalize_reach_id(reach, "main") if reach else "main",
        "station_km": _cell_num(station_km) if station_km != "" else input_cell(station_km),
        "xs_id": input_cell(xs_id),
        "lon": _cell_num(lon) if lon != "" else "",
        "lat": _cell_num(lat) if lat != "" else "",
        "crest_m": _cell_num(crest_m),
        "width_m": _cell_num(width_m),
        "length_m": _cell_num(length_m),
        "height_m": _cell_num(height_m),
        "gate_opening_m": _cell_num(gate_opening_m),
        "invert_m": _cell_num(invert_m),
        "cd": _cell_num(cd),
        "submerged_exp": _cell_num(submerged_exp) or "1.5",
        "formula": normalize_structure_formula(formula),
        "placement": input_cell(placement) or "auto",
        "valve": normalize_structure_valve(valve),
        "q_max_m3s": _cell_num(q_max_m3s),
        "q_min_m3s": _cell_num(q_min_m3s) or "0",
        "storage_area_m2": _cell_num(storage_area_m2),
        "initial_level_m": _cell_num(initial_level_m),
        "dam_crest_m": _cell_num(dam_crest_m),
        "outlet_sill_m": _cell_num(outlet_sill_m),
        "spillway_crest_m": _cell_num(spillway_crest_m),
        "gate_left_offset_m": _cell_num(gate_left_offset_m),
        "gate_spacing_m": _cell_num(gate_spacing_m),
        "outlet_gate_count": _cell_num(outlet_gate_count),
        "outlet_gate_widths_m": input_cell(outlet_gate_widths_m),
        "control": input_cell(control) or "free",
        "upstream_reach": input_cell(upstream_reach),
        "downstream_reach": input_cell(downstream_reach),
        "file": input_cell(file).replace("\\", "/"),
        "value_col": input_cell(value_col),
        "unit": input_cell(unit),
        "note": input_cell(note),
    }


def default_constructions() -> list[dict[str, str]]:
    """Mau cong trinh: de, dap dang, ho chua — giong khai bao Structure MIKE."""
    return [
        _structure(
            "DK_L",
            "Đê tả ngạn",
            "dike",
            reach="main",
            station_km=12.0,
            crest_m=14.5,
            length_m=2500.0,
            width_m=2500.0,
            cd=WEIR_C_SI,
            formula="villemonte",
            placement="lateral",
            valve="both",
            note="Overbank weir (Villemonte) — lateral q_lat",
        ),
        _structure(
            "DK_R",
            "Đê hữu ngạn",
            "dike",
            reach="main",
            station_km=12.0,
            crest_m=14.8,
            length_m=2200.0,
            width_m=2200.0,
            cd=WEIR_C_SI,
            formula="villemonte",
            placement="lateral",
            valve="both",
        ),
        _structure(
            "WR_1",
            "Weir chính",
            "weir",
            reach="main",
            station_km=28.5,
            crest_m=11.2,
            width_m=80.0,
            cd=WEIR_C_SI,
            submerged_exp=1.5,
            formula="villemonte",
            placement="inline",
            valve="both",
            control="free",
            note="Q-point — MIKE Weir Formula 1 (Villemonte)",
        ),
        _structure(
            "RS_1",
            "Đập / Hồ thượng lưu",
            "reservoir",
            reach="main",
            station_km=0.5,
            crest_m=18.0,
            width_m=40.0,
            invert_m=8.0,
            storage_area_m2=2.5e6,
            initial_level_m=12.0,
            cd=WEIR_C_SI,
            formula="villemonte",
            placement="inline",
            valve="positive",
            q_max_m3s=1200.0,
            note="Dam spillway tren Q-point + kho chua",
        ),
        _structure(
            "GT_1",
            "Cống điều tiết",
            "gate",
            reach="main",
            station_km=28.6,
            invert_m=9.5,
            width_m=12.0,
            height_m=4.0,
            gate_opening_m=1.5,
            cd=GATE_CD,
            placement="inline",
            valve="both",
            control="controlled",
            unit="m",
            note="Control structure / sluice tren Q-point",
        ),
        _structure(
            "CV_1",
            "Culvert nhánh",
            "culvert",
            reach="trib_1",
            station_km=1.2,
            invert_m=7.8,
            width_m=3.0,
            height_m=2.5,
            length_m=35.0,
            cd=CULVERT_CD,
            placement="inline",
            valve="both",
        ),
    ]


def _normalize_outlet_gates(rec: dict[str, str], index: int) -> None:
    """So cua xa va do rong tung cua. Rong luu dang '12;10;8'."""
    raw_widths = input_cell(rec.get("outlet_gate_widths_m"))
    tokens = [part.strip() for part in raw_widths.split(";")] if raw_widths else []
    if len(tokens) == 1 and "," in tokens[0]:
        tokens = [part.strip() for part in tokens[0].split(",")]
    widths: list[float] = []
    for part in tokens:
        if part == "":
            continue
        value = _parse_float(part, None)
        if value is None or value < 0:
            raise ValueError(f"Dong {index}: do rong cua xa khong hop le ({part}).")
        widths.append(value)
    count_raw = input_cell(rec.get("outlet_gate_count"))
    if count_raw:
        count_val = _parse_float(count_raw, None)
        if (
            count_val is None
            or count_val < 0
            or abs(count_val - round(count_val)) > 1e-6
        ):
            raise ValueError(f"Dong {index}: outlet_gate_count khong hop le.")
        count = int(round(count_val))
    else:
        count = len(widths)
    if count > 60:
        raise ValueError(f"Dong {index}: so cua xa toi da 60.")
    if widths and len(widths) < count:
        pad = widths[-1]
        widths.extend([pad] * (count - len(widths)))
    widths = widths[:count] if widths else []
    rec["outlet_gate_count"] = str(count) if count or count_raw else ""
    rec["outlet_gate_widths_m"] = ";".join(_cell_num(v) for v in widths)


def _normalize_structure(raw: Any, index: int) -> dict[str, str]:
    row = raw if isinstance(raw, dict) else {}
    rec = {col: input_cell(row.get(col)) for col in STRUCTURE_COLS}
    if not rec["id"]:
        rec["id"] = f"st_{index}"
    rec["type"] = normalize_structure_type(rec.get("type") or "weir")
    if rec["type"] not in STRUCTURE_TYPES:
        raise ValueError(
            f"Dong {index}: type khong hop le ({rec['type']}). "
            f"Chap nhan: {', '.join(STRUCTURE_TYPES)}"
        )
    rec["reach"] = normalize_reach_id(rec.get("reach") or "main", "main")
    for key in (
        "lon", "lat", "station_km", "crest_m", "width_m", "length_m",
        "height_m", "gate_opening_m", "invert_m", "cd", "submerged_exp",
        "q_max_m3s", "q_min_m3s", "storage_area_m2", "initial_level_m",
        "dam_crest_m",
        "outlet_sill_m", "spillway_crest_m", "gate_left_offset_m", "gate_spacing_m",
    ):
        if rec[key]:
            v = _parse_float(rec[key], None)
            if v is None:
                raise ValueError(f"Dong {index}: {key} khong hop le.")
            rec[key] = _cell_num(v)
    if rec["lon"]:
        lon = float(rec["lon"])
        if not (-180.0 <= lon <= 180.0):
            raise ValueError(f"Dong {index}: lon phai trong [-180, 180].")
    if rec["lat"]:
        lat = float(rec["lat"])
        if not (-90.0 <= lat <= 90.0):
            raise ValueError(f"Dong {index}: lat phai trong [-90, 90].")
    if rec["file"]:
        rec["file"] = rec["file"].replace("\\", "/")
    if not rec["control"]:
        rec["control"] = "free"
    if not rec["submerged_exp"]:
        rec["submerged_exp"] = "1.5"
    if not rec["q_min_m3s"]:
        rec["q_min_m3s"] = "0"
    rec["formula"] = normalize_structure_formula(rec.get("formula") or "villemonte")
    place = input_cell(rec.get("placement")).lower() or "auto"
    if place not in STRUCTURE_PLACEMENTS and place not in ("inline", "lateral", "auto"):
        place = "auto"
    rec["placement"] = place
    rec["valve"] = normalize_structure_valve(rec.get("valve") or "both")
    _normalize_outlet_gates(rec, index)
    return rec


def load_constructions(*, seed: bool = True) -> list[dict[str, str]]:
    # File/DB da ton tai (ke ca 0 hang) = nguoi dung da xoa het — khong seed lai mau.
    if csv_available(STRUCTURE_CSV):
        rows: list[dict[str, str]] = []
        with csv_open(STRUCTURE_CSV) as f:
            for i, row in enumerate(csv.DictReader(f), start=1):
                rows.append(_normalize_structure(row, i))
        return rows
    rows = default_constructions()
    if seed:
        write_csv_rows(STRUCTURE_CSV, STRUCTURE_COLS, rows, rebuild_hydro=False)
    return rows


def save_constructions(raw_rows: Any) -> list[dict[str, str]]:
    if not isinstance(raw_rows, list):
        raise ValueError("Can JSON {rows: [{id, name, type, reach, ...}, ...]}.")
    rows = [
        _normalize_structure(row, i + 1)
        for i, row in enumerate(raw_rows)
        if isinstance(row, dict)
    ]
    # Cho phep []: song khong co cong trinh, Model 1D van chay binh thuong.
    write_csv_rows(STRUCTURE_CSV, STRUCTURE_COLS, rows, rebuild_hydro=False)
    saved = load_constructions(seed=False)
    try:
        from flood_model.reservoir_web import sync_params_from_construction
        sync_params_from_construction(saved)
    except Exception:
        # Loi dong bo ho khong duoc chan viec luu cong trinh.
        pass
    return saved


def find_construction(
    *,
    sid: str = "",
    stype: str = "",
    reach: str = "",
) -> dict[str, str] | None:
    want_type = normalize_structure_type(stype) if stype else ""
    want_reach = normalize_reach_id(reach, "main") if reach else ""
    for rec in load_constructions(seed=False):
        if sid and rec.get("id") != sid:
            continue
        if want_type and normalize_structure_type(rec.get("type")) != want_type:
            continue
        if want_reach and normalize_reach_id(rec.get("reach"), "main") != want_reach:
            continue
        return rec
    return None


def constructions_by_type(stype: str) -> list[dict[str, str]]:
    key = normalize_structure_type(stype)
    return [
        rec for rec in load_constructions(seed=False)
        if normalize_structure_type(rec.get("type")) == key
    ]


def constructions_on_reach(reach: str) -> list[dict[str, str]]:
    rid = normalize_reach_id(reach, "main")
    return [
        rec for rec in load_constructions(seed=False)
        if normalize_reach_id(rec.get("reach"), "main") == rid
    ]


def control_series(rec: dict[str, str]) -> tuple[np.ndarray, np.ndarray] | None:
    """Doc chuoi dieu khien (do mo cua / Q bom / muc nuoc) neu co file."""
    path = resolve_data_path(rec.get("file"))
    col = input_cell(rec.get("value_col"))
    if not rec.get("file") or not col or not csv_available(path):
        return None
    hours, vals = read_series_csv(path, col)
    if hours.size < 1:
        return None
    return hours, vals


def control_value_at(rec: dict[str, str], hour: float, default: float | None = None) -> float | None:
    series = control_series(rec)
    if series is None:
        return default
    hours, vals = series
    if hours.size == 1:
        return float(vals[0])
    return float(np.interp(float(hour), hours, vals))


def structure_discharge(
    rec: dict[str, str],
    h_up: float,
    h_down: float,
    *,
    hour: float = 0.0,
    gate_opening_m: float | None = None,
) -> float:
    """Tinh |Q| qua cong trinh (MIKE 11 HD) — chieu Hus -> Hds."""
    stype = normalize_structure_type(rec.get("type"))
    if input_cell(rec.get("control")).lower() == "closed":
        return float(_parse_float(rec.get("q_min_m3s"), 0.0) or 0.0)

    crest = weir_crest_level(rec)
    width = _parse_float(rec.get("width_m"), 0.0) or 0.0
    length = _parse_float(rec.get("length_m"), width) or width
    height = _parse_float(rec.get("height_m"), 0.0) or 0.0
    invert = _parse_float(rec.get("invert_m"), crest) or crest
    cd = _parse_float(rec.get("cd"), None)
    exp = _parse_float(rec.get("submerged_exp"), WEIR_EXP_K) or WEIR_EXP_K
    formula = normalize_structure_formula(rec.get("formula") or "villemonte")
    qmin = _parse_float(rec.get("q_min_m3s"), 0.0) or 0.0
    qmax = _parse_float(rec.get("q_max_m3s"), None)

    if stype == "dike":
        return dike_overflow_q(
            h_up, h_down, crest, length or width,
            cd=cd if cd is not None else WEIR_C_SI,
            exponent=exp, q_min=qmin, q_max=qmax,
        )
    if stype in ("weir", "reservoir"):
        use_cd = cd
        if use_cd is None:
            use_cd = HONMA_C1 if formula in ("honma", "honma_ext") else WEIR_C_SI
        return weir_mike_q(
            h_up, h_down, crest, width or length,
            formula=formula,
            cd=use_cd,
            exponent=exp, q_min=qmin, q_max=qmax,
        )
    if stype == "gate":
        opening = gate_opening_m
        if opening is None:
            opening = control_value_at(
                rec, hour, _parse_float(rec.get("gate_opening_m"), 0.0) or 0.0
            )
        return gate_underflow_q(
            h_up, h_down, invert, float(opening or 0.0), width,
            cd=cd if cd is not None else GATE_CD,
            q_min=qmin, q_max=qmax,
        )
    if stype == "culvert":
        return culvert_q(
            h_up, h_down, invert, height, width,
            cd=cd if cd is not None else CULVERT_CD,
            q_min=qmin, q_max=qmax,
        )
    if stype == "pump":
        q = control_value_at(rec, hour, _parse_float(rec.get("q_max_m3s"), 0.0) or 0.0)
        q = float(q or 0.0)
        return _clamp_q(q, qmin, qmax)
    return 0.0


# ---------------------------------------------------------------------------
# Gan cong trinh vao solver 1D (MIKE 11 HD):
#   inline  -> thay Q tren mat (Q-point) giua 2 nut H
#   lateral -> q_lat tai nut H (de / bom)
# ---------------------------------------------------------------------------

@dataclass
class BoundStructure:
    """Cong trinh da gan vao luoi Abbott (H-point / Q-point)."""

    rec: dict[str, str]
    reach: str
    node: int
    face: int
    station_m: float
    placement: str = "inline"


@dataclass
class StructureRuntime:
    """Trang thai runtime (kho chua ho, log)."""

    bound: list[BoundStructure] = field(default_factory=list)
    reservoir_level: dict[str, float] = field(default_factory=dict)
    reservoir_history: dict[str, list[float]] = field(default_factory=dict)
    logged: bool = False


def snapshot_reservoir_levels(runtime: StructureRuntime | None) -> None:
    """Ghi muc ho tai buoc xuat ket qua (theo gio Model 1D)."""
    if runtime is None or not runtime.reservoir_level:
        return
    for sid, h in runtime.reservoir_level.items():
        key = str(sid or "")
        if not key:
            continue
        try:
            hv = float(h)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(hv):
            continue
        runtime.reservoir_history.setdefault(key, []).append(hv)


def merge_reservoir_histories(
    *runtimes: StructureRuntime | None,
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for rt in runtimes:
        if rt is None:
            continue
        for sid, series in (rt.reservoir_history or {}).items():
            if sid and series:
                out[str(sid)] = [float(v) for v in series]
    return out


def write_reservoir_levels_csv(
    path: Path | str,
    hours: Sequence[float] | np.ndarray,
    history: dict[str, Sequence[float]] | None,
) -> None:
    """Ghi CSV: hour + <id>_level_m cho tung ho."""
    hh = np.asarray(hours, dtype=float).ravel()
    hist = {str(k): list(v) for k, v in (history or {}).items() if k and v}
    if hh.size < 1 or not hist:
        return
    ids = sorted(hist.keys())
    fields = ["hour"] + [f"{sid}_level_m" for sid in ids]
    rows: list[dict[str, str]] = []
    for t in range(int(hh.size)):
        row = {"hour": f"{float(hh[t]):.4f}"}
        for sid in ids:
            series = hist[sid]
            if t < len(series):
                row[f"{sid}_level_m"] = f"{float(series[t]):.4f}"
            elif series:
                row[f"{sid}_level_m"] = f"{float(series[-1]):.4f}"
            else:
                row[f"{sid}_level_m"] = ""
        rows.append(row)
    write_csv_rows(path, fields, rows)


def load_reservoir_levels_csv(path: Path | str) -> dict[str, np.ndarray]:
    """Doc CSV muc ho (hour + <id>_level_m). Tra {id: ndarray}."""
    p = Path(path)
    if not csv_available(p):
        return {}
    with csv_open(p) as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {}
        names = [str(n or "").strip() for n in reader.fieldnames]
        level_cols = [
            (n, n[: -len("_level_m")])
            for n in names
            if n.endswith("_level_m") and len(n) > 8
        ]
        out: dict[str, list[float]] = {}
        if level_cols:
            for row in reader:
                for col, sid in level_cols:
                    raw = row.get(col)
                    if raw is None or str(raw).strip() == "":
                        continue
                    try:
                        lv = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(lv):
                        out.setdefault(sid, []).append(lv)
            return {sid: np.asarray(vals, dtype=float) for sid, vals in out.items() if vals}
        # Long format: hour, id, level_m
        by_id: dict[str, list[float]] = {}
        for row in reader:
            sid = str(row.get("id") or row.get("structure_id") or "").strip()
            if not sid:
                continue
            try:
                lv = float(row.get("level_m") or row.get("h_m") or "")
            except (TypeError, ValueError):
                continue
            if math.isfinite(lv):
                by_id.setdefault(sid, []).append(lv)
        return {sid: np.asarray(vals, dtype=float) for sid, vals in by_id.items() if vals}


def structures_enabled() -> bool:
    return bool(STRUCTURES_IN_SOLVER)


def _geom_station_m(geom: Any) -> np.ndarray:
    if hasattr(geom, "distance_m"):
        return np.asarray(geom.distance_m, dtype=float)
    if isinstance(geom, dict) and "station_m" in geom:
        return np.asarray(geom["station_m"], dtype=float)
    if isinstance(geom, dict) and "distance_m" in geom:
        return np.asarray(geom["distance_m"], dtype=float)
    raise TypeError("geom can distance_m / station_m")


def nearest_structure_node(station_m: np.ndarray, station_km: float) -> int:
    s = np.asarray(station_m, dtype=float)
    if s.size < 1:
        return 0
    target = float(station_km) * 1000.0
    return int(np.argmin(np.abs(s - target)))


def bind_structures_for_reach(
    reach_id: str,
    geom: Any,
    *,
    enabled: bool | None = None,
) -> list[BoundStructure]:
    """Gan constructions.csv vao nut H + mat Q (MIKE grid)."""
    if enabled is None:
        enabled = structures_enabled()
    if not enabled:
        return []
    try:
        rows = constructions_on_reach(reach_id)
    except Exception:
        return []
    if not rows:
        return []
    try:
        station_m = _geom_station_m(geom)
    except Exception:
        return []
    n_x = int(station_m.size)
    if n_x < 1:
        return []
    n_face = max(n_x - 1, 0)
    bound: list[BoundStructure] = []
    rid = normalize_reach_id(reach_id, "main")
    for rec in rows:
        st_km = _parse_float(rec.get("station_km"), None)
        if st_km is None:
            continue
        node = nearest_structure_node(station_m, float(st_km))
        node = min(max(node, 0), n_x - 1)
        # Q-point MIKE: doan giua node va node+1 (neu cuoi thi doan truoc).
        if n_face <= 0:
            face = 0
        elif node >= n_face:
            face = n_face - 1
        else:
            face = node
        place = structure_placement(rec)
        bound.append(
            BoundStructure(
                rec=dict(rec),
                reach=rid,
                node=node,
                face=int(face),
                station_m=float(station_m[node]),
                placement=place,
            )
        )
    return bound


def init_structure_runtime(
    reach_id: str,
    geom: Any,
    *,
    enabled: bool | None = None,
) -> StructureRuntime:
    bound = bind_structures_for_reach(reach_id, geom, enabled=enabled)
    levels: dict[str, float] = {}
    for item in bound:
        if normalize_structure_type(item.rec.get("type")) != "reservoir":
            continue
        sid = item.rec.get("id") or ""
        h0 = _parse_float(item.rec.get("initial_level_m"), None)
        if h0 is None:
            h0 = weir_crest_level(item.rec)
        levels[sid] = float(h0)
    return StructureRuntime(bound=bound, reservoir_level=levels, logged=False)


def log_bound_structures(runtime: StructureRuntime, *, label: str = "") -> None:
    if runtime.logged or not runtime.bound:
        return
    prefix = f"[{label}] " if label else ""
    print(
        f"{prefix}Cong trinh MIKE HD: {len(runtime.bound)} diem "
        f"(STRUCTURES_IN_SOLVER={'1' if structures_enabled() else '0'})",
        flush=True,
    )
    for item in runtime.bound:
        print(
            f"  - {item.rec.get('id')}  {structure_type_label(item.rec.get('type'))}  "
            f"{item.placement}/{normalize_structure_formula(item.rec.get('formula'))}  "
            f"reach={item.reach}  node={item.node}  face={item.face}  "
            f"station={item.station_m/1000.0:.3f} km",
            flush=True,
        )
    runtime.logged = True


def inline_face_discharges(
    runtime: StructureRuntime | None,
    h: np.ndarray,
    *,
    hour: float = 0.0,
) -> dict[int, float]:
    """Q quy dinh tren cac mat inline (thay dong luong Abbott)."""
    out: dict[int, float] = {}
    if runtime is None or not runtime.bound or not structures_enabled():
        return out
    hh = np.asarray(h, dtype=float)
    n_x = int(hh.size)
    if n_x < 2:
        return out
    for item in runtime.bound:
        if item.placement != "inline":
            continue
        j = int(item.face)
        if j < 0 or j >= n_x - 1:
            continue
        rec = dict(item.rec)
        sid = str(rec.get("id") or "")
        stype = normalize_structure_type(rec.get("type"))
        h_left = float(hh[j])
        h_right = float(hh[j + 1])
        # Ho chua: H thuong luu = muc ho (neu co), H ha luu = nut phai.
        if stype == "reservoir" and sid in runtime.reservoir_level:
            h_res = float(runtime.reservoir_level[sid])
            # Dam: dong tu ho -> ha luu (nut phai); neu H_ds cao hon thi nguoc.
            q = structure_face_q(h_res, h_right, rec, hour=hour)
        else:
            q = structure_face_q(h_left, h_right, rec, hour=hour)
        if j in out:
            out[j] = float(out[j]) + float(q)
        else:
            out[j] = float(q)
    return out


def apply_structures_to_q_lat(
    q_lat: np.ndarray | None,
    runtime: StructureRuntime | None,
    h: np.ndarray,
    *,
    hour: float = 0.0,
    dt_s: float = 0.0,
) -> np.ndarray:
    """Chi cong trinh lateral (de / bom): q_lat tai nut H.

    Inline (weir/gate/culvert/dam) khong vao day — dung inline_face_discharges.
    """
    n_x = int(np.asarray(h, dtype=float).size)
    out = np.zeros(n_x, dtype=float) if q_lat is None else np.asarray(q_lat, dtype=float).copy()
    if runtime is None or not runtime.bound or not structures_enabled():
        return out
    hh = np.asarray(h, dtype=float)
    for item in runtime.bound:
        if item.placement != "lateral":
            continue
        i = int(item.node)
        if i < 0 or i >= n_x:
            continue
        h_up = float(hh[i])
        stype = normalize_structure_type(item.rec.get("type"))
        sid = str(item.rec.get("id") or "")
        if stype == "dike":
            # Overbank: H_ds xap xi bai boi ~ crest (MIKE SHE / 2D dike).
            crest = weir_crest_level(item.rec)
            h_down = min(h_up, float(crest))
            try:
                q = float(structure_discharge(item.rec, h_up, h_down, hour=float(hour)))
            except Exception:
                q = 0.0
            if math.isfinite(q) and abs(q) >= 1e-12:
                out[i] -= q  # mat nuoc khoi long
        elif stype == "pump":
            try:
                q = float(structure_discharge(item.rec, h_up, h_up, hour=float(hour)))
            except Exception:
                q = 0.0
            if math.isfinite(q) and abs(q) >= 1e-12:
                out[i] -= q
        elif stype == "reservoir" and sid in runtime.reservoir_level and dt_s > 0.0:
            # Cap nhat kho chua theo Q spill inline (neu co cung id) — xu ly o apply_reservoir_storage.
            pass
    return out


def apply_reservoir_storage(
    runtime: StructureRuntime | None,
    face_q: dict[int, float] | None,
    *,
    dt_s: float = 0.0,
) -> None:
    """Cap nhat muc ho sau buoc: dV/dt = -Q_spill (Q>0 ra khoi ho)."""
    if runtime is None or not runtime.bound or dt_s <= 0.0 or not face_q:
        return
    for item in runtime.bound:
        if item.placement != "inline":
            continue
        if normalize_structure_type(item.rec.get("type")) != "reservoir":
            continue
        sid = str(item.rec.get("id") or "")
        if sid not in runtime.reservoir_level:
            continue
        q = float(face_q.get(int(item.face), 0.0))
        area = _parse_float(item.rec.get("storage_area_m2"), 0.0) or 0.0
        bed = _parse_float(item.rec.get("invert_m"), None)
        if bed is None:
            bed = weir_crest_level(item.rec) - 5.0
        if area <= 1.0:
            continue
        vol = reservoir_storage_m3(runtime.reservoir_level[sid], area, bed)
        # Q>0: tu ho xuong ha luu -> mat nuoc ho
        vol = max(vol - q * float(dt_s), 0.0)
        runtime.reservoir_level[sid] = reservoir_level_from_storage(vol, area, bed)


def apply_inline_to_face_q(
    qf: np.ndarray,
    runtime: StructureRuntime | None,
    h: np.ndarray,
    *,
    hour: float = 0.0,
) -> np.ndarray:
    """Ep Q tren mat inline (dung cho Saint-Venant / sau Picard)."""
    out = np.asarray(qf, dtype=float).copy()
    faces = inline_face_discharges(runtime, h, hour=hour)
    for j, q in faces.items():
        if 0 <= int(j) < out.size:
            out[int(j)] = float(q)
    return out



def load_constructions_payload() -> dict[str, Any]:
    rows = load_constructions()
    out: list[dict[str, Any]] = []
    for rec in rows:
        item: dict[str, Any] = dict(rec)
        item["type_label"] = structure_type_label(rec.get("type"))
        item["placement"] = structure_placement(rec)
        item["bank_side"] = structure_bank_side(rec)
        item["formula"] = normalize_structure_formula(rec.get("formula"))
        item["formula_label"] = structure_formula_label(rec.get("formula"))
        item["valve"] = normalize_structure_valve(rec.get("valve"))
        item["file_ok"] = bool(rec.get("file")) and csv_available(resolve_data_path(rec.get("file")))
        out.append(item)
    counts: dict[str, int] = {t: 0 for t in STRUCTURE_TYPES}
    for rec in rows:
        t = normalize_structure_type(rec.get("type"))
        counts[t] = counts.get(t, 0) + 1
    reaches: list[dict[str, str]] = []
    try:
        from flood_model.boundary import list_boundary_reaches

        reaches = [
            r for r in list_boundary_reaches()
            if str(r.get("kind") or "") != "basin"
        ]
    except Exception:
        reaches = [
            {"id": "main", "kind": "main", "label": "Sông chính 1"},
            {"id": "trib_1", "kind": "trib", "label": "Sông nhánh 1"},
        ]
    return {
        "ok": True,
        "path": str(STRUCTURE_CSV),
        "database": "data_flood",
        "n": len(out),
        "types": list(STRUCTURE_TYPES),
        "type_labels": {t: structure_type_label(t) for t in STRUCTURE_TYPES},
        "formulas": list(STRUCTURE_FORMULAS),
        "formula_labels": {f: structure_formula_label(f) for f in STRUCTURE_FORMULAS},
        "valves": list(STRUCTURE_VALVES),
        "counts": counts,
        "columns": list(STRUCTURE_COLS),
        "reaches": reaches,
        "rows": out,
    }


def main() -> int:
    rows = load_constructions(seed=True)
    print(f"constructions.csv  ({len(rows)} cong trinh)", flush=True)
    for rec in rows:
        print(
            f"  {rec['id']:8s}  {structure_type_label(rec['type']):18s}  "
            f"reach={rec['reach']:8s}  station={rec['station_km'] or '-':>8s} km  "
            f"{rec['name']}",
            flush=True,
        )
    demo = find_construction(sid="WR_1") or rows[0]
    q = structure_discharge(demo, h_up=12.5, h_down=11.0, hour=0.0)
    print(f"Vi du Q({demo['id']}) @ H_up=12.5 / H_down=11.0 -> {q:.2f} m3/s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

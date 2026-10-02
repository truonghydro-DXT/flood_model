"""Web UI va API cho mo hinh ho chua HEC-ResSim."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import asdict, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request


ROOT = Path(__file__).resolve().parent
NAM_RESULT_CSV = ROOT / "rainfall_runoff_output" / "mike_nam_result.csv"
PARAMS_JSON = ROOT / "construction_input" / "reservoir_params.json"


@lru_cache(maxsize=1)
def _model_module() -> Any:
    path = ROOT / "reservoir.py"
    spec = importlib.util.spec_from_file_location("flood_model.hec_resim", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Khong nap duoc mo hinh: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _gate_control(raw: Any) -> str:
    control = str(raw or "free").strip().lower()
    if control not in ("free", "controlled", "closed"):
        return "free"
    return control


def _gate_specs(raw: Any) -> tuple[tuple[float, str, float], ...] | None:
    """(be rong, che do, do mo) cho tung cua. None neu payload khong gui danh sach."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return ()
    specs: list[tuple[float, str, float]] = []
    for item in raw:
        if isinstance(item, dict):
            width = _finite(item.get("width_m"))
            opening = _finite(item.get("opening_m"))
            control = _gate_control(item.get("control"))
        elif isinstance(item, (list, tuple)) and item:
            width = _finite(item[0])
            control = _gate_control(item[1] if len(item) > 1 else "free")
            opening = _finite(item[2]) if len(item) > 2 else 0.0
        else:
            width = _finite(item)
            control = "free"
            opening = 0.0
        if width is None:
            continue
        specs.append((max(width, 0.0), control, max(opening or 0.0, 0.0)))
    return tuple(specs)


def _gates_public(specs: tuple[tuple[float, str, float], ...]) -> list[dict[str, Any]]:
    return [
        {"width_m": width, "control": control, "opening_m": opening}
        for width, control, opening in specs
    ]


def _config_from_json(raw: dict[str, Any]) -> Any:
    model = _model_module()
    allowed = {item.name for item in fields(model.ReservoirConfig)}
    text_keys = ("name", "operation_set", "has_file", "spillway_control", "generation_schedule", "generation_hours")
    values = {
        key: value if key in text_keys else float(value)
        for key, value in raw.items()
        if key in allowed and key != "spillway_gates" and str(value).strip() != ""
    }
    if "spillway_gates" in raw:
        values["spillway_gates"] = _gate_specs(raw.get("spillway_gates")) or ()
    if "flood_reception_level_m" not in values:
        low = float(values.get("min_level_m", 9.0))
        high = float(values.get("conservation_level_m", 12.0))
        values["flood_reception_level_m"] = (low + high) / 2.0 if high > low else high
    return model.ReservoirConfig(**values)


def _finite(raw: Any) -> float | None:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# Thong so hinh hoc dung chung giua form ho va dam/ho trong cong trinh.
_SHARED_FIELDS = {
    "bed_m": "invert_m",
    "initial_level_m": "initial_level_m",
    "storage_area_m2": "storage_area_m2",
    "spillway_crest_m": "spillway_crest_m",
    "spillway_coefficient": "cd",
    "outlet_capacity_m3s": "q_max_m3s",
    "dam_crest_m": "dam_crest_m",
    "dam_width_m": "width_m",
    "outlet_sill_m": "outlet_sill_m",
    "gate_left_offset_m": "gate_left_offset_m",
    "gate_spacing_m": "gate_spacing_m",
}
_pushing_construction = False


def _reservoir_row(rows: list[dict[str, str]] | None = None) -> dict[str, str] | None:
    from flood_model.construction import STRUCTURE_CSV, load_constructions, normalize_structure_type
    from flood_model.csv_io import csv_available

    if rows is None:
        try:
            if not csv_available(STRUCTURE_CSV):
                return None
        except Exception:
            return None
        source = load_constructions(seed=False)
    else:
        source = rows
    for row in source:
        if normalize_structure_type(row.get("type")) == "reservoir":
            return row
    return None


def _gates_from_construction(row: dict[str, str]) -> list[float] | None:
    raw = str(row.get("outlet_gate_widths_m") or "").strip()
    count_raw = str(row.get("outlet_gate_count") or "").strip()
    if raw == "" and count_raw == "":
        return None
    widths: list[float] = []
    for part in raw.replace(",", ";").split(";"):
        value = _finite(part)
        if value is not None and value >= 0:
            widths.append(value)
    if count_raw == "":
        return widths
    count_value = _finite(count_raw)
    if count_value is None:
        return widths
    count = int(round(count_value))
    if count <= 0:
        return []
    if not widths:
        return None
    if len(widths) < count:
        widths.extend([widths[-1]] * (count - len(widths)))
    return widths[:count]


def _apply_construction_row(
    config: dict[str, Any],
    row: dict[str, str],
) -> list[float] | None:
    name = str(row.get("name") or "").strip()
    if name:
        config["name"] = name
    for key, column in _SHARED_FIELDS.items():
        value = _finite(row.get(column))
        if value is not None:
            config[key] = value
    control = str(row.get("control") or "").strip().lower()
    if control in ("free", "controlled", "closed"):
        config["spillway_control"] = control
    opening = _finite(row.get("gate_opening_m"))
    if opening is not None:
        config["spillway_opening_m"] = max(opening, 0.0)
    gates = _gates_from_construction(row)
    if gates is not None:
        config["spillway_width_m"] = sum(gates)
    return gates


def _has_bed_m() -> float | None:
    """H thap nhat trong bang H-A-S (moc co dien tich mat > 0)."""
    model = _model_module()
    try:
        bed = model.has_bed_m()
    except (OSError, TypeError, ValueError):
        return None
    if bed is None or not math.isfinite(float(bed)):
        return None
    return float(bed)


def _overlay_reservoir_construction(
    config: dict[str, Any],
    gates: list[float],
) -> tuple[dict[str, Any], list[float], bool]:
    """Lay thong so dung chung tu dam/ho trong cong trinh."""
    row = _reservoir_row()
    if not row:
        return config, gates, False
    linked_gates = _apply_construction_row(config, row)
    if linked_gates is not None:
        gates = linked_gates
    return config, gates, True


def _push_reservoir_to_construction(config: dict[str, Any], gates: list[float]) -> bool:
    """Ghi thong so dung chung sang dong dam/ho, giu nguyen cong trinh khac."""
    global _pushing_construction
    from flood_model.construction import _cell_num, load_constructions, save_constructions

    rows = [dict(row) for row in load_constructions(seed=False)]
    target = _reservoir_row(rows)
    if target is None:
        return False
    index = next(
        i for i, row in enumerate(rows)
        if row.get("id") == target.get("id") and row.get("type") == target.get("type")
    )
    row = dict(rows[index])
    name = str(config.get("name") or "").strip()
    if name:
        row["name"] = name
    for key, column in _SHARED_FIELDS.items():
        value = _finite(config.get(key))
        if value is not None:
            row[column] = _cell_num(value)
    control = str(config.get("spillway_control") or "").strip().lower()
    if control in ("free", "controlled", "closed"):
        row["control"] = control
    opening = _finite(config.get("spillway_opening_m"))
    if opening is not None:
        row["gate_opening_m"] = _cell_num(max(opening, 0.0))
    row["outlet_gate_count"] = str(len(gates))
    row["outlet_gate_widths_m"] = ";".join(_cell_num(width) for width in gates)
    rows[index] = row
    _pushing_construction = True
    try:
        save_constructions(rows)
    finally:
        _pushing_construction = False
    return True


def sync_params_from_construction(rows: list[dict[str, str]] | None = None) -> None:
    """Cap nhat thong so ho khi luu cong trinh. Khong doi kich ban van hanh."""
    if _pushing_construction:
        return
    row = _reservoir_row(rows)
    data = _read_saved_params()
    if row is None or not data:
        return
    config = dict(data.get("config") or {})
    before = dict(config)
    before_gates = data.get("spillway_gates_m")
    gates = _apply_construction_row(config, row)
    if gates is not None:
        data["spillway_gates_m"] = gates
    if config == before and data.get("spillway_gates_m") == before_gates:
        return
    data["config"] = config
    _write_saved_params(data)


def _turbine_units_from_payload(raw: Any) -> list[dict[str, float]]:
    if not isinstance(raw, list):
        return []
    units: list[dict[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        power = _finite(item.get("rated_mw"))
        flow = _finite(item.get("qmax_m3s"))
        units.append({
            "rated_mw": max(power or 0.0, 0.0),
            "qmax_m3s": max(flow or 0.0, 0.0),
        })
    return units


def _gates_from_payload(raw: Any, width_m: float) -> list[float]:
    if isinstance(raw, list):
        return [max(float(item), 0.0) for item in raw]
    if width_m > 1e-9:
        return [float(width_m)]
    return []


def _read_saved_params() -> dict[str, Any] | None:
    if not PARAMS_JSON.is_file():
        return None
    data = json.loads(PARAMS_JSON.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("File thong so ho khong hop le")
    return data


def _write_saved_params(payload: dict[str, Any]) -> None:
    PARAMS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = PARAMS_JSON.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(PARAMS_JSON)


def create_reservoir_blueprint(
    *, name: str = "reservoir_web", url_prefix: str = "/reservoir"
) -> Blueprint:
    bp = Blueprint(
        name,
        __name__,
        url_prefix=url_prefix,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
        static_url_path="/reservoir-static",
    )

    @bp.get("/")
    def index():
        return render_template(
            "reservoir.html",
            reservoir_static_endpoint=f"{name}.static",
            reservoir_api_url=f"{url_prefix.rstrip('/')}/api/simulate",
        )

    @bp.post("/api/simulate")
    def simulate():
        try:
            payload = request.get_json(silent=False) or {}
            model = _model_module()
            points = [
                model.ReservoirPoint(
                    float(item["hour"]), float(item["inflow_m3s"])
                )
                for item in payload.get("inflow", [])
            ]
            result = model.simulate_reservoir(
                points,
                _config_from_json(payload.get("config") or {}),
            )
            data = asdict(result)
            peak_index = max(range(len(data["level_m"])), key=data["level_m"].__getitem__)
            powers = data.get("power_mw") or []
            energy = 0.0
            for index in range(1, len(data["hour"])):
                dt_h = float(data["hour"][index]) - float(data["hour"][index - 1])
                if powers and dt_h > 0:
                    energy += 0.5 * (float(powers[index]) + float(powers[index - 1])) * dt_h
            return jsonify(
                ok=True,
                result=data,
                summary={
                    "peak_level_m": data["level_m"][peak_index],
                    "peak_level_hour": data["hour"][peak_index],
                    "peak_release_m3s": max(data["release_m3s"]),
                    "peak_spill_m3s": max(data["spill_m3s"]),
                    "peak_power_mw": max(powers) if powers else 0.0,
                    "energy_mwh": energy,
                    "final_zone": data["zone"][-1],
                },
            )
        except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @bp.get("/api/nam-inflow")
    def nam_inflow():
        try:
            model = _model_module()
            points = model.read_inflow_csv(NAM_RESULT_CSV)
            return jsonify(
                ok=True,
                source=str(NAM_RESULT_CSV),
                inflow=[asdict(point) for point in points],
                count=len(points),
            )
        except (ValueError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 404

    @bp.get("/api/has")
    def has_table():
        try:
            model = _model_module()
            table = model.resolve_has(model.ReservoirConfig())
            if table is None or table.empty():
                return jsonify(ok=False, error="Chua co bang H-A-S"), 404
            return jsonify(ok=True, has=asdict(table))
        except (TypeError, ValueError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @bp.post("/api/has-from-dem")
    def has_from_dem():
        try:
            payload = request.get_json(silent=True) or {}
            model = _model_module()
            saved = _read_saved_params() or {}
            raw_config = dict(saved.get("config") or {})
            incoming = payload.get("config") or {}
            if isinstance(incoming, dict):
                raw_config.update(incoming)
            config = _config_from_json(raw_config)
            station_raw = payload.get("station_km", None)
            reach = str(payload.get("reach") or "").strip()
            if station_raw is None or str(station_raw).strip() == "":
                row = _reservoir_row()
                if row is not None:
                    station_raw = row.get("station_km")
                    if not reach:
                        reach = str(row.get("reach") or "").strip()
            if not reach:
                reach = "main"
            station_km = float(station_raw if station_raw not in (None, "") else 0.5)
            table = model.has_from_dem(
                station_km=station_km,
                reach=reach,
                bed_m=config.bed_m if payload.get("use_config_bed") else None,
                max_level_m=config.max_level_m,
                step_m=float(payload.get("step_m") or 0.25),
                storage_area_m2=config.storage_area_m2,
            )
            out = Path(payload.get("has_file") or model.DEFAULT_HAS_CSV)
            if not out.is_absolute():
                out = ROOT / out
            if payload.get("write", True):
                model.write_has_csv(out, table)
                table.source = str(out)
            return jsonify(ok=True, has=asdict(table), path=str(out))
        except (TypeError, ValueError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @bp.get("/api/params")
    def params_get():
        try:
            data = _read_saved_params()
            if not data:
                return jsonify(ok=False, error="Chua luu thong so ho"), 404
            config = asdict(_config_from_json(data.get("config") or {}))
            saved_gates = data.get("spillway_gates_m")
            gates = _gates_from_payload(
                saved_gates,
                float(config.get("spillway_width_m") or 0.0),
            )
            config, gates, linked = _overlay_reservoir_construction(config, gates)
            has_bed = _has_bed_m()
            if has_bed is not None:
                config["bed_m"] = has_bed
            config["spillway_width_m"] = sum(gates)
            saved_modes = _gate_specs(data.get("spillway_gates"))
            fallback_control = _gate_control(config.get("spillway_control"))
            fallback_opening = float(config.get("spillway_opening_m") or 0.0)
            rich_specs: list[tuple[float, str, float]] = []
            for index, width in enumerate(gates):
                if saved_modes is not None and index < len(saved_modes):
                    _width, control, opening = saved_modes[index]
                    rich_specs.append((float(width), control, opening))
                else:
                    rich_specs.append((float(width), fallback_control, fallback_opening))
            rich = _gates_public(tuple(rich_specs))
            units = _turbine_units_from_payload(data.get("turbine_units"))
            if not units:
                count = int(float(config.get("unit_count") or 0))
                power = float(config.get("unit_rated_mw") or 0)
                flow = float(config.get("unit_qmax_m3s") or 0)
                if count > 0:
                    units = [{"rated_mw": power, "qmax_m3s": flow} for _ in range(count)]
            response = jsonify(
                ok=True,
                config=config,
                spillway_gates_m=gates,
                spillway_gates=rich,
                turbine_units=units,
                start_date=str(data.get("start_date") or ""),
                from_construction=linked,
            )
            response.headers["Cache-Control"] = "no-store"
            return response
        except (TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @bp.route("/api/params", methods=["PUT", "POST"])
    def params_put():
        try:
            payload = request.get_json(silent=True) or {}
            raw_config = dict(payload.get("config") or {})
            saved = _read_saved_params() or {}
            merged = dict(saved.get("config") or {})
            merged.update(raw_config)
            units = _turbine_units_from_payload(payload.get("turbine_units"))
            if units and merged.get("operation_set") in ("hydropower", "flood_power"):
                merged["unit_count"] = len(units)
                merged["installed_mw"] = sum(item["rated_mw"] for item in units)
                merged["unit_rated_mw"] = 0
                merged["unit_qmax_m3s"] = 0
                if merged.get("operation_set") == "hydropower":
                    merged["outlet_capacity_m3s"] = sum(item["qmax_m3s"] for item in units)
            config = _config_from_json(merged)
            config.validate()
            values = asdict(config)
            values.pop("spillway_gates", None)
            specs = _gate_specs(payload.get("spillway_gates"))
            if specs is None and isinstance(payload.get("config"), dict):
                specs = _gate_specs(payload["config"].get("spillway_gates"))
            if specs is not None:
                gates = [width for width, _control, _opening in specs]
            else:
                gates = _gates_from_payload(
                    payload.get("spillway_gates_m"),
                    float(values.get("spillway_width_m") or 0.0),
                )
                control = _gate_control(values.get("spillway_control"))
                opening = max(float(values.get("spillway_opening_m") or 0.0), 0.0)
                specs = tuple((width, control, opening) for width in gates)
            if specs:
                values["spillway_control"] = specs[0][1]
                controlled = [item[2] for item in specs if item[1] == "controlled"]
                if controlled:
                    values["spillway_opening_m"] = controlled[0]
            values["spillway_width_m"] = sum(gates)
            rich = _gates_public(specs)
            start_date = str(payload.get("start_date") or saved.get("start_date") or "").strip()
            synced = _push_reservoir_to_construction(values, gates)
            out = {
                "config": values,
                "spillway_gates_m": gates,
                "spillway_gates": rich,
                "turbine_units": units,
            }
            if start_date:
                out["start_date"] = start_date
            _write_saved_params(out)
            return jsonify(
                ok=True,
                config=values,
                spillway_gates_m=gates,
                spillway_gates=rich,
                turbine_units=units,
                start_date=start_date,
                synced_construction=synced,
                path=str(PARAMS_JSON),
            )
        except (TypeError, ValueError, OSError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    @bp.get("/api/curve")
    def curve():
        try:
            model = _model_module()
            config = _config_from_json(request.args.to_dict())
            data = asdict(model.reservoir_curve(config))
            return jsonify(ok=True, curve=data)
        except (TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400

    return bp
"""API chay Model RR (TANK / NAM) va Model 1D (Saint-venant / Saint-venant-1D).

POST /api/flow-3d/simulate       Model 1D
GET  /api/flow-3d/simulate       trang thai job RR / NAM / 1D
POST /api/flow-3d/simulate-rr    Model RR (TANK)
POST /api/flow-3d/simulate-nam   Model NAM
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.boundary import NAM_RESULT_CSV, TANK_OUT_DIR, TANK_RAIN_CSV, TANK_RESULT_CSV
from flood_model.csv_io import csv_available
from flood_model.gis import resolve_dem_path
from flood_model.routing import parse_hydro1d_source

HYDRO1D_MAX_MAINS = 8
HYDRO1D_MAX_TRIBS = 12

ROOT = PACKAGE_DIR
SAINT_VENANT_N_CSV = ROOT / "saint_venant_output" / "demo_manning_n.csv"
SAINT_VENANT_RESULT_CSV = ROOT / "saint_venant_output" / "saint_venant_result.csv"
TANK_PARAMS_CSV = TANK_OUT_DIR / "demo_tank_params.csv"
NAM_PARAMS_CSV = TANK_OUT_DIR / "demo_nam_params.csv"
MIKE_HD_OUT_DIR = ROOT / "mike_hd_output"
MIKE_HD_RESULT_COPIES = (
    ("mike_hd_result.csv", "saint_venant_result.csv"),
    ("mike_hd_river_geometry.csv", "demo_river_geometry.csv"),
    ("mike_hd_cross_sections.csv", "demo_cross_sections.csv"),
    ("mike_hd_tributary_geometry.csv", "demo_tributary_geometry.csv"),
    ("mike_hd_tributary_result.csv", "saint_venant_tributary_result.csv"),
    ("mike_hd_extra_main_result.csv", "demo_extra_main_result.csv"),
    ("mike_hd_network_reaches.csv", "demo_network_reaches.csv"),
)

_SIM_LOCK = threading.Lock()
_SIM_LOG_MAX = 120
_SIM_JOB: dict[str, Any] = {
    "status": "idle",
    "kind": None,
    "water_source": None,
    "label": None,
    "message": "",
    "error": None,
    "result_csv": None,
    "started_at": None,
    "elapsed_s": 0.0,
    "log": [],
    "log_tail": None,
}


def _f3():
    from flood_model import flow_3d

    return flow_3d


def _sim_snapshot() -> dict[str, Any]:
    with _SIM_LOCK:
        job = dict(_SIM_JOB)
        started = job.get("started_at")
        if job.get("status") == "running" and started:
            job["elapsed_s"] = round(time.time() - float(started), 1)
        elif job.get("elapsed_s") is not None:
            job["elapsed_s"] = round(float(job["elapsed_s"]), 1)
        log = list(job.get("log") or [])
    job.pop("started_at", None)
    job["log"] = log
    job["ok"] = True
    return job


def _append_sim_log(line: str) -> None:
    text = (line or "").rstrip()
    if not text:
        return
    with _SIM_LOCK:
        log = list(_SIM_JOB.get("log") or [])
        log.append(text)
        if len(log) > _SIM_LOG_MAX:
            log = log[-_SIM_LOG_MAX:]
        _SIM_JOB["log"] = log
        _SIM_JOB["message"] = text[:240]


def _sim_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _begin_job(**fields: Any) -> bool:
    with _SIM_LOCK:
        if _SIM_JOB.get("status") == "running":
            return False
        _SIM_JOB.update(
            {
                "status": "running",
                "error": None,
                "result_csv": None,
                "started_at": time.time(),
                "elapsed_s": 0.0,
                "log": [],
                "log_tail": None,
                **fields,
            }
        )
        return True


def _finish_ok(result_csv: Optional[Path], t0: float, done: str, tail: str) -> None:
    with _SIM_LOCK:
        _SIM_JOB["status"] = "ok"
        _SIM_JOB["error"] = None
        _SIM_JOB["result_csv"] = str(result_csv) if result_csv and csv_available(result_csv) else None
        _SIM_JOB["elapsed_s"] = time.time() - t0
        _SIM_JOB["message"] = done
        _SIM_JOB["log_tail"] = tail or None
    _append_sim_log(done)


def _finish_err(prefix: str, exc: BaseException, t0: float) -> None:
    traceback.print_exc()
    with _SIM_LOCK:
        _SIM_JOB["status"] = "error"
        _SIM_JOB["error"] = str(exc)
        _SIM_JOB["elapsed_s"] = time.time() - t0
        _SIM_JOB["message"] = f"{prefix}: {exc}"
    _append_sim_log(_SIM_JOB["message"])


def _run_logged_cmd(cmd: list[str]) -> str:
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
    return tail


def _hydro1d_script(src: str = "saint-venant") -> tuple[Path, str, Path]:
    kind = parse_hydro1d_source(src)
    if kind == "saint-venant-1d":
        script = ROOT / "sv-mike-by-dhi.py"
        if not script.is_file():
            raise FileNotFoundError("Khong thay sv-mike-by-dhi.py")
        return script, "Saint-venant-1D", SAINT_VENANT_RESULT_CSV
    script = ROOT / "saint-venant.py"
    if not script.is_file():
        raise FileNotFoundError("Khong thay saint-venant.py")
    return script, "Saint-venant", SAINT_VENANT_RESULT_CSV


def _publish_mike_hd_results() -> None:
    dest_dir = ROOT / "saint_venant_output"
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_name, dest_name in MIKE_HD_RESULT_COPIES:
        src = MIKE_HD_OUT_DIR / src_name
        if not src.is_file():
            continue
        shutil.copy2(src, dest_dir / dest_name)
        copied += 1
    if copied:
        _append_sim_log(
            f"Da chep {copied} file ket qua MIKE HD sang saint_venant_output (mat cat / ngap lut)."
        )


def _run_simulate_job(
    src: str,
    dem: Path,
    xs_spacing_m: float | None = None,
    dt_hours: float | None = None,
    hd_params: dict[str, Any] | None = None,
) -> None:
    t0 = time.time()
    f3 = _f3()
    try:
        script, label, result_csv = _hydro1d_script(src)
        cmd = [sys.executable, "-u", str(script), "--no-plot", "--dem", str(dem)]
        xs_m = f3.parse_request_xs_spacing_m(xs_spacing_m)
        dt_h = f3.parse_request_dt_hours(dt_hours)
        hd = f3.parse_sv_hd_params(hd_params, src)
        sv_max_mains = HYDRO1D_MAX_MAINS
        sv_max_tribs = HYDRO1D_MAX_TRIBS
        cmd += ["--xs-spacing", str(xs_m), "--dt", str(dt_h)]
        cmd += ["--cfl", str(hd["cfl"]), "--dt-hydro", str(hd["dt_hydro_max_s"])]
        cmd += ["--max-mains", str(sv_max_mains), "--max-tribs", str(sv_max_tribs)]
        if parse_hydro1d_source(src) == "saint-venant-1d":
            cmd += [
                "--theta",
                str(hd["theta"]),
                "--picard",
                str(int(hd["n_picard"])),
                "--convective",
                str(hd["convective"]),
                "--manning-blend",
                str(hd["manning_blend"]),
                "--q-relax",
                str(hd["q_relax"]),
            ]
        if not csv_available(SAINT_VENANT_N_CSV):
            raise FileNotFoundError(
                "Thieu saint_venant_output/demo_manning_n.csv "
                "(he so nham n theo tung mat cat)."
            )
        cmd += ["--n-csv", str(SAINT_VENANT_N_CSV)]
        extra = f", CFL {hd['cfl']:g}, dt hydro {hd['dt_hydro_max_s']:g} s"
        if parse_hydro1d_source(src) == "saint-venant-1d":
            extra += (
                f", theta {hd['theta']:g}, Picard {int(hd['n_picard'])}, "
                f"convective {hd['convective']:g}"
            )
        with _SIM_LOCK:
            _SIM_JOB["message"] = (
                f"Dang chay {label} (XS moi {xs_m:.0f} m, dt {dt_h:g} h"
                f"{extra}, toi da {sv_max_mains} song chinh / {sv_max_tribs} song nhanh, "
                f"n tu {SAINT_VENANT_N_CSV.name})..."
            )
        _append_sim_log(
            f"Chay {label}: {script.name}  --xs-spacing {xs_m:.0f}  --dt {dt_h:g}  "
            f"--cfl {hd['cfl']:g}  --dt-hydro {hd['dt_hydro_max_s']:g}  "
            f"--max-mains {sv_max_mains}  --max-tribs {sv_max_tribs}  "
            f"--n-csv {SAINT_VENANT_N_CSV}"
        )
        tail = _run_logged_cmd(cmd)
        if parse_hydro1d_source(src) == "saint-venant-1d":
            _publish_mike_hd_results()
        f3._clear_hydro_caches()
        done = f"Xong {label} ({time.time() - t0:.0f} s)."
        _finish_ok(result_csv, t0, done, tail)
    except Exception as exc:
        _finish_err("Loi mo phong 1D", exc, t0)


def _rr_script() -> tuple[Path, str, Path]:
    script = ROOT / "rainfall-runoff.py"
    if not script.is_file():
        raise FileNotFoundError("Khong thay rainfall-runoff.py")
    return script, "TANK mưa-dòng chảy", TANK_RESULT_CSV


def _run_rr_simulate_job() -> None:
    t0 = time.time()
    f3 = _f3()
    try:
        script, label, result_csv = _rr_script()
        basin, params = f3.load_runoff_params()
        if not csv_available(TANK_PARAMS_CSV):
            f3.save_runoff_params(f3._tank_mod()["params_to_dict"](basin, params))
        cmd = [
            sys.executable,
            "-u",
            str(script),
            "--no-plot",
            "--area",
            str(basin.area_km2),
            "--dt",
            str(basin.dt_hours),
            "--params-csv",
            str(TANK_PARAMS_CSV),
            "--out-dir",
            str(TANK_OUT_DIR),
        ]
        if csv_available(TANK_RAIN_CSV):
            cmd += ["--csv", str(TANK_RAIN_CSV)]
        with _SIM_LOCK:
            _SIM_JOB["message"] = f"Dang chay {label}..."
        _append_sim_log(f"Chay {label}: {script.name}")
        tail = _run_logged_cmd(cmd)
        f3._clear_runoff_cache()
        done = f"Xong {label} ({time.time() - t0:.0f} s)."
        _finish_ok(result_csv, t0, done, tail)
    except Exception as exc:
        _finish_err("Loi mo phong RR", exc, t0)


def _nam_script() -> tuple[Path, str, Path]:
    script = ROOT / "mike-nam.py"
    if not script.is_file():
        raise FileNotFoundError("Khong thay mike-nam.py")
    return script, "MIKE NAM mưa-dòng chảy", NAM_RESULT_CSV


def _run_nam_simulate_job() -> None:
    t0 = time.time()
    f3 = _f3()
    try:
        script, label, result_csv = _nam_script()
        basin, params = f3.load_nam_params()
        if not csv_available(NAM_PARAMS_CSV):
            f3.save_nam_params(f3._nam_mod()["params_to_dict"](basin, params))
        cmd = [
            sys.executable,
            "-u",
            str(script),
            "--no-plot",
            "--area",
            str(basin.area_km2),
            "--dt",
            str(basin.dt_hours),
            "--params-csv",
            str(NAM_PARAMS_CSV),
            "--out-dir",
            str(TANK_OUT_DIR),
        ]
        if csv_available(TANK_RAIN_CSV):
            cmd += ["--csv", str(TANK_RAIN_CSV)]
        with _SIM_LOCK:
            _SIM_JOB["message"] = f"Dang chay {label}..."
        _append_sim_log(f"Chay {label}: {script.name}")
        tail = _run_logged_cmd(cmd)
        done = f"Xong {label} ({time.time() - t0:.0f} s)."
        _finish_ok(result_csv, t0, done, tail)
    except Exception as exc:
        _finish_err("Loi mo phong NAM", exc, t0)


def start_hydro1d_simulate(
    water_source: Any,
    dem: Path,
    xs_spacing: Any = None,
    dt_hours: Any = None,
    hd_params: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    f3 = _f3()
    src = parse_hydro1d_source(water_source)
    _script, label, _csv = _hydro1d_script(src)
    xs_m = f3.parse_request_xs_spacing_m(xs_spacing)
    dt_h = f3.parse_request_dt_hours(dt_hours)
    hd = f3.parse_sv_hd_params(hd_params, src)
    started = _begin_job(
        kind="1d",
        water_source=src,
        label=label,
        xs_spacing_m=xs_m,
        dt_hours=dt_h,
        cfl=hd["cfl"],
        dt_hydro_max_s=hd["dt_hydro_max_s"],
        message=f"Dang chay {label} (XS moi {xs_m:.0f} m, dt {dt_h:g} h)...",
    )
    if not started:
        return False, _sim_snapshot()
    threading.Thread(
        target=_run_simulate_job,
        args=(src, dem, xs_m, dt_h, hd),
        daemon=True,
    ).start()
    return True, _sim_snapshot()


def start_rr_simulate() -> tuple[bool, dict[str, Any]]:
    _script, label, _csv = _rr_script()
    started = _begin_job(
        kind="rr",
        water_source="tank",
        label=label,
        message=f"Dang chay {label}...",
    )
    if not started:
        return False, _sim_snapshot()
    threading.Thread(target=_run_rr_simulate_job, daemon=True).start()
    return True, _sim_snapshot()


def start_nam_simulate() -> tuple[bool, dict[str, Any]]:
    _script, label, _csv = _nam_script()
    started = _begin_job(
        kind="nam",
        water_source="nam",
        label=label,
        message=f"Dang chay {label}...",
    )
    if not started:
        return False, _sim_snapshot()
    threading.Thread(target=_run_nam_simulate_job, daemon=True).start()
    return True, _sim_snapshot()


def create_run_model_blueprint(
    name: str = "run_model",
    url_prefix: str = "/api/flow-3d",
) -> Blueprint:
    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    def _fail(message: str, status: int = 500):
        return jsonify({"ok": False, "error": str(message)}), status

    @bp.route("/simulate", methods=["GET"])
    def api_simulate_status():
        return jsonify(_sim_snapshot())

    @bp.route("/simulate", methods=["POST"])
    def api_simulate_start():
        try:
            data = request.get_json(silent=True) or {}
            dem = resolve_dem_path(data.get("file_id"), data.get("dem_path"))
            started, job = start_hydro1d_simulate(
                data.get("water_source"),
                dem,
                data.get("xs_spacing_m", data.get("xs_spacing")),
                data.get("dt_hours", data.get("dt")),
                data,
            )
            code = 202 if started else 409
            job["started"] = started
            return jsonify(job), code
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mo phong 1D: {exc}", 500)

    @bp.route("/simulate-rr", methods=["POST"])
    def api_simulate_rr_start():
        try:
            started, job = start_rr_simulate()
            code = 202 if started else 409
            job["started"] = started
            return jsonify(job), code
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mo phong RR: {exc}", 500)

    @bp.route("/simulate-nam", methods=["POST"])
    def api_simulate_nam_start():
        try:
            started, job = start_nam_simulate()
            code = 202 if started else 409
            job["started"] = started
            return jsonify(job), code
        except FileNotFoundError as exc:
            return _fail(exc, 404)
        except ValueError as exc:
            return _fail(exc, 400)
        except Exception as exc:
            traceback.print_exc()
            return _fail(f"Loi mo phong NAM: {exc}", 500)

    return bp

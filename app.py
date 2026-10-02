"""Ung dung web doc lap: DEM 3D, TANK, Saint-Venant 1D va ngap lut.

Chay:
  flood_model\\run.bat
  .\\venv\\Scripts\\python.exe app.py
  .\\venv\\Scripts\\python.exe -m flood_model --web
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
# Chi de import goi `flood_model` khi chay app.py truc tiep; du lieu nam trong PACKAGE_DIR.
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flask import Blueprint, Flask, render_template
from flask.json.provider import DefaultJSONProvider

from flood_model.csv_io import csv_available
from flood_model.dem_3d import create_dem3d_blueprint
from flood_model.flood_3d import create_flood3d_blueprint
from flood_model.api_run_model import create_run_model_blueprint
from flood_model.flow_3d import create_flow3d_blueprint
from flood_model.flow_run import create_flowrun_blueprint
from flood_model.gis import resolve_dem_path
from flood_model.paths import SAINT_VENANT_H_CSV
from flood_model.routing import HYDRO1D_FORCE_MIKE
from flood_model.reservoir_web import create_reservoir_blueprint


class Utf8JSONProvider(DefaultJSONProvider):
    """JSON UTF-8 (tiếng Việt), không escape \\uXXXX."""

    ensure_ascii = False


def _enable_utf8_json(flask_app: Flask) -> None:
    flask_app.json_provider_class = Utf8JSONProvider
    flask_app.json = Utf8JSONProvider(flask_app)
    flask_app.config["JSON_AS_ASCII"] = False


def create_web_blueprint(url_prefix: str = "/flood-model") -> Blueprint:
    bp = Blueprint(
        "flood_model_web",
        __name__,
        url_prefix=url_prefix,
        template_folder=str(PACKAGE_DIR / "templates"),
        static_folder=str(PACKAGE_DIR / "static"),
        static_url_path="/static",
    )

    @bp.route("/")
    def index():
        return render_template(
            "flood_app.html",
            has_1d=csv_available(SAINT_VENANT_H_CSV),
            hydro1d_force_mike=HYDRO1D_FORCE_MIKE,
            flood_base=url_prefix,
            flood_static_endpoint="flood_model_web.static",
            home_2d_url="/",
            reservoir_url=f"{url_prefix.rstrip('/')}/reservoir/",
        )

    return bp


def register_flood_model(flask_app: Flask, url_prefix: str = "/flood-model") -> None:
    """Gắn UI + API flood_model lên Flask app chính (có đăng nhập)."""
    _enable_utf8_json(flask_app)
    prefix = url_prefix.rstrip("/") or "/flood-model"
    flask_app.register_blueprint(create_web_blueprint(prefix))
    flask_app.register_blueprint(create_dem3d_blueprint(name="fm_dem3d"), url_prefix=prefix)
    flask_app.register_blueprint(
        create_flood3d_blueprint(
            name="fm_flood3d",
            url_prefix=f"{prefix}/api/flood-3d",
            with_static=False,
        )
    )
    flask_app.register_blueprint(
        create_flow3d_blueprint(name="fm_flow3d", url_prefix=f"{prefix}/api/flow-3d")
    )
    flask_app.register_blueprint(
        create_run_model_blueprint(name="fm_run_model", url_prefix=f"{prefix}/api/flow-3d")
    )
    flask_app.register_blueprint(
        create_flowrun_blueprint(
            name="fm_flowrun",
            url_prefix=f"{prefix}/api/flow-run",
            root=PACKAGE_DIR,
            resolve_dem=lambda file_id, dem_path: resolve_dem_path(file_id, dem_path),
        )
    )
    flask_app.register_blueprint(
        create_reservoir_blueprint(
            name="fm_reservoir",
            url_prefix=f"{prefix}/reservoir",
        )
    )


app = Flask(
    __name__,
    template_folder=str(PACKAGE_DIR / "templates"),
    static_folder=str(PACKAGE_DIR / "static"),
    static_url_path="/static",
)
_enable_utf8_json(app)
app.register_blueprint(create_dem3d_blueprint())
app.register_blueprint(create_flood3d_blueprint())
app.register_blueprint(create_flow3d_blueprint())
app.register_blueprint(create_run_model_blueprint())
app.register_blueprint(
    create_flowrun_blueprint(
        name="flowrun",
        url_prefix="/api/flow-run",
        root=PACKAGE_DIR,
        resolve_dem=lambda file_id, dem_path: resolve_dem_path(file_id, dem_path),
    )
)
app.register_blueprint(create_reservoir_blueprint())


@app.route("/")
def index():
    return render_template(
        "flood_app.html",
        has_1d=csv_available(SAINT_VENANT_H_CSV),
        hydro1d_force_mike=HYDRO1D_FORCE_MIKE,
        flood_base="",
        home_2d_url="",
        reservoir_url="/reservoir/",
    )


def run_server() -> int:
    debug = os.environ.get("FLASK_DEBUG", "1") != "0"
    host = os.environ.get("FLOOD_HOST", "0.0.0.0")
    port = int(os.environ.get("FLOOD_PORT", os.environ.get("PORT", "5001")))
    print(f"Flood model: http://127.0.0.1:{port}/", flush=True)
    try:
        from flood_model.db import db_status

        st = db_status()
        # if st.get("ok"):
        #     print(
        #         f"data_flood: {st['n_datasets']} bang CSV, "
        #         f"hydro_timeseries {st['hydro_timeseries_rows']} moc "
        #         f"({st['user']}@{st['host']}:{st['port']})",
        #         flush=True,
        #     )
        # else:
        #     print(f"data_flood chua san sang: {st.get('error')}", flush=True)
    except Exception as exc:
        print(f"data_flood: {exc}", flush=True)
    app.run(debug=debug, host=host, port=port, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())

"""
File nay chi giu phan can thiet de "view du lieu" theo kieu Digital Twin:
- `static/vendors/scripts/Render-3D.js`
- `static/vendors/scripts/view3D_simulator.js`

"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_SCRIPT_DIR = PROJECT_ROOT / "static" / "vendors" / "scripts"
RENDER_3D_PATH = STATIC_SCRIPT_DIR / "Render-3D.js"
VIEW3D_SIMULATOR_PATH = STATIC_SCRIPT_DIR / "view3D_simulator.js"

BUTTON_IDS = ("toggle3D", "render3DBtn")
SCRIPT_FILES = (
    "vendors/scripts/view3D_simulator.js",
    "vendors/scripts/Render-3D.js",
)


@dataclass(frozen=True)
class DigitalTwinAsset:
    key: str
    path: str
    kind: str
    description: str


ASSETS: tuple[DigitalTwinAsset, ...] = (
    DigitalTwinAsset(
        key="render_3d",
        path=str(RENDER_3D_PATH),
        kind="javascript",
        description="Three.js overlay renderer cho Digital Twin tren nen Leaflet.",
    ),
    DigitalTwinAsset(
        key="view3d_simulator",
        path=str(VIEW3D_SIMULATOR_PATH),
        kind="javascript",
        description="Simulator/bridge 3D va dong bo overlay lien quan Digital Twin.",
    ),
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig")


def _asset_by_key(asset_key: str) -> DigitalTwinAsset:
    normalized = str(asset_key or "").strip().lower()
    for item in ASSETS:
        if item.key == normalized:
            return item
    raise KeyError(f"Khong tim thay asset Digital Twin: {asset_key}")


def asset_exists(asset_key: str) -> bool:
    try:
        asset = _asset_by_key(asset_key)
    except KeyError:
        return False
    return Path(asset.path).is_file()


def load_asset_text(asset_key: str) -> str:
    asset = _asset_by_key(asset_key)
    path = Path(asset.path)
    if not path.is_file():
        raise FileNotFoundError(f"Khong tim thay file nguon: {path}")
    return _read_text(path)


def load_render_3d_script() -> str:
    return load_asset_text("render_3d")


def load_view3d_simulator_script() -> str:
    return load_asset_text("view3d_simulator")


def build_digital_twin_bundle(include_source_text: bool = False) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "project_root": str(PROJECT_ROOT),
        "button_ids": list(BUTTON_IDS),
        "script_files": list(SCRIPT_FILES),
        "assets": [],
    }
    for item in ASSETS:
        payload = asdict(item)
        payload["exists"] = Path(item.path).is_file()
        if include_source_text and payload["exists"] and item.kind in {"html", "javascript", "css"}:
            payload["text"] = load_asset_text(item.key)
        bundle["assets"].append(payload)
    return bundle


def create_digital_twin_blueprint(
    name: str = "digital_twin",
    url_prefix: str = "/api/digital-twin",
) -> Blueprint:
    """
    Blueprint phuc vu thong tin/asset Digital Twin da trich xuat tu du an nguon.

    Khong tu dong gan vao `app.py`; chi tao san de co the register khi can.
    """

    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.route("/manifest", methods=["GET"])
    def manifest() -> Response:
        return jsonify(
            {
                "success": True,
                "digitalTwin": build_digital_twin_bundle(include_source_text=False),
            }
        )

    @bp.route("/snippet", methods=["GET"])
    def snippet() -> Response:
        return Response(
            "<button id=\"render3DBtn\">Digital Twins</button>",
            mimetype="text/html",
        )

    @bp.route("/render-3d.js", methods=["GET"])
    def render_3d_js() -> Response:
        return Response(load_render_3d_script(), mimetype="application/javascript")

    @bp.route("/view3d-simulator.js", methods=["GET"])
    def view3d_simulator_js() -> Response:
        return Response(load_view3d_simulator_script(), mimetype="application/javascript")

    return bp

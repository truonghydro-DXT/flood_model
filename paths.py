"""Duong dan mac dinh — toan bo nam trong thu muc flood_model."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR

DEFAULT_DEM = PROJECT_ROOT / "projects" / "data" / "dem-song-hong.tif"
DEFAULT_OUT_DIR = PROJECT_ROOT / "flood_output"
SAINT_VENANT_GEOM_CSV = PROJECT_ROOT / "saint_venant_output" / "demo_river_geometry.csv"
SAINT_VENANT_H_CSV = PROJECT_ROOT / "saint_venant_output" / "saint_venant_result.csv"
SAINT_VENANT_TRIB_GEOM_CSV = PROJECT_ROOT / "saint_venant_output" / "demo_tributary_geometry.csv"
SAINT_VENANT_TRIB_H_CSV = PROJECT_ROOT / "saint_venant_output" / "saint_venant_tributary_result.csv"
RAINFALL_RUNOFF_PY = PACKAGE_DIR / "rainfall-runoff.py"
SAINT_VENANT_PY = PACKAGE_DIR / "saint-venant.py"
MUSKINGUM_CUNG_PY = PACKAGE_DIR / "muskingum-cung.py"

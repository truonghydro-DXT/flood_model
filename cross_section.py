"""Importable alias for hyphenated cross-section.py."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).with_name("cross-section.py")
_spec = importlib.util.spec_from_file_location("flood_model._cross_section_impl", _path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Khong nap duoc {_path}")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["flood_model._cross_section_impl"] = _mod
_spec.loader.exec_module(_mod)

from flood_model._cross_section_impl import (  # noqa: F401  # pyright: ignore[reportMissingImports]
    ChannelParams,
    CrossSection,
    MAX_PROFILE_POINTS,
    RiverGeom,
    XS_SPACING_M,
    chainage_m,
    densify_xy,
    extract_reach_from_lonlat,
    extract_river,
    extend_section_tables,
    geom_from_polyline,
    load_official_xs,
    lower_envelope,
    profile_from_lonlat,
    sample_cross_section,
    sample_z,
    section_tables,
    write_cross_sections_csv,
)

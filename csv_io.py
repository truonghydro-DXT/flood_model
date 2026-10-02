"""Đọc/ghi số liệu flood_model qua PostgreSQL data_flood (không ghi file CSV)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))


def _db():
    try:
        from flood_model import db as mod
    except ImportError:
        import db as mod  # type: ignore
    return mod


def csv_open(path: Path):
    return _db().csv_open(path)


def csv_available(path: Path) -> bool:
    return _db().csv_available(Path(path))


def write_csv_rows(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    extrasaction: str = "ignore",
    rebuild_hydro: bool = True,
) -> None:
    _db().write_csv_rows(
        Path(path),
        fieldnames,
        rows,
        extrasaction=extrasaction,
        rebuild_hydro=rebuild_hydro,
    )


def export_csv_file(path: Path) -> bool:
    return _db().export_csv_file(Path(path))


def export_all_csvs(root: Path | None = None) -> dict[str, Any]:
    return _db().export_all_csvs(root)


def sync_csv_file_safe(path: Path) -> None:
    _db().sync_csv_file_safe(Path(path))

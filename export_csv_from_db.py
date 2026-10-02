"""Xuat toan bo CSV tu PostgreSQL data_flood ra thu muc o dia tuong ung.

Chay:
  .\\venv\\Scripts\\python.exe flood_model\\export_csv_from_db.py
  .\\venv\\Scripts\\python.exe -m flood_model.export_csv_from_db
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.db import db_config, export_all_csvs  # noqa: E402


def main() -> int:
    cfg = db_config()
    print(
        f"PostgreSQL {cfg['user']}@{cfg['host']}:{cfg['port']} / {cfg['dbname']}",
        flush=True,
    )
    result = export_all_csvs()
    for item in result["exported"]:
        print(
            f"  {item['n_rows']:6d} hang  -> {item['file']}",
            flush=True,
        )
    for err in result["errors"]:
        print(f"  LOI {err['file']}: {err['error']}", flush=True)
    print(
        f"Xong. {result['n_files']} file CSV tai {result['root']}",
        flush=True,
    )
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

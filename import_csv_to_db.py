"""Nhap toan bo CSV flood_model vao PostgreSQL (CSDL data_flood).

Chay:
  .\\venv\\Scripts\\python.exe flood_model\\import_csv_to_db.py
  .\\venv\\Scripts\\python.exe -m flood_model.import_csv_to_db
"""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.db import db_config, import_all_csvs  # noqa: E402


def main() -> int:
    cfg = db_config()
    print(
        f"PostgreSQL {cfg['user']}@{cfg['host']}:{cfg['port']} / {cfg['dbname']}",
        flush=True,
    )
    result = import_all_csvs()
    for item in result["imported"]:
        print(
            f"  {item['table']:40s}  {item['n_rows']:6d} hang  <- {item['file']}",
            flush=True,
        )
    for err in result["errors"]:
        print(f"  LOI {err['file']}: {err['error']}", flush=True)
    print(
        f"Xong. {result['n_files']} bang, hydro_timeseries = {result['hydro_timeseries_rows']} moc thoi gian.",
        flush=True,
    )
    if result["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""PostgreSQL data_flood — nhập CSV flood_model, đọc/ghi, chuỗi thủy văn.

Kết nối lấy host/user/password/port từ .env (thư mục flood_model rồi gốc dự án).
Tên CSDL luôn là data_flood (hoặc FLOOD_DB_NAME).
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence, TextIO

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
TZ_VN = timezone(timedelta(hours=7))
TIMESERIES_START = datetime(2024, 7, 1, 0, 0, tzinfo=TZ_VN)
TS_LABEL_FMT = "%Y-%m-%d %H:%M"
FLOOD_DB_NAME = "data_flood"
HYDRO_KEYS = ("rainfall_mm", "pet_mm", "et_mm", "q_m3s", "h_m")

_IDENT_RE = re.compile(r"[^a-z0-9_]+")
_FOLDER_PREFIX = {
    "rainfall_runoff_output": "rr",
    "saint_venant_output": "sv",
    "muskingum_output": "mk",
}

_DOTENV_LOADED = False
_DB_CREATED = False


def _load_env() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        _DOTENV_LOADED = True
        return
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PACKAGE_DIR / ".env", override=True)
    _DOTENV_LOADED = True


def db_config(*, admin: bool = False) -> dict[str, Any]:
    _load_env()
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": "postgres" if admin else os.getenv("FLOOD_DB_NAME", FLOOD_DB_NAME),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
    }


def _psycopg2():
    try:
        import psycopg2
        from psycopg2 import sql
        from psycopg2.extras import Json, RealDictCursor, execute_values
    except ImportError as exc:
        raise ImportError(
            "Can psycopg2 de noi PostgreSQL. Cai: "
            ".\\venv\\Scripts\\python.exe -m pip install psycopg2-binary python-dotenv"
        ) from exc
    return psycopg2, sql, Json, RealDictCursor, execute_values


def ensure_database() -> str:
    """Tạo CSDL data_flood nếu chưa có. Trả về tên CSDL."""
    global _DB_CREATED
    psycopg2, sql, *_rest = _psycopg2()
    cfg = db_config()
    name = str(cfg["dbname"])
    if _DB_CREATED:
        return name
    admin = db_config(admin=True)
    conn = psycopg2.connect(**admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    finally:
        conn.close()
    _DB_CREATED = True
    return name


@contextmanager
def get_connection(*, autocommit: bool = False):
    psycopg2, *_rest = _psycopg2()
    ensure_database()
    conn = psycopg2.connect(**db_config())
    conn.autocommit = autocommit
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE '+07'")
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def sql_ident(name: str, *, prefix: str = "c") -> str:
    s = _IDENT_RE.sub("_", str(name).strip().lower()).strip("_")
    if not s:
        s = "col"
    if s[0].isdigit():
        s = f"{prefix}_{s}"
    if s in {"user", "table", "column", "order", "group", "index"}:
        s = f"{prefix}_{s}"
    return s[:63]


def dataset_key(path: Path) -> str:
    path = Path(path).resolve()
    try:
        rel = path.relative_to(PACKAGE_DIR.resolve())
    except ValueError:
        rel = Path(path.name)
    return str(rel).replace("\\", "/")


def table_name_for(path: Path) -> str:
    rel = Path(dataset_key(path))
    folder = rel.parent.name if rel.parent != Path(".") else ""
    prefix = _FOLDER_PREFIX.get(folder, "fm")
    stem = sql_ident(rel.stem)
    return f"{prefix}_{stem}"[:63]


def _parse_number(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _is_int_like(v: float) -> bool:
    return abs(v - round(v)) < 1e-9 and abs(v) < 2_147_483_647


def _infer_pg_type(values: Sequence[str]) -> str:
    nums: list[float] = []
    seen = False
    all_int = True
    for raw in values:
        s = str(raw).strip() if raw is not None else ""
        if s == "":
            continue
        seen = True
        n = _parse_number(s)
        if n is None:
            return "TEXT"
        nums.append(n)
        if not _is_int_like(n):
            all_int = False
    if not seen:
        return "TEXT"
    return "INTEGER" if all_int else "DOUBLE PRECISION"


def _hour_of(row: dict[str, Any], index: int) -> float:
    for key in ("hour", "t", "time"):
        if key in row:
            n = _parse_number(row.get(key))
            if n is not None:
                return n
    return float(index)


def _pick_hydro(row: dict[str, Any]) -> dict[str, float | None]:
    lower = {str(k).strip().lower().replace(" ", ""): k for k in row}
    def grab(*names: str) -> float | None:
        for name in names:
            src = lower.get(name)
            if src is not None:
                return _parse_number(row.get(src))
        return None

    return {
        "rainfall_mm": grab("rainfall_mm", "rain_mm", "p_mm"),
        "pet_mm": grab("pet_mm", "evap_mm", "e_mm"),
        "et_mm": grab("et_mm"),
        "q_m3s": grab("q_m3s", "q_m3/s", "q_in_m3s", "q_total_m3s", "q_obs_m3s"),
        "h_m": grab("h_m", "h_down_m", "h_na1_m"),
    }


def ts_label(hour: float) -> str:
    """Nhãn thời gian JSONB: 2024-07-01 00:00."""
    dt = TIMESERIES_START + timedelta(hours=float(hour))
    return dt.strftime(TS_LABEL_FMT)


def parse_ts_label(raw: Any, hour: float = 0.0) -> datetime:
    s = str(raw or "").strip()
    if not s:
        return TIMESERIES_START + timedelta(hours=float(hour))
    compact = s.replace("T", " ")
    if len(compact) >= 16:
        try:
            dt = datetime.strptime(compact[:16], TS_LABEL_FMT)
            return dt.replace(tzinfo=TZ_VN)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ_VN)
        return dt.astimezone(TZ_VN)
    except ValueError:
        return TIMESERIES_START + timedelta(hours=float(hour))


def row_timeseries(row: dict[str, Any], index: int = 0) -> str:
    return ts_label(_hour_of(row, index))


def example_ts_labels(n: int = 72) -> list[str]:
    return [ts_label(float(i)) for i in range(n)]


def example_timeseries(n: int = 72) -> list[dict[str, Any]]:
    """Chuỗi ví dụ: mưa, bốc hơi (PET/ET), lưu lượng, mực nước."""
    series: list[dict[str, Any]] = []
    for i in range(n):
        hod = i % 24
        storm = 18.0 * math.exp(-0.5 * ((i - 36) / 7.5) ** 2) if 18 <= i <= 54 else 0.0
        drizzle = 0.05 if 7 <= hod <= 19 else 0.0
        rain = round(storm + drizzle, 4)
        pet = 0.0
        if 6 <= hod <= 18:
            pet = round(max(0.0, 0.48 * math.sin(math.pi * (hod - 6) / 12.0)), 4)
        et = round(min(pet, pet * 0.85 + rain * 0.02), 4)
        q = round(480.0 + 160.0 * math.sin(i / 14.0) + rain * 18.0, 4)
        h = round(5.45 + 0.55 * math.sin(i / 14.0) + rain * 0.035, 4)
        series.append(
            {
                "ts": ts_label(float(i)),
                "hour": float(i),
                "rainfall_mm": rain,
                "pet_mm": pet,
                "et_mm": et,
                "q_m3s": q,
                "h_m": h,
            }
        )
    return series


def _read_csv_file(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = [str(c) for c in (reader.fieldnames or [])]
        rows = [{k: ("" if v is None else str(v)) for k, v in row.items()} for row in reader]
    return columns, rows


def _pg_columns(columns: Sequence[str]) -> list[str]:
    out: list[str] = []
    used = {"id", "timeseries"}
    for col in columns:
        name = sql_ident(col)
        base = name
        n = 2
        while name in used:
            name = f"{base}_{n}"[:63]
            n += 1
        used.add(name)
        out.append(name)
    return out


def ensure_schema(conn=None) -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS csv_dataset (
        dataset_key TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        source_relpath TEXT NOT NULL,
        columns TEXT[] NOT NULL,
        pg_columns TEXT[] NOT NULL,
        n_rows INTEGER NOT NULL DEFAULT 0,
        timeseries JSONB,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS hydro_timeseries (
        t_index INTEGER PRIMARY KEY,
        hour DOUBLE PRECISION NOT NULL,
        ts TIMESTAMPTZ NOT NULL,
        rainfall_mm DOUBLE PRECISION,
        pet_mm DOUBLE PRECISION,
        et_mm DOUBLE PRECISION,
        q_m3s DOUBLE PRECISION,
        h_m DOUBLE PRECISION,
        timeseries TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS hydro_timeseries_ts_idx ON hydro_timeseries (ts);
    """
    own = conn is None
    if own:
        with get_connection() as c:
            with c.cursor() as cur:
                cur.execute(ddl)
        return
    with conn.cursor() as cur:
        cur.execute(ddl)


def _replace_table(
    conn,
    path: Path,
    columns: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> str:
    psycopg2, sql, Json, _cur, execute_values = _psycopg2()
    table = table_name_for(path)
    key = dataset_key(path)
    pg_cols = _pg_columns(columns)
    samples = {c: [r.get(c, "") for r in rows[: min(len(rows), 400)]] for c in columns}
    types = [_infer_pg_type(samples[c]) for c in columns]

    ident_table = sql.Identifier(table)
    col_defs = [sql.SQL("id SERIAL PRIMARY KEY")]
    for pg_c, typ in zip(pg_cols, types):
        col_defs.append(
            sql.SQL("{} {}").format(sql.Identifier(pg_c), sql.SQL(typ))
        )
    col_defs.append(sql.SQL("timeseries TEXT"))

    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident_table))
        cur.execute(
            sql.SQL("CREATE TABLE {} ({})").format(
                ident_table, sql.SQL(", ").join(col_defs)
            )
        )

        insert_cols = [sql.Identifier(c) for c in pg_cols] + [sql.Identifier("timeseries")]
        template = "(" + ",".join(["%s"] * (len(pg_cols) + 1)) + ")"
        payload: list[tuple[Any, ...]] = []
        series_for_dataset: list[str] = []
        has_hour = any(str(c).strip().lower() == "hour" for c in columns)
        for i, row in enumerate(rows):
            vals: list[Any] = []
            for src, typ in zip(columns, types):
                raw = row.get(src, "")
                if typ == "TEXT":
                    vals.append(None if raw is None or str(raw).strip() == "" else str(raw))
                else:
                    n = _parse_number(raw)
                    if n is None:
                        vals.append(None)
                    elif typ == "INTEGER":
                        vals.append(int(round(n)))
                    else:
                        vals.append(n)
            hydro = _pick_hydro(row)
            hydro_present = any(hydro.get(k) is not None for k in HYDRO_KEYS)
            if has_hour or hydro_present:
                label = row_timeseries(row, i)
                series_for_dataset.append(label)
                vals.append(label)
            else:
                vals.append(None)
            payload.append(tuple(vals))

        if payload:
            execute_values(
                cur,
                sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                    ident_table, sql.SQL(", ").join(insert_cols)
                ).as_string(conn),
                payload,
                template=template,
                page_size=1000,
            )

        if not series_for_dataset:
            series_for_dataset = example_ts_labels()

        cur.execute(
            """
            INSERT INTO csv_dataset
                (dataset_key, table_name, source_relpath, columns, pg_columns, n_rows, timeseries, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (dataset_key) DO UPDATE SET
                table_name = EXCLUDED.table_name,
                source_relpath = EXCLUDED.source_relpath,
                columns = EXCLUDED.columns,
                pg_columns = EXCLUDED.pg_columns,
                n_rows = EXCLUDED.n_rows,
                timeseries = EXCLUDED.timeseries,
                updated_at = now()
            """,
            (
                key,
                table,
                key,
                list(columns),
                pg_cols,
                len(rows),
                Json(series_for_dataset),
            ),
        )
    return table


def sync_csv_file(path: Path, *, rebuild_hydro: bool = True) -> str | None:
    """Đọc CSV trên đĩa và ghi vào data_flood (kèm trường timeseries)."""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".csv":
        return None
    columns, rows = _read_csv_file(path)
    if not columns:
        return None
    with get_connection() as conn:
        ensure_schema(conn)
        table = _replace_table(conn, path, columns, rows)
        if rebuild_hydro:
            rebuild_hydro_timeseries(conn)
    return table


def discover_csv_files(root: Path | None = None) -> list[Path]:
    root = Path(root or PACKAGE_DIR)
    skip = {".git", "__pycache__", "venv", "node_modules", "static", "templates"}
    found: list[Path] = []
    for p in root.rglob("*.csv"):
        if any(part in skip for part in p.parts):
            continue
        found.append(p)
    found.sort()
    return found


def import_all_csvs(root: Path | None = None) -> dict[str, Any]:
    files = discover_csv_files(root)
    imported: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with get_connection() as conn:
        ensure_schema(conn)
        for path in files:
            try:
                columns, rows = _read_csv_file(path)
                if not columns:
                    continue
                table = _replace_table(conn, path, columns, rows)
                imported.append(
                    {
                        "file": dataset_key(path),
                        "table": table,
                        "n_rows": len(rows),
                        "n_cols": len(columns),
                    }
                )
            except Exception as exc:
                errors.append({"file": dataset_key(path), "error": str(exc)})
        n_hydro = rebuild_hydro_timeseries(conn)
    return {
        "ok": not errors,
        "database": db_config()["dbname"],
        "n_files": len(imported),
        "imported": imported,
        "errors": errors,
        "hydro_timeseries_rows": n_hydro,
    }


def _index_by_hour(rows: Sequence[dict[str, Any]]) -> dict[float, dict[str, Any]]:
    out: dict[float, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        hour = round(_hour_of(row, i), 6)
        out[hour] = row
    return out


def _load_table_rows(conn, table: str) -> list[dict[str, Any]]:
    psycopg2, sql, _Json, RealDictCursor, _ev = _psycopg2()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            (table,),
        )
        if cur.fetchone() is None:
            return []
        cur.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table)))
        return [dict(r) for r in cur.fetchall()]


def rebuild_hydro_timeseries(conn=None) -> int:
    """Ghép mưa / PET / ET / Q / H theo giờ; thiếu thì bổ sung chuỗi ví dụ."""
    psycopg2, sql, Json, _cur, execute_values = _psycopg2()

    def _run(c) -> int:
        rain_rows = _load_table_rows(c, "rr_demo_rainfall")
        tank_rows = _load_table_rows(c, "rr_tank_result")
        nam_rows = _load_table_rows(c, "rr_mike_nam_result")
        q_rows = _load_table_rows(c, "sv_demo_inflow_q_m3s")
        h_sv = _load_table_rows(c, "sv_demo_downstream_stage")
        h_mk = _load_table_rows(c, "mk_demo_downstream_stage")

        by_rain = _index_by_hour(rain_rows)
        by_tank = _index_by_hour(tank_rows)
        by_nam = _index_by_hour(nam_rows)
        by_q = _index_by_hour(q_rows)
        by_h = _index_by_hour(h_sv) or _index_by_hour(h_mk)

        hours = sorted(
            set(by_rain) | set(by_tank) | set(by_nam) | set(by_q) | set(by_h)
        )
        example = {round(float(p["hour"]), 6): p for p in example_timeseries(max(72, len(hours) or 72))}
        if not hours:
            hours = sorted(example)

        payload: list[tuple[Any, ...]] = []
        for i, hour in enumerate(hours):
            rain = by_rain.get(hour, {})
            tank = by_tank.get(hour, {})
            nam = by_nam.get(hour, {})
            qrow = by_q.get(hour, {})
            hrow = by_h.get(hour, {})
            ex = example.get(hour, {})
            rainfall = _parse_number(rain.get("rainfall_mm") or tank.get("rainfall_mm") or nam.get("rainfall_mm"))
            pet = _parse_number(rain.get("pet_mm") or tank.get("pet_mm") or nam.get("pet_mm"))
            et = _parse_number(tank.get("et_mm") or nam.get("et_mm"))
            q = _parse_number(
                tank.get("q_m3s") or nam.get("q_m3s") or qrow.get("q_m3s") or qrow.get("q_m3_s")
            )
            h = _parse_number(hrow.get("h_m") or tank.get("h_m"))
            if rainfall is None:
                rainfall = ex.get("rainfall_mm")
            if pet is None:
                pet = ex.get("pet_mm")
            if et is None:
                et = ex.get("et_mm")
            if q is None:
                q = ex.get("q_m3s")
            if h is None:
                h = ex.get("h_m")
            ts = TIMESERIES_START + timedelta(hours=float(hour))
            payload.append(
                (i, float(hour), ts, rainfall, pet, et, q, h, ts_label(float(hour)))
            )

        with c.cursor() as cur:
            cur.execute("TRUNCATE hydro_timeseries")
            if payload:
                execute_values(
                    cur,
                    """
                    INSERT INTO hydro_timeseries
                        (t_index, hour, ts, rainfall_mm, pet_mm, et_mm, q_m3s, h_m, timeseries)
                    VALUES %s
                    """,
                    payload,
                    page_size=500,
                )
        return len(payload)

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        ensure_schema(c)
        return _run(c)


def flatten_timeseries_column(conn=None) -> int:
    """Đổi cột timeseries thành TEXT '2024-07-01 00:00', bỏ object JSON."""
    psycopg2, sql, _Json, _cur, _ev = _psycopg2()
    using_sql = """
        CASE
            WHEN timeseries IS NULL THEN NULL
            WHEN jsonb_typeof(timeseries) = 'object' THEN NULLIF(timeseries->>'ts', '')
            WHEN jsonb_typeof(timeseries) = 'string' THEN NULLIF(timeseries #>> '{}', '')
            ELSE NULL
        END
    """

    def _is_jsonb(cur, table: str) -> bool:
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s AND column_name = 'timeseries'
            """,
            (table,),
        )
        row = cur.fetchone()
        return bool(row) and str(row[0]).lower() == "jsonb"

    def _run(c) -> int:
        n = 0
        with c.cursor() as cur:
            if _is_jsonb(cur, "hydro_timeseries"):
                cur.execute(
                    f"ALTER TABLE hydro_timeseries ALTER COLUMN timeseries TYPE TEXT USING {using_sql}"
                )
                n += 1
            cur.execute(
                """
                UPDATE csv_dataset
                SET timeseries = (
                    SELECT COALESCE(jsonb_agg(
                        to_jsonb(
                            CASE
                                WHEN jsonb_typeof(elem) = 'object'
                                    THEN COALESCE(NULLIF(elem->>'ts', ''), '')
                                WHEN jsonb_typeof(elem) = 'string'
                                    THEN COALESCE(elem #>> '{}', '')
                                ELSE ''
                            END
                        )
                        ORDER BY ord
                    ), '[]'::jsonb)
                    FROM jsonb_array_elements(COALESCE(timeseries, '[]'::jsonb))
                        WITH ORDINALITY AS t(elem, ord)
                )
                WHERE timeseries IS NOT NULL
                  AND jsonb_typeof(timeseries) = 'array'
                  AND jsonb_typeof(timeseries -> 0) = 'object'
                """
            )
            n += cur.rowcount
            cur.execute("SELECT table_name FROM csv_dataset")
            tables = [r[0] for r in cur.fetchall()]
            for table in tables:
                if not _is_jsonb(cur, table):
                    continue
                tbl = sql.Identifier(table).as_string(c)
                cur.execute(
                    f"ALTER TABLE {tbl} ALTER COLUMN timeseries TYPE TEXT USING {using_sql}"
                )
                n += 1
        return n

    if conn is not None:
        return _run(conn)
    with get_connection() as c:
        ensure_schema(c)
        n = _run(c)
        rebuild_hydro_timeseries(c)
        return n


def relabel_timeseries_jsonb(conn=None) -> int:
    return flatten_timeseries_column(conn)


def list_datasets() -> list[dict[str, Any]]:
    _psycopg2_mod, _sql, _Json, RealDictCursor, _ev = _psycopg2()
    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT dataset_key, table_name, source_relpath, n_rows,
                       columns, pg_columns, updated_at,
                       jsonb_array_length(COALESCE(timeseries, '[]'::jsonb)) AS timeseries_n
                FROM csv_dataset
                ORDER BY dataset_key
                """
            )
            return [dict(r) for r in cur.fetchall()]


def fetch_table_rows(table: str, *, limit: int | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    psycopg2, sql, _Json, RealDictCursor, _ev = _psycopg2()
    table = sql_ident(table) if not table.startswith(("rr_", "sv_", "mk_", "fm_", "hydro")) else table
    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT columns, pg_columns FROM csv_dataset WHERE table_name = %s",
                (table,),
            )
            meta = cur.fetchone()
            q = sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table))
            if limit is not None:
                q = q + sql.SQL(" LIMIT %s")
                cur.execute(q, (int(limit),))
            else:
                cur.execute(q)
            raw_rows = [dict(r) for r in cur.fetchall()]
    columns = list(meta["columns"]) if meta else [k for k in (raw_rows[0].keys() if raw_rows else []) if k != "id"]
    pg_cols = list(meta["pg_columns"]) if meta else columns
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        item: dict[str, Any] = {}
        for src, pg_c in zip(columns, pg_cols):
            item[src] = raw.get(pg_c)
        item["timeseries"] = raw.get("timeseries")
        rows.append(item)
    return columns + (["timeseries"] if "timeseries" not in columns else []), rows


def fetch_rows_for_path(path: Path) -> tuple[list[str], list[dict[str, str]]] | None:
    key = dataset_key(path)
    _psycopg2_mod, sql, _Json, RealDictCursor, _ev = _psycopg2()
    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT table_name, columns, pg_columns FROM csv_dataset WHERE dataset_key = %s",
                (key,),
            )
            meta = cur.fetchone()
            if not meta:
                return None
            cur.execute(
                sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(meta["table_name"]))
            )
            raw_rows = [dict(r) for r in cur.fetchall()]
    columns = list(meta["columns"])
    pg_cols = list(meta["pg_columns"])
    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        item: dict[str, str] = {}
        for src, pg_c in zip(columns, pg_cols):
            v = raw.get(pg_c)
            item[src] = "" if v is None else str(v)
        rows.append(item)
    return columns, rows


def fetch_csv_text(path: Path) -> str | None:
    loaded = fetch_rows_for_path(path)
    if loaded is None:
        return None
    columns, rows = loaded
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return buf.getvalue()


@contextmanager
def csv_open(path: Path) -> Iterator[TextIO]:
    """Mở CSV: ưu tiên data_flood, không có thì đọc file."""
    path = Path(path)
    text = None
    try:
        text = fetch_csv_text(path)
    except Exception:
        text = None
    if text is not None:
        yield io.StringIO(text)
        return
    if path.is_file():
        with path.open(newline="", encoding="utf-8-sig") as f:
            yield f
        return
    raise FileNotFoundError(
        f"Khong co du lieu trong data_flood ({dataset_key(path)}) va khong co file {path}"
    )


def csv_available(path: Path) -> bool:
    """True nếu có bảng trong data_flood hoặc còn file CSV."""
    path = Path(path)
    if path.is_file():
        return True
    return dataset_exists(path)


def write_csv_rows(
    path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[dict[str, Any]],
    *,
    extrasaction: str = "ignore",
    rebuild_hydro: bool = True,
) -> None:
    """Ghi hàng vào bảng data_flood (không ghi file CSV)."""
    path = Path(path)
    cols = list(fieldnames)
    material: list[dict[str, Any]] = []
    for row in rows:
        if extrasaction == "ignore":
            material.append({k: row.get(k, "") for k in cols})
        else:
            extra = [k for k in row if k not in cols]
            if extra:
                raise ValueError(f"Cot thua khi ghi {path.name}: {extra}")
            material.append({k: row.get(k, "") for k in cols})
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            _replace_table(conn, path, cols, material)
            if rebuild_hydro:
                rebuild_hydro_timeseries(conn)
    except Exception as exc:
        print(f"[data_flood] khong ghi {path.name}: {exc}", flush=True)
        raise


def export_csv_file(path: Path) -> bool:
    """Xuat 1 dataset tu data_flood ra file CSV tren dia. True neu ghi duoc."""
    path = Path(path)
    text = fetch_csv_text(path)
    if text is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig", newline="")
    return True


def export_all_csvs(root: Path | None = None) -> dict[str, Any]:
    """Xuat toan bo bang csv_dataset trong data_flood ra thu muc o dia tuong ung."""
    root = Path(root or PACKAGE_DIR).resolve()
    exported: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    datasets = list_datasets()
    for ds in datasets:
        key = str(ds.get("dataset_key") or "").replace("\\", "/").lstrip("/")
        if not key:
            continue
        path = root / key.replace("/", os.sep)
        try:
            loaded = fetch_rows_for_path(path)
            if loaded is None:
                # thu theo dataset_key truc tiep trong PACKAGE_DIR
                alt = PACKAGE_DIR / key.replace("/", os.sep)
                loaded = fetch_rows_for_path(alt)
                path = alt
            if loaded is None:
                raise FileNotFoundError(f"Khong doc duoc dataset {key}")
            columns, rows = loaded
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({c: row.get(c, "") for c in columns})
            exported.append({
                "file": key,
                "path": str(path),
                "table": ds.get("table_name"),
                "n_rows": len(rows),
                "n_cols": len(columns),
            })
        except Exception as exc:
            errors.append({"file": key, "error": str(exc)})
    return {
        "ok": not errors,
        "database": db_config()["dbname"],
        "root": str(root),
        "n_files": len(exported),
        "exported": exported,
        "errors": errors,
    }


def dataset_exists(path: Path) -> bool:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM csv_dataset WHERE dataset_key = %s", (dataset_key(path),))
                return cur.fetchone() is not None
    except Exception:
        return False


def fetch_hydro_timeseries() -> list[dict[str, Any]]:
    _psycopg2_mod, _sql, _Json, RealDictCursor, _ev = _psycopg2()
    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t_index, hour, ts, rainfall_mm, pet_mm, et_mm, q_m3s, h_m, timeseries
                FROM hydro_timeseries
                ORDER BY t_index
                """
            )
            rows = []
            for r in cur.fetchall():
                item = dict(r)
                hour = item.get("hour")
                if hour is not None:
                    item["ts"] = ts_label(float(hour))
                elif hasattr(item.get("ts"), "strftime"):
                    item["ts"] = item["ts"].strftime(TS_LABEL_FMT)
                rows.append(item)
            return rows


def write_hydro_timeseries(points: Sequence[dict[str, Any]]) -> int:
    """Ghi chuỗi mưa / bốc hơi / Q / H vào hydro_timeseries và rr_demo_rainfall."""
    if not points:
        raise ValueError("Can it nhat 1 diem timeseries.")
    psycopg2, _sql, Json, _cur, execute_values = _psycopg2()
    payload: list[tuple[Any, ...]] = []
    rain_rows: list[dict[str, str]] = []
    for i, raw in enumerate(points):
        if not isinstance(raw, dict):
            continue
        hour = _parse_number(raw.get("hour"))
        if hour is None:
            hour = float(i)
        ts = parse_ts_label(raw.get("ts") or raw.get("timeseries"), hour)
        rainfall = _parse_number(raw.get("rainfall_mm"))
        pet = _parse_number(raw.get("pet_mm"))
        et = _parse_number(raw.get("et_mm"))
        q = _parse_number(raw.get("q_m3s"))
        h = _parse_number(raw.get("h_m"))
        payload.append((i, hour, ts, rainfall, pet, et, q, h, ts_label(hour)))
        rain_rows.append(
            {
                "hour": f"{hour:.1f}",
                "rainfall_mm": "" if rainfall is None else f"{rainfall:.4f}",
                "pet_mm": "" if pet is None else f"{pet:.4f}",
            }
        )
    if not payload:
        raise ValueError("Khong co diem timeseries hop le.")

    rain_path = PACKAGE_DIR / "rainfall_runoff_output" / "demo_rainfall.csv"

    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE hydro_timeseries")
            execute_values(
                cur,
                """
                INSERT INTO hydro_timeseries
                    (t_index, hour, ts, rainfall_mm, pet_mm, et_mm, q_m3s, h_m, timeseries)
                VALUES %s
                """,
                payload,
                page_size=500,
            )
        _replace_table(conn, rain_path, ["hour", "rainfall_mm", "pet_mm"], rain_rows)
    return len(payload)


def write_dataset_rows(table: str, rows: Sequence[dict[str, Any]]) -> int:
    """Ghi đè một bảng dataset trong data_flood (không ghi file CSV)."""
    _psycopg2_mod, _sql, _Json, RealDictCursor, _ev = _psycopg2()
    with get_connection() as conn:
        ensure_schema(conn)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT dataset_key, columns FROM csv_dataset WHERE table_name = %s",
                (table,),
            )
            meta = cur.fetchone()
            if not meta:
                raise ValueError(f"Khong co bang {table} trong csv_dataset.")
        columns = list(meta["columns"])
        path = PACKAGE_DIR / str(meta["dataset_key"]).replace("/", os.sep)
        norm_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = {c: "" if row.get(c) is None else str(row.get(c)) for c in columns}
            norm_rows.append(item)
        _replace_table(conn, path, columns, norm_rows)
        rebuild_hydro_timeseries(conn)
    return len(norm_rows)


def db_status() -> dict[str, Any]:
    cfg = db_config()
    info: dict[str, Any] = {
        "ok": False,
        "database": cfg["dbname"],
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "n_datasets": 0,
        "hydro_timeseries_rows": 0,
    }
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM csv_dataset")
                info["n_datasets"] = int(cur.fetchone()[0])
                cur.execute("SELECT COUNT(*) FROM hydro_timeseries")
                info["hydro_timeseries_rows"] = int(cur.fetchone()[0])
        info["ok"] = True
    except Exception as exc:
        info["error"] = str(exc)
    return info


def sync_csv_file_safe(path: Path) -> None:
    """Gọi sau khi ghi CSV; lỗi DB không làm hỏng mô phỏng."""
    try:
        sync_csv_file(path)
    except Exception as exc:
        print(f"[data_flood] khong dong bo {Path(path).name}: {exc}", flush=True)


def _ensure_pkg_path() -> None:
    parent = str(PROJECT_ROOT)
    if parent not in sys.path:
        sys.path.insert(0, parent)

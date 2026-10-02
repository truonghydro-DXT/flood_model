"""Doc muc nuoc 1D Saint-Venant (CSV) — dau vao cua mo hinh ngap lut."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from flood_model.csv_io import csv_available, csv_open
from flood_model.paths import (
    PACKAGE_DIR,
    SAINT_VENANT_GEOM_CSV,
    SAINT_VENANT_H_CSV,
    SAINT_VENANT_TRIB_GEOM_CSV,
    SAINT_VENANT_TRIB_H_CSV,
)

HYDRO1D_ALIASES = {
    "sv": "saint-venant",
    "saint-venant": "saint-venant",
    "saintvenant": "saint-venant",
    "saint-venant-1d": "saint-venant-1d",
    "saintvenant-1d": "saint-venant-1d",
    "sv-mike": "saint-venant-1d",
    "mike-hd": "saint-venant-1d",
    "mike": "saint-venant-1d",
}

# True: chi Saint-venant-1D. False: cho chon Saint-venant hoac Saint-venant-1D.
HYDRO1D_FORCE_MIKE = True


def parse_hydro1d_source(raw: Any = None, *, respect_force: bool = True) -> str:
    if respect_force and HYDRO1D_FORCE_MIKE:
        return "saint-venant-1d"
    key = str(raw or "saint-venant").strip().lower().replace(" ", "-").replace("_", "-")
    if key in ("mc", "muskingum", "muskingum-cunge"):
        return "saint-venant"
    src = HYDRO1D_ALIASES.get(key, "saint-venant")
    return "saint-venant-1d" if src == "saint-venant-1d" else "saint-venant"


def hydro1d_label(src: Any = None, *, respect_force: bool = True) -> str:
    kind = parse_hydro1d_source(src, respect_force=respect_force)
    return "Saint-venant-1D" if kind == "saint-venant-1d" else "Saint-venant"


def hydro1d_water_sources() -> list[str]:
    if HYDRO1D_FORCE_MIKE:
        return ["saint-venant-1d"]
    return ["saint-venant", "saint-venant-1d"]


def normalize_reach_id(rid: Any, default: str = "main") -> str:
    """main, main_2, trib_1 — cung quy uoc voi CSV 1D."""
    s = str(rid or default).strip() or default
    key = s.lower().replace("_", "-").replace(" ", "-")
    if key in ("main", "song-chinh", "sông-chính", "chinh", "chính"):
        return "main"
    numbered = re.match(r"^(main|trib)[-_]?(\d+)$", key)
    if numbered:
        kind, idx = numbered.group(1), int(numbered.group(2))
        if kind == "main" and idx <= 1:
            return "main"
        return f"{kind}_{idx}"
    return s.strip() or default


def is_primary_main_reach(rid: Any) -> bool:
    """Hang hinh hoc long chinh dung cho 1D/3D (bo main_2, trib_... )."""
    s = str(rid or "").strip()
    if not s:
        return True
    return normalize_reach_id(s) == "main"


def is_main_reach_id(rid: Any) -> bool:
    s = str(rid or "").strip()
    if not s:
        return False
    key = normalize_reach_id(s)
    return key == "main" or key.startswith("main_")


def pick_route_by_id(
    routes: Sequence[dict[str, Any]], rid: Any, default: str = "trib_1"
) -> dict[str, Any] | None:
    want = normalize_reach_id(rid, default)
    for rec in routes:
        if normalize_reach_id(rec.get("id") or rec.get("reach_id"), default) == want:
            return rec
    return None


def hydro1d_csv_paths(src: Any = None, root: Path | None = None) -> dict[str, Path | str]:
    kind = parse_hydro1d_source(src, respect_force=False)
    base = Path(root) if root else PACKAGE_DIR
    if kind == "saint-venant-1d":
        d = base / "mike_hd_output"
        return {
            "kind": kind,
            "dir": d,
            "geom": d / "mike_hd_river_geometry.csv",
            "h": d / "mike_hd_result.csv",
            "trib_geom": d / "mike_hd_tributary_geometry.csv",
            "trib_h": d / "mike_hd_tributary_result.csv",
            "extra_h": d / "mike_hd_extra_main_result.csv",
            "xs": d / "mike_hd_cross_sections.csv",
        }
    d = base / "saint_venant_output"
    return {
        "kind": kind,
        "dir": d,
        "geom": SAINT_VENANT_GEOM_CSV if root is None else d / "demo_river_geometry.csv",
        "h": SAINT_VENANT_H_CSV if root is None else d / "saint_venant_result.csv",
        "trib_geom": SAINT_VENANT_TRIB_GEOM_CSV if root is None else d / "demo_tributary_geometry.csv",
        "trib_h": SAINT_VENANT_TRIB_H_CSV if root is None else d / "saint_venant_tributary_result.csv",
        "extra_h": d / "demo_extra_main_result.csv",
        "xs": d / "demo_cross_sections.csv",
    }


def _xs_value_cols(fields: Sequence[str], kind: str) -> list[tuple[int, str]]:
    cols: list[tuple[int, str]] = []
    for name in fields:
        m = re.match(rf"^{kind}_xs(\d+)_", name or "")
        if m:
            cols.append((int(m.group(1)), name))
    cols.sort(key=lambda x: x[0])
    return cols


def _row_xs_vals(row: dict[str, str], cols: Sequence[tuple[int, str]], n_x: int) -> list[float]:
    vals: list[float] = []
    for _, col in list(cols)[:n_x]:
        try:
            vals.append(float(row[col]))
        except (KeyError, TypeError, ValueError):
            vals.append(float("nan"))
    return vals


def load_route_csv(geom_csv: Path, h_csv: Path) -> dict[str, Any] | None:
    if not csv_available(geom_csv) or not csv_available(h_csv):
        return None
    stations: list[float] = []
    lons: list[float] = []
    lats: list[float] = []
    z_bed: list[float] = []
    with csv_open(geom_csv) as f:
        for row in csv.DictReader(f):
            if not is_primary_main_reach(row.get("reach_id") or row.get("reach")):
                continue
            try:
                stations.append(float(row["station_km"]) * 1000.0)
                lons.append(float(row["lon"]))
                lats.append(float(row["lat"]))
                z_bed.append(float(row["z_bed_m"]))
            except (KeyError, TypeError, ValueError):
                continue
    if len(stations) < 2:
        return None
    hours: list[float] = []
    h_rows: list[list[float]] = []
    q_rows: list[list[float]] = []
    h_ds_rows: list[float] = []
    have_q = False
    with csv_open(h_csv) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        h_cols = _xs_value_cols(fields, "h")
        q_cols = _xs_value_cols(fields, "q")
        if len(h_cols) < 2:
            return None
        n_x = min(len(h_cols), len(stations))
        q_by_id = {xs_id: name for xs_id, name in q_cols}
        q_aligned = [(xs_id, q_by_id[xs_id]) for xs_id, _ in h_cols[:n_x] if xs_id in q_by_id]
        have_q = len(q_aligned) == n_x
        have_h_ds = "h_down_m" in fields
        for row in reader:
            try:
                hours.append(float(row["hour"]))
            except (KeyError, TypeError, ValueError):
                continue
            h_rows.append(_row_xs_vals(row, h_cols, n_x))
            if have_q:
                q_rows.append(_row_xs_vals(row, q_aligned, n_x))
            if have_h_ds:
                try:
                    h_ds_rows.append(float(row["h_down_m"]))
                except (TypeError, ValueError):
                    h_ds_rows.append(float("nan"))
    if not hours:
        return None
    h = np.asarray(h_rows, dtype=float)
    n_x = h.shape[1]
    if h_ds_rows and len(h_ds_rows) == h.shape[0]:
        h_ds = np.asarray(h_ds_rows, dtype=float)
        ok = np.isfinite(h_ds)
        if ok.any():
            h[ok] = np.maximum(h[ok], h_ds[ok, None])
    return {
        "hours": np.asarray(hours, dtype=float),
        "station_m": np.asarray(stations[:n_x], dtype=float),
        "lon": np.asarray(lons[:n_x], dtype=float),
        "lat": np.asarray(lats[:n_x], dtype=float),
        "z_bed": np.asarray(z_bed[:n_x], dtype=float),
        "h": h,
        "q": np.asarray(q_rows, dtype=float) if have_q else None,
        "source": "data_flood",
    }


def _series_along_ends(
    station_m: np.ndarray,
    hours_out: np.ndarray,
    hours_src: np.ndarray,
    v_near: np.ndarray,
    v_far: np.ndarray,
) -> np.ndarray:
    s = np.asarray(station_m, dtype=float)
    s = s - float(s[0]) if s.size else s
    length = float(s[-1]) if s.size else 1.0
    a = np.interp(hours_out, hours_src, np.nan_to_num(v_near, nan=0.0))
    b = np.interp(hours_out, hours_src, np.nan_to_num(v_far, nan=0.0))
    out = np.empty((hours_out.size, max(s.size, 1)), dtype=float)
    for t in range(int(hours_out.size)):
        out[t] = np.interp(s, [0.0, max(length, 1.0)], [float(a[t]), float(b[t])])
    return out


def _h_along_join_outer(
    station_m: np.ndarray,
    z_bed: np.ndarray,
    hours_out: np.ndarray,
    hours_src: np.ndarray,
    h_join: np.ndarray,
    h_outer: np.ndarray,
) -> np.ndarray:
    h = _series_along_ends(station_m, hours_out, hours_src, h_join, h_outer)
    z = np.asarray(z_bed, dtype=float)
    if z.size == h.shape[1]:
        h = np.maximum(h, z[None, :] + 0.05)
    return h


def load_grouped_geom_csv(path: Path, *, which: str = "trib") -> list[dict[str, Any]]:
    """Doc hinh hoc CSV gom theo reach_id. which: trib | extra_main | primary."""
    if not csv_available(path):
        return []
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    with csv_open(path) as f:
        for row in csv.DictReader(f):
            raw = row.get("reach_id") or row.get("reach") or ""
            if which == "trib":
                if is_main_reach_id(raw):
                    continue
                rid = normalize_reach_id(raw, "trib_1")
            elif which == "extra_main":
                if not is_main_reach_id(raw) or is_primary_main_reach(raw):
                    continue
                rid = normalize_reach_id(raw, "main_2")
            else:
                if not is_primary_main_reach(raw):
                    continue
                rid = "main"
            if rid not in groups:
                groups[rid] = {
                    "station_m": [],
                    "lon": [],
                    "lat": [],
                    "z_bed": [],
                    "width_m": [],
                    "kind": str(row.get("kind") or ("main" if which != "trib" else "outlet")),
                    "bc": str(row.get("bc") or ("Q/H" if which != "trib" else "H")),
                }
                order.append(rid)
            g = groups[rid]
            try:
                g["station_m"].append(float(row["station_km"]) * 1000.0)
                g["lon"].append(float(row["lon"]))
                g["lat"].append(float(row["lat"]))
                g["z_bed"].append(float(row.get("z_bed_m") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                g["width_m"].append(float(row.get("top_width_at_3m") or 200.0))
            except (TypeError, ValueError):
                g["width_m"].append(200.0)
            if row.get("kind"):
                g["kind"] = str(row["kind"])
            if row.get("bc"):
                g["bc"] = str(row["bc"])
    out: list[dict[str, Any]] = []
    for rid in order:
        g = groups[rid]
        if len(g["lon"]) < 2:
            continue
        out.append(
            {
                "id": rid,
                "kind": g["kind"] or ("main" if which != "trib" else "outlet"),
                "bc": g["bc"] or ("Q/H" if which != "trib" else "H"),
                "station_m": np.asarray(g["station_m"], dtype=float),
                "lon": np.asarray(g["lon"], dtype=float),
                "lat": np.asarray(g["lat"], dtype=float),
                "z_bed": np.asarray(g["z_bed"], dtype=float),
                "width_m": np.asarray(g["width_m"], dtype=float),
            }
        )
    return out


def load_extra_main_end_series(path: Path) -> tuple[np.ndarray, dict[str, dict[str, np.ndarray]]]:
    """CSV: hour, main_2_h_us_m, main_2_h_ds_m, ..."""
    if not csv_available(path):
        return np.array([], dtype=float), {}
    with csv_open(path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        ids: list[str] = []
        for name in fields:
            m = re.match(r"^(.+)_h_us_m$", name or "")
            if m:
                ids.append(normalize_reach_id(m.group(1), "main_2"))
        if not ids:
            return np.array([], dtype=float), {}
        hours: list[float] = []
        store = {rid: {"h_us": [], "h_ds": [], "q_us": [], "q_ds": []} for rid in ids}
        raw_ids = []
        for name in fields:
            m = re.match(r"^(.+)_h_us_m$", name or "")
            if m:
                raw_ids.append(m.group(1))
        for row in reader:
            try:
                hours.append(float(row["hour"]))
            except (KeyError, TypeError, ValueError):
                continue
            for raw in raw_ids:
                rid = normalize_reach_id(raw, "main_2")

                def _cell(col: str, rec: dict[str, str] = row) -> float:
                    try:
                        return float(rec[col])
                    except (KeyError, TypeError, ValueError):
                        return float("nan")

                store[rid]["h_us"].append(_cell(f"{raw}_h_us_m"))
                store[rid]["h_ds"].append(_cell(f"{raw}_h_ds_m"))
                store[rid]["q_us"].append(_cell(f"{raw}_q_up_m3s"))
                store[rid]["q_ds"].append(_cell(f"{raw}_q_ds_m3s"))
    if not hours:
        return np.array([], dtype=float), {}
    t = np.asarray(hours, dtype=float)
    out: dict[str, dict[str, np.ndarray]] = {}
    for rid, d in store.items():
        out[rid] = {
            "h_us": np.asarray(d["h_us"], dtype=float),
            "h_ds": np.asarray(d["h_ds"], dtype=float),
            "q_us": np.asarray(d["q_us"], dtype=float),
            "q_ds": np.asarray(d["q_ds"], dtype=float),
        }
    return t, out


def load_extra_main_routes(
    hours_main: np.ndarray,
    geom_csv: Path | None = None,
    h_csv: Path | None = None,
) -> list[dict[str, Any]]:
    """Song chinh doc lap (main_2...) + H noi suy thuong luu -> ha luu."""
    geom_csv = Path(geom_csv) if geom_csv else SAINT_VENANT_GEOM_CSV
    items = load_grouped_geom_csv(geom_csv, which="extra_main")
    if not items:
        return []
    hours_out = np.asarray(hours_main, dtype=float)
    t_src, series = load_extra_main_end_series(Path(h_csv) if h_csv else geom_csv.parent / "demo_extra_main_result.csv")
    out: list[dict[str, Any]] = []
    for g in items:
        station = np.asarray(g["station_m"], dtype=float)
        z_bed = np.asarray(g["z_bed"], dtype=float)
        ser = series.get(str(g["id"]))
        if ser is not None and t_src.size >= 2:
            h = _h_along_join_outer(
                station,
                z_bed,
                hours_out,
                t_src,
                ser["h_us"],
                ser["h_ds"],
            )
        else:
            h = np.tile((z_bed + 1.0)[None, :], (max(hours_out.size, 1), 1))
        rec = dict(g)
        rec["h"] = h
        rec["q"] = None
        if ser is not None and t_src.size >= 2 and "q_us" in ser:
            rec["q"] = _series_along_ends(
                station, hours_out, t_src, ser["q_us"], ser["q_ds"]
            )
        rec["kind"] = "main"
        out.append(rec)
    return out


def load_tributary_routes(
    hours_main: np.ndarray,
    geom_csv: Path | None = None,
    h_csv: Path | None = None,
) -> list[dict[str, Any]]:
    """Hinh hoc + H doc nhanh (noi suy H nut giao -> H dau xa)."""
    geom_csv = Path(geom_csv) if geom_csv else SAINT_VENANT_TRIB_GEOM_CSV
    h_csv = Path(h_csv) if h_csv else SAINT_VENANT_TRIB_H_CSV
    if not csv_available(geom_csv):
        return []
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    with csv_open(geom_csv) as f:
        for row in csv.DictReader(f):
            rid = normalize_reach_id(row.get("reach_id") or row.get("reach") or "trib", "trib_1")
            if is_main_reach_id(rid):
                continue
            if rid not in groups:
                groups[rid] = {
                    "station_m": [],
                    "lon": [],
                    "lat": [],
                    "z_bed": [],
                    "kind": "outlet",
                }
                order.append(rid)
            g = groups[rid]
            try:
                g["station_m"].append(float(row["station_km"]) * 1000.0)
                g["lon"].append(float(row["lon"]))
                g["lat"].append(float(row["lat"]))
                g["z_bed"].append(float(row.get("z_bed_m") or 0.0))
            except (KeyError, TypeError, ValueError):
                continue
    series: dict[str, dict[str, list]] = {}
    hours_trib: list[float] = []
    if csv_available(h_csv):
        with csv_open(h_csv) as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            ids = []
            for name in fields:
                m = re.match(r"^(.+)_h_join_m$", name or "")
                if m:
                    ids.append(normalize_reach_id(m.group(1), "trib_1"))
            raw_by_norm = {}
            for name in fields:
                m = re.match(r"^(.+)_h_join_m$", name or "")
                if m:
                    raw_by_norm[normalize_reach_id(m.group(1), "trib_1")] = m.group(1)
            store = {rid: {"h_join": [], "h_outer": [], "q_join": [], "q_outer": []} for rid in ids}
            for row in reader:
                try:
                    hours_trib.append(float(row["hour"]))
                except (KeyError, TypeError, ValueError):
                    continue
                for rid in ids:
                    raw = raw_by_norm.get(rid, rid)

                    def _cell(col: str, rec: dict[str, str] = row) -> float:
                        try:
                            return float(rec[col])
                        except (KeyError, TypeError, ValueError):
                            return float("nan")

                    store[rid]["h_join"].append(_cell(f"{raw}_h_join_m"))
                    store[rid]["h_outer"].append(_cell(f"{raw}_h_outer_m"))
                    store[rid]["q_join"].append(_cell(f"{raw}_q_join_m3s"))
                    store[rid]["q_outer"].append(_cell(f"{raw}_q_outer_m3s"))
            series = store
    t_trib = np.asarray(hours_trib, dtype=float) if hours_trib else np.asarray(hours_main, dtype=float)
    hours_out = np.asarray(hours_main, dtype=float)
    out: list[dict[str, Any]] = []
    for rid in order:
        g = groups[rid]
        if len(g["lon"]) < 2:
            continue
        station = np.asarray(g["station_m"], dtype=float)
        z_bed = np.asarray(g["z_bed"], dtype=float)
        lon = np.asarray(g["lon"], dtype=float)
        lat = np.asarray(g["lat"], dtype=float)
        ser = series.get(rid)
        if ser and t_trib.size >= 2:
            h = _h_along_join_outer(
                station,
                z_bed,
                hours_out,
                t_trib,
                np.asarray(ser["h_join"], dtype=float),
                np.asarray(ser["h_outer"], dtype=float),
            )
            q = _series_along_ends(
                station,
                hours_out,
                t_trib,
                np.asarray(ser["q_join"], dtype=float),
                np.asarray(ser["q_outer"], dtype=float),
            )
        else:
            h = np.tile((z_bed + 1.0)[None, :], (hours_out.size, 1))
            q = None
        out.append(
            {
                "id": normalize_reach_id(rid, "trib_1"),
                "kind": g.get("kind") or "outlet",
                "station_m": station,
                "lon": lon,
                "lat": lat,
                "z_bed": z_bed,
                "h": h,
                "q": q,
            }
        )
    return out


@lru_cache(maxsize=4)
def tributary_routes(water_source: str = "saint-venant") -> tuple:
    src = parse_hydro1d_source(water_source, respect_force=False)
    mus = saint_venant_route(src)
    hours = np.asarray(mus["hours"], dtype=float)
    paths = hydro1d_csv_paths(src)
    return tuple(
        load_tributary_routes(hours, Path(paths["trib_geom"]), Path(paths["trib_h"]))
    )


@lru_cache(maxsize=4)
def extra_main_routes(water_source: str = "saint-venant") -> tuple:
    src = parse_hydro1d_source(water_source, respect_force=False)
    mus = saint_venant_route(src)
    hours = np.asarray(mus["hours"], dtype=float)
    paths = hydro1d_csv_paths(src)
    return tuple(load_extra_main_routes(hours, Path(paths["geom"]), Path(paths["extra_h"])))


def network_sv_routes(water_source: str = "saint-venant") -> list[dict[str, Any]]:
    """Long chinh thu nhat + song chinh doc lap + song nhanh, theo reach_id."""
    kind = parse_hydro1d_source(water_source, respect_force=False)
    main = saint_venant_route(kind)
    hours = np.asarray(main["hours"], dtype=float)
    n_t = int(np.asarray(main["h"]).shape[0])
    routes = [dict(main, id="main", kind="main")]
    for rec in extra_main_routes(kind):
        if int(np.asarray(rec.get("h")).shape[0]) != n_t:
            continue
        item = dict(rec)
        item["hours"] = hours
        item["id"] = normalize_reach_id(item.get("id"), "main_2")
        routes.append(item)
    for rec in tributary_routes(kind):
        h = np.asarray(rec.get("h"), dtype=float)
        if h.ndim != 2 or h.shape[1] < 2 or int(h.shape[0]) != n_t:
            continue
        item = dict(rec)
        item["hours"] = hours
        item["id"] = normalize_reach_id(item.get("id"), "trib_1")
        if item.get("q") is None:
            item["q"] = rec.get("q")
        routes.append(item)
    return routes


@lru_cache(maxsize=4)
def saint_venant_route(water_source: str = "saint-venant") -> dict[str, Any]:
    src = parse_hydro1d_source(water_source, respect_force=False)
    paths = hydro1d_csv_paths(src)
    loaded = load_route_csv(Path(paths["geom"]), Path(paths["h"]))
    if loaded is not None:
        loaded = dict(loaded)
        loaded["water_source"] = src
        return loaded
    if src == "saint-venant-1d":
        raise FileNotFoundError(
            "Thieu mike_hd_output/mike_hd_result.csv. Chay Model 1D Saint-venant-1D truoc."
        )
    try:
        from flood_model.flow_3d import _run_saint_venant_model

        return _run_saint_venant_model()
    except Exception as exc:
        raise FileNotFoundError(
            "Thieu saint_venant_output/demo_river_geometry.csv hoac saint_venant_result.csv. "
            "Chay saint-venant.py truoc."
        ) from exc


def clear_route_cache() -> None:
    saint_venant_route.cache_clear()
    tributary_routes.cache_clear()
    extra_main_routes.cache_clear()

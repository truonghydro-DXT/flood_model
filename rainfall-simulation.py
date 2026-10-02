"""
Mo phong hat mua roi tu do cao 5 km xuong be mat DEM.

- Mua: rainfall_runoff_output/demo_rainfall.csv (mm/gio)
- DEM: projects/data/dem-song-hong.tif
- Hat mua sinh o z = 5000 m, roi theo trong luc + can (van toc gioi han),
  cham DEM thi tich luy lop nuoc (mm).

Chay:
  .\\venv\\Scripts\\python.exe flood_model\\rainfall-simulation.py
  .\\venv\\Scripts\\python.exe flood_model\\rainfall-simulation.py --video
  .\\venv\\Scripts\\python.exe flood_model\\rainfall-simulation.py --no-plot
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
from rasterio.enums import Resampling
from rasterio.transform import Affine, rowcol, xy

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open, write_csv_rows
from flood_model.gis import open_dem, resolve_dem_path
from flood_model.paths import DEFAULT_DEM, PACKAGE_DIR as FM_DIR
from flood_model.boundary import align_rainfall, load_rainfall_hours as load_rainfall_csv

DEFAULT_RAIN_CSV = FM_DIR / "rainfall_runoff_output" / "demo_rainfall.csv"
DEFAULT_OUT_DIR = FM_DIR / "rainfall_runoff_output"

CLOUD_HEIGHT_M = 5000.0
G_MS2 = 9.81
# Van toc gioi han hat mua (~2 mm): 6-9 m/s
V_TERM_MS = 8.0


@dataclass
class DemGrid:
    z: np.ndarray
    x_m: np.ndarray
    y_m: np.ndarray
    transform: Affine
    cell_m: float
    path: Path

    @property
    def shape(self) -> tuple[int, int]:
        return int(self.z.shape[0]), int(self.z.shape[1])

    @property
    def x_km(self) -> np.ndarray:
        return (self.x_m - float(self.x_m[0])) / 1000.0

    @property
    def y_km(self) -> np.ndarray:
        return (self.y_m - float(self.y_m[-1])) / 1000.0

    @property
    def width_km(self) -> float:
        return abs(float(self.x_m[-1] - self.x_m[0])) / 1000.0

    @property
    def height_km(self) -> float:
        return abs(float(self.y_m[0] - self.y_m[-1])) / 1000.0


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def rain_scene_params() -> dict[str, float]:
    return {
        "cloud_height_m": float(CLOUD_HEIGHT_M),
        "v_term_ms": float(V_TERM_MS),
        "wind_east_ms": 1.6,
        "wind_north_ms": 0.4,
    }


# IDF Ha Dong / Ha Noi (Anh et al., HUCE 2025): q = A(1+C lg T)/(t+b)^n
# A=2320, C=0.655, b=9 phut, n=0.633. He so A,C chi ty le khi chuan hoa.
HANOI_IDF_B_MIN = 9.0
HANOI_IDF_N = 0.633
# Dinh mua ~ 38% thoi luong (Chicago, Keifer & Chu 1957) — mua doi luu Bac Bo
HANOI_CHICAGO_R = 0.38
# Gom trong so 1 phut thanh buoc 15 phut (4 o / gio)
RAIN_STEP_MIN = 15


def _chicago_intensity(dt_min: float, *, before: bool, a: float = 1.0) -> float:
    b = HANOI_IDF_B_MIN
    n = HANOI_IDF_N
    r = HANOI_CHICAGO_R
    dt = max(float(dt_min), 0.0)
    if before:
        den = dt / max(r, 1e-6) + b
        return a * ((1.0 - n) * dt / max(r, 1e-6) + b) / (den ** (n + 1.0))
    omr = max(1.0 - r, 1e-6)
    den = dt / omr + b
    return a * ((1.0 - n) * dt / omr + b) / (den ** (n + 1.0))


def hanoi_minute_weights(n_min: int = 60) -> np.ndarray:
    """Trong so vo thu nguyen trong 1 gio (tong = 1) theo duong Chicago-IDF Ha Noi."""
    n_min = max(2, int(n_min))
    tp = HANOI_CHICAGO_R * float(n_min)
    inten = np.empty(n_min, dtype=float)
    for k in range(n_min):
        t_mid = k + 0.5
        if t_mid <= tp:
            inten[k] = _chicago_intensity(tp - t_mid, before=True)
        else:
            inten[k] = _chicago_intensity(t_mid - tp, before=False)
    s = float(inten.sum())
    if s <= 0:
        return np.full(n_min, 1.0 / n_min)
    return inten / s


def aggregate_weights(weights: np.ndarray, step_min: int = RAIN_STEP_MIN) -> np.ndarray:
    """Cong don trong so 1 phut thanh buoc step_min (mac dinh 15 phut)."""
    w = np.asarray(weights, dtype=float).ravel()
    step = max(1, int(step_min))
    if w.size == 0:
        n = max(1, 60 // step)
        return np.full(n, 1.0 / n)
    if w.size % step == 0:
        grouped = w.reshape(-1, step).sum(axis=1)
    else:
        n = int(math.ceil(w.size / step))
        pad = np.zeros(n * step, dtype=float)
        pad[: w.size] = w
        grouped = pad.reshape(n, step).sum(axis=1)
    s = float(grouped.sum())
    return grouped / s if s > 0 else np.full(grouped.size, 1.0 / grouped.size)


def hanoi_step_weights(step_min: int = RAIN_STEP_MIN, n_min: int = 60) -> np.ndarray:
    """Duong Chicago 1 phut, roi gom ve 15 phut (tong van = 1)."""
    return aggregate_weights(hanoi_minute_weights(n_min), step_min)


def disaggregate_hour_mm(p_mm: float, weights: np.ndarray | None = None) -> np.ndarray:
    w = hanoi_step_weights() if weights is None else np.asarray(weights, dtype=float)
    return float(max(p_mm, 0.0)) * w


def rainfall_for_flow(
    hours: np.ndarray,
    csv_path: Path | None = None,
) -> dict[str, Any]:
    """Goi API Dòng chảy 3D: chuoi mua + trong so 15 phut kieu Ha Noi."""
    path = Path(csv_path) if csv_path else DEFAULT_RAIN_CSV
    params = rain_scene_params()
    weights = hanoi_step_weights(RAIN_STEP_MIN, 60)
    extra = {
        "step_min": int(RAIN_STEP_MIN),
        "minute_weights": [round(float(v), 6) for v in weights.tolist()],
        "minute_weight_max": round(float(np.max(weights)), 6),
        "pattern": "chicago-hanoi-hadong-15min",
        "idf_b_min": HANOI_IDF_B_MIN,
        "idf_n": HANOI_IDF_N,
        "chicago_r": HANOI_CHICAGO_R,
    }
    if not csv_available(path):
        n = int(np.asarray(hours).size)
        return {
            "rainfall_mm": np.zeros(n, dtype=float),
            "rain_peak_mm": 0.0,
            "rain_csv": str(path),
            **params,
            **extra,
        }
    rh, rm = load_rainfall_csv(path)
    aligned = align_rainfall(hours, rh, rm)
    peak_h = float(np.max(aligned)) if aligned.size else 0.0
    return {
        "rainfall_mm": aligned,
        "rain_peak_mm": peak_h,
        "rain_peak_minute_mm": round(peak_h * float(np.max(weights)), 6),
        "rain_csv": str(path),
        **params,
        **extra,
    }


def load_dem(path: Path, max_dim: int = 220) -> DemGrid:
    with open_dem(path) as src:
        scale = max(src.width, src.height) / float(max(32, max_dim))
        if scale > 1.0:
            out_w = max(32, int(round(src.width / scale)))
            out_h = max(32, int(round(src.height / scale)))
        else:
            out_w, out_h = int(src.width), int(src.height)
        z = np.empty((out_h, out_w), dtype=np.float32)
        src.read(1, out=z, resampling=Resampling.average)
        nodata = src.nodata
        if nodata is not None:
            z = np.where(z == nodata, np.nan, z)
        z[~np.isfinite(z)] = np.nan
        fill = float(np.nanmean(z)) if np.isfinite(z).any() else 0.0
        z = np.where(np.isfinite(z), z, fill).astype(np.float32)
        transform = src.transform * Affine.scale(src.width / out_w, src.height / out_h)
        xs = np.array([xy(transform, 0, c, offset="center")[0] for c in range(out_w)], dtype=float)
        ys = np.array([xy(transform, r, 0, offset="center")[1] for r in range(out_h)], dtype=float)
        cell_m = max(abs(float(transform.a)), abs(float(transform.e)), 1.0)
    return DemGrid(z=z, x_m=xs, y_m=ys, transform=transform, cell_m=cell_m, path=path)


def _hillshade(z: np.ndarray, cell_m: float) -> np.ndarray:
    zf = np.asarray(z, dtype=np.float64)
    dy, dx = np.gradient(zf, max(cell_m, 1.0), max(cell_m, 1.0))
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    az = math.radians(315.0)
    alt = math.radians(45.0)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip(0.22 + 0.78 * hs, 0.0, 1.0).astype(np.float32)


def sample_dem_z(dem: DemGrid, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    rows, cols = rowcol(dem.transform, x, y, op=np.floor)
    rr = np.clip(np.asarray(rows, dtype=np.int32), 0, dem.z.shape[0] - 1)
    cc = np.clip(np.asarray(cols, dtype=np.int32), 0, dem.z.shape[1] - 1)
    return dem.z[rr, cc]


def deposit_rain(
    accum_mm: np.ndarray,
    dem: DemGrid,
    x: np.ndarray,
    y: np.ndarray,
    mass_mm: np.ndarray,
) -> None:
    rows, cols = rowcol(dem.transform, x, y, op=np.floor)
    rr = np.asarray(rows, dtype=np.int32)
    cc = np.asarray(cols, dtype=np.int32)
    ok = (rr >= 0) & (cc >= 0) & (rr < dem.z.shape[0]) & (cc < dem.z.shape[1])
    if not np.any(ok):
        return
    np.add.at(accum_mm, (rr[ok], cc[ok]), mass_mm[ok])


class RainColumn:
    """Dam hat mua: sinh o 5 km, roi, cham DEM thi tich luy."""

    def __init__(
        self,
        dem: DemGrid,
        *,
        max_particles: int = 14000,
        v_term: float = V_TERM_MS,
        wind_east_ms: float = 1.6,
        wind_north_ms: float = 0.4,
        seed: int = 7,
    ) -> None:
        self.dem = dem
        self.max_particles = int(max_particles)
        self.v_term = float(v_term)
        self.wind_e = float(wind_east_ms)
        self.wind_n = float(wind_north_ms)
        self.rng = np.random.default_rng(seed)
        n = self.max_particles
        self.x = np.zeros(n, dtype=np.float64)
        self.y = np.zeros(n, dtype=np.float64)
        self.z = np.full(n, CLOUD_HEIGHT_M, dtype=np.float64)
        self.vz = np.zeros(n, dtype=np.float64)
        self.mass = np.zeros(n, dtype=np.float64)
        self.alive = np.zeros(n, dtype=bool)
        self.xmin = float(min(dem.x_m[0], dem.x_m[-1]))
        self.xmax = float(max(dem.x_m[0], dem.x_m[-1]))
        self.ymin = float(min(dem.y_m[0], dem.y_m[-1]))
        self.ymax = float(max(dem.y_m[0], dem.y_m[-1]))
        self.landed = 0
        self.spawned = 0

    def _free_slots(self, n: int) -> np.ndarray:
        dead = np.flatnonzero(~self.alive)
        if dead.size >= n:
            return dead[:n]
        # Tai su dung hat xa nhat (da gan dat) neu het slot
        take = n - dead.size
        live = np.flatnonzero(self.alive)
        extra = live[np.argsort(self.z[live])[:take]] if live.size else np.array([], dtype=int)
        return np.concatenate([dead, extra])[:n]

    def spawn(self, n: int, mass_mm: float) -> int:
        n = max(0, int(n))
        if n <= 0 or mass_mm <= 0:
            return 0
        idx = self._free_slots(n)
        k = int(idx.size)
        if k <= 0:
            return 0
        self.x[idx] = self.rng.uniform(self.xmin, self.xmax, size=k)
        self.y[idx] = self.rng.uniform(self.ymin, self.ymax, size=k)
        jitter = self.rng.uniform(-8.0, 8.0, size=k)
        self.z[idx] = CLOUD_HEIGHT_M + jitter
        self.vz[idx] = 0.0
        self.mass[idx] = float(mass_mm)
        self.alive[idx] = True
        self.spawned += k
        return k

    def step(self, dt_s: float, accum_mm: np.ndarray) -> int:
        if not np.any(self.alive):
            return 0
        a = self.alive
        # dv/dt = g - (g/v_term^2) v^2  ~ dat v_term nhanh
        drag = (G_MS2 / max(self.v_term * self.v_term, 1e-6)) * self.vz[a] * self.vz[a]
        az = G_MS2 - drag
        self.vz[a] = np.minimum(self.vz[a] + az * dt_s, self.v_term * 1.05)
        self.z[a] -= self.vz[a] * dt_s
        self.x[a] += self.wind_e * dt_s
        self.y[a] += self.wind_n * dt_s
        z_ground = sample_dem_z(self.dem, self.x[a], self.y[a])
        hit = self.z[a] <= z_ground
        if np.any(hit):
            live_idx = np.flatnonzero(a)
            hid = live_idx[hit]
            deposit_rain(accum_mm, self.dem, self.x[hid], self.y[hid], self.mass[hid])
            self.alive[hid] = False
            self.z[hid] = z_ground[hit]
            self.landed += int(hid.size)
            return int(hid.size)
        # Hat ra ngoai vung DEM
        out = (
            (self.x[a] < self.xmin)
            | (self.x[a] > self.xmax)
            | (self.y[a] < self.ymin)
            | (self.y[a] > self.ymax)
        )
        if np.any(out):
            live_idx = np.flatnonzero(a)
            self.alive[live_idx[out]] = False
        return 0

    def positions_km(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        a = self.alive
        x0 = float(self.dem.x_m[0])
        y0 = float(self.dem.y_m[-1])
        return (
            (self.x[a] - x0) / 1000.0,
            (self.y[a] - y0) / 1000.0,
            self.z[a].copy(),
        )


def _spawn_count(rain_mm: float, rain_peak: float, max_new: int) -> int:
    if rain_mm <= 1e-6 or rain_peak <= 1e-6:
        return 0
    frac = min(1.0, rain_mm / rain_peak)
    return max(1, int(round(frac * max_new)))


def _mass_per_drop(rain_mm: float, n_new: int, n_cells: int) -> float:
    if n_new <= 0 or rain_mm <= 0:
        return 0.0
    # Tong khoi luong dat xuong ~ rain_mm * so o (neu du hat cham dat)
    return float(rain_mm) * float(n_cells) / float(n_new)


def simulate(
    dem: DemGrid,
    hours: np.ndarray,
    rain_mm: np.ndarray,
    *,
    dt_s: float = 4.0,
    steps_per_hour: int = 80,
    max_particles: int = 14000,
    max_new_per_step: int = 220,
) -> tuple[RainColumn, np.ndarray, list[dict]]:
    col = RainColumn(dem, max_particles=max_particles)
    accum = np.zeros(dem.z.shape, dtype=np.float64)
    peak = float(np.max(rain_mm)) if rain_mm.size else 1.0
    n_cells = int(dem.z.size)
    log: list[dict] = []
    n = int(hours.size)
    print(
        f"Mo phong {n} gio, hat roi tu {CLOUD_HEIGHT_M:.0f} m, "
        f"v_term={col.v_term:.1f} m/s, dt={dt_s:g} s...",
        flush=True,
    )
    for i, (h, r) in enumerate(zip(hours.tolist(), rain_mm.tolist())):
        n_new = _spawn_count(r, peak, max_new_per_step)
        mass = _mass_per_drop(r, n_new * steps_per_hour, n_cells) if n_new else 0.0
        landed0 = col.landed
        for _ in range(steps_per_hour):
            if n_new:
                col.spawn(n_new, mass)
            col.step(dt_s, accum)
        log.append(
            {
                "hour": float(h),
                "rainfall_mm": float(r),
                "spawned": int(n_new * steps_per_hour),
                "landed_hour": int(col.landed - landed0),
                "alive": int(np.count_nonzero(col.alive)),
                "accum_mean_mm": float(accum.mean()),
                "accum_max_mm": float(accum.max()),
            }
        )
        if n >= 12 and ((i + 1) % 24 == 0 or i == n - 1):
            print(
                f"  gio {h:.0f}/{hours[-1]:.0f}  mua={r:.2f} mm  "
                f"hat={int(np.count_nonzero(col.alive))}  "
                f"tich luy TB={accum.mean():.2f} mm",
                flush=True,
            )
    # Cho hat con lai roi het
    extra = 0
    while np.any(col.alive) and extra < 4000:
        col.step(dt_s, accum)
        extra += 1
    return col, accum, log


def _terrain_colors(z: np.ndarray, accum: np.ndarray, cell_m: float) -> np.ndarray:
    hs = _hillshade(z, cell_m)
    zmin, zmax = float(np.min(z)), float(np.max(z))
    zn = (z - zmin) / max(zmax - zmin, 1e-6)
    ground = np.stack(
        [
            0.28 + 0.45 * zn + 0.18 * hs,
            0.34 + 0.38 * zn + 0.20 * hs,
            0.22 + 0.22 * zn + 0.16 * hs,
        ],
        axis=-1,
    )
    wet = accum / max(float(np.max(accum)), 1e-6)
    water = np.array([0.12, 0.42, 0.82])
    alpha = np.clip(0.15 + 0.75 * np.sqrt(wet), 0.0, 0.88)[..., None]
    rgb = ground * (1.0 - alpha) + water * alpha
    return np.clip(rgb, 0.0, 1.0)


def _setup_mpl():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def render_scene(
    dem: DemGrid,
    col: RainColumn,
    accum: np.ndarray,
    *,
    hour: float,
    rain_mm: float,
    path: Optional[Path] = None,
    elev: float = 18.0,
    azim: float = -55.0,
) -> np.ndarray:
    plt = _setup_mpl()

    xg, yg = np.meshgrid(dem.x_km, dem.y_km)
    facecolors = _terrain_colors(dem.z, accum, dem.cell_m)
    px, py, pz = col.positions_km()

    fig = plt.figure(figsize=(11.2, 8.4), facecolor="#0b1220")
    ax = fig.add_subplot(111, projection="3d", computed_zorder=False)
    ax_any: Any = ax
    ax.set_facecolor("#0b1220")
    ax.plot_surface(
        xg,
        yg,
        dem.z,
        facecolors=facecolors,
        rstride=1,
        cstride=1,
        linewidth=0,
        antialiased=False,
        shade=False,
        zorder=1,
    )
    # Mat may 5 km
    cloud_z = np.full_like(xg, CLOUD_HEIGHT_M, dtype=float)
    ax.plot_surface(
        xg,
        yg,
        cloud_z,
        color=(0.75, 0.82, 0.92, 0.18),
        linewidth=0,
        shade=False,
        zorder=3,
    )
    if px.size:
        ax_any.scatter(
            px,
            py,
            pz,
            s=2.2,
            c="#9fd4ff",
            depthshade=False,
            alpha=0.55,
            linewidths=0,
            zorder=4,
        )
    ax.set_xlabel("X (km)", color="#d5e4f5")
    ax.set_ylabel("Y (km)", color="#d5e4f5")
    ax.set_zlabel("Z (m)", color="#d5e4f5")
    ax.set_zlim(float(np.min(dem.z)) - 30.0, CLOUD_HEIGHT_M + 80.0)
    try:
        ax.set_box_aspect((dem.width_km, dem.height_km, 8.5))
    except Exception:
        pass
    ax.view_init(elev=elev, azim=azim)
    ax.tick_params(colors="#9bb0c7")
    for pane in (ax_any.xaxis, ax_any.yaxis, ax_any.zaxis):
        try:
            pane.pane.fill = False
            pane.pane.set_edgecolor((1, 1, 1, 0.08))
        except Exception:
            pass
    ax.set_title(
        f"Mưa rơi từ {CLOUD_HEIGHT_M/1000:.0f} km  ·  giờ {hour:.0f} h  ·  "
        f"{rain_mm:.2f} mm/h  ·  hạt {int(np.count_nonzero(col.alive)):,}",
        color="#e8f1fa",
        pad=10,
        fontsize=11,
    )
    fig.tight_layout()
    fig.canvas.draw()
    rgba = np.asarray(getattr(fig.canvas, "buffer_rgba")())
    rgb = rgba[..., :3].copy()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=130, facecolor=fig.get_facecolor())
    plt.close(fig)
    return rgb


def plot_maps(
    dem: DemGrid,
    accum: np.ndarray,
    hours: np.ndarray,
    rain_mm: np.ndarray,
    path: Path,
) -> None:
    plt = _setup_mpl()
    hs = _hillshade(dem.z, dem.cell_m)
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 5.8))
    ax = axes[0]
    ax.imshow(hs, cmap="gray", origin="upper")
    im = ax.imshow(np.ma.masked_where(accum <= 1e-6, accum), cmap="Blues", origin="upper", alpha=0.75)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="Mưa tích lũy (mm)")
    ax.set_title("Tích lũy trên bề mặt DEM")
    ax.set_axis_off()

    ax = axes[1]
    ax.bar(hours, rain_mm, width=0.9, color="#4a90d9")
    ax.set_xlabel("Giờ")
    ax.set_ylabel("Mưa (mm/h)")
    ax.set_title("Cường độ mưa đầu vào")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def write_hourly_csv(log: list[dict], path: Path) -> None:
    fields = ["hour", "rainfall_mm", "spawned", "landed_hour", "alive", "accum_mean_mm", "accum_max_mm"]
    rows = [{k: row[k] for k in fields} for row in log]
    write_csv_rows(path, fields, rows)


def write_accum_tif(dem: DemGrid, accum: np.ndarray, path: Path) -> None:
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    with open_dem(dem.path) as src:
        profile = src.profile.copy()
    profile.update(
        driver="GTiff",
        height=accum.shape[0],
        width=accum.shape[1],
        count=1,
        dtype="float32",
        transform=dem.transform,
        compress="deflate",
        nodata=None,
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(accum.astype(np.float32), 1)


def write_video(
    dem: DemGrid,
    hours: np.ndarray,
    rain_mm: np.ndarray,
    out_dir: Path,
    *,
    dt_s: float = 6.0,
    steps_per_frame: int = 8,
    frames_per_hour: int = 2,
    fps: float = 12.0,
    max_particles: int = 12000,
    max_new_per_step: int = 180,
) -> dict[str, Path]:
    col = RainColumn(dem, max_particles=max_particles)
    accum = np.zeros(dem.z.shape, dtype=np.float64)
    peak = float(np.max(rain_mm)) if rain_mm.size else 1.0
    n_cells = int(dem.z.size)
    frames: list[np.ndarray] = []
    wet = [i for i, r in enumerate(rain_mm.tolist()) if r > 0.02]
    if not wet:
        wet = list(range(int(hours.size)))
    i0, i1 = wet[0], wet[-1]
    print(f"Tao video gio {hours[i0]:.0f}–{hours[i1]:.0f}...", flush=True)
    for i in range(i0, i1 + 1):
        r = float(rain_mm[i])
        n_new = _spawn_count(r, peak, max_new_per_step)
        mass = _mass_per_drop(r, max(n_new * steps_per_frame * frames_per_hour, 1), n_cells)
        for f in range(frames_per_hour):
            for _ in range(steps_per_frame):
                if n_new:
                    col.spawn(n_new, mass)
                col.step(dt_s, accum)
            rgb = render_scene(
                dem,
                col,
                accum,
                hour=float(hours[i]),
                rain_mm=r,
            )
            frames.append(rgb)
        if (i - i0 + 1) % 12 == 0 or i == i1:
            print(
                f"  khung {len(frames)}  gio {hours[i]:.0f}  hat={int(np.count_nonzero(col.alive))}",
                flush=True,
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    h, w = frames[0].shape[:2]
    we, he = w - (w % 2), h - (h % 2)
    if we != w or he != h:
        frames = [fr[:he, :we] for fr in frames]
        h, w = he, we

    mp4 = out_dir / "rainfall_fall_animation.mp4"
    try:
        import cv2

        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:
            pass
        fourcc = getattr(cv2, "VideoWriter_fourcc")
        vw = cv2.VideoWriter(str(mp4), fourcc(*"mp4v"), float(fps), (w, h))
        if not vw.isOpened():
            vw.release()
            raise RuntimeError("VideoWriter khong mo duoc")
        for fr in frames:
            vw.write(fr[:, :, ::-1])
        vw.release()
        if mp4.is_file() and mp4.stat().st_size > 0:
            written["mp4"] = mp4
    except Exception as exc:
        print(f"Khong ghi MP4 (cv2): {exc}", flush=True)

    gif = out_dir / "rainfall_fall_animation.gif"
    try:
        from PIL import Image

        pal = getattr(Image, "ADAPTIVE", None) or Image.Palette.ADAPTIVE
        step = max(1, len(frames) // 80)
        imgs = [
            Image.fromarray(fr, mode="RGB").convert("P", palette=pal, colors=64)
            for fr in frames[::step]
        ]
        dur = max(50, int(round(1000.0 / max(fps, 1.0))))
        imgs[0].save(
            gif,
            save_all=True,
            append_images=imgs[1:],
            duration=dur,
            loop=0,
            optimize=True,
        )
        written["gif"] = gif
    except Exception as exc:
        print(f"Khong ghi GIF: {exc}", flush=True)
    return written


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mo phong mua roi tu 5 km xuong DEM")
    p.add_argument("--csv", type=Path, default=DEFAULT_RAIN_CSV, help="CSV cot rainfall_mm")
    p.add_argument("--dem", type=Path, default=DEFAULT_DEM)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--max-dim", type=int, default=180, help="Giam kich thuoc DEM de ve 3D")
    p.add_argument("--hour", type=float, default=None, help="Gio chup canh 3D (mac dinh: gio mua lon nhat)")
    p.add_argument("--video", action="store_true", help="Ghi MP4/GIF hat mua roi")
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    _configure_stdio()
    args = parse_args(argv)
    rain_path = Path(args.csv)
    if not csv_available(rain_path):
        raise FileNotFoundError(f"Khong thay file mua: {rain_path}")
    dem_path = resolve_dem_path(None, str(args.dem) if args.dem else None)
    hours, rain = load_rainfall_csv(rain_path)
    dem = load_dem(dem_path, max_dim=int(args.max_dim))
    print(f"DEM {dem.z.shape[1]}x{dem.z.shape[0]}  "
          f"{dem.width_km:.1f} x {dem.height_km:.1f} km  "
          f"z={float(np.min(dem.z)):.1f}…{float(np.max(dem.z)):.1f} m", flush=True)
    print(f"Mua {rain_path.name}: {int(hours.size)} gio, tong {float(rain.sum()):.1f} mm, "
          f"dinh {float(np.max(rain)):.2f} mm/h", flush=True)

    col, accum, log = simulate(dem, hours, rain)
    peak_i = int(np.argmax(rain))
    snap_i = peak_i
    if args.hour is not None:
        snap_i = int(np.argmin(np.abs(hours - float(args.hour))))

    # Cot mua 5 km dung gio chup: hat rai deu tu may xuong DEM
    snap_col = RainColumn(dem, max_particles=12000)
    r_snap = float(rain[snap_i])
    n_vis = max(800, _spawn_count(r_snap, float(np.max(rain)), 9000))
    snap_col.spawn(n_vis, 0.0)
    z_g = sample_dem_z(snap_col.dem, snap_col.x[:n_vis], snap_col.y[:n_vis])
    snap_col.z[:n_vis] = snap_col.rng.uniform(z_g, CLOUD_HEIGHT_M)
    snap_col.vz[:n_vis] = snap_col.v_term
    snap_col.alive[:n_vis] = True

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "rainfall_fall_hourly.csv"
    write_hourly_csv(log, csv_path)
    print(f"Nhat ky CSV : {csv_path}", flush=True)
    tif_path = out_dir / "rainfall_accumulation.tif"
    write_accum_tif(dem, accum, tif_path)
    print(f"Tich luy TIF: {tif_path}", flush=True)

    if not args.no_plot:
        map_path = out_dir / "rainfall_accumulation.png"
        plot_maps(dem, accum, hours, rain, map_path)
        print(f"Ban do      : {map_path}", flush=True)
        fig_path = out_dir / "rainfall_fall_3d.png"
        render_scene(
            dem,
            snap_col,
            accum,
            hour=float(hours[snap_i]),
            rain_mm=r_snap,
            path=fig_path,
        )
        print(f"Canh 3D     : {fig_path}", flush=True)

    if args.video:
        written = write_video(dem, hours, rain, out_dir)
        for kind, pth in written.items():
            print(f"Video {kind:4s} : {pth}", flush=True)

    print(
        f"Xong. Hat sinh {col.spawned:,}  cham dat {col.landed:,}  "
        f"tich luy max {float(accum.max()):.2f} mm",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Vẽ sơ đồ dữ liệu đầu vào mô hình 1D Saint-Venant (PNG)."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

from flood_model.csv_io import csv_available, csv_open

OUT = ROOT / "so_do_du_lieu_dau_vao_1d_saint_venant.png"

NAVY = "#1B365D"
TEAL = "#0E7490"
BLUE = "#1D4ED8"
BLUE_BG = "#EFF6FF"
BLUE_BD = "#93C5FD"
ORANGE = "#C2410C"
ORANGE_BG = "#FFF7ED"
ORANGE_BD = "#FDBA74"
GREEN = "#166534"
GREEN_BG = "#F0FDF4"
GREEN_BD = "#86EFAC"
PURPLE = "#6B21A8"
PURPLE_BG = "#FAF5FF"
PURPLE_BD = "#D8B4FE"
CYAN = "#0E7490"
CYAN_BG = "#ECFEFF"
CYAN_BD = "#67E8F9"
INDIGO = "#3730A3"
INDIGO_BG = "#EEF2FF"
INDIGO_BD = "#A5B4FC"
AMBER = "#92400E"
AMBER_BG = "#FFFBEB"
AMBER_BD = "#FCD34D"
SLATE = "#334155"
MUTED = "#64748B"
WHITE = "#FFFFFF"
LINE = "#E2E8F0"


def _pick_font() -> str:
    names = {f.name for f in fm.fontManager.ttflist}
    for cand in ("Segoe UI", "Calibri", "Tahoma", "Arial", "DejaVu Sans"):
        if cand in names:
            return cand
    return "DejaVu Sans"


def rbox(ax, x, y, w, h, fc, ec, lw=1.15, r=0.01, z=2):
    p = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.005,rounding_size={r}",
        facecolor=fc,
        edgecolor=ec,
        linewidth=lw,
        zorder=z,
        mutation_aspect=0.55,
    )
    ax.add_patch(p)
    return p


def arrow(ax, x1, y1, x2, y2, color, lw=1.6, ms=11, z=6):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=ms,
            linewidth=lw,
            color=color,
            zorder=z,
        )
    )


def txt(ax, x, y, s, **kw):
    ax.text(x, y, s, zorder=8, **kw)


def bullet(ax, x, y, s, color=SLATE, fs=6.7, dy=0.0):
    txt(ax, x, y, "•  " + s, ha="left", va="top", fontsize=fs, color=color)


def draw_table(ax, x, y, col_w, row_h, headers, rows, head_fc, head_tc, bd="#94A3B8"):
    n_c = len(headers)
    n_r = len(rows)
    w = sum(col_w)
    h = row_h * (n_r + 1)
    # header
    xx = x
    for i, (lab, cw) in enumerate(zip(headers, col_w)):
        ax.add_patch(Rectangle((xx, y + n_r * row_h), cw, row_h, fc=head_fc, ec=bd, lw=0.6, zorder=5))
        txt(
            ax,
            xx + cw / 2,
            y + n_r * row_h + row_h / 2,
            lab,
            ha="center",
            va="center",
            fontsize=6.0,
            fontweight="bold",
            color=head_tc,
        )
        xx += cw
    for r, row in enumerate(rows):
        xx = x
        bg = WHITE if r % 2 == 0 else "#F8FAFC"
        for val, cw in zip(row, col_w):
            ax.add_patch(
                Rectangle((xx, y + (n_r - 1 - r) * row_h), cw, row_h, fc=bg, ec=bd, lw=0.55, zorder=5)
            )
            txt(
                ax,
                xx + cw / 2,
                y + (n_r - 1 - r) * row_h + row_h / 2,
                val,
                ha="center",
                va="center",
                fontsize=5.8,
                color=SLATE,
            )
            xx += cw
    return w, h


def mini_hydro(ax, x0, y0, w, h, series, line, fill, xlabel="Thời gian", ylabel=""):
    ax.add_patch(Rectangle((x0, y0), w, h, fc=WHITE, ec="#CBD5E1", lw=0.7, zorder=4))
    if len(series) < 2:
        return
    vals = list(series)
    vmin, vmax = min(vals), max(vals)
    span = max(vmax - vmin, 1e-6)
    xs, ys = [], []
    n = len(vals)
    pad = 0.08
    for i, v in enumerate(vals):
        xs.append(x0 + pad * w + (1 - 2 * pad) * w * i / (n - 1))
        ys.append(y0 + pad * h + (1 - 2 * pad) * h * (v - vmin) / span)
    poly = [(xs[0], y0 + pad * h)] + list(zip(xs, ys)) + [(xs[-1], y0 + pad * h)]
    ax.add_patch(Polygon(poly, closed=True, fc=fill, ec="none", alpha=0.55, zorder=5))
    ax.plot(xs, ys, color=line, lw=1.35, zorder=6, solid_capstyle="round")
    txt(ax, x0 + w / 2, y0 - 0.002, xlabel, ha="center", va="top", fontsize=5.2, color=MUTED)
    if ylabel:
        txt(ax, x0 - 0.002, y0 + h / 2, ylabel, ha="right", va="center", fontsize=5.2, color=MUTED, rotation=90)


def load_col(path: Path, col: str, nmax: int = 80) -> list[float]:
    if not csv_available(path):
        return []
    out = []
    with csv_open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                out.append(float(row[col]))
            except (KeyError, TypeError, ValueError):
                continue
            if len(out) >= 2000:
                break
    if len(out) > nmax:
        step = max(1, len(out) // nmax)
        out = out[::step]
    return out


def draw_xs_icon(ax, x, y, w, h, title):
    ax.add_patch(Rectangle((x, y), w, h, fc=WHITE, ec="#86EFAC", lw=0.7, zorder=4))
    # trapezoid channel
    ax.add_patch(
        Polygon(
            [
                (x + 0.12 * w, y + 0.22 * h),
                (x + 0.88 * w, y + 0.22 * h),
                (x + 0.70 * w, y + 0.55 * h),
                (x + 0.30 * w, y + 0.55 * h),
            ],
            closed=True,
            fc="#A8A29E",
            ec="#57534E",
            lw=0.6,
            zorder=5,
        )
    )
    ax.add_patch(
        Polygon(
            [
                (x + 0.34 * w, y + 0.52 * h),
                (x + 0.66 * w, y + 0.52 * h),
                (x + 0.66 * w, y + 0.72 * h),
                (x + 0.34 * w, y + 0.72 * h),
            ],
            closed=True,
            fc="#3B82F6",
            alpha=0.85,
            zorder=6,
        )
    )
    txt(ax, x + w / 2, y + 0.06 * h, title, ha="center", va="bottom", fontsize=5.4, color=GREEN, fontweight="bold")


def draw_dem_icon(ax, x, y, w, h, title):
    ax.add_patch(Rectangle((x, y), w, h, fc=WHITE, ec="#86EFAC", lw=0.7, zorder=4))
    colors = ["#166534", "#4ADE80", "#FDE047", "#FDBA74", "#F97316"]
    for i, c in enumerate(colors):
        ax.add_patch(
            Rectangle((x + 0.08 * w, y + 0.18 * h + i * 0.12 * h), 0.84 * w, 0.12 * h, fc=c, ec="none", zorder=5)
        )
    txt(ax, x + w / 2, y + 0.06 * h, title, ha="center", va="bottom", fontsize=5.4, color=GREEN, fontweight="bold")


def draw_net_icon(ax, x, y, w, h, title):
    ax.add_patch(Rectangle((x, y), w, h, fc=WHITE, ec="#86EFAC", lw=0.7, zorder=4))
    ax.plot([x + 0.12 * w, x + 0.88 * w], [y + 0.48 * h, y + 0.42 * h], color="#1D4ED8", lw=2.0, zorder=5)
    ax.plot([x + 0.52 * w, x + 0.78 * w], [y + 0.45 * h, y + 0.78 * h], color="#EA580C", lw=1.6, zorder=5)
    ax.add_patch(Circle((x + 0.52 * w, y + 0.45 * h), 0.012, fc="#F97316", ec="#9A3412", lw=0.6, zorder=6))
    txt(ax, x + w / 2, y + 0.06 * h, title, ha="center", va="bottom", fontsize=5.4, color=GREEN, fontweight="bold")


def draw_n_icon(ax, x, y, w, h, title):
    ax.add_patch(Rectangle((x, y), w, h, fc=WHITE, ec="#D8B4FE", lw=0.7, zorder=4))
    ax.plot(
        [x + 0.12 * w, x + 0.35 * w, x + 0.55 * w, x + 0.88 * w],
        [y + 0.28 * h, y + 0.55 * h, y + 0.48 * h, y + 0.72 * h],
        color=PURPLE,
        lw=1.5,
        zorder=5,
    )
    txt(ax, x + w / 2, y + 0.78 * h, r"$n(x)$", ha="center", va="center", fontsize=7.0, color=PURPLE)
    txt(ax, x + w / 2, y + 0.06 * h, title, ha="center", va="bottom", fontsize=5.4, color=PURPLE, fontweight="bold")


def main():
    font = _pick_font()
    plt.rcParams.update(
        {
            "font.family": font,
            "mathtext.fontset": "dejavusans",
            "text.color": NAVY,
            "axes.unicode_minus": False,
        }
    )

    q_series = load_col(ROOT / "saint_venant_output" / "demo_inflow_q_m3s.csv", "q_m3s", 90)
    if not q_series:
        q_series = load_col(ROOT / "rainfall_runoff_output" / "tank_result.csv", "q_m3s", 90)
    h_series = load_col(ROOT / "saint_venant_output" / "demo_downstream_stage.csv", "h_m", 90)
    if not h_series:
        h_series = load_col(ROOT / "muskingum_output" / "demo_downstream_stage.csv", "h_m", 90)

    fig, ax = plt.subplots(figsize=(16.4, 11.0), dpi=180)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)
    ax.set_facecolor(WHITE)

    txt(
        ax,
        0.50,
        0.972,
        "DỮ LIỆU ĐẦU VÀO CHO MÔ HÌNH THỦY LỰC 1D SAINT-VENANT",
        ha="center",
        va="center",
        fontsize=16.2,
        fontweight="bold",
        color=NAVY,
    )

    rbox(ax, 0.055, 0.905, 0.890, 0.048, AMBER_BG, AMBER_BD, lw=1.1, r=0.01, z=2)
    txt(ax, 0.078, 0.940, "MỤC TIÊU", ha="left", va="top", fontsize=7.4, fontweight="bold", color=AMBER)
    txt(
        ax,
        0.078,
        0.922,
        "Cung cấp dữ liệu cần thiết để mô hình Saint-Venant mô phỏng lan truyền dòng chảy / lũ 1D trên mạng sông\n"
        "(lòng chính + nhánh) và hiệu chỉnh tham số thủy lực (Manning n, điều kiện biên, điều kiện ban đầu).",
        ha="left",
        va="top",
        fontsize=6.8,
        color=SLATE,
        linespacing=1.25,
    )

    # ===== 1. Q vào =====
    rbox(ax, 0.018, 0.575, 0.312, 0.312, BLUE_BG, BLUE_BD, lw=1.2, r=0.011)
    txt(ax, 0.032, 0.868, "1. DỮ LIỆU LƯU LƯỢNG VÀO  Q(t)", ha="left", va="top", fontsize=8.0, fontweight="bold", color=BLUE)
    for i, s in enumerate(
        [
            "Lưu lượng thượng lưu lòng chính — đầu ra mô hình TANK",
            "Đơn vị: m³/s    ·    Cột CSV: hour, q_m3s",
            "Bước thời gian: giờ (khớp --dt xuất kết quả)",
            "File: rainfall_runoff_output/tank_result.csv",
            "CLI: --inflow   (bắt buộc có chuỗi liên tục)",
        ]
    ):
        bullet(ax, 0.032, 0.842 - i * 0.020, s, fs=6.35)

    draw_table(
        ax,
        0.032,
        0.590,
        [0.055, 0.070],
        0.0185,
        ["hour", "q_m3s"],
        [
            ["0.00", "3945.3"],
            ["1.00", "3957.6"],
            ["2.00", "3969.4"],
            ["...", "..."],
            ["191.00", "7820.1"],
        ],
        "#1D4ED8",
        WHITE,
    )
    mini_hydro(ax, 0.175, 0.590, 0.140, 0.112, q_series or [4, 5, 8, 12, 9, 6], BLUE, "#93C5FD", "Thời gian (giờ)", "Q (m³/s)")
    txt(ax, 0.245, 0.708, "Ví dụ thủy đồ Q vào", ha="center", va="bottom", fontsize=5.6, color=BLUE, fontweight="bold")

    # ===== 2. H hạ lưu =====
    rbox(ax, 0.344, 0.575, 0.312, 0.312, ORANGE_BG, ORANGE_BD, lw=1.2, r=0.011)
    txt(ax, 0.358, 0.868, "2. DỮ LIỆU CỐT NƯỚC HẠ LƯU  H(t)", ha="left", va="top", fontsize=8.0, fontweight="bold", color=ORANGE)
    for i, s in enumerate(
        [
            "Biên mực nước tại cửa ra lòng chính",
            "Đơn vị: m    ·    Cột CSV: hour, h_m",
            "Hoặc H hằng: --h-down  (ưu tiên hơn CSV)",
            "File: saint_venant_output/demo_downstream_stage.csv",
            "CLI: --h-csv",
        ]
    ):
        bullet(ax, 0.358, 0.842 - i * 0.020, s, fs=6.35)

    draw_table(
        ax,
        0.358,
        0.590,
        [0.055, 0.062],
        0.0185,
        ["hour", "h_m"],
        [
            ["0.00", "5.800"],
            ["1.00", "5.812"],
            ["2.00", "5.824"],
            ["...", "..."],
            ["191.00", "..."],
        ],
        "#C2410C",
        WHITE,
    )
    mini_hydro(ax, 0.495, 0.590, 0.145, 0.112, h_series or [5.8, 5.85, 6.1, 6.4, 6.2, 5.9], ORANGE, "#FED7AA", "Thời gian (giờ)", "H (m)")
    txt(ax, 0.567, 0.708, "Ví dụ chuỗi H hạ lưu", ha="center", va="bottom", fontsize=5.6, color=ORANGE, fontweight="bold")

    # ===== 3. Hình học =====
    rbox(ax, 0.670, 0.575, 0.312, 0.312, GREEN_BG, GREEN_BD, lw=1.2, r=0.011)
    txt(ax, 0.684, 0.868, "3. DỮ LIỆU HÌNH HỌC LÒNG SÔNG", ha="left", va="top", fontsize=8.0, fontweight="bold", color=GREEN)
    for i, s in enumerate(
        [
            "DEM cắt mặt cắt ngang: A(H), P(H), B(H), z_bed",
            "File DEM: projects/data/dem-song-hong.tif   (--dem)",
            "Khoảng XS: --xs-spacing  (m, mặc định 1500)",
            "Nửa bề rộng XS: --xs-half 1500 m  ·  nhanh: --trib-half",
            "Mạng 1D: lòng chính + nhánh (DEM); --no-network = 1 lòng",
            "CSV hình học xuất: demo_river_geometry.csv,",
            "demo_cross_sections.csv  (offset_m, z_m)",
        ]
    ):
        bullet(ax, 0.684, 0.842 - i * 0.0195, s, fs=6.25)

    draw_dem_icon(ax, 0.688, 0.588, 0.088, 0.088, "DEM")
    draw_xs_icon(ax, 0.786, 0.588, 0.088, 0.088, "Mặt cắt XS")
    draw_net_icon(ax, 0.884, 0.588, 0.082, 0.088, "Mạng 1D")

    # ===== 4. Manning n =====
    rbox(ax, 0.018, 0.248, 0.312, 0.312, PURPLE_BG, PURPLE_BD, lw=1.2, r=0.011)
    txt(ax, 0.032, 0.542, "4. DỮ LIỆU HỆ SỐ MANNING  n(x)", ha="left", va="top", fontsize=8.0, fontweight="bold", color=PURPLE)
    for i, s in enumerate(
        [
            "Ma sát đáy theo từng mặt cắt",
            "CSV: xs_id, manning_n [, station_km]",
            "Thiếu id → nội suy theo lý trình; còn lại dùng --n",
            "Mặc định --n 0.030; tự đọc demo_manning_n.csv nếu có",
            "CLI: --n   /   --n-csv",
        ]
    ):
        bullet(ax, 0.032, 0.516 - i * 0.0195, s, fs=6.3)

    draw_table(
        ax,
        0.032,
        0.262,
        [0.048, 0.072, 0.062],
        0.018,
        ["xs_id", "station_km", "n"],
        [
            ["1", "0.00", "0.075"],
            ["2", "3.03", "0.075"],
            ["3", "6.05", "0.068"],
            ["...", "...", "..."],
            ["21", "60.51", "0.047"],
        ],
        PURPLE,
        WHITE,
    )
    draw_n_icon(ax, 0.228, 0.268, 0.088, 0.100, "n theo lý trình")

    # ===== 5. IC =====
    rbox(ax, 0.344, 0.248, 0.312, 0.312, CYAN_BG, CYAN_BD, lw=1.2, r=0.011)
    txt(ax, 0.358, 0.542, "5. ĐIỀU KIỆN BAN ĐẦU  Q0, H0", ha="left", va="top", fontsize=8.0, fontweight="bold", color=CYAN)
    for i, s in enumerate(
        [
            "Q0: --ic-csv (xs_id) → --q0 → Q vào lúc t = 0",
            "H0: --ic-csv (nội suy lý trình) → backwater từ --h0",
            "     hoặc H hạ lưu lúc t = 0",
            "CSV: xs_id, q0, h0 [, station_km]",
            "CLI: --q0  --h0  --ic-csv demo_initial_qh.csv",
        ]
    ):
        bullet(ax, 0.358, 0.516 - i * 0.0195, s, fs=6.3)

    draw_table(
        ax,
        0.358,
        0.262,
        [0.048, 0.072, 0.062],
        0.018,
        ["xs_id", "q0 (m³/s)", "h0 (m)"],
        [
            ["1", "3945.3", "14.48"],
            ["2", "3945.3", "7.85"],
            ["3", "3945.3", "6.16"],
            ["...", "...", "..."],
            ["21", "3945.3", "..."],
        ],
        CYAN,
        WHITE,
    )
    txt(
        ax,
        0.548,
        0.368,
        "Nếu không có CSV:\nbackwater từ H_ds(0)",
        ha="left",
        va="top",
        fontsize=6.1,
        color=CYAN,
        fontweight="bold",
        linespacing=1.3,
    )

    # ===== 6. Mạng + quản lý =====
    rbox(ax, 0.670, 0.248, 0.312, 0.312, INDIGO_BG, INDIGO_BD, lw=1.2, r=0.011)
    txt(ax, 0.684, 0.542, "6. NHÁNH / BIÊN THOÁT  +  THAM SỐ", ha="left", va="top", fontsize=7.7, fontweight="bold", color=INDIGO)
    for i, s in enumerate(
        [
            "Nhánh thoát (vd. Sông Đuống): biên H = h_na1_m",
            "File: saint_venant_output/demo_downstream_stage.csv",
            "CLI: --trib-h-csv   ·   --max-tribs 1   --min-trib-km 2",
            "Nhánh nhập lưu: biên Q (tỷ lệ q_frac từ Q tank)",
            "CFL = 0.45  ·  dt thủy lực tối đa 60 s  (--cfl, --dt-hydro)",
            "Xuất kết quả theo giờ: --dt 1.0",
            "Q ≥ Qmin (mặc định); --allow-reverse cho phép Q âm",
        ]
    ):
        bullet(ax, 0.684, 0.516 - i * 0.0198, s, fs=6.2)

    draw_table(
        ax,
        0.684,
        0.258,
        [0.055, 0.048, 0.055, 0.070],
        0.0175,
        ["hour", "h_m", "h_na1_m", "vai trò"],
        [
            ["0.00", "5.800", "5.240", "H chính / H nhánh"],
            ["1.00", "5.812", "5.252", "biên hạ lưu"],
            ["...", "...", "...", "weir thoát"],
        ],
        INDIGO,
        WHITE,
    )

    # ===== Tổng hợp =====
    rbox(ax, 0.018, 0.018, 0.618, 0.214, WHITE, "#CBD5E1", lw=1.15, r=0.011)
    txt(ax, 0.032, 0.212, "TỔNG HỢP DỮ LIỆU ĐẦU VÀO", ha="left", va="top", fontsize=8.0, fontweight="bold", color=NAVY)

    chips = [
        (0.040, BLUE_BG, BLUE_BD, BLUE, "Q(t)\nthượng lưu\n(m³/s)"),
        (0.132, ORANGE_BG, ORANGE_BD, ORANGE, "H_ds(t)\nhạ lưu\n(m)"),
        (0.224, GREEN_BG, GREEN_BD, GREEN, "DEM / XS\nmạng 1D\n(m)"),
        (0.316, PURPLE_BG, PURPLE_BD, PURPLE, "n(x)\nManning\n(-)"),
        (0.408, CYAN_BG, CYAN_BD, CYAN, "Q0, H0\nban đầu\n(m³/s, m)"),
        (0.500, INDIGO_BG, INDIGO_BD, INDIGO, "H nhánh\nh_na1\n(m)"),
    ]
    for x, bg, bd, fg, lab in chips:
        rbox(ax, x, 0.095, 0.082, 0.092, bg, bd, lw=1.0, r=0.008, z=3)
        txt(ax, x + 0.041, 0.140, lab, ha="center", va="center", fontsize=6.3, color=fg, fontweight="bold", linespacing=1.25)
    for x in (0.122, 0.214, 0.306, 0.398, 0.490):
        txt(ax, x, 0.138, "+", ha="center", va="center", fontsize=12, color=MUTED, fontweight="bold")

    rbox(ax, 0.032, 0.030, 0.588, 0.052, AMBER_BG, AMBER_BD, lw=0.9, r=0.008, z=3)
    txt(
        ax,
        0.044,
        0.056,
        "Lưu ý:  Chất lượng DEM, khoảng XS, chuỗi Q/H biên và Manning n quyết định trực tiếp độ chính xác\n"
        "cốt nước và lưu lượng. Chuỗi thời gian Q vào và H hạ lưu phải cùng bước (thường 1 giờ) và đủ dài.",
        ha="left",
        va="center",
        fontsize=6.35,
        color=AMBER,
        linespacing=1.28,
    )

    # ===== Sơ đồ luồng =====
    rbox(ax, 0.650, 0.018, 0.332, 0.214, "#F8FAFC", "#93C5FD", lw=1.15, r=0.011)
    txt(ax, 0.816, 0.212, "ĐẦU VÀO  →  MÔ HÌNH 1D", ha="center", va="top", fontsize=7.6, fontweight="bold", color=NAVY)

    rbox(ax, 0.668, 0.145, 0.070, 0.042, BLUE_BG, BLUE_BD, lw=0.9, r=0.006, z=3)
    txt(ax, 0.703, 0.166, "Q(t)", ha="center", va="center", fontsize=7.2, fontweight="bold", color=BLUE)
    rbox(ax, 0.668, 0.092, 0.070, 0.042, ORANGE_BG, ORANGE_BD, lw=0.9, r=0.006, z=3)
    txt(ax, 0.703, 0.113, r"$H_{ds}$", ha="center", va="center", fontsize=7.2, fontweight="bold", color=ORANGE)
    rbox(ax, 0.668, 0.040, 0.070, 0.042, GREEN_BG, GREEN_BD, lw=0.9, r=0.006, z=3)
    txt(ax, 0.703, 0.061, "DEM, n", ha="center", va="center", fontsize=6.6, fontweight="bold", color=GREEN)

    arrow(ax, 0.740, 0.166, 0.768, 0.130, MUTED, lw=1.2, ms=9)
    arrow(ax, 0.740, 0.113, 0.768, 0.113, MUTED, lw=1.2, ms=9)
    arrow(ax, 0.740, 0.061, 0.768, 0.095, MUTED, lw=1.2, ms=9)

    # mini channel
    ax.add_patch(
        Polygon(
            [(0.770, 0.078), (0.900, 0.070), (0.900, 0.155), (0.770, 0.168)],
            closed=True,
            fc="#3B82F6",
            alpha=0.85,
            zorder=4,
        )
    )
    ax.add_patch(
        Polygon(
            [(0.770, 0.062), (0.900, 0.054), (0.900, 0.078), (0.770, 0.086)],
            closed=True,
            fc="#A8A29E",
            zorder=4,
        )
    )
    for t in (0.10, 0.35, 0.60, 0.85):
        xx = 0.770 + t * 0.130
        ax.plot([xx, xx], [0.072, 0.150], color=NAVY, lw=0.7, ls="--", zorder=5)
        ax.add_patch(Circle((xx, 0.150), 0.0055, fc="#FDE68A", ec=NAVY, lw=0.6, zorder=6))
    txt(ax, 0.835, 0.040, "Saint-Venant 1D", ha="center", va="bottom", fontsize=6.2, color=NAVY, fontweight="bold")

    arrow(ax, 0.902, 0.112, 0.928, 0.112, BLUE, lw=1.5, ms=10)
    rbox(ax, 0.930, 0.078, 0.038, 0.070, "#DBEAFE", "#60A5FA", lw=0.9, r=0.006, z=3)
    txt(ax, 0.949, 0.128, "Q, H", ha="center", va="center", fontsize=6.6, fontweight="bold", color=BLUE)
    txt(ax, 0.949, 0.102, "cửa ra", ha="center", va="center", fontsize=5.5, color=SLATE)

    fig.savefig(OUT, dpi=180, bbox_inches="tight", facecolor=WHITE, pad_inches=0.10)
    plt.close(fig)
    print(OUT)


if __name__ == "__main__":
    main()

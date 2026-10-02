"""Vẽ sơ đồ và nguyên lý tính toán ngập lụt 2D (PNG)."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle

OUT = Path(__file__).resolve().parent / "so_do_nguyen_ly_tinh_toan_ngap_lut.png"

NAVY = "#1B365D"
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
TEAL = "#0E7490"
CYAN = "#0E7490"
CYAN_BG = "#ECFEFF"
CYAN_BD = "#67E8F9"
ROSE = "#9F1239"
ROSE_BG = "#FFE4E6"
ROSE_BD = "#FECDD3"
INDIGO = "#3730A3"
INDIGO_BG = "#EEF2FF"
INDIGO_BD = "#A5B4FC"
AMBER = "#92400E"
AMBER_BG = "#FFFBEB"
AMBER_BD = "#FCD34D"
SLATE = "#334155"
MUTED = "#64748B"
WHITE = "#FFFFFF"
WATER = "#3B82F6"
BED = "#A8A29E"


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


def draw_x(ax, x, y, s=0.008, color=ROSE, lw=1.4, z=9):
    ax.plot([x - s, x + s], [y - s, y + s], color=color, lw=lw, zorder=z, solid_capstyle="round")
    ax.plot([x - s, x + s], [y + s, y - s], color=color, lw=lw, zorder=z, solid_capstyle="round")


def bullet(ax, x, y, s, fs=6.5, color=SLATE):
    txt(ax, x, y, "•  " + s, ha="left", va="top", fontsize=fs, color=color)


def draw_pipeline(ax):
    rbox(ax, 0.018, 0.818, 0.964, 0.092, WHITE, BLUE_BD, lw=1.2, r=0.01)
    txt(ax, 0.032, 0.896, "CHUỖI TÍNH TOÁN", ha="left", va="top", fontsize=7.6, fontweight="bold", color=NAVY)

    steps = [
        (0.040, GREEN_BG, GREEN_BD, GREEN, "1. TANK", "Q(t) mưa–dòng chảy"),
        (0.198, BLUE_BG, BLUE_BD, BLUE, "2. Saint-Venant 1D", "H(s, t) dọc sông / nhánh"),
        (0.400, ORANGE_BG, ORANGE_BD, ORANGE, "3. Gán WSE lên DEM", "ô gần nhất trong buffer"),
        (0.590, CYAN_BG, CYAN_BD, CYAN, "4. Độ sâu + liên thông", r"$d=$ WSE $- z$,  BFS"),
        (0.780, INDIGO_BG, INDIGO_BD, INDIGO, "5. Bản đồ ngập", "A, V, max/TB, GeoTIFF"),
    ]
    for x, bg, bd, fg, title, sub in steps:
        rbox(ax, x, 0.832, 0.148, 0.058, bg, bd, lw=1.0, r=0.008, z=3)
        txt(ax, x + 0.074, 0.872, title, ha="center", va="center", fontsize=7.0, fontweight="bold", color=fg)
        txt(ax, x + 0.074, 0.848, sub, ha="center", va="center", fontsize=5.9, color=SLATE)
    for x in (0.188, 0.346, 0.538, 0.738):
        arrow(ax, x, 0.861, x + 0.010, 0.861, MUTED, lw=1.4, ms=10)


def draw_cross_section(ax):
    """Mat cat ngang: WSE ngang, ngap noi WSE > z va lien thong voi long."""
    rbox(ax, 0.268, 0.430, 0.464, 0.372, BLUE_BG, BLUE_BD, lw=1.25, r=0.012)
    txt(
        ax,
        0.500,
        0.786,
        "NGUYÊN LÝ MẶT CẮT NGANG  ·  WSE nằm ngang tại mỗi trạm",
        ha="center",
        va="center",
        fontsize=7.6,
        fontweight="bold",
        color=NAVY,
    )

    x0, x1 = 0.292, 0.708
    # terrain polyline in axes coords
    # x frac along channel CS, y elevation in axes
    # Left floodplain, left berm, channel, right berm, right floodplain, ridge, isolated pit
    xs_t = [0.00, 0.10, 0.22, 0.32, 0.38, 0.50, 0.62, 0.68, 0.78, 0.86, 0.90, 0.94, 1.00]
    zs_t = [0.58, 0.55, 0.52, 0.50, 0.38, 0.34, 0.38, 0.50, 0.53, 0.62, 0.48, 0.47, 0.56]
    y_base = 0.455
    y_span = 0.28
    wse_frac = 0.545  # WSE elevation as fraction of terrain y mapping

    def X(u):
        return x0 + u * (x1 - x0)

    def Y(zf):
        return y_base + zf * y_span

    wse_y = Y(wse_frac)

    terrain = [(X(u), Y(z)) for u, z in zip(xs_t, zs_t)]
    ground = [(X(0), y_base), *terrain, (X(1), y_base)]
    ax.add_patch(Polygon(ground, closed=True, fc="#D6D3D1", ec="none", zorder=3))
    ax.plot([p[0] for p in terrain], [p[1] for p in terrain], color="#57534E", lw=1.6, zorder=5)

    # Flood water: where WSE > terrain, except isolated pit (u > 0.86) unless we clip
    # Build wet polygon from left to ridge (~0.86)
    wet_pts = [(X(0.0), wse_y)]
    for u, z in zip(xs_t, zs_t):
        if u > 0.855:
            break
        yu = Y(z)
        wet_pts.append((X(u), min(yu, wse_y) if yu > wse_y else yu))
    # find last point before ridge that is below WSE
    wet_pts.append((X(0.78), wse_y))
    ax.add_patch(Polygon(wet_pts, closed=True, fc=WATER, alpha=0.78, ec=BLUE, lw=0.5, zorder=4))

    # Isolated depression - hatched conceptually with gray water + X
    pit = [
        (X(0.88), Y(0.62)),
        (X(0.90), Y(0.48)),
        (X(0.94), Y(0.47)),
        (X(0.99), Y(0.55)),
        (X(0.99), wse_y),
        (X(0.875), wse_y),
    ]
    # only fill below WSE in pit
    ax.add_patch(
        Polygon(
            [(X(0.882), Y(0.545)), (X(0.90), Y(0.48)), (X(0.94), Y(0.47)), (X(0.985), Y(0.545))],
            closed=True,
            fc="#94A3B8",
            alpha=0.45,
            ec="#64748B",
            lw=0.8,
            ls="--",
            zorder=4,
        )
    )
    draw_x(ax, X(0.93), Y(0.505), s=0.008, color=ROSE, lw=1.6)

    # WSE line
    ax.plot([X(0.02), X(0.84)], [wse_y, wse_y], color="#1E3A8A", lw=1.7, ls="--", zorder=6)
    txt(ax, X(0.04), wse_y + 0.012, r"WSE $= H(s,t)$  (nằm ngang)", ha="left", va="bottom", fontsize=6.5, color="#1E3A8A", fontweight="bold")

    # depth arrow in floodplain
    xf = X(0.18)
    yt = Y(0.535)
    arrow(ax, xf, yt + 0.004, xf, wse_y - 0.004, GREEN, lw=1.3, ms=8)
    txt(ax, xf - 0.004, 0.5 * (yt + wse_y), r"$d=$ WSE $-z$", ha="right", va="center", fontsize=6.3, color=GREEN)

    # river thalweg
    txt(ax, X(0.50), Y(0.30) + 0.012, "lòng sông", ha="center", va="bottom", fontsize=6.0, color="#44403C")
    ax.add_patch(Circle((X(0.50), Y(0.34)), 0.006, fc="#FDE68A", ec=NAVY, lw=0.8, zorder=7))
    txt(ax, X(0.50), Y(0.34) - 0.018, "hạt giống\n(seed)", ha="center", va="top", fontsize=5.5, color=AMBER)

    txt(ax, X(0.12), y_base + 0.008, "bãi bồi", ha="center", va="bottom", fontsize=6.0, color=MUTED)
    txt(ax, X(0.93), wse_y + 0.016, "hố cô lập\n(không liên thông)", ha="center", va="bottom", fontsize=5.7, color=ROSE)

    txt(
        ax,
        0.500,
        0.442,
        r"Ngập khi  $d \geq 0.05\,\mathrm{m}$  và ô liên thông 4-hướng với lòng sông  ·  buffer mặc định 1500 m",
        ha="center",
        va="center",
        fontsize=6.3,
        color=SLATE,
    )


def draw_plan(ax):
    rbox(ax, 0.746, 0.430, 0.236, 0.372, GREEN_BG, GREEN_BD, lw=1.2, r=0.011)
    txt(ax, 0.864, 0.786, "MẶT BẰNG  (lưới DEM)", ha="center", va="center", fontsize=7.4, fontweight="bold", color=GREEN)

    # grid
    gx0, gy0, gw, gh = 0.768, 0.500, 0.192, 0.250
    nr, nc = 7, 8
    cw, ch = gw / nc, gh / nr
    # river column-ish diagonal
    river_cells = {(3, 0), (3, 1), (3, 2), (4, 3), (4, 4), (4, 5), (4, 6), (3, 7)}
    wet_cells = river_cells | {
        (2, 0),
        (2, 1),
        (2, 2),
        (1, 1),
        (1, 2),
        (3, 3),
        (5, 3),
        (5, 4),
        (5, 5),
        (6, 4),
        (2, 4),
        (2, 5),
        (3, 4),
        (3, 5),
    }
    isolated = {(0, 6), (0, 7), (1, 7)}
    # dry high
    for r in range(nr):
        for c in range(nc):
            x = gx0 + c * cw
            y = gy0 + (nr - 1 - r) * ch
            key = (r, c)
            if key in river_cells:
                fc, ec = "#1D4ED8", "#1E3A8A"
            elif key in wet_cells:
                fc, ec = "#93C5FD", "#3B82F6"
            elif key in isolated:
                fc, ec = "#E2E8F0", "#94A3B8"
            else:
                fc, ec = "#FEF3C7", "#D6D3D1"
            ax.add_patch(Rectangle((x, y), cw * 0.94, ch * 0.90, fc=fc, ec=ec, lw=0.45, zorder=4))
    for r, c in isolated:
        x = gx0 + c * cw + cw * 0.47
        y = gy0 + (nr - 1 - r) * ch + ch * 0.42
        draw_x(ax, x, y, s=0.005, color=ROSE, lw=1.15)

    # buffer hint
    txt(ax, 0.864, 0.488, "buffer 1500 m quanh sông", ha="center", va="center", fontsize=5.8, color=GREEN, fontweight="bold")
    txt(ax, 0.768, 0.458, "■ lòng    ■ ngập liên thông    ■ khô    × cô lập", ha="left", va="center", fontsize=5.5, color=SLATE)


def draw_equations(ax):
    rbox(ax, 0.018, 0.175, 0.236, 0.240, WHITE, "#CBD5E1", lw=1.15, r=0.011)
    rbox(ax, 0.018, 0.375, 0.236, 0.040, "#DBEAFE", "#60A5FA", lw=0.8, r=0.008, z=3)
    txt(ax, 0.136, 0.395, "PHƯƠNG TRÌNH NGẬP", ha="center", va="center", fontsize=7.4, fontweight="bold", color=NAVY)

    txt(ax, 0.030, 0.358, "1. Gán mặt nước từ sông 1D", ha="left", va="top", fontsize=6.4, fontweight="bold", color=TEAL)
    txt(ax, 0.136, 0.325, r"$\mathrm{WSE}(x,y)=H(s^{\ast},t)$", ha="center", va="center", fontsize=8.6, color=NAVY)
    txt(ax, 0.136, 0.305, r"$s^{\ast}$: điểm sông gần nhất,  $d_{\perp}\leq R$", ha="center", va="center", fontsize=6.0, color=MUTED)

    txt(ax, 0.030, 0.284, "2. Độ sâu ô DEM", ha="left", va="top", fontsize=6.4, fontweight="bold", color=TEAL)
    txt(ax, 0.136, 0.252, r"$d=\max(0,\,\mathrm{WSE}-z_{\mathrm{DEM}})$", ha="center", va="center", fontsize=8.4, color=NAVY)

    txt(ax, 0.030, 0.230, "3. Diện tích / thể tích", ha="left", va="top", fontsize=6.4, fontweight="bold", color=TEAL)
    txt(ax, 0.136, 0.202, r"$A=N_{\mathrm{wet}}\Delta x\Delta y$", ha="center", va="center", fontsize=7.8, color=NAVY)
    txt(ax, 0.136, 0.184, r"$V=\sum d\cdot\Delta x\Delta y$", ha="center", va="center", fontsize=7.8, color=NAVY)


def draw_steps(ax):
    rbox(ax, 0.268, 0.175, 0.464, 0.240, WHITE, "#CBD5E1", lw=1.15, r=0.011)
    rbox(ax, 0.268, 0.375, 0.464, 0.040, "#DBEAFE", "#60A5FA", lw=0.8, r=0.008, z=3)
    txt(ax, 0.500, 0.395, "SÁU BƯỚC TÍNH TRÊN LƯỚI DEM", ha="center", va="center", fontsize=7.5, fontweight="bold", color=NAVY)

    steps = [
        ("1", "Đọc H(s,t) 1D", "CSV Saint-Venant + hình học lon/lat\nlòng chính và nhánh"),
        ("2", "Làm dày tuyến sông", "Nội suy điểm ~40 m; gán H theo lý trình"),
        ("3", "Ô gần nhất + buffer", "Mỗi ô DEM trong R = 1500 m nhận\nWSE của điểm sông gần nhất"),
        ("4", "Tính độ sâu", r"$d =$ WSE $- z$;  ướt nếu $d\geq 0.05$ m"),
        ("5", "Lọc liên thông", "Chỉ giữ cụm 4-hướng dính hạt giống lòng sông\n(BFS / scipy.ndimage.label)"),
        ("6", "Thống kê & xuất", "A, V, d_max, d_TB  ·  flood_depth.tif\nflood_map.png  ·  video theo giờ"),
    ]
    for i, (n, title, body) in enumerate(steps):
        col, row = i % 3, i // 3
        x = 0.280 + col * 0.150
        y = 0.278 - row * 0.092
        ax.add_patch(Circle((x + 0.012, y + 0.072), 0.010, fc=BLUE, ec="none", zorder=5))
        txt(ax, x + 0.012, y + 0.072, n, ha="center", va="center", fontsize=6.4, fontweight="bold", color=WHITE)
        txt(ax, x + 0.026, y + 0.072, title, ha="left", va="center", fontsize=6.7, fontweight="bold", color=NAVY)
        txt(ax, x + 0.026, y + 0.054, body, ha="left", va="top", fontsize=5.7, color=SLATE, linespacing=1.25)


def draw_outputs(ax):
    rbox(ax, 0.746, 0.175, 0.236, 0.240, INDIGO_BG, INDIGO_BD, lw=1.2, r=0.011)
    txt(ax, 0.758, 0.398, "PHÂN LOẠI ĐỘ SÂU  &  ĐẦU RA", ha="left", va="top", fontsize=6.8, fontweight="bold", color=INDIGO)

    classes = [
        ("#c6dbef", "0.05 – 0.5 m", "ngập nông"),
        ("#6baed6", "0.5 – 1.0 m", "ngập vừa"),
        ("#2171b5", "1.0 – 2.0 m", "ngập sâu"),
        ("#084594", "2.0 – 4.0 m", "ngập rất sâu"),
        ("#041c3a", "> 4.0 m", "ngập cực sâu"),
    ]
    yy = 0.362
    for col, lab, note in classes:
        ax.add_patch(Rectangle((0.762, yy - 0.008), 0.022, 0.016, fc=col, ec="#64748B", lw=0.4, zorder=5))
        txt(ax, 0.790, yy, f"{lab}   {note}", ha="left", va="center", fontsize=6.0, color=SLATE)
        yy -= 0.022

    txt(ax, 0.758, 0.248, "Kết quả (flood_output/)", ha="left", va="top", fontsize=6.4, fontweight="bold", color=INDIGO)
    for i, s in enumerate(
        [
            "flood_depth.tif  ·  flood_map.png",
            "flood_summary.csv  (A, V, d_max, d_TB)",
            "video mp4/gif khi --video",
            "Mặc định giờ đỉnh H trung bình",
        ]
    ):
        bullet(ax, 0.758, 0.228 - i * 0.0165, s, fs=5.9)


def draw_inputs_left(ax):
    rbox(ax, 0.018, 0.430, 0.236, 0.372, WHITE, "#CBD5E1", lw=1.15, r=0.011)
    rbox(ax, 0.018, 0.762, 0.236, 0.040, "#DBEAFE", "#60A5FA", lw=0.8, r=0.008, z=3)
    txt(ax, 0.136, 0.782, "DỮ LIỆU CHO MÔ HÌNH NGẬP", ha="center", va="center", fontsize=7.2, fontweight="bold", color=NAVY)

    items = [
        (BLUE, "H(s,t) Saint-Venant", "saint_venant_result.csv\n+ demo_river_geometry.csv"),
        (ORANGE, "Sông nhánh (nếu có)", "tributary_result + geometry"),
        (GREEN, "DEM địa hình", "dem-song-hong.tif   (--dem)"),
        (PURPLE, "Buffer / lưới", "--buffer 1500 m  ·  --max-dim 720"),
        (CYAN, "Thời điểm", "--hour / --time-index\n(mặc định: giờ H TB lớn nhất)"),
    ]
    yy = 0.748
    for c, title, body in items:
        ax.add_patch(Circle((0.038, yy - 0.006), 0.007, fc=c, ec="none", zorder=5))
        txt(ax, 0.052, yy, title, ha="left", va="top", fontsize=6.7, fontweight="bold", color=c)
        txt(ax, 0.052, yy - 0.016, body, ha="left", va="top", fontsize=5.9, color=MUTED, linespacing=1.2)
        yy -= 0.062


def draw_bottom(ax):
    rbox(ax, 0.018, 0.018, 0.618, 0.142, WHITE, "#CBD5E1", lw=1.15, r=0.011)
    txt(ax, 0.032, 0.142, "ĐIỂM MẤU CHỐT", ha="left", va="top", fontsize=7.6, fontweight="bold", color=NAVY)
    notes = [
        "Đây là ngập tĩnh 2D từ mặt nước 1D (không giải Saint-Venant 2D trên bãi bồi).",
        "WSE tại mỗi ô = H của điểm sông gần nhất — giả thiết mặt nước nằm ngang vuông góc trục sông.",
        "Lọc liên thông loại hố trũng / ô thấp không nối được với lòng (tránh ngập giả).",
        "Buffer R giới hạn phạm vi lan; tăng R nếu bãi bồi rộng, giảm R nếu DEM nhiễu.",
    ]
    for i, s in enumerate(notes):
        bullet(ax, 0.032, 0.118 - i * 0.022, s, fs=6.35)

    rbox(ax, 0.650, 0.018, 0.332, 0.142, "#EEF2FF", "#A5B4FC", lw=1.15, r=0.011)
    txt(ax, 0.816, 0.142, "ỨNG DỤNG", ha="center", va="top", fontsize=7.6, fontweight="bold", color=INDIGO)
    apps = [
        "Bản đồ ngập theo giờ / giờ đỉnh lũ",
        "Cảnh báo diện tích – thể tích – độ sâu",
        "Phủ lớp ngập lên DEM 3D (web)",
        "Đánh giá ảnh hưởng hạ du, phân lưu",
    ]
    yy = 0.112
    for a in apps:
        ax.add_patch(Circle((0.670, yy), 0.0055, fc="#4F46E5", ec="none", zorder=5))
        txt(ax, 0.684, yy, a, ha="left", va="center", fontsize=6.5, color="#312E81")
        yy -= 0.022


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
        "SƠ ĐỒ VÀ NGUYÊN LÝ TÍNH TOÁN NGẬP LỤT  (1D → 2D)",
        ha="center",
        va="center",
        fontsize=16.0,
        fontweight="bold",
        color=NAVY,
    )
    txt(
        ax,
        0.50,
        0.942,
        "Lan truyền mặt nước Saint-Venant lên DEM  ·  độ sâu d = max(0, WSE − z)  ·  chỉ ô liên thông với lòng sông",
        ha="center",
        va="center",
        fontsize=8.2,
        color=MUTED,
        style="italic",
    )

    rbox(ax, 0.055, 0.900, 0.890, 0.028, AMBER_BG, AMBER_BD, lw=0.9, r=0.008)
    txt(
        ax,
        0.50,
        0.914,
        "Mục tiêu: từ mực nước 1D H(s,t) và địa hình z(x,y) suy ra bản đồ độ sâu, diện tích và thể tích ngập tại mỗi thời điểm.",
        ha="center",
        va="center",
        fontsize=6.8,
        color=AMBER,
    )

    draw_pipeline(ax)
    draw_inputs_left(ax)
    draw_cross_section(ax)
    draw_plan(ax)
    draw_equations(ax)
    draw_steps(ax)
    draw_outputs(ax)
    draw_bottom(ax)

    fig.savefig(OUT, dpi=180, bbox_inches="tight", facecolor=WHITE, pad_inches=0.10)
    plt.close(fig)
    print(OUT)


if __name__ == "__main__":
    main()

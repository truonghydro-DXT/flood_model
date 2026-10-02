"""Vẽ sơ đồ thủy lực 1D Saint-Venant (PNG) — cùng kiểu infographic tank model."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Polygon

OUT = Path(__file__).resolve().parent / "so_do_thuy_luc_1d_saint_venant.png"

NAVY = "#1B365D"
TEAL = "#0E7490"
BLUE = "#2563EB"
BLUE_DK = "#1D4ED8"
WATER = "#3B82F6"
BED_DK = "#57534E"
GREEN = "#166534"
GREEN_BG = "#DCFCE7"
GREEN_BD = "#86EFAC"
AMBER = "#92400E"
AMBER_BG = "#FEF3C7"
AMBER_BD = "#FCD34D"
ROSE = "#9F1239"
ROSE_BG = "#FFE4E6"
ROSE_BD = "#FECDD3"
CYAN = "#155E75"
CYAN_BG = "#CFFAFE"
CYAN_BD = "#67E8F9"
SLATE = "#334155"
MUTED = "#64748B"
BOX_BD = "#CBD5E1"
WHITE = "#FFFFFF"


def _pick_font() -> str:
    names = {f.name for f in fm.fontManager.ttflist}
    for cand in ("Segoe UI", "Calibri", "Tahoma", "Arial", "DejaVu Sans"):
        if cand in names:
            return cand
    return "DejaVu Sans"


def rbox(ax, x, y, w, h, fc, ec, lw=1.2, r=0.012, z=2):
    p = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle=f"round,pad=0.006,rounding_size={r}",
        facecolor=fc,
        edgecolor=ec,
        linewidth=lw,
        zorder=z,
        mutation_aspect=0.6,
    )
    ax.add_patch(p)
    return p


def arrow(ax, x1, y1, x2, y2, color, lw=1.8, ms=12, z=6):
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


def draw_channel(ax):
    rbox(ax, 0.268, 0.28, 0.464, 0.58, "#F0F9FF", "#93C5FD", lw=1.4, r=0.014, z=1)

    x0, x1 = 0.30, 0.71
    y_bed_up, y_bed_dn = 0.445, 0.398
    y_ws_up, y_ws_dn = 0.558, 0.528

    ax.add_patch(
        Polygon(
            [
                (x0, y_bed_up - 0.012),
                (x1, y_bed_dn - 0.012),
                (x1, y_bed_dn + 0.018),
                (x0, y_bed_up + 0.018),
            ],
            closed=True,
            facecolor="#A8A29E",
            edgecolor=BED_DK,
            linewidth=0.8,
            zorder=3,
        )
    )
    ax.add_patch(
        Polygon(
            [
                (x0, y_bed_up + 0.016),
                (x1, y_bed_dn + 0.016),
                (x1, y_ws_dn),
                (x0, y_ws_up),
            ],
            closed=True,
            facecolor=WATER,
            edgecolor=BLUE_DK,
            linewidth=0.6,
            alpha=0.88,
            zorder=4,
        )
    )

    xs = [x0 + i * (x1 - x0) / 24 for i in range(25)]
    ys = []
    for i, x in enumerate(xs):
        t = (x - x0) / (x1 - x0)
        base = y_ws_up + t * (y_ws_dn - y_ws_up)
        ys.append(base + 0.002 * (1 if i % 2 == 0 else -1))
    ax.plot(xs, ys, color="#DBEAFE", lw=1.6, zorder=5, solid_capstyle="round")

    stations = [0.0, 0.22, 0.44, 0.66, 1.0]
    labels = ["XS1", "XS2", "XSj", "XS4", "XSn"]
    h_labs = [r"$H_1,A_1$", r"$H_2,A_2$", r"$H_j,A_j$", r"$H_4,A_4$", r"$H_n,A_n$"]
    nodes = []
    for s, lab, hl in zip(stations, labels, h_labs):
        x = x0 + s * (x1 - x0)
        t = s
        yb = y_bed_up + t * (y_bed_dn - y_bed_up)
        yw = y_ws_up + t * (y_ws_dn - y_ws_up)
        ax.plot([x, x], [yb + 0.01, yw + 0.012], color=NAVY, lw=1.15, ls="--", zorder=5)
        ax.add_patch(
            Circle((x, yw + 0.018), 0.0072, facecolor="#FDE68A", edgecolor=NAVY, lw=1.1, zorder=7)
        )
        txt(ax, x, yw + 0.048, lab, ha="center", va="bottom", fontsize=7.2, fontweight="bold", color=NAVY)
        txt(ax, x, yw + 0.033, hl, ha="center", va="bottom", fontsize=6.2, color=TEAL)
        nodes.append((x, yw, yb))

    q_labs = [r"$Q_{1/2}$", r"$Q_{3/2}$", r"$Q_{j}$", r"$Q_{n-1/2}$"]
    for i in range(len(nodes) - 1):
        xa, ya, _ = nodes[i]
        xb, yb2, _ = nodes[i + 1]
        xm = 0.5 * (xa + xb)
        ym = 0.5 * (ya + yb2) - 0.028
        arrow(ax, xa + 0.018, ym, xb - 0.018, ym, "#F8FAFC", lw=1.9, ms=11, z=6)
        txt(
            ax,
            xm,
            ym - 0.016,
            q_labs[i],
            ha="center",
            va="top",
            fontsize=7.2,
            fontweight="bold",
            color="#F8FAFC",
        )

    xh, yw0, yb0 = nodes[0]
    arrow(ax, xh - 0.018, yb0 + 0.022, xh - 0.018, yw0 - 0.004, GREEN, lw=1.2, ms=8)
    txt(ax, xh - 0.028, 0.5 * (yb0 + yw0) + 0.02, r"$y=H-z$", ha="right", va="center", fontsize=6.4, color=GREEN)
    txt(ax, 0.505, y_bed_dn - 0.028, r"đáy sông $z(x)$  (DEM)", ha="center", va="top", fontsize=7.0, color=BED_DK)

    rbox(ax, 0.278, 0.695, 0.118, 0.078, GREEN_BG, GREEN_BD, lw=1.3, r=0.01, z=5)
    txt(ax, 0.337, 0.752, "BIÊN THƯỢNG LƯU", ha="center", va="top", fontsize=6.3, fontweight="bold", color=GREEN)
    txt(ax, 0.337, 0.728, r"$Q_{\mathrm{vao}}(t)$", ha="center", va="top", fontsize=8.5, fontweight="bold", color=GREEN)
    txt(ax, 0.337, 0.705, "từ Tank Model", ha="center", va="top", fontsize=6.2, color=SLATE)
    arrow(ax, 0.337, 0.693, nodes[0][0], nodes[0][1] + 0.055, GREEN, lw=1.6, ms=10)

    rbox(ax, 0.612, 0.695, 0.112, 0.078, ROSE_BG, ROSE_BD, lw=1.3, r=0.01, z=5)
    txt(ax, 0.668, 0.752, "BIÊN HẠ LƯU", ha="center", va="top", fontsize=6.4, fontweight="bold", color=ROSE)
    txt(ax, 0.668, 0.728, r"$H_{\mathrm{ds}}(t)$", ha="center", va="top", fontsize=8.5, fontweight="bold", color=ROSE)
    txt(ax, 0.668, 0.705, "cốt nước cửa ra", ha="center", va="top", fontsize=6.2, color=SLATE)
    arrow(ax, 0.668, 0.693, nodes[-1][0], nodes[-1][1] + 0.055, ROSE, lw=1.6, ms=10)

    rbox(ax, 0.455, 0.705, 0.125, 0.068, AMBER_BG, AMBER_BD, lw=1.3, r=0.01, z=5)
    txt(ax, 0.517, 0.755, "NHÁNH NHẬP LƯU", ha="center", va="top", fontsize=6.3, fontweight="bold", color=AMBER)
    txt(ax, 0.517, 0.735, r"$Q_{\mathrm{lat}}$  (biên Q)", ha="center", va="top", fontsize=7.0, color=AMBER)
    txt(ax, 0.517, 0.714, r"$H_{\mathrm{nhanh}}=H_j$", ha="center", va="top", fontsize=6.4, color=SLATE)
    jx, jy, _ = nodes[2]
    ax.plot([0.517, 0.517, jx], [0.703, 0.635, jy + 0.05], color="#D97706", lw=1.8, zorder=5)
    ax.add_patch(
        FancyArrowPatch(
            (0.517, 0.635),
            (jx, jy + 0.05),
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=1.8,
            color="#D97706",
            zorder=6,
        )
    )

    ax.add_patch(Circle((jx, jy + 0.018), 0.011, facecolor="#F97316", edgecolor="#9A3412", lw=1.3, zorder=8))
    txt(ax, jx + 0.012, jy - 0.055, "NÚT GIAO", ha="left", va="top", fontsize=6.4, fontweight="bold", color="#9A3412")

    rbox(ax, 0.278, 0.293, 0.155, 0.072, CYAN_BG, CYAN_BD, lw=1.3, r=0.01, z=5)
    txt(ax, 0.355, 0.350, "NHÁNH THOÁT (weir)", ha="center", va="top", fontsize=6.3, fontweight="bold", color=CYAN)
    txt(ax, 0.355, 0.332, r"$Q_{\mathrm{weir}}$ từ $H_j$ lòng chính", ha="center", va="top", fontsize=6.4, color=CYAN)
    txt(ax, 0.355, 0.314, r"biên xa: $H=h_{\mathrm{na1}}$", ha="center", va="top", fontsize=6.3, color=SLATE)
    arrow(ax, jx - 0.01, jy - 0.02, 0.40, 0.368, CYAN, lw=1.6, ms=10)

    rbox(ax, 0.575, 0.293, 0.145, 0.055, "#DBEAFE", "#93C5FD", lw=1.3, r=0.01, z=5)
    txt(ax, 0.647, 0.332, r"$Q_{\mathrm{ra}}(t)=Q_{n-1/2}$", ha="center", va="top", fontsize=7.4, fontweight="bold", color=NAVY)
    txt(ax, 0.647, 0.310, "lưu lượng tại cửa ra lòng chính", ha="center", va="top", fontsize=6.1, color=SLATE)

    txt(
        ax,
        0.500,
        0.842,
        "LÒNG SÔNG 1D  ·  lưới lệch (staggered grid)",
        ha="center",
        va="center",
        fontsize=8.2,
        fontweight="bold",
        color=NAVY,
    )


def draw_staggered_inset(ax):
    rbox(ax, 0.268, 0.175, 0.464, 0.092, WHITE, "#93C5FD", lw=1.1, r=0.01, z=2)
    txt(
        ax,
        0.500,
        0.252,
        "LƯỚI LỆCH:  H, A tại mặt cắt (nút)   ·   Q tại đoạn (giữa hai XS)",
        ha="center",
        va="center",
        fontsize=6.8,
        fontweight="bold",
        color=NAVY,
    )
    xs = [0.32, 0.40, 0.48, 0.56, 0.64, 0.70]
    y = 0.210
    for i, x in enumerate(xs):
        ax.add_patch(Circle((x, y), 0.007, facecolor="#FDE68A", edgecolor=NAVY, lw=1.0, zorder=6))
        txt(ax, x, y + 0.016, rf"$H_{i+1}$", ha="center", fontsize=6.4, color=NAVY)
        if i < len(xs) - 1:
            arrow(ax, x + 0.012, y, xs[i + 1] - 0.012, y, BLUE, lw=1.4, ms=9)
            txt(
                ax,
                0.5 * (x + xs[i + 1]),
                y - 0.016,
                rf"$Q_{{{i+1}/{i+2}}}$",
                ha="center",
                fontsize=6.2,
                color=BLUE_DK,
            )


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

    fig, ax = plt.subplots(figsize=(16.2, 10.4), dpi=180)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.patch.set_facecolor(WHITE)
    ax.set_facecolor(WHITE)

    txt(
        ax,
        0.50,
        0.965,
        "SƠ ĐỒ MÔ HÌNH THỦY LỰC 1D SAINT-VENANT  (KÊNH MỞ)",
        ha="center",
        va="center",
        fontsize=16.5,
        fontweight="bold",
        color=NAVY,
    )
    txt(
        ax,
        0.50,
        0.932,
        "Điện toán động lực dọc lòng sông  ·  liên tục + động lượng (local-inertial / Manning)",
        ha="center",
        va="center",
        fontsize=9.0,
        color=MUTED,
        style="italic",
    )
    ax.plot([0.08, 0.92], [0.912, 0.912], color="#93C5FD", lw=1.5)

    rbox(ax, 0.018, 0.68, 0.232, 0.215, WHITE, BOX_BD, lw=1.15, r=0.012, z=2)
    rbox(ax, 0.018, 0.855, 0.232, 0.040, "#DBEAFE", "#60A5FA", lw=0.8, r=0.01, z=3)
    txt(ax, 0.134, 0.875, "ĐẦU VÀO", ha="center", va="center", fontsize=8.5, fontweight="bold", color=NAVY)

    items_in = [
        (GREEN, r"$Q(t)$", "lưu lượng thượng lưu  (tank_result.csv)"),
        (ROSE, r"$H_{\mathrm{ds}}(t)$", "cốt nước hạ lưu  (demo_downstream_stage.csv)"),
        (AMBER, r"$n(x)$", "Hệ số Manning theo mặt cắt  (--n / --n-csv)"),
        (CYAN, "DEM / XS", "mặt cắt ngang, dx ≈ 1500 m  (--xs-spacing)"),
        (NAVY, r"$Q_0,\,H_0$", "điều kiện ban đầu  (--q0 --h0 / --ic-csv)"),
    ]
    yy = 0.838
    for c, a, b in items_in:
        ax.add_patch(Circle((0.040, yy), 0.007, facecolor=c, edgecolor="none", zorder=4))
        txt(ax, 0.055, yy + 0.008, a, ha="left", va="center", fontsize=7.6, fontweight="bold", color=c)
        txt(ax, 0.055, yy - 0.012, b, ha="left", va="center", fontsize=6.15, color=MUTED)
        yy -= 0.038

    rbox(ax, 0.018, 0.355, 0.232, 0.310, WHITE, BOX_BD, lw=1.15, r=0.012, z=2)
    rbox(ax, 0.018, 0.625, 0.232, 0.040, "#DBEAFE", "#60A5FA", lw=0.8, r=0.01, z=3)
    txt(ax, 0.134, 0.645, "PHƯƠNG TRÌNH KÊNH MỞ 1D", ha="center", va="center", fontsize=7.5, fontweight="bold", color=NAVY)

    txt(ax, 0.030, 0.608, "Liên tục (bảo toàn khối):", ha="left", va="top", fontsize=6.8, fontweight="bold", color=TEAL)
    txt(
        ax,
        0.134,
        0.575,
        r"$\dfrac{\partial A}{\partial t}+\dfrac{\partial Q}{\partial x}=q_{\mathrm{lat}}$",
        ha="center",
        va="center",
        fontsize=10.5,
        color=NAVY,
    )

    txt(ax, 0.030, 0.548, "Động lượng (local-inertial):", ha="left", va="top", fontsize=6.8, fontweight="bold", color=TEAL)
    txt(
        ax,
        0.134,
        0.508,
        r"$\dfrac{\partial Q}{\partial t}+gA\dfrac{\partial H}{\partial x}+gA\,S_f=0$",
        ha="center",
        va="center",
        fontsize=9.4,
        color=NAVY,
    )

    txt(ax, 0.030, 0.478, "Ma sát Manning:", ha="left", va="top", fontsize=6.8, fontweight="bold", color=TEAL)
    txt(
        ax,
        0.134,
        0.448,
        r"$S_f=\dfrac{n^{2}\,Q\,|Q|}{A^{2}R^{4/3}}$",
        ha="center",
        va="center",
        fontsize=9.8,
        color=NAVY,
    )

    txt(ax, 0.030, 0.418, "Q đoạn (cân bằng):", ha="left", va="top", fontsize=6.8, fontweight="bold", color=TEAL)
    txt(
        ax,
        0.134,
        0.385,
        r"$Q=\dfrac{1}{n}AR^{2/3}\sqrt{S_f}$",
        ha="center",
        va="center",
        fontsize=9.6,
        color=NAVY,
    )
    txt(
        ax,
        0.134,
        0.365,
        r"$R=A/P$   ·   $S_f \approx -\Delta H/\Delta x$",
        ha="center",
        va="center",
        fontsize=6.3,
        color=MUTED,
    )

    rbox(ax, 0.018, 0.175, 0.232, 0.165, WHITE, BOX_BD, lw=1.15, r=0.012, z=2)
    rbox(ax, 0.018, 0.300, 0.232, 0.040, "#E2E8F0", "#94A3B8", lw=0.8, r=0.01, z=3)
    txt(ax, 0.134, 0.320, "CHÚ THÍCH", ha="center", va="center", fontsize=8.2, fontweight="bold", color=NAVY)

    legs = [
        ("#FDE68A", NAVY, "Nút mặt cắt: H, A"),
        (BLUE, BLUE, "Đoạn: lưu lượng Q"),
        ("#F97316", "#9A3412", "Nút giao / nhập–thoát"),
        (GREEN, GREEN, "Biên Q thượng lưu"),
        (ROSE, ROSE, "Biên H hạ lưu"),
    ]
    yy = 0.285
    for fc, ec, lab in legs:
        ax.add_patch(Circle((0.040, yy), 0.007, facecolor=fc, edgecolor=ec, lw=0.9, zorder=4))
        txt(ax, 0.055, yy, lab, ha="left", va="center", fontsize=6.6, color=SLATE)
        yy -= 0.022

    draw_channel(ax)
    draw_staggered_inset(ax)

    cards = [
        (
            0.750,
            0.705,
            GREEN_BG,
            GREEN_BD,
            GREEN,
            "LÒNG CHÍNH",
            "• Kênh mở 1D theo DEM\n• Liên tục tại nút, Q tại đoạn\n• Bước thời gian theo CFL\n• Q > 0 về hạ lưu",
        ),
        (
            0.750,
            0.545,
            AMBER_BG,
            AMBER_BD,
            AMBER,
            "NHÁNH NHẬP LƯU",
            "• Biên Q ở đầu xa\n• H_nhánh = H_chính tại nút\n• Q_nhánh cộng vào liên tục\n  lòng chính (q_lat)",
        ),
        (
            0.750,
            0.385,
            ROSE_BG,
            ROSE_BD,
            ROSE,
            "NHÁNH THOÁT NƯỚC",
            "• Ví dụ: Sông Đuống (weir)\n• Q lấy từ H lòng chính\n• Biên đầu xa = H (h_na1)\n• q_lat trừ Q_weir",
        ),
        (
            0.750,
            0.225,
            CYAN_BG,
            CYAN_BD,
            CYAN,
            "MA SÁT / HÌNH HỌC",
            "• Manning n theo XS\n• A(H), P(H), B(H) từ DEM\n• R = A/P,  Sf từ dốc mặt nước\n• dx mặc định 1500 m",
        ),
    ]
    for x, y, bg, bd, fg, title, body in cards:
        rbox(ax, x, y, 0.232, 0.148, bg, bd, lw=1.25, r=0.012, z=2)
        txt(ax, x + 0.012, y + 0.125, title, ha="left", va="top", fontsize=8.0, fontweight="bold", color=fg)
        txt(ax, x + 0.012, y + 0.100, body, ha="left", va="top", fontsize=6.7, color=SLATE, linespacing=1.45)

    rbox(ax, 0.018, 0.022, 0.55, 0.138, WHITE, BOX_BD, lw=1.15, r=0.012, z=2)
    rbox(ax, 0.018, 0.122, 0.55, 0.038, "#DBEAFE", "#60A5FA", lw=0.8, r=0.01, z=3)
    txt(
        ax,
        0.293,
        0.141,
        "RỜI RẠC KHÔNG GIAN  ·  BƯỚC THỜI GIAN",
        ha="center",
        va="center",
        fontsize=7.6,
        fontweight="bold",
        color=NAVY,
    )
    txt(
        ax,
        0.035,
        0.108,
        r"Liên tục tại nút $i$:   $A_i^{n+1}=A_i^n-\Delta t\,(Q_{i+1/2}-Q_{i-1/2}-q_{\mathrm{lat},i})/\Delta x_i$",
        ha="left",
        va="top",
        fontsize=7.2,
        color=NAVY,
    )
    txt(
        ax,
        0.035,
        0.078,
        r"Q đoạn: hỗn hợp local-inertial + Manning theo dốc mặt nước  (unidirectional, $Q\geq Q_{\min}$)",
        ha="left",
        va="top",
        fontsize=6.8,
        color=SLATE,
    )
    txt(
        ax,
        0.035,
        0.050,
        r"$\Delta t$ thủy lực theo CFL ($c=0.45$), xuất kết quả theo $\Delta t$ giờ  ·  IC: backwater từ $H_{\mathrm{ds}}(0)$",
        ha="left",
        va="top",
        fontsize=6.8,
        color=SLATE,
    )

    rbox(ax, 0.582, 0.022, 0.400, 0.138, "#EEF2FF", "#A5B4FC", lw=1.15, r=0.012, z=2)
    rbox(ax, 0.582, 0.122, 0.400, 0.038, "#C7D2FE", "#818CF8", lw=0.8, r=0.01, z=3)
    txt(ax, 0.782, 0.141, "ỨNG DỤNG", ha="center", va="center", fontsize=7.8, fontweight="bold", color="#3730A3")
    apps = [
        "Dự báo lũ / lan truyền lũ 1D",
        "Cảnh báo sớm cốt nước và lưu lượng",
        "Quản lý tài nguyên nước trên mạng sông",
        "Tính toán phân lưu – thoát nước (weir)",
    ]
    yy = 0.108
    for a in apps:
        ax.add_patch(Circle((0.602, yy), 0.0055, facecolor="#4F46E5", edgecolor="none", zorder=4))
        txt(ax, 0.616, yy, a, ha="left", va="center", fontsize=7.0, color="#312E81")
        yy -= 0.022

    fig.savefig(OUT, dpi=180, bbox_inches="tight", facecolor=WHITE, pad_inches=0.12)
    plt.close(fig)
    print(OUT)


if __name__ == "__main__":
    main()

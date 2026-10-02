"""
Giai Saint-Venant 1D theo so do MIKE by DHI (MIKE 11 / MIKE Hydro River HD).

Phuong trinh (kenh ho 1D, dong luc day du):
  Lien tuc :  B dh/dt + dQ/dx = q_lat
  Dong luong: dQ/dt + d(Q^2/A)/dx + g A dh/dx + g A Sf = 0
  Sf = n^2 Q|Q| / (A^2 R^{4/3})   (Manning)

So do Abbott-Ionescu (MIKE 11 HD):
  - Luoi lech: H tai mat cat, Q tai doan
  - Sai phan 6 diem, he so trong so theta (mac dinh 0.7)
  - Ma sat tuyen tinh an (DHI): g A Sf^{n+1} ~ g n^2 |Q^n| Q^{n+1} / (A R^{4/3})
  - He tuyen tinh khoi (h, Q) moi buoc, lap Picard cap nhat A, B, R
  - Bien: Q thuong luu, H ha luu (giong saint-venant.py)

Mang song / mat cat / bien lay dung nhu saint-venant.py:
  long chinh + nhanh, n Manning, Q vao, H ha luu Hong, H nhanh Duong.

Khong goi phan mem MIKE thuong mai — chi tai hien thuat toan HD da cong bo.

Chay:
  .\\venv\\Scripts\\python.exe flood_model/sv-mike-by-dhi.py --no-plot
  .\\venv\\Scripts\\python.exe flood_model/sv-mike-by-dhi.py --theta 1 --dt-hydro 300 --no-plot
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

PACKAGE_DIR = Path(__file__).resolve().parent
PARENT = PACKAGE_DIR.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

G = 9.81
DEFAULT_OUT_DIR = PACKAGE_DIR / "mike_hd_output"


def _load_saint_venant():
    path = PACKAGE_DIR / "saint-venant.py"
    if not path.is_file():
        raise FileNotFoundError(f"Khong thay {path}")
    spec = importlib.util.spec_from_file_location("saint_venant_sv", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Khong nap duoc {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sv = _load_saint_venant()


@dataclass
class MikeHdParams(sv.SaintVenantParams):
    """Tham so HD MIKE: ke thua SaintVenantParams + theta / Picard / doi luu."""

    theta: float = 1.0
    n_picard: int = 3
    convective: float = 0.15
    manning_blend: float = 0.25
    q_relax: float = 0.35


def _theta(par: MikeHdParams) -> float:
    return float(min(1.0, max(0.5, par.theta)))


def _states(geom, h: np.ndarray):
    return sv._states(geom.sections, h)


def _conveyance_terms(a: np.ndarray, p: np.ndarray, n_face: np.ndarray):
    af = np.maximum(0.5 * (a[:-1] + a[1:]), 1e-3)
    pf = np.maximum(0.5 * (p[:-1] + p[1:]), 1e-3)
    rf = np.maximum(af / pf, 1e-4)
    return af, rf, n_face


def _convective_face(qf: np.ndarray, a: np.ndarray, dx: np.ndarray, dt: float) -> np.ndarray:
    """d(Q^2/A)/dx tai doan, chan de khong lam Q nhay qua 20% moi buoc."""
    qn = sv._node_q(qf, float(qf[0]) if qf.size else 0.0)
    uq = (qn * qn) / np.maximum(a, 1e-3)
    conv = (uq[1:] - uq[:-1]) / np.maximum(dx, 1.0)
    lim = 0.20 * np.maximum(np.abs(qf), 1.0) / max(float(dt), 1.0)
    return np.clip(conv, -lim, lim)


def _smooth_h_ws(h: np.ndarray) -> np.ndarray:
    """Lam tron mat nuoc 3 diem, giu H ha luu."""
    s = np.asarray(h, dtype=float).copy()
    if s.size >= 3:
        s[1:-1] = 0.25 * h[:-2] + 0.5 * h[1:-1] + 0.25 * h[2:]
        s[-1] = float(h[-1])
    return s


def _smooth_face_q(q: np.ndarray, passes: int = 2) -> np.ndarray:
    """Loc khong gian Q tren doan; cua ra neo theo doan truoc de het rang cua."""
    out = np.asarray(q, dtype=float).copy()
    if out.size < 3:
        return out
    for _ in range(max(1, passes)):
        nxt = out.copy()
        nxt[1:-1] = 0.25 * out[:-2] + 0.5 * out[1:-1] + 0.25 * out[2:]
        nxt[0] = 0.85 * out[0] + 0.15 * out[1]
        nxt[-1] = 0.30 * out[-1] + 0.70 * out[-2]
        out = nxt
    return out


def _manning_face_q(h: np.ndarray, geom, par: MikeHdParams, n_face: np.ndarray, dx: np.ndarray) -> np.ndarray:
    """Q Manning: doc local + 4 doan cuoi dung doc backwater toi bien H (tranh rang cua)."""
    hs = _smooth_h_ws(h)
    a, p, _b, _y = _states(geom, hs)
    af, rf, nn = _conveyance_terms(a, p, n_face)
    sf = np.maximum(-(hs[1:] - hs[:-1]) / dx, par.min_slope)
    n_tail = min(4, int(sf.size))
    if n_tail:
        dist = np.cumsum(dx[-n_tail:][::-1])[::-1]
        s_bw = (hs[-n_tail - 1 : -1] - hs[-1]) / np.maximum(dist, dx[-n_tail:])
        sf[-n_tail:] = np.maximum(s_bw, par.min_slope)
    return (1.0 / nn) * af * (rf ** (2.0 / 3.0)) * np.sqrt(sf)


def mike_hd_step(
    qf: np.ndarray,
    h: np.ndarray,
    geom,
    par: MikeHdParams,
    q_up: float,
    h_ds: float,
    dt: float,
    q_lat: Optional[np.ndarray] = None,
    q_up_old: Optional[float] = None,
    structures=None,
    hour: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Mot buoc an Abbott-Ionescu.

    An toan: h[-1] = H ha luu; Q_up vao lien tuc nut 0.
    Ma sat an tuyen tinh; ap suat va lien tuc trong so theta.
    Cong trinh MIKE: mat inline thay dong luong bang Q = f(H_us, H_ds).
    """
    from flood_model.construction import (
        apply_reservoir_storage,
        inline_face_discharges,
    )

    sections = geom.sections
    z = np.asarray(geom.z_bed, dtype=float)
    n_x = int(h.size)
    if n_x < 2:
        return qf.copy(), h.copy()

    dx = np.maximum(np.asarray(geom.dx_m, dtype=float), 1.0)
    dxn = sv._node_dx(dx)
    n_face = sv.reach_n(sections, par.manning_n)
    theta = _theta(par)
    dt = max(float(dt), 1e-3)
    q_up = float(q_up)
    q_up_n = float(q_up if q_up_old is None else q_up_old)
    lat = np.zeros(n_x, dtype=float) if q_lat is None else np.asarray(q_lat, dtype=float).copy()
    if lat.size != n_x:
        lat = np.zeros(n_x, dtype=float)

    h_n = np.asarray(h, dtype=float).copy()
    q_n = np.asarray(qf, dtype=float).copy()
    h_est = h_n.copy()
    q_est = q_n.copy()
    n_u = 2 * n_x - 1
    n_picard = max(1, int(par.n_picard))
    face_q_last: dict[int, float] = {}

    def ih(i: int) -> int:
        return i

    def iq(j: int) -> int:
        return n_x + j

    for _it in range(n_picard):
        a, p, b, _y = _states(geom, h_est)
        b = np.maximum(b, 1.0)
        af, rf, nn = _conveyance_terms(a, p, n_face)
        conv = _convective_face(q_est, a, dx, dt) * float(par.convective)
        fric_coef = G * (nn ** 2) * np.abs(q_est) / np.maximum(af * (rf ** (4.0 / 3.0)), 1e-6)
        face_q = inline_face_discharges(structures, h_est, hour=float(hour))
        face_q_last = face_q

        a_mat = np.zeros((n_u, n_u), dtype=float)
        rhs = np.zeros(n_u, dtype=float)

        # Lien tuc tai nut 0 .. n_x-2
        for i in range(n_x - 1):
            row = ih(i)
            inv_dx = theta / dxn[i]
            a_mat[row, ih(i)] += b[i] / dt
            a_mat[row, iq(i)] += inv_dx
            if i == 0:
                rhs[row] += inv_dx * q_up
            else:
                a_mat[row, iq(i - 1)] -= inv_dx
            q_left_n = q_up_n if i == 0 else float(q_n[i - 1])
            rhs[row] += (b[i] / dt) * h_n[i]
            rhs[row] -= ((1.0 - theta) / dxn[i]) * (float(q_n[i]) - q_left_n)
            rhs[row] += float(lat[i]) / dxn[i]

        # Bien H ha luu
        a_mat[ih(n_x - 1), ih(n_x - 1)] = 1.0
        rhs[ih(n_x - 1)] = max(float(h_ds), float(z[-1]) + par.y_min)

        # Dong luong tai doan — mat co structure: Q = Q_struct (MIKE)
        for j in range(n_x - 1):
            row = iq(j)
            if j in face_q:
                a_mat[row, :] = 0.0
                a_mat[row, iq(j)] = 1.0
                rhs[row] = float(face_q[j])
                continue
            coef_q = 1.0 + dt * theta * float(fric_coef[j])
            coef_h = dt * theta * G * float(af[j]) / dx[j]
            a_mat[row, iq(j)] += coef_q
            a_mat[row, ih(j)] -= coef_h
            a_mat[row, ih(j + 1)] += coef_h
            dh_n = float(h_n[j + 1] - h_n[j])
            rhs[row] += float(q_n[j])
            rhs[row] -= dt * (1.0 - theta) * G * float(af[j]) * dh_n / dx[j]
            rhs[row] -= dt * (1.0 - theta) * float(fric_coef[j]) * float(q_n[j])
            rhs[row] -= dt * float(conv[j])

        try:
            sol = np.linalg.solve(a_mat, rhs)
        except np.linalg.LinAlgError:
            q_exp, h_exp = sv.saint_venant_step(
                q_n, h_n, geom, par, q_up, h_ds, dt, lat,
                structures=structures, hour=hour,
            )
            return q_exp, h_exp

        h_sol = sol[:n_x].copy()
        q_sol = sol[n_x:].copy()
        for j, qv in face_q.items():
            if 0 <= j < q_sol.size:
                q_sol[j] = float(qv)
        h_est = np.maximum(0.55 * h_sol + 0.45 * h_est, z + par.y_min)
        h_est[-1] = max(float(h_ds), float(z[-1]) + par.y_min)
        # WSE long chinh khong tut duoi H ha luu — tranh rut can long DEM sau.
        h_est = np.maximum(h_est, float(h_ds))

        q_man = _manning_face_q(h_est, geom, par, n_face, dx)
        blend = float(min(0.85, max(0.0, par.manning_blend)))
        q_mix = (1.0 - blend) * q_sol + blend * q_man
        for j, qv in face_q.items():
            if 0 <= j < q_mix.size:
                q_mix[j] = float(qv)
        q_floor = max(float(par.q_min), 0.05 * q_up)
        if par.unidirectional:
            # Khong ep san Q_min len mat structure (co the Q nho / nguoc).
            mask = np.ones(q_mix.size, dtype=bool)
            for j in face_q:
                if 0 <= j < mask.size:
                    mask[j] = False
            q_mix = np.where(mask, np.maximum(q_mix, q_floor), q_mix)
        else:
            q_mix = np.where(np.abs(q_mix) < q_floor, np.sign(q_mix + 1e-12) * q_floor, q_mix)
            for j, qv in face_q.items():
                if 0 <= j < q_mix.size:
                    q_mix[j] = float(qv)
        omega = float(min(1.0, max(0.15, par.q_relax)))
        q_est = (1.0 - omega) * q_n + omega * q_mix
        for j, qv in face_q.items():
            if 0 <= j < q_est.size:
                q_est[j] = float(qv)
        q_est = _smooth_face_q(q_est, passes=2)
        for j, qv in face_q.items():
            if 0 <= j < q_est.size:
                q_est[j] = float(qv)

    apply_reservoir_storage(structures, face_q_last, dt_s=dt)
    return q_est, h_est


def _outlet_node_q(qf: np.ndarray, q_up: float) -> np.ndarray:
    """Q tai nut: cua ra trung binh 3 mat cat cuoi de het rang cua."""
    q = sv._node_q(qf, float(q_up))
    if q.size >= 3:
        q[-1] = 0.20 * q[-3] + 0.35 * q[-2] + 0.45 * q[-1]
    elif q.size >= 2:
        q[-1] = 0.40 * q[-1] + 0.60 * q[-2]
    return q


def _hydro_dt_implicit(a: np.ndarray, b: np.ndarray, qf: np.ndarray, dx: np.ndarray, par: MikeHdParams) -> float:
    """Buoc an: CFL mem (theta-scheme cho phep CFL > 1)."""
    af = 0.5 * (a[:-1] + a[1:])
    bf = 0.5 * (b[:-1] + b[1:])
    u = qf / np.maximum(af, 1e-3)
    c = np.abs(u) + np.sqrt(G * np.maximum(af / np.maximum(bf, 1.0), 0.05))
    dt_cfl = float(par.cfl) * float(np.min(dx / np.maximum(c, 0.2)))
    return float(min(max(dt_cfl, 5.0), par.dt_hydro_max_s))


def route_mike_hd(
    hours: np.ndarray,
    q_in: np.ndarray,
    h_down: np.ndarray,
    geom,
    par: MikeHdParams,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
    reach_id: str = "main",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, int, dict]:
    n_t = int(hours.size)
    n_x = int(geom.distance_m.size)
    q_out = np.zeros((n_t, n_x), dtype=float)
    h_out = np.zeros((n_t, n_x), dtype=float)
    y_out = np.zeros((n_t, n_x), dtype=float)

    qf, h, y, q_node = sv._init_state(
        geom, par, float(q_in[0]), float(h_down[0]), q0_by_id, h0_by_id
    )
    q_out[0] = q_node
    h_out[0], y_out[0] = h, y

    dx = np.maximum(np.asarray(geom.dx_m, dtype=float), 1.0)
    dt_out_s = float(par.dt_hours) * 3600.0
    dt_used = par.dt_hydro_max_s
    n_sub_max = 1
    t_abs = float(hours[0]) * 3600.0
    hours_s = hours * 3600.0
    q_up_old = float(q_in[0])
    from flood_model.construction import (
        apply_structures_to_q_lat,
        init_structure_runtime,
        log_bound_structures,
    )

    st_runtime = init_structure_runtime(reach_id or "main", geom)
    log_bound_structures(st_runtime, label=f"MIKE:{reach_id or 'main'}")
    from flood_model.construction import snapshot_reservoir_levels

    snapshot_reservoir_levels(st_runtime)
    for k in range(1, n_t):
        t_end = float(hours_s[k])
        while t_abs < t_end - 1e-9:
            a, _p, b, _y = _states(geom, h)
            dt = _hydro_dt_implicit(a, b, qf, dx, par)
            dt = min(dt, t_end - t_abs)
            w = (t_abs + dt - float(hours_s[k - 1])) / max(float(hours_s[k] - hours_s[k - 1]), 1.0)
            w = min(max(w, 0.0), 1.0)
            q_bc = float((1.0 - w) * q_in[k - 1] + w * q_in[k])
            h_bc = float((1.0 - w) * h_down[k - 1] + w * h_down[k])
            hour_now = float(hours[k - 1] + w * (hours[k] - hours[k - 1]))
            q_lat = apply_structures_to_q_lat(
                None, st_runtime, h, hour=hour_now, dt_s=dt
            )
            qf, h = mike_hd_step(
                qf, h, geom, par, q_bc, h_bc, dt, q_lat, q_up_old,
                structures=st_runtime, hour=hour_now,
            )
            q_up_old = q_bc
            t_abs += dt
            dt_used = min(dt_used, dt)
            n_sub_max = max(n_sub_max, int(math.ceil(dt_out_s / max(dt, 1.0))))
        q_node = _outlet_node_q(qf, float(q_in[k]))
        h[-1] = max(float(h_down[k]), float(geom.z_bed[-1]) + par.y_min)
        _a, _p, _b, y = _states(geom, h)
        q_out[k] = q_node
        h_out[k] = h
        y_out[k] = y
        snapshot_reservoir_levels(st_runtime)
        if k == 1 or k % 24 == 0 or k == n_t - 1:
            print(
                f"  t = {hours[k]:6.1f} h   Q_ds = {q_node[-1]:8.1f} m3/s   "
                f"H_us = {h[0]:6.2f} m",
                flush=True,
            )

    return q_out, h_out, y_out, float(dt_used), n_sub_max, dict(st_runtime.reservoir_history)


def route_network_mike(
    hours: np.ndarray,
    q_tank: np.ndarray,
    h_down: np.ndarray,
    main_geom,
    tribs: list[dict[str, Any]],
    par: MikeHdParams,
    q0_by_id: Optional[dict[int, float]] = None,
    h0_by_id: Optional[dict[int, float]] = None,
    h_outlet: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, int, np.ndarray, list, dict]:
    """Long chinh + nhanh: bien va nut route_network."""
    n_t = int(hours.size)
    frac_sum = float(sum(float(t["q_frac"]) for t in tribs if not sv._is_outlet(t)))
    q_main_up = np.maximum(q_tank * max(1.0 - frac_sum, 0.08), par.q_min)
    z_main_ds = float(main_geom.z_bed[-1])
    depth_ds = np.maximum(np.asarray(h_down, dtype=float) - z_main_ds, par.y_min)
    h_out_bc = None if h_outlet is None else np.asarray(h_outlet, dtype=float)

    trib_qin = []
    trib_qmin = []
    trib_h_outer = []
    for t in tribs:
        outlet = sv._is_outlet(t)
        if outlet:
            qmin = max(0.4, par.q_min * 0.05)
            trib_qin.append(np.zeros(n_t, dtype=float))
            z_far = float(t["geom"].z_bed[-1])
            if h_out_bc is not None and h_out_bc.size == n_t:
                trib_h_outer.append(np.maximum(h_out_bc, z_far + par.y_min))
            else:
                trib_h_outer.append(z_far + depth_ds)
        else:
            qmin = max(0.4, par.q_min * max(float(t["q_frac"]), 0.05))
            trib_qin.append(np.maximum(q_tank * float(t["q_frac"]), qmin))
            trib_h_outer.append(None)
        trib_qmin.append(qmin)

    n_x_m = int(main_geom.distance_m.size)
    q_out = np.zeros((n_t, n_x_m), dtype=float)
    h_out = np.zeros((n_t, n_x_m), dtype=float)
    y_out = np.zeros((n_t, n_x_m), dtype=float)

    qf_m, h_m, _y_m, q_node_m = sv._init_state(
        main_geom, par, float(q_main_up[0]), float(h_down[0]), q0_by_id, h0_by_id
    )
    q_out[0] = q_node_m
    h_out[0] = h_m
    y_out[0] = h_m - main_geom.z_bed

    trib_states = []
    trib_res_q = []
    trib_res_h = []
    trib_q_up_old = []
    for t, q_in_t, qmin, h_outer in zip(tribs, trib_qin, trib_qmin, trib_h_outer):
        tpar = MikeHdParams(**{**par.__dict__, "q_min": qmin})
        join = int(t["join_xs"])
        geom = t["geom"]
        if sv._is_outlet(t):
            q_cap = sv.OFFTAKE_Q_FRAC_MAX * float(q_main_up[0])
            q_up0 = sv._weir_offtake_q(
                float(h_m[join]), geom.sections[0], float(geom.z_bed[0]), tpar, q_cap
            )
            q_in_t[0] = q_up0
            qf, h, _y, q_node = sv._init_state(geom, tpar, q_up0, float(h_outer[0]))
            trib_q_up_old.append(q_up0)
        else:
            qf, h, _y, q_node = sv._init_state(geom, tpar, float(q_in_t[0]), float(h_m[join]))
            trib_q_up_old.append(float(q_in_t[0]))
        n_x = int(geom.distance_m.size)
        tq = np.zeros((n_t, n_x), dtype=float)
        th = np.zeros((n_t, n_x), dtype=float)
        tq[0], th[0] = q_node, h
        trib_states.append(
            {
                "qf": qf,
                "h": h,
                "par": tpar,
                "geom": geom,
                "join": join,
                "outlet": sv._is_outlet(t),
                "h_outer": h_outer,
            }
        )
        trib_res_q.append(tq)
        trib_res_h.append(th)

    dx_m = np.maximum(np.asarray(main_geom.dx_m, dtype=float), 1.0)
    dt_out_s = float(par.dt_hours) * 3600.0
    dt_used = par.dt_hydro_max_s
    n_sub_max = 1
    t_abs = float(hours[0]) * 3600.0
    hours_s = hours * 3600.0
    q_up_old_m = float(q_main_up[0])
    from flood_model.construction import (
        apply_structures_to_q_lat,
        init_structure_runtime,
        log_bound_structures,
    )

    main_st = init_structure_runtime("main", main_geom)
    log_bound_structures(main_st, label="MIKE:main")
    trib_st = []
    for t in tribs:
        rid = str(t.get("reach_id") or "trib_1")
        rt = init_structure_runtime(rid, t["geom"])
        log_bound_structures(rt, label=f"MIKE:{rid}")
        trib_st.append(rt)
    from flood_model.construction import snapshot_reservoir_levels, merge_reservoir_histories

    snapshot_reservoir_levels(main_st)
    for rt in trib_st:
        snapshot_reservoir_levels(rt)

    for k in range(1, n_t):
        t_end = float(hours_s[k])
        q_up_last = [float(trib_qin[i][k - 1]) for i in range(len(tribs))]
        while t_abs < t_end - 1e-9:
            a, _p, b, _y = _states(main_geom, h_m)
            dt = _hydro_dt_implicit(a, b, qf_m, dx_m, par)
            for st in trib_states:
                ta, _tp, tb, _ty = _states(st["geom"], st["h"])
                tdx = np.maximum(np.asarray(st["geom"].dx_m, dtype=float), 1.0)
                dt = min(dt, _hydro_dt_implicit(ta, tb, st["qf"], tdx, st["par"]))
            dt = min(dt, t_end - t_abs)
            w = (t_abs + dt - float(hours_s[k - 1])) / max(float(hours_s[k] - hours_s[k - 1]), 1.0)
            w = min(max(w, 0.0), 1.0)
            q_bc = float((1.0 - w) * q_main_up[k - 1] + w * q_main_up[k])
            h_bc = float((1.0 - w) * h_down[k - 1] + w * h_down[k])
            hour_now = float(hours[k - 1] + w * (hours[k] - hours[k - 1]))
            q_lat = np.zeros(n_x_m, dtype=float)
            for i, st in enumerate(trib_states):
                h_j = float(h_m[st["join"]])
                if st["outlet"]:
                    q_cap = sv.OFFTAKE_Q_FRAC_MAX * max(q_bc, par.q_min)
                    q_t = sv._weir_offtake_q(
                        h_j,
                        st["geom"].sections[0],
                        float(st["geom"].z_bed[0]),
                        st["par"],
                        q_cap,
                    )
                    h_ds_t = float((1.0 - w) * st["h_outer"][k - 1] + w * st["h_outer"][k])
                    q_lat_t = apply_structures_to_q_lat(
                        None, trib_st[i], st["h"], hour=hour_now, dt_s=dt
                    )
                    st["qf"], st["h"] = mike_hd_step(
                        st["qf"],
                        st["h"],
                        st["geom"],
                        st["par"],
                        q_t,
                        h_ds_t,
                        dt,
                        q_lat_t,
                        trib_q_up_old[i],
                        structures=trib_st[i],
                        hour=hour_now,
                    )
                    trib_q_up_old[i] = q_t
                    q_lat[st["join"]] -= q_t
                    q_up_last[i] = q_t
                else:
                    q_t = float((1.0 - w) * trib_qin[i][k - 1] + w * trib_qin[i][k])
                    q_lat_t = apply_structures_to_q_lat(
                        None, trib_st[i], st["h"], hour=hour_now, dt_s=dt
                    )
                    st["qf"], st["h"] = mike_hd_step(
                        st["qf"],
                        st["h"],
                        st["geom"],
                        st["par"],
                        q_t,
                        h_j,
                        dt,
                        q_lat_t,
                        trib_q_up_old[i],
                        structures=trib_st[i],
                        hour=hour_now,
                    )
                    trib_q_up_old[i] = q_t
                    q_lat[st["join"]] += float(st["qf"][-1])
            q_lat = apply_structures_to_q_lat(
                q_lat, main_st, h_m, hour=hour_now, dt_s=dt
            )
            qf_m, h_m = mike_hd_step(
                qf_m, h_m, main_geom, par, q_bc, h_bc, dt, q_lat, q_up_old_m,
                structures=main_st, hour=hour_now,
            )
            q_up_old_m = q_bc
            t_abs += dt
            dt_used = min(dt_used, dt)
            n_sub_max = max(n_sub_max, int(math.ceil(dt_out_s / max(dt, 1.0))))

        q_node_m = _outlet_node_q(qf_m, float(q_main_up[k]))
        h_m[-1] = max(float(h_down[k]), float(main_geom.z_bed[-1]) + par.y_min)
        h_m = np.maximum(h_m, float(h_down[k]))
        _a, _p, _b, y_m = _states(main_geom, h_m)
        q_out[k] = q_node_m
        h_out[k] = h_m
        y_out[k] = y_m
        snapshot_reservoir_levels(main_st)
        for rt in trib_st:
            snapshot_reservoir_levels(rt)
        for i, st in enumerate(trib_states):
            if st["outlet"]:
                trib_qin[i][k] = q_up_last[i]
                tq = sv._node_q(st["qf"], float(trib_qin[i][k]))
                st["h"][-1] = max(float(st["h_outer"][k]), float(st["geom"].z_bed[-1]) + st["par"].y_min)
            else:
                tq = sv._node_q(st["qf"], float(trib_qin[i][k]))
                st["h"][-1] = max(float(h_m[st["join"]]), float(st["geom"].z_bed[-1]) + st["par"].y_min)
            trib_res_q[i][k] = tq
            trib_res_h[i][k] = st["h"]
        if k == 1 or k % 24 == 0 or k == n_t - 1:
            extra = "  ".join(
                f"{tribs[i]['reach_id']} {trib_res_q[i][k, sv._trib_join_idx(tribs[i])]:.0f}"
                for i in range(len(tribs))
            )
            print(
                f"  t = {hours[k]:6.1f} h   Q_up = {q_node_m[0]:8.1f}  Q_ds = {q_node_m[-1]:8.1f} m3/s   "
                f"H_us = {h_m[0]:6.2f} m   {extra}",
                flush=True,
            )

    trib_out = []
    for i, t in enumerate(tribs):
        trib_out.append(
            sv.TribResult(
                reach_id=str(t["reach_id"]),
                geom=t["geom"],
                q=trib_res_q[i],
                h=trib_res_h[i],
                q_in=trib_qin[i],
                join_station_m=float(t["join_station_m"]),
                join_xs=int(t["join_xs"]),
                q_frac=float(t["q_frac"]),
                length_m=float(t["length_m"]),
                lon=np.asarray(t["lon"], dtype=float),
                lat=np.asarray(t["lat"], dtype=float),
                kind=str(t.get("kind", "inflow")),
                bc=str(t.get("bc", "Q")),
                z_near=float(t.get("z_near", 0.0)),
                z_far=float(t.get("z_far", 0.0)),
            )
        )
    return (
        q_out,
        h_out,
        y_out,
        float(dt_used),
        n_sub_max,
        q_main_up,
        trib_out,
        merge_reservoir_histories(main_st, *trib_st),
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    raw = list(argv) if argv is not None else sys.argv[1:]
    extra = argparse.ArgumentParser(add_help=False)
    extra.add_argument(
        "--theta",
        type=float,
        default=1.0,
        help="Trong so Abbott-Ionescu (0.5..1). Mac dinh 1 = an hoan toan, bot rang cua Q",
    )
    extra.add_argument(
        "--picard",
        type=int,
        default=3,
        dest="n_picard",
        help="So lap Picard moi buoc (cap nhat A, B, R)",
    )
    extra.add_argument(
        "--convective",
        type=float,
        default=0.15,
        help="He so so hang doi luu d(Q^2/A)/dx (0 = tat). Mac dinh 0.15",
    )
    extra.add_argument(
        "--manning-blend",
        type=float,
        default=0.25,
        dest="manning_blend",
        help="Tron Q an voi Q Manning (0..1)",
    )
    extra.add_argument(
        "--q-relax",
        type=float,
        default=0.35,
        dest="q_relax",
        help="He so cap nhat Q moi buoc (0.2..1). Nho hon = muot hon, het rang cua",
    )
    known, rest = extra.parse_known_args(raw)
    args = sv.parse_args(rest)
    args.theta = float(known.theta)
    args.n_picard = int(known.n_picard)
    args.convective = float(known.convective)
    args.manning_blend = float(known.manning_blend)
    args.q_relax = float(known.q_relax)
    if Path(str(args.out_dir)).resolve() == sv.DEFAULT_OUT_DIR.resolve():
        args.out_dir = DEFAULT_OUT_DIR
    if "--cfl" not in raw:
        args.cfl = 1.0
    if "--dt-hydro" not in raw:
        args.dt_hydro_max_s = 60.0
    return args


def run_model(args: argparse.Namespace) -> Any:
    par = MikeHdParams(
        manning_n=args.manning_n,
        xs_spacing_m=sv.parse_xs_spacing_m(args.xs_spacing),
        xs_half_width_m=float(args.xs_half),
        dt_hours=sv.parse_dt_hours(args.dt),
        cfl=float(args.cfl),
        dt_hydro_max_s=float(args.dt_hydro_max_s),
        unidirectional=not bool(getattr(args, "allow_reverse", False)),
        q0=getattr(args, "q0", None),
        h0=getattr(args, "h0", None),
        theta=float(getattr(args, "theta", 1.0)),
        n_picard=int(getattr(args, "n_picard", 3)),
        convective=float(getattr(args, "convective", 0.15)),
        manning_blend=float(getattr(args, "manning_blend", 0.25)),
        q_relax=float(getattr(args, "q_relax", 0.35)),
    )
    hours0, q0 = sv.load_inflow_q(args.inflow)
    hours, q_in = sv.resample_series(hours0, q0, par.dt_hours)
    q_in = np.maximum(q_in, par.q_min)
    print(
        f"dt xuat        : {par.dt_hours:g} gio  ({hours.size} buoc, "
        f"{float(hours[0]):.2f} .. {float(hours[-1]):.2f} h)",
        flush=True,
    )
    print(
        f"MIKE HD        : Abbott-Ionescu  theta={par.theta:g}  "
        f"Picard={par.n_picard}  conv={par.convective:g}  "
        f"Manning blend={par.manning_blend:g}  q_relax={par.q_relax:g}  "
        f"dt_hydro max {par.dt_hydro_max_s:g} s  CFL={par.cfl:g}",
        flush=True,
    )

    n_csv = sv.resolve_n_csv(getattr(args, "n_csv", None))
    n_by_id: dict[int, float] = {}
    n_station = np.array([], dtype=float)
    n_along = np.array([], dtype=float)
    if n_csv is not None:
        n_by_id, n_station, n_along = sv.load_manning_n_table(n_csv, reach_id="main")
        print(
            f"Manning n CSV  : {n_csv}  ({len(n_by_id)} mat cat long chinh, "
            f"{int(n_station.size)} tram ly trinh)",
            flush=True,
        )
    else:
        raw = getattr(args, "n_csv", None)
        if raw:
            raise FileNotFoundError(f"Khong thay file Manning n: {raw}")
        print(
            f"Manning n CSV  : khong co {sv.DEFAULT_N_CSV.name} — dung --n {par.manning_n:g}",
            flush=True,
        )

    max_mains = sv.parse_max_mains(getattr(args, "max_mains", sv.DEFAULT_MAX_MAINS))
    max_tribs = (
        0
        if getattr(args, "no_network", False)
        else sv.parse_max_tribs(getattr(args, "max_tribs", sv.DEFAULT_MAX_TRIBS))
    )
    print(
        f"Trich mang 1D  : toi da {max_mains} song chinh, {max_tribs} song nhanh...",
        flush=True,
    )
    geom, extra_specs, trib_specs = sv.extract_model_network(
        args.dem,
        par,
        max_mains=max_mains,
        max_tribs=max_tribs,
        water_source="saint-venant-1d",
        no_network=bool(getattr(args, "no_network", False)),
        n_default=par.manning_n,
        n_csv=n_csv,
        min_trib_m=max(1500.0, float(getattr(args, "min_trib_km", 2.0)) * 1000.0),
        trib_half_m=float(getattr(args, "trib_half", 600.0)),
    )
    dx_m = float(np.mean(geom.dx_m)) if geom.dx_m.size else par.xs_spacing_m
    print(
        f"Mat cat        : {len(geom.sections)} tram long chinh, dx TB {dx_m:.0f} m "
        f"(yeu cau {par.xs_spacing_m:.0f} m)",
        flush=True,
    )
    nn = [sv.section_n(sec, par.manning_n) for sec in geom.sections]
    if nn:
        print(
            f"Manning n ap dung: {min(nn):.4f} .. {max(nn):.4f}  "
            f"({len(nn)} mat cat long chinh)",
            flush=True,
        )
    if extra_specs:
        print(f"Song chinh them: {len(extra_specs)} long doc lap", flush=True)
        for spec in extra_specs:
            print(
                f"  {spec['reach_id']}: {float(spec['length_m'])/1000.0:.2f} km, "
                f"{len(spec['geom'].sections)} mat cat",
                flush=True,
            )
    else:
        print("Song chinh them: khong tim thay long doc lap du dieu kien.", flush=True)
    if trib_specs:
        for t in trib_specs:
            if sv._is_outlet(t):
                role = (
                    f"thoat nuoc (bien H ha luu), z_gan={t['z_near']:.2f} z_xa={t['z_far']:.2f} m"
                )
            else:
                role = (
                    f"nhap luu (bien Q), Q_frac={t['q_frac']:.3f}, "
                    f"z_gan={t['z_near']:.2f} z_xa={t['z_far']:.2f} m"
                )
            print(
                f"  {t['reach_id']}: {t['length_m']/1000:.2f} km, "
                f"nut XS{t['join_xs']+1} ({t['join_station_m']/1000:.2f} km), {role}",
                flush=True,
            )
    elif not getattr(args, "no_network", False):
        print("  Khong tim thay nhanh du dieu kien — chay khong song nhanh.", flush=True)

    q0_by_id: dict[int, float] = {}
    h0_by_id: dict[int, float] = {}
    ic_csv = getattr(args, "ic_csv", None)
    if ic_csv is not None:
        ic_path = Path(ic_csv)
        if not sv.csv_available(ic_path):
            raise FileNotFoundError(f"Khong thay file dieu kien ban dau Q0/H0: {ic_path}")
        q0_by_id, h0_by_id = sv.load_initial_qh_csv(ic_path)
        print(
            f"IC CSV         : {ic_path}  (Q0 {len(q0_by_id)} mat cat, H0 {len(h0_by_id)} mat cat)",
            flush=True,
        )
    h_down = sv.load_downstream_stage(
        hours,
        getattr(args, "h_csv", None),
        getattr(args, "h_down", None),
        float(geom.z_bed[-1]),
        q_in,
    )
    trib_h_path = Path(getattr(args, "trib_h_csv", None) or sv.DEFAULT_TRIB_H_CSV)
    h_trib_down = sv.load_trib_outlet_stage(hours, trib_h_path)
    if h_trib_down is not None:
        print(
            f"H ha luu nhanh thoat : cot {sv.TRIB_H_COL} trong {trib_h_path}  "
            f"({float(np.min(h_trib_down)):.2f} .. {float(np.max(h_trib_down)):.2f} m)",
            flush=True,
        )
    else:
        print(
            f"H ha luu nhanh thoat : khong doc duoc {sv.TRIB_H_COL} tu {trib_h_path} — dung do sau H ha luu Hong",
            flush=True,
        )
    h_need = max(float(np.max(h_down)), float(np.max(geom.z_bed))) + 12.0
    if h_trib_down is not None:
        h_need = max(h_need, float(np.max(h_trib_down)) + 12.0)
    if par.h0 is not None:
        h_need = max(h_need, float(par.h0) + 12.0)
    if h0_by_id:
        h_need = max(h_need, max(h0_by_id.values()) + 12.0)
    for spec in extra_specs:
        zb = np.asarray(spec["geom"].z_bed, dtype=float)
        if zb.size:
            h_need = max(h_need, float(np.max(zb)) + 12.0)
    sv.extend_section_tables(geom, h_need)
    for spec in extra_specs:
        sv.extend_section_tables(spec["geom"], h_need)
    for t in trib_specs:
        sv.extend_section_tables(t["geom"], h_need)

    trib_res = []
    q_main_up = q_in
    reservoir_levels: dict = {}
    if trib_specs:
        print("Dang giai MIKE HD mang (Abbott-Ionescu an)...", flush=True)
        q, h, y, dt_h, n_sub, q_main_up, trib_res, reservoir_levels = route_network_mike(
            hours, q_in, h_down, geom, trib_specs, par, q0_by_id, h0_by_id, h_trib_down
        )
        q_in_total = np.array(q_main_up, dtype=float)
        q_out_total = np.array(q[:, -1], dtype=float)
        for tr in trib_res:
            if sv._is_outlet(tr):
                q_out_total = q_out_total + tr.q[:, sv._trib_outer_idx(tr)]
            else:
                q_in_total = q_in_total + tr.q_in
        mb = sv.mass_balance(q_in_total, q_out_total, par.dt_hours * 3600.0)
    else:
        print("Dang giai MIKE HD 1 long (Abbott-Ionescu an)...", flush=True)
        q, h, y, dt_h, n_sub, reservoir_levels = route_mike_hd(
            hours, q_in, h_down, geom, par, q0_by_id, h0_by_id
        )
        mb = sv.mass_balance(q_in, q[:, -1], par.dt_hours * 3600.0)
    extra_mains = sv.route_extra_main_reaches(
        hours, q_in, h_down, extra_specs, par, route_mike_hd, q0_by_id, h0_by_id
    )
    pk = int(np.argmax(q[:, 0]))
    return sv.RouteResult(
        hours=hours,
        q_in=q_in,
        h_down=h_down,
        q=q,
        h=h,
        y=y,
        geom=geom,
        params=par,
        station_km=geom.distance_m / 1000.0,
        peak_time_index=pk,
        mass_balance_m3=mb,
        dt_hydro_s=dt_h,
        n_substep=n_sub,
        q_main_up=q_main_up,
        tribs=trib_res,
        extra_mains=extra_mains,
        h_trib_down=h_trib_down,
        reservoir_levels=dict(reservoir_levels or {}),
    )


def print_summary(res: Any) -> None:
    print("Mo hinh        : MIKE by DHI HD  (Abbott-Ionescu implicit, mang 1D)" if res.tribs else "Mo hinh        : MIKE by DHI HD  (Abbott-Ionescu implicit)")
    theta = float(getattr(res.params, "theta", 0.7))
    n_pic = int(getattr(res.params, "n_picard", 3))
    print(f"So do          : theta={theta:g}, Picard={n_pic}, CFL mem={res.params.cfl:g}")
    sv.print_summary(res)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    print(f"DEM long song : {args.dem}")
    q_src = sv.resolve_inflow_csv(args.inflow)
    print(f"Q vao          : {q_src}  (cot q_m3s)")
    if args.h_down is not None:
        print(f"H ha luu       : hang {args.h_down:.3f} m")
    else:
        h_path = args.h_csv if args.h_csv is not None else sv.DEFAULT_H_CSV
        print(f"H ha luu       : {h_path}")
    if args.q0 is not None:
        print(f"Q0 ban dau     : {args.q0:.3f} m3/s")
    if args.h0 is not None:
        print(f"H0 ban dau     : {args.h0:.3f} m  (ha luu, tinh backwater)")
    if getattr(args, "ic_csv", None) is not None:
        print(f"IC CSV         : {args.ic_csv}")
    print(f"Lay mat cat ngang moi {sv.parse_xs_spacing_m(args.xs_spacing):.0f} m doc long song...")
    res = run_model(args)
    print_summary(res)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = sv.reach_geom_items(res)
    sv.write_geometries_csv(items, res.params, out / "mike_hd_river_geometry.csv")
    sv.write_manning_network_csv(items, out / "mike_hd_manning_n_applied.csv")
    sv.write_initial_csv(res.geom, res.q[0], res.h[0], out / "mike_hd_initial_qh.csv")
    sv.write_cross_sections_csv(res.geom, out / "mike_hd_cross_sections.csv")
    sv.write_result_csv(res, out / "mike_hd_result.csv")
    sv.write_network_reaches_csv(res, out / "mike_hd_network_reaches.csv")
    try:
        from flood_model.construction import write_reservoir_levels_csv

        write_reservoir_levels_csv(
            out / "mike_hd_reservoir.csv",
            res.hours,
            getattr(res, "reservoir_levels", None) or {},
        )
    except Exception:
        traceback.print_exc()
    if res.tribs:
        sv.write_tributary_geometry_csv(res, out / "mike_hd_tributary_geometry.csv")
        sv.write_tributary_result_csv(res, out / "mike_hd_tributary_result.csv")
    if res.extra_mains:
        sv.write_extra_main_result_csv(res, out / "mike_hd_extra_main_result.csv")
    print(f"Thu muc ket qua: {out}")
    print(f"Mat cat ngang  : {out / 'mike_hd_cross_sections.csv'}")
    print(f"Tram XS        : {out / 'mike_hd_river_geometry.csv'}")
    print(f"Mang 1D        : {out / 'mike_hd_network_reaches.csv'}")
    if res.tribs:
        print(f"Hinh hoc nhanh : {out / 'mike_hd_tributary_geometry.csv'}")
        print(f"Q/H nhanh      : {out / 'mike_hd_tributary_result.csv'}")
    if res.extra_mains:
        print(f"Q/H song chinh them: {out / 'mike_hd_extra_main_result.csv'}")
    print(f"Ket qua Q/H    : {out / 'mike_hd_result.csv'}")
    if not args.no_plot:
        sv.plot_result(res, out)
        print(f"Bieu do        : {out / 'saint_venant_hydrograph.png'}")
        print(f"Mat cat doc    : {out / 'saint_venant_profile.png'}")
        print(f"Mat cat ngang  : {out / 'saint_venant_cross_sections.png'}")
        if res.tribs:
            print(f"Mang 1D        : {out / 'saint_venant_network.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

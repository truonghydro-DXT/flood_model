/**
 * Model RR (TANK) va Model 1D (Saint-Venant) tren ung dung flood_model.
 */
(function (global) {
  "use strict";

  const API = (window.floodUrl ? window.floodUrl("/api/flow-3d") : "/api/flow-3d");

  let simPollTimer = null;
  let rrHydroData = null;
  let rrHydroResizeObs = null;

  function $(id) {
    return document.getElementById(id);
  }

  function selectedWaterSource() {
    return window.pickWaterSource ? window.pickWaterSource() : "saint-venant";
  }

  function waterSourceLabel(src) {
    return src === "saint-venant-1d" ? "Saint-venant-1D" : "Saint-venant";
  }

  function setHint(text) {
    const el = $("flood3dStats");
    if (el && text) el.textContent = text;
  }

  function setHydroBusy(busy, kind) {
    const rr = $("hydroRrRunBtn");
    const one = $("hydro1dRunBtn");
    if (rr) {
      rr.disabled = !!busy;
      rr.textContent = (busy && kind === "rr") ? "Đang chạy…" : "Model RR";
    }
    if (one) {
      one.disabled = !!busy;
      one.textContent = (busy && kind === "1d") ? "Đang chạy…" : "Model 1D";
    }
  }

  function fmtElapsed(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m ? (m + " phút " + r + " s") : (r + " s");
  }

  function progressModal() {
    return $("hydro1dProgressModal");
  }

  function openProgressModal(job, title) {
    const modal = progressModal();
    if (!modal) return;
    const titleEl = $("hydro1dProgressTitle");
    const kind = job && job.kind;
    if (titleEl) {
      titleEl.textContent = title || (kind === "rr" ? "Model RR" : "Model 1D");
    }
    modal.hidden = false;
    const closeBtn = $("hydro1dProgressClose");
    if (closeBtn) closeBtn.disabled = true;
    updateProgressModal(job || {
      status: "running",
      kind: kind || "1d",
      message: kind === "rr" ? "Đang chạy Model RR…" : "Đang chạy Model 1D…",
      log: []
    });
  }

  function closeProgressModal() {
    const modal = progressModal();
    if (modal) modal.hidden = true;
  }

  function updateProgressModal(job) {
    const modal = progressModal();
    if (!modal || modal.hidden) return;
    const statusEl = $("hydro1dProgressStatus");
    const metaEl = $("hydro1dProgressMeta");
    const logEl = $("hydro1dProgressLog");
    const closeBtn = $("hydro1dProgressClose");
    const st = job && job.status ? job.status : "running";
    if (statusEl) {
      statusEl.className = "hydro1d-progress-status is-" + (st === "ok" || st === "error" ? st : "running");
      if (st === "ok") statusEl.textContent = "Hoàn thành";
      else if (st === "error") statusEl.textContent = "Lỗi";
      else statusEl.textContent = "Đang chạy…";
    }
    if (metaEl) {
      const label = job.label || (job.kind === "rr" ? "TANK mưa-dòng chảy" : waterSourceLabel(job.water_source));
      const elapsed = job.elapsed_s != null ? fmtElapsed(job.elapsed_s) : "0 s";
      metaEl.textContent = (label || (job.kind === "rr" ? "Model RR" : "Model 1D")) + " · " + elapsed;
    }
    if (logEl) {
      const lines = Array.isArray(job.log) ? job.log : [];
      const text = lines.length
        ? lines.join("\n")
        : (job.message || job.error || "Đang khởi động…");
      if (logEl.textContent !== text) {
        logEl.textContent = text;
        logEl.scrollTop = logEl.scrollHeight;
      }
    }
    if (closeBtn) closeBtn.disabled = st === "running";
  }

  function stopSimPoll() {
    if (simPollTimer) {
      clearInterval(simPollTimer);
      simPollTimer = null;
    }
  }

  function applySimStatus(job) {
    if (!job) return;
    updateProgressModal(job);
    const kind = job.kind === "rr" ? "rr" : "1d";
    if (job.status === "running") {
      setHydroBusy(true, kind);
      if (kind === "rr") setRrActionBusy(true);
      return;
    }
    stopSimPoll();
    setHydroBusy(false, kind);
    if (kind === "rr") setRrActionBusy(false);
    if (job.status === "ok") {
      if (global.Flood3D && global.Flood3D.invalidate) global.Flood3D.invalidate();
      if (kind === "1d") setHint("");
      if (kind === "rr") {
        fetch(API + "/runoff-params", { headers: { "Accept": "application/json" } })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (!data || !data.ok) return;
            rrHydroData = data.hydrograph || null;
            const modal = rrParamsModal();
            if (modal && !modal.hidden) {
              drawRrHydrograph(rrHydroData || {});
              closeProgressModal();
            }
          })
          .catch(function () {});
      }
    }
  }

  async function pollHydro1d() {
    try {
      const res = await fetch(API + "/simulate", { headers: { "Accept": "application/json" } });
      const job = await res.json();
      applySimStatus(job);
    } catch (_e) {
      stopSimPoll();
      setHydroBusy(false);
      updateProgressModal({
        status: "error",
        message: "Không đọc được trạng thái mô phỏng.",
        log: ["Không đọc được trạng thái mô phỏng."]
      });
    }
  }

  async function startHydro1d() {
    const src = selectedWaterSource();
    const label = waterSourceLabel(src);
    stopSimPoll();
    setHydroBusy(true, "1d");
    openProgressModal({
      status: "running",
      kind: "1d",
      label: label,
      water_source: src,
      elapsed_s: 0,
      message: "Đang chạy Model 1D " + label + "…",
      log: ["Đang chạy Model 1D " + label + "…"]
    }, "Model 1D");
    try {
      const res = await fetch(API + "/simulate", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({ water_source: src })
      });
      const job = await res.json();
      if (!res.ok && res.status !== 409) {
        throw new Error((job && (job.error || job.message)) || "Không chạy được Model 1D");
      }
      if (res.status === 409 && job.kind && job.kind !== "1d") {
        throw new Error((job && (job.error || job.message)) || "Đang có mô phỏng khác chạy.");
      }
      applySimStatus(job);
      if (job.status === "running") {
        simPollTimer = setInterval(pollHydro1d, 1000);
      }
    } catch (err) {
      setHydroBusy(false, "1d");
      updateProgressModal({
        status: "error",
        kind: "1d",
        label: label,
        water_source: src,
        message: err.message || String(err),
        log: [err.message || String(err)]
      });
    }
  }

  function manningModal() {
    return $("svManningModal");
  }

  function setManningError(text) {
    const el = $("svManningError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeManningModal() {
    const modal = manningModal();
    if (modal) modal.hidden = true;
    setManningError("");
  }

  function collectManningRows() {
    const body = $("svManningBody");
    if (!body) return [];
    const rows = [];
    body.querySelectorAll("tr").forEach(function (tr) {
      const id = Number(tr.getAttribute("data-xs-id"));
      const nEl = tr.querySelector("input[data-n]");
      const stEl = tr.querySelector("[data-station]");
      if (!nEl) return;
      rows.push({
        xs_id: id,
        station_km: stEl ? stEl.getAttribute("data-station") : null,
        manning_n: nEl.value
      });
    });
    return rows;
  }

  function fillManningRange() {
    const fromEl = $("svManningFrom");
    const toEl = $("svManningTo");
    const nEl = $("svManningFillN");
    const from = Number(fromEl && fromEl.value);
    const to = Number(toEl && toEl.value);
    const n = Number(nEl && nEl.value);
    if (!Number.isFinite(from) || !Number.isFinite(to) || !Number.isFinite(n)) {
      setManningError("Nhập khoảng XS và n hợp lệ.");
      return;
    }
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    const body = $("svManningBody");
    if (!body) return;
    let count = 0;
    body.querySelectorAll("tr").forEach(function (tr) {
      const id = Number(tr.getAttribute("data-xs-id"));
      if (id < lo || id > hi) return;
      const input = tr.querySelector("input[data-n]");
      if (input) {
        input.value = n.toFixed(4);
        count += 1;
      }
    });
    setManningError(count ? "" : "Không có mặt cắt trong khoảng đã chọn.");
  }

  function renderManningRows(rows) {
    const body = $("svManningBody");
    if (!body) return;
    body.innerHTML = "";
    (rows || []).forEach(function (row) {
      const tr = document.createElement("tr");
      const xs = Number(row.xs_id);
      tr.setAttribute("data-xs-id", String(xs));
      const km = row.station_km != null && Number.isFinite(Number(row.station_km))
        ? Number(row.station_km).toFixed(2)
        : "—";
      const n = Number(row.manning_n);
      tr.innerHTML =
        "<td>XS" + xs + "</td>" +
        "<td data-station=\"" + (row.station_km != null ? row.station_km : "") + "\">" + km + "</td>" +
        "<td><input data-n type=\"number\" min=\"0.001\" max=\"0.2\" step=\"0.001\" value=\"" +
        (Number.isFinite(n) ? n.toFixed(4) : "0.0300") + "\" /></td>";
      body.appendChild(tr);
    });
    const ids = (rows || []).map(function (r) { return Number(r.xs_id); }).filter(Number.isFinite);
    const fromEl = $("svManningFrom");
    const toEl = $("svManningTo");
    if (fromEl && ids.length) fromEl.value = String(Math.min.apply(null, ids));
    if (toEl && ids.length) toEl.value = String(Math.max.apply(null, ids));
  }

  async function openManningModal() {
    const modal = manningModal();
    if (!modal) {
      startHydro1d();
      return;
    }
    setManningError("");
    const body = $("svManningBody");
    if (body) body.innerHTML = "<tr><td colspan=\"3\">Đang tải…</td></tr>";
    modal.hidden = false;
    try {
      const res = await fetch(API + "/manning-n", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được hệ số nhám.");
      }
      if (!data.rows || !data.rows.length) {
        throw new Error("Chưa có demo_manning_n.csv. Chạy Saint-Venant một lần để tạo file.");
      }
      renderManningRows(data.rows);
    } catch (err) {
      if (body) body.innerHTML = "";
      setManningError(err.message || String(err));
    }
  }

  async function saveManningAndRun() {
    const runBtn = $("svManningRun");
    setManningError("");
    const rows = collectManningRows();
    if (!rows.length) {
      setManningError("Không có mặt cắt để lưu.");
      return;
    }
    if (runBtn) runBtn.disabled = true;
    try {
      const res = await fetch(API + "/manning-n", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({ rows: rows })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không lưu được hệ số nhám.");
      }
      closeManningModal();
      startHydro1d();
    } catch (err) {
      setManningError(err.message || String(err));
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  }

  function onHydro1dClick() {
    openManningModal();
  }

  function rrParamsModal() {
    return $("rrParamsModal");
  }

  function setRrParamsError(text) {
    const el = $("rrParamsError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeRrParamsModal() {
    const modal = rrParamsModal();
    if (modal) modal.hidden = true;
    setRrParamsError("");
  }

  function fmtRrValue(key, val) {
    const n = Number(val);
    if (!Number.isFinite(n)) return "";
    if (key === "area_km2") return String(Math.round(n * 100) / 100);
    if (key === "dt_hours" || key === "d1") return String(n);
    if (Math.abs(n) >= 1) return n.toFixed(2).replace(/\.?0+$/, "") || String(n);
    return String(n);
  }

  function renderRrParams(data) {
    const body = $("rrParamsBody");
    if (!body) return;
    body.innerHTML = "";
    const values = (data && data.values) || {};
    (data.groups || []).forEach(function (group) {
      const fs = document.createElement("fieldset");
      fs.className = "rr-params-group";
      const legend = document.createElement("legend");
      legend.textContent = group.title || "";
      fs.appendChild(legend);
      const grid = document.createElement("div");
      grid.className = "rr-params-fields";
      (group.fields || []).forEach(function (field) {
        const lab = document.createElement("label");
        const caption = document.createElement("span");
        caption.textContent = field.label || field.key;
        const inp = document.createElement("input");
        inp.type = "number";
        inp.setAttribute("data-rr-key", field.key);
        if (field.min != null) inp.min = field.min;
        if (field.max != null) inp.max = field.max;
        if (field.step != null) inp.step = field.step;
        const val = values[field.key];
        inp.value = val == null ? "" : fmtRrValue(field.key, val);
        lab.appendChild(caption);
        lab.appendChild(inp);
        grid.appendChild(lab);
      });
      fs.appendChild(grid);
      body.appendChild(fs);
    });
  }

  function collectRrParams() {
    const body = $("rrParamsBody");
    const values = {};
    if (!body) return values;
    body.querySelectorAll("input[data-rr-key]").forEach(function (inp) {
      values[inp.getAttribute("data-rr-key")] = inp.value;
    });
    return values;
  }

  function zipSeries(hours, qs) {
    const pts = [];
    const n = Math.min((hours || []).length, (qs || []).length);
    for (let i = 0; i < n; i++) {
      const x = Number(hours[i]);
      const y = Number(qs[i]);
      if (!isFinite(x) || !isFinite(y)) continue;
      pts.push({ x: x, y: y });
    }
    return pts;
  }

  function seriesStats(pts) {
    if (!pts.length) return null;
    let peak = pts[0].y;
    let tPeak = pts[0].x;
    let sum = 0;
    pts.forEach(function (p) {
      sum += p.y;
      if (p.y > peak) {
        peak = p.y;
        tPeak = p.x;
      }
    });
    return { peak: peak, tPeak: tPeak, mean: sum / pts.length };
  }

  function interpY(pts, x) {
    if (!pts.length) return NaN;
    if (x <= pts[0].x) return pts[0].y;
    const last = pts[pts.length - 1];
    if (x >= last.x) return last.y;
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      if (x <= b.x) {
        const span = b.x - a.x;
        const t = span ? (x - a.x) / span : 0;
        return a.y + t * (b.y - a.y);
      }
    }
    return last.y;
  }

  function nashSutcliffe(obs, sim) {
    if (!obs.length || !sim.length) return null;
    const simSorted = sim.slice().sort(function (a, b) { return a.x - b.x; });
    const o = [];
    const s = [];
    obs.forEach(function (p) {
      const y = interpY(simSorted, p.x);
      if (!isFinite(p.y) || !isFinite(y)) return;
      o.push(p.y);
      s.push(y);
    });
    if (o.length < 2) return null;
    let mean = 0;
    o.forEach(function (v) { mean += v; });
    mean /= o.length;
    let num = 0;
    let den = 0;
    for (let i = 0; i < o.length; i++) {
      const d = o[i] - s[i];
      num += d * d;
      const e = o[i] - mean;
      den += e * e;
    }
    if (den <= 1e-12) return null;
    const nse = 1 - num / den;
    return isFinite(nse) ? nse : null;
  }

  function setRrNse(nse) {
    const el = $("rrHydroNse");
    if (!el) return;
    el.className = "rr-hydro-nse";
    if (nse == null || !isFinite(nse)) {
      el.textContent = "NSE —";
      return;
    }
    el.textContent = "NSE " + nse.toFixed(3);
    if (nse >= 0.75) el.classList.add("is-good");
    else if (nse >= 0.50) el.classList.add("is-ok");
    else el.classList.add("is-poor");
  }

  function fmtQ(v) {
    if (!isFinite(v)) return "—";
    if (Math.abs(v) >= 100) return v.toFixed(1);
    if (Math.abs(v) >= 10) return v.toFixed(2);
    return v.toFixed(3);
  }

  function drawPolyline(ctx, pts, X, Y) {
    if (!pts.length) return;
    ctx.beginPath();
    pts.forEach(function (p, i) {
      const x = X(p.x);
      const y = Y(p.y);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function drawRrHydrograph(hydro) {
    const canvas = $("rrHydroChart");
    const meta = $("rrHydroMeta");
    if (!canvas) return;
    const wrap = canvas.parentElement;
    const w = Math.max(240, (wrap && wrap.clientWidth) || canvas.clientWidth || 480);
    const h = Math.max(200, (wrap && wrap.clientHeight) || 300);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#101820";
    ctx.fillRect(0, 0, w, h);

    const sim = zipSeries(hydro && hydro.hours_sim, hydro && hydro.q_sim_m3s);
    const obs = zipSeries(hydro && hydro.hours_obs, hydro && hydro.q_obs_m3s);
    if (!sim.length && !obs.length) {
      ctx.fillStyle = "#aeb9c6";
      ctx.font = "12px Segoe UI, Be Vietnam Pro, sans-serif";
      ctx.fillText("Chưa có Q tính toán hoặc Q thực đo.", 16, h / 2);
      if (meta) meta.textContent = "Thiếu tank_result.csv hoặc demo_flow.csv (cột q_m3/s).";
      setRrNse(null);
      return;
    }

    let xMin = Infinity;
    let xMax = -Infinity;
    let yMin = Infinity;
    let yMax = -Infinity;
    function acc(pts) {
      pts.forEach(function (p) {
        xMin = Math.min(xMin, p.x);
        xMax = Math.max(xMax, p.x);
        yMin = Math.min(yMin, p.y);
        yMax = Math.max(yMax, p.y);
      });
    }
    acc(sim);
    acc(obs);
    if (xMax <= xMin) xMax = xMin + 1;
    if (yMax <= yMin) yMax = yMin + 1;
    yMin = Math.min(0, yMin);
    const yPad = Math.max(1, (yMax - yMin) * 0.08);
    yMax += yPad;
    const pad = { l: 52, r: 14, t: 12, b: 36 };

    function X(x) {
      return pad.l + ((x - xMin) / (xMax - xMin)) * (w - pad.l - pad.r);
    }
    function Y(y) {
      return pad.t + (1 - (y - yMin) / (yMax - yMin)) * (h - pad.t - pad.b);
    }

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1;
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.fillStyle = "#c5d0dc";
    for (let k = 0; k <= 4; k++) {
      const frac = k / 4;
      const val = yMin + (yMax - yMin) * (1 - frac);
      const yy = pad.t + frac * (h - pad.t - pad.b);
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(w - pad.r, yy);
      ctx.stroke();
      ctx.fillText(fmtQ(val), 6, yy + 4);
    }
    for (let k = 0; k <= 5; k++) {
      const val = xMin + (xMax - xMin) * (k / 5);
      const xx = X(val);
      ctx.fillText(String(Math.round(val)), xx - 8, h - 10);
    }
    ctx.fillText("Giờ", w / 2 - 10, h - 2);
    ctx.save();
    ctx.translate(12, h / 2 + 28);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("Q (m³/s)", 0, 0);
    ctx.restore();

    if (obs.length) {
      ctx.strokeStyle = "#e8c547";
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.8;
      drawPolyline(ctx, obs, X, Y);
      ctx.setLineDash([]);
    }
    if (sim.length) {
      ctx.strokeStyle = "#3d9cf0";
      ctx.lineWidth = 2;
      drawPolyline(ctx, sim, X, Y);
    }

    const stSim = seriesStats(sim);
    const stObs = seriesStats(obs);
    const nse = (hydro && hydro.nse != null && isFinite(Number(hydro.nse)))
      ? Number(hydro.nse)
      : nashSutcliffe(obs, sim);
    setRrNse(nse);
    if (meta) {
      const parts = [];
      if (nse != null && isFinite(nse)) parts.push("Nash–Sutcliffe NSE = " + nse.toFixed(3));
      if (stSim) parts.push("Q tính toán đỉnh " + fmtQ(stSim.peak) + " m³/s (giờ " + Math.round(stSim.tPeak) + ")");
      if (stObs) parts.push("Q thực đo đỉnh " + fmtQ(stObs.peak) + " m³/s (giờ " + Math.round(stObs.tPeak) + ")");
      meta.textContent = parts.join(" · ");
    }
  }

  function bindRrHydroResize() {
    const wrap = document.querySelector(".rr-hydro-canvas-wrap");
    if (!wrap || typeof ResizeObserver === "undefined") return;
    if (rrHydroResizeObs) return;
    rrHydroResizeObs = new ResizeObserver(function () {
      if (rrHydroData) drawRrHydrograph(rrHydroData);
    });
    rrHydroResizeObs.observe(wrap);
  }

  async function openRrParamsModal() {
    const modal = rrParamsModal();
    if (!modal) {
      startRainfallRunoff();
      return;
    }
    setRrParamsError("");
    const body = $("rrParamsBody");
    if (body) body.innerHTML = "<p class=\"sv-manning-hint\">Đang tải thông số…</p>";
    modal.hidden = false;
    try {
      const res = await fetch(API + "/runoff-params", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được thông số TANK.");
      }
      renderRrParams(data);
      rrHydroData = data.hydrograph || null;
      bindRrHydroResize();
      requestAnimationFrame(function () {
        drawRrHydrograph(rrHydroData || {});
      });
    } catch (err) {
      if (body) body.innerHTML = "";
      setRrParamsError(err.message || String(err));
    }
  }

  function setRrActionBusy(busy) {
    const saveBtn = $("rrParamsSave");
    const calBtn = $("rrParamsCalibrate");
    if (saveBtn) saveBtn.disabled = !!busy;
    if (calBtn) {
      calBtn.disabled = !!busy;
      calBtn.textContent = busy ? "Đang hiệu chỉnh…" : "Hiệu chỉnh";
    }
  }

  async function saveRrParams() {
    setRrParamsError("");
    const values = collectRrParams();
    if (!Object.keys(values).length) {
      throw new Error("Không có thông số để lưu.");
    }
    const res = await fetch(API + "/runoff-params", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
      },
      body: JSON.stringify({ values: values })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Không lưu được thông số TANK.");
    }
    return data;
  }

  async function openRrInputCsv() {
    setRrParamsError("");
    try {
      const res = await fetch(API + "/runoff-input", {
        method: "POST",
        headers: { "Accept": "application/json" }
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không mở được file dữ liệu đầu vào.");
      }
    } catch (err) {
      setRrParamsError(err.message || String(err));
    }
  }

  async function saveRrParamsOnly() {
    const saveBtn = $("rrParamsSave");
    setRrParamsError("");
    if (saveBtn) saveBtn.disabled = true;
    try {
      await saveRrParams();
      if (saveBtn) saveBtn.textContent = "Đã lưu";
      setTimeout(function () {
        if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu thông số";
      }, 1400);
    } catch (err) {
      setRrParamsError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function startRainfallRunoff(opts) {
    const keepModal = !!(opts && opts.keepModal);
    stopSimPoll();
    setHydroBusy(true, "rr");
    setRrActionBusy(true);
    openProgressModal({
      status: "running",
      kind: "rr",
      label: "TANK mưa-dòng chảy",
      water_source: "tank",
      elapsed_s: 0,
      message: "Đang hiệu chỉnh Model RR…",
      log: ["Đang hiệu chỉnh Model RR…"]
    }, "Model RR");
    try {
      const res = await fetch(API + "/simulate-rr", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({})
      });
      const job = await res.json();
      if (!res.ok && res.status !== 409) {
        throw new Error((job && (job.error || job.message)) || "Không chạy được Model RR");
      }
      if (res.status === 409 && job.kind && job.kind !== "rr") {
        throw new Error((job && (job.error || job.message)) || "Đang có mô phỏng khác chạy.");
      }
      applySimStatus(job);
      if (job.status === "running") {
        simPollTimer = setInterval(pollHydro1d, 1000);
      } else if (job.status !== "running") {
        setRrActionBusy(false);
      }
    } catch (err) {
      setHydroBusy(false, "rr");
      setRrActionBusy(false);
      updateProgressModal({
        status: "error",
        kind: "rr",
        label: "TANK mưa-dòng chảy",
        message: err.message || String(err),
        log: [err.message || String(err)]
      });
    }
    return keepModal;
  }

  async function calibrateRrModel() {
    setRrParamsError("");
    setRrActionBusy(true);
    try {
      await saveRrParams();
      await startRainfallRunoff({ keepModal: true });
    } catch (err) {
      setRrActionBusy(false);
      setRrParamsError(err.message || String(err));
    }
  }

  function wireUi() {
    const simBtn = $("hydro1dRunBtn");
    if (simBtn) simBtn.addEventListener("click", onHydro1dClick);
    const rrBtn = $("hydroRrRunBtn");
    if (rrBtn) rrBtn.addEventListener("click", openRrParamsModal);
    const rrCancel = $("rrParamsCancel");
    const rrInput = $("rrParamsInput");
    const rrSave = $("rrParamsSave");
    const rrCalibrate = $("rrParamsCalibrate");
    const rrModalEl = rrParamsModal();
    if (rrCancel) rrCancel.addEventListener("click", closeRrParamsModal);
    if (rrInput) rrInput.addEventListener("click", openRrInputCsv);
    if (rrSave) rrSave.addEventListener("click", saveRrParamsOnly);
    if (rrCalibrate) rrCalibrate.addEventListener("click", calibrateRrModel);
    if (rrModalEl) {
      rrModalEl.addEventListener("click", function (ev) {
        if (ev.target === rrModalEl) closeRrParamsModal();
      });
    }
    const manningCancel = $("svManningCancel");
    const manningRun = $("svManningRun");
    const manningFill = $("svManningFillBtn");
    const manningModalEl = manningModal();
    if (manningCancel) manningCancel.addEventListener("click", closeManningModal);
    if (manningRun) manningRun.addEventListener("click", saveManningAndRun);
    if (manningFill) manningFill.addEventListener("click", fillManningRange);
    if (manningModalEl) {
      manningModalEl.addEventListener("click", function (ev) {
        if (ev.target === manningModalEl) closeManningModal();
      });
    }
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (rrModalEl && !rrModalEl.hidden) {
        closeRrParamsModal();
        return;
      }
      if (manningModalEl && !manningModalEl.hidden) {
        closeManningModal();
        return;
      }
      const prog = progressModal();
      const closeBtn = $("hydro1dProgressClose");
      if (prog && !prog.hidden && closeBtn && !closeBtn.disabled) {
        closeProgressModal();
      }
    });
    const progressClose = $("hydro1dProgressClose");
    if (progressClose) progressClose.addEventListener("click", closeProgressModal);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireUi);
  } else {
    wireUi();
  }
})(window);

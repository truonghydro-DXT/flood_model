/**
 * Ban do ngap lut: WSE 1D Saint-Venant + DEM.
 */
(function (global) {
  "use strict";

  const API = (window.floodUrl ? window.floodUrl("/api/flood-3d") : "/api/flood-3d");
  const DRAPE_LIFT = 0.18;
  const FLOOD_RGB = [
    [198 / 255, 219 / 255, 239 / 255],
    [107 / 255, 174 / 255, 214 / 255],
    [33 / 255, 113 / 255, 181 / 255],
    [8 / 255, 69 / 255, 148 / 255],
    [4 / 255, 28 / 255, 58 / 255],
  ];

  const PLAY_MS = 100;
  const PLAY_STEP = 1;

  let overlay = null;
  let meshGroup = null;
  let last = null;
  let playTimer = null;
  let busy = false;
  let pendingTime = null;
  let drapeSeq = 0;
  let retryTimer = null;
  let drapeMesh = null;
  let drapeTex = null;
  let drapeCanvas = null;
  const frameCache = Object.create(null);
  let prefetchGen = 0;
  let playFetch = null;
  let floodBuilding = false;
  const floodProgressLog = [];
  let lastFloodSource = null;

  function $(id) {
    return document.getElementById(id);
  }

  function fileId() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (ctx && ctx.terrain && ctx.terrain.file_id) return ctx.terrain.file_id;
    if (global.Dem3D && global.Dem3D.pickFileIdFor3D) return global.Dem3D.pickFileIdFor3D();
    return null;
  }

  function selectedWaterSource() {
    return window.pickWaterSource ? window.pickWaterSource() : "saint-venant";
  }

  function sourceLabel(src) {
    return src === "saint-venant-1d" ? "Saint-venant-1D" : "Saint-venant";
  }

  function syncFloodSourceCache() {
    const src = selectedWaterSource();
    if (lastFloodSource && lastFloodSource !== src) {
      Object.keys(frameCache).forEach(function (k) { delete frameCache[k]; });
      last = null;
    }
    lastFloodSource = src;
    return src;
  }

  function setHint(text) {
    const el = $("dem3dHint");
    if (el) el.textContent = text || "";
  }

  function setPanel(show) {
    const panel = $("flood3dPanel");
    if (panel) panel.hidden = !show;
  }

  function setStats(text) {
    const el = $("flood3dStats");
    if (el) el.textContent = text;
  }

  function setLegend(show) {
    const el = $("flood3dLegend");
    if (el) el.hidden = !show;
  }

  function removeLeaflet() {
    if (overlay && global.map) {
      try {
        global.map.removeLayer(overlay);
      } catch (_e) { /* ignore */ }
      if (global.unregisterLeafletOverlay) global.unregisterLeafletOverlay(overlay);
    }
    overlay = null;
    removeDigitalTwinFlood();
  }

  function disposeMesh() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (meshGroup && ctx && ctx.scene) ctx.scene.remove(meshGroup);
    if (meshGroup) {
      meshGroup.traverse(function (obj) {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(function (m) {
            if (m.map) m.map.dispose();
            m.dispose();
          });
        }
      });
    }
    meshGroup = null;
    drapeMesh = null;
    drapeTex = null;
    drapeCanvas = null;
  }

  function addMesh(obj) {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (!ctx || !ctx.scene || !global.THREE) return;
    if (!meshGroup) {
      meshGroup = new THREE.Group();
      meshGroup.name = "flood3dWater";
      ctx.scene.add(meshGroup);
    }
    meshGroup.add(obj);
  }

  function drawLeaflet(data) {
    if (!global.L || !global.map || !data.png_b64 || !data.leaflet_bounds) return;
    const url = "data:image/png;base64," + data.png_b64;
    if (overlay) {
      overlay.setUrl(url);
      overlay.setBounds(data.leaflet_bounds);
    } else {
      overlay = global.L.imageOverlay(
        url,
        data.leaflet_bounds,
        { opacity: 0.78, pane: "overlayPane", interactive: false }
      );
      overlay.addTo(global.map);
      if (global.registerLeafletOverlay) global.registerLeafletOverlay(overlay, "Ngập lụt");
    }
    drawDigitalTwinFlood(data);
  }

  function drawDigitalTwinFlood(data) {
    const api = global.population3DSimulator;
    if (!api || typeof api.setFloodOverlay !== "function") return;
    api.setFloodOverlay(data);
  }

  function removeDigitalTwinFlood() {
    const api = global.population3DSimulator;
    if (api && typeof api.clearFloodOverlay === "function") api.clearFloodOverlay();
  }

  function demTerrainMesh() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    return ctx && ctx.mesh ? ctx.mesh : null;
  }

  function isDem3dOn() {
    const el = $("view3d");
    return !!(el && !el.hidden);
  }

  function depthRgb(depth) {
    let i = 0;
    if (depth >= 4) i = 4;
    else if (depth >= 2) i = 3;
    else if (depth >= 1) i = 2;
    else if (depth >= 0.5) i = 1;
    return FLOOD_RGB[i];
  }

  function gridSize(mesh) {
    const depths = mesh.depth || mesh.wse || mesh.lats || [];
    const nr = depths.length;
    const row = depths[0];
    const nc = Array.isArray(row) ? row.length : (mesh.lons && mesh.lons.length) || 0;
    return { nr: nr, nc: nc };
  }

  function sampleDemY(src, u, v) {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    const terrain = ctx && ctx.terrain;
    if (!src || !src.geometry || !terrain) return null;
    const pos = src.geometry.attributes.position;
    if (!pos) return null;
    const width = terrain.width;
    const height = terrain.height;
    const col = Math.min(width - 1, Math.max(0, Math.round(u * (width - 1))));
    const row = Math.min(height - 1, Math.max(0, Math.round(v * (height - 1))));
    const idx = row * width + col;
    if (idx < 0 || idx >= pos.count) return null;
    const y = pos.getY(idx);
    return isFinite(y) ? y : null;
  }

  function drawColoredMesh(data, src) {
    const mesh = data.mesh;
    if (!mesh || !global.Dem3D || !global.THREE) return false;
    const size = gridSize(mesh);
    const nr = size.nr;
    const nc = size.nc;
    if (nr < 2 || nc < 2) return false;
    const depths = mesh.depth || [];
    const zs = mesh.z || [];
    const wse = mesh.wse || [];
    const lons = mesh.lons || [];
    const lats = mesh.lats || [];
    const positions = [];
    const colors = [];
    const indices = [];
    const idxOf = [];
    let nVert = 0;

    for (let i = 0; i < nr; i++) {
      idxOf[i] = [];
      for (let j = 0; j < nc; j++) {
        const d = depths[i] && depths[i][j];
        const h = wse[i] && wse[i][j];
        if ((d == null || !isFinite(d)) && (h == null || !isFinite(h))) {
          idxOf[i][j] = -1;
          continue;
        }
        const u = (j + 0.5) / nc;
        const v = (i + 0.5) / nr;
        const p = (global.Dem3D.rasterUVToWorld)
          ? global.Dem3D.rasterUVToWorld(u, v)
          : global.Dem3D.lonLatToWorld(
            Array.isArray(lons[0]) ? lons[i][j] : lons[j],
            Array.isArray(lats[0]) ? lats[i][j] : lats[i]
          );
        let y = sampleDemY(src, u, v);
        if (y == null) {
          let elev = zs[i] && zs[i][j];
          const lon = Array.isArray(lons[0]) ? lons[i][j] : lons[j];
          const lat = Array.isArray(lats[0]) ? lats[i][j] : lats[i];
          if ((elev == null || !isFinite(elev)) && lon != null && lat != null && global.Dem3D.sampleElevation) {
            elev = global.Dem3D.sampleElevation(lon, lat);
          }
          if (elev == null || !isFinite(elev)) {
            idxOf[i][j] = -1;
            continue;
          }
          y = global.Dem3D.elevToWorldY(elev);
        }
        const rgb = depthRgb(d != null && isFinite(d) ? d : 0.2);
        positions.push(p.x, y + DRAPE_LIFT, p.z);
        colors.push(rgb[0], rgb[1], rgb[2]);
        idxOf[i][j] = nVert++;
      }
    }
    for (let i = 0; i < nr - 1; i++) {
      for (let j = 0; j < nc - 1; j++) {
        const a = idxOf[i][j];
        const b = idxOf[i][j + 1];
        const c = idxOf[i + 1][j];
        const d = idxOf[i + 1][j + 1];
        if (a < 0 || b < 0 || c < 0) continue;
        indices.push(a, c, b);
        if (d >= 0) indices.push(b, c, d);
      }
    }
    if (!indices.length) return false;
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    geo.setIndex(indices);
    geo.computeVertexNormals();
    const mat = new THREE.MeshBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.82,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2,
    });
    const obj = new THREE.Mesh(geo, mat);
    obj.renderOrder = 21;
    addMesh(obj);
    return true;
  }

  function drapePngOnTerrain(data, src) {
    if (!data.png_b64 || !src || !src.geometry) return;
    const seq = ++drapeSeq;
    const img = new Image();
    let applied = false;
    const apply = function () {
      if (applied || seq !== drapeSeq) return;
      applied = true;
      if (!drapeCanvas) drapeCanvas = document.createElement("canvas");
      if (drapeCanvas.width !== img.width || drapeCanvas.height !== img.height) {
        drapeCanvas.width = img.width || 1;
        drapeCanvas.height = img.height || 1;
      }
      const c2 = drapeCanvas.getContext("2d");
      c2.clearRect(0, 0, drapeCanvas.width, drapeCanvas.height);
      c2.drawImage(img, 0, 0);
      if (drapeTex && drapeMesh) {
        drapeTex.needsUpdate = true;
        return;
      }
      const geo = src.geometry.clone();
      if (geo.deleteAttribute) geo.deleteAttribute("color");
      const pos = geo.attributes.position;
      if (pos) {
        for (let i = 0; i < pos.count; i++) {
          pos.setY(i, pos.getY(i) + DRAPE_LIFT * 0.45);
        }
        pos.needsUpdate = true;
      }
      drapeTex = new THREE.CanvasTexture(drapeCanvas);
      drapeTex.flipY = true;
      drapeTex.generateMipmaps = false;
      drapeTex.minFilter = THREE.NearestFilter;
      drapeTex.magFilter = THREE.NearestFilter;
      drapeTex.wrapS = THREE.ClampToEdgeWrapping;
      drapeTex.wrapT = THREE.ClampToEdgeWrapping;
      drapeTex.needsUpdate = true;
      if (THREE.sRGBEncoding) drapeTex.encoding = THREE.sRGBEncoding;
      const mat = new THREE.MeshBasicMaterial({
        map: drapeTex,
        transparent: true,
        opacity: 0.88,
        alphaTest: 0.04,
        depthWrite: false,
        side: THREE.DoubleSide,
        polygonOffset: true,
        polygonOffsetFactor: -4,
        polygonOffsetUnits: -4,
      });
      drapeMesh = new THREE.Mesh(geo, mat);
      drapeMesh.renderOrder = 23;
      addMesh(drapeMesh);
    };
    img.onload = apply;
    img.src = "data:image/png;base64," + data.png_b64;
    if (img.complete && img.naturalWidth) apply();
  }

  function draw3d(data, light) {
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (!global.THREE || !data) return;
    const src = demTerrainMesh();
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (!src || !src.geometry) {
      if (isDem3dOn() || (ctx && ctx.scene)) {
        retryTimer = setTimeout(function () {
          if (last) draw3d(last, light);
        }, 280);
      }
      return;
    }
    if (light && drapeMesh && drapeTex) {
      drapePngOnTerrain(data, src);
      return;
    }
    if (!light) disposeMesh();
    if (!drapeMesh) drawColoredMesh(data, src);
    drapePngOnTerrain(data, src);
  }

  function rememberFrame(data) {
    if (!data || data.time_index == null) return;
    frameCache[data.time_index] = data;
  }

  function applyResult(data, light) {
    last = data;
    rememberFrame(data);
    setStats("");
    const slider = $("flood3dTime");
    const label = $("flood3dTimeLabel");
    if (slider && data.n_times) {
      slider.max = String(Math.max(0, data.n_times - 1));
      slider.value = String(data.time_index || 0);
    }
    if (label) label.textContent = "Giờ " + data.hour;
    drawLeaflet(data);
    draw3d(data, !!light);
    setLegend(true);
    setPanel(true);
    const mapBtn = $("flood3dMapBtn");
    if (mapBtn) mapBtn.classList.add("is-active");
  }

  async function fetchFrame(timeIndex, includeMesh) {
    if (timeIndex != null && frameCache[timeIndex] && !includeMesh) {
      return frameCache[timeIndex];
    }
    const res = await fetch(API + "/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: fileId(),
        time_index: timeIndex,
        include_mesh: !!includeMesh,
        water_source: selectedWaterSource()
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Loi ngap lut");
    rememberFrame(data);
    return data;
  }

  function prefetchFrames(nTimes, startAt) {
    const gen = ++prefetchGen;
    const n = Math.max(0, nTimes || 0);
    const begin = Math.max(0, startAt || 0);
    (async function () {
      for (let k = 0; k < n; k++) {
        if (gen !== prefetchGen) return;
        const t = n ? (begin + k) % n : 0;
        if (frameCache[t]) continue;
        try {
          await fetchFrame(t, false);
        } catch (_e) {
          return;
        }
      }
    })();
  }

  function setFloodBusy(on) {
    ["flood3dRunBtn", "flood3dMapBtn"].forEach(function (id) {
      const el = $(id);
      if (el) el.disabled = !!on;
    });
  }

  function cacheReady(nTimes) {
    const n = nTimes || (last && last.n_times) || 0;
    if (!n) return false;
    for (let i = 0; i < n; i++) {
      if (!frameCache[i]) return false;
    }
    return true;
  }

  function openFloodProgress() {
    const modal = $("flood3dProgressModal");
    if (!modal) return;
    floodProgressLog.length = 0;
    modal.hidden = false;
    const closeBtn = $("flood3dProgressClose");
    if (closeBtn) closeBtn.disabled = true;
    const bar = $("flood3dProgressBar");
    if (bar) bar.style.width = "0%";
    updateFloodProgress({
      status: "running",
      message: "Đang tính chuỗi ngập lụt…",
      current: 0,
      total: 0
    });
  }

  function closeFloodProgress() {
    const modal = $("flood3dProgressModal");
    if (modal) modal.hidden = true;
  }

  function updateFloodProgress(info) {
    const modal = $("flood3dProgressModal");
    if (!modal || modal.hidden) return;
    const statusEl = $("flood3dProgressStatus");
    const metaEl = $("flood3dProgressMeta");
    const logEl = $("flood3dProgressLog");
    const bar = $("flood3dProgressBar");
    const closeBtn = $("flood3dProgressClose");
    const st = info.status || "running";
    if (statusEl) {
      statusEl.className = "hydro1d-progress-status is-" + (st === "ok" || st === "error" ? st : "running");
      if (st === "ok") statusEl.textContent = "Hoàn thành";
      else if (st === "error") statusEl.textContent = "Lỗi";
      else statusEl.textContent = "Đang tính…";
    }
    if (metaEl) {
      const label = sourceLabel(selectedWaterSource());
      if (info.total) {
        metaEl.textContent = label + " · khung " + (info.current || 0) + " / " + info.total +
          (info.hour != null ? (" · giờ " + info.hour) : "");
      } else {
        metaEl.textContent = label;
      }
    }
    if (bar && info.total) {
      bar.style.width = Math.round(100 * (info.current || 0) / info.total) + "%";
    }
    if (info.line) {
      floodProgressLog.push(info.line);
      if (floodProgressLog.length > 40) floodProgressLog.shift();
    }
    if (logEl) {
      const text = floodProgressLog.length ? floodProgressLog.join("\n") : (info.message || "");
      if (logEl.textContent !== text) {
        logEl.textContent = text;
        logEl.scrollTop = logEl.scrollHeight;
      }
    }
    if (closeBtn) closeBtn.disabled = st === "running";
  }

  async function runAt(timeIndex, includeMesh) {
    if (floodBuilding) return;
    if (busy) {
      pendingTime = timeIndex;
      return;
    }
    if (timeIndex != null && frameCache[timeIndex] && !includeMesh) {
      applyResult(frameCache[timeIndex], true);
      return;
    }
    busy = true;
    setStats("Đang tính WSE − DEM...");
    setPanel(true);
    try {
      const data = await fetchFrame(timeIndex, includeMesh !== false);
      applyResult(data, false);
    } catch (err) {
      setStats(String(err.message || err));
      setPanel(true);
    } finally {
      busy = false;
      if (pendingTime != null) {
        const t = pendingTime;
        pendingTime = null;
        runAt(t, false);
      }
    }
  }

  function stopPlay() {
    if (playTimer) {
      clearInterval(playTimer);
      playTimer = null;
    }
    const play = $("flood3dPlayBtn");
    if (play) play.textContent = "Phát";
  }

  function startPlay() {
    const play = $("flood3dPlayBtn");
    const s = $("flood3dTime");
    if (!s) return;
    if (playTimer) return;
    if (play) play.textContent = "Dừng";
    if (last && last.n_times && !cacheReady(last.n_times)) {
      prefetchFrames(last.n_times, last.time_index || 0);
    }
    playTimer = setInterval(function () {
      const slider = $("flood3dTime");
      if (!slider) return;
      const max = parseInt(slider.max, 10) || 167;
      let next = (parseInt(slider.value, 10) || 0) + PLAY_STEP;
      if (next > max) next = 0;
      const cached = frameCache[next];
      if (!cached) {
        if (!playFetch) {
          playFetch = fetchFrame(next, false).then(function (data) {
            if (!playTimer) return;
            slider.value = String(next);
            applyResult(data, true);
          }).catch(function () { /* hold */ }).finally(function () {
            playFetch = null;
          });
        }
        return;
      }
      slider.value = String(next);
      applyResult(cached, true);
    }, PLAY_MS);
  }

  async function computeSequenceThenPlay() {
    if (floodBuilding) return;
    floodBuilding = true;
    busy = true;
    prefetchGen += 1;
    const gen = prefetchGen;
    stopPlay();
    setFloodBusy(true);
    setHint("");
    setPanel(true);
    setStats("Đang tính chuỗi ngập lụt…");
    openFloodProgress();
    try {
      const src = syncFloodSourceCache();
      const metaRes = await fetch(
        API + "/meta?water_source=" + encodeURIComponent(src),
        { headers: { "Accept": "application/json" } }
      );
      const meta = await metaRes.json();
      if (!meta.ok) throw new Error(meta.error || "Không đọc được chuỗi ngập.");
      const n = Math.max(1, parseInt(meta.n_times, 10) || 0);
      const hours = meta.hours || [];
      const nBr = parseInt(meta.n_branches, 10) || 0;
      updateFloodProgress({
        status: "running",
        current: 0,
        total: n,
        line: "Bắt đầu tính " + n + " khung (" + sourceLabel(selectedWaterSource()) +
          (nBr ? (", " + nBr + " sông nhánh") : "") + ")"
      });
      for (let t = 0; t < n; t++) {
        if (gen !== prefetchGen) return;
        const hour = hours[t] != null ? hours[t] : t;
        updateFloodProgress({
          status: "running",
          current: t,
          total: n,
          hour: hour,
          line: "Khung " + (t + 1) + "/" + n + " · giờ " + hour
        });
        const data = await fetchFrame(t, t === 0);
        if (t === 0) applyResult(data, false);
        updateFloodProgress({
          status: "running",
          current: t + 1,
          total: n,
          hour: hour
        });
      }
      if (gen !== prefetchGen) return;
      updateFloodProgress({
        status: "ok",
        current: n,
        total: n,
        line: "Xong " + n + " khung. Đang phát video."
      });
      if (frameCache[0]) applyResult(frameCache[0], true);
      const slider = $("flood3dTime");
      if (slider) slider.value = "0";
      closeFloodProgress();
      startPlay();
    } catch (err) {
      updateFloodProgress({
        status: "error",
        message: err.message || String(err),
        line: err.message || String(err)
      });
      setStats(String(err.message || err));
    } finally {
      floodBuilding = false;
      busy = false;
      setFloodBusy(false);
    }
  }

  function startFlood() {
    if (floodBuilding) return;
    syncFloodSourceCache();
    if (cacheReady()) {
      const slider = $("flood3dTime");
      if (slider) slider.value = "0";
      if (frameCache[0]) applyResult(frameCache[0], true);
      stopPlay();
      startPlay();
      return;
    }
    computeSequenceThenPlay();
  }

  function clearFlood() {
    prefetchGen += 1;
    stopPlay();
    closeFloodProgress();
    removeLeaflet();
    disposeMesh();
    last = null;
    lastFloodSource = null;
    Object.keys(frameCache).forEach(function (k) { delete frameCache[k]; });
    setLegend(false);
    setPanel(false);
    setHint("");
    const mapBtn = $("flood3dMapBtn");
    if (mapBtn) mapBtn.classList.remove("is-active");
  }

  function bind() {
    function start() {
      startFlood();
    }
    ["flood3dRunBtn", "flood3dMapBtn"].forEach(function (id) {
      const el = $(id);
      if (el) el.addEventListener("click", start);
    });
    const clearBtn = $("flood3dClearBtn");
    if (clearBtn) clearBtn.addEventListener("click", clearFlood);
    const closeBtn = $("flood3dCloseBtn");
    if (closeBtn) closeBtn.addEventListener("click", function () { setPanel(false); });
    const slider = $("flood3dTime");
    if (slider) {
      slider.addEventListener("input", function () {
        const t = parseInt(slider.value, 10) || 0;
        if (frameCache[t]) applyResult(frameCache[t], true);
      });
      slider.addEventListener("change", function () {
        runAt(parseInt(slider.value, 10) || 0, false);
      });
    }
    const play = $("flood3dPlayBtn");
    if (play) {
      play.addEventListener("click", function () {
        if (playTimer) {
          stopPlay();
          return;
        }
        startPlay();
      });
    }
    const progressClose = $("flood3dProgressClose");
    if (progressClose) progressClose.addEventListener("click", closeFloodProgress);
    document.querySelectorAll('input[name="dtSolver"], input[name="svSolver"]').forEach(function (el) {
      el.addEventListener("change", function () {
        const mapBtn = $("flood3dMapBtn");
        if (!last && !(mapBtn && mapBtn.classList.contains("is-active"))) return;
        startFlood();
      });
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      const modal = $("flood3dProgressModal");
      const btn = $("flood3dProgressClose");
      if (modal && !modal.hidden && btn && !btn.disabled) closeFloodProgress();
    });
  }

  global.Flood3D = {
    teardown: function () {
      disposeMesh();
    },
    clear: clearFlood,
    start: startFlood,
    invalidate: function () {
      prefetchGen += 1;
      Object.keys(frameCache).forEach(function (k) { delete frameCache[k]; });
      last = null;
    },
    onTerrainReady: function () {
      if (last) draw3d(last, false);
    },
    redrawOverlay: function () {
      if (last) draw3d(last, false);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})(window);

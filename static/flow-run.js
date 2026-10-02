/**
 * Khoi nuoc 3D bam mat cat DEM: day theo dia hinh, mat thoang = H Saint-Venant.
 */
(function (global) {
  "use strict";

  const API = (window.floodUrl ? window.floodUrl("/api/flow-run") : "/api/flow-run");
  const WATER = 0x1d6fe8;
  const PLAY_MS = 80;
  const MIN_PER_HOUR = 60;
  const RAIN_STEP_MIN_DEFAULT = 15;
  const MIN_DEPTH = 0.05;
  const N_ACROSS = 97;
  const N_ACROSS_MAX = 281;
  const N_ALONG_MAX = 1600;
  const MAIN_HALF_WIDTH_MIN_M = 1500;
  const BED_LIFT = 0.004;
  const FLOW_ARROW_SPACING_M = 1500;
  const FLOW_ARROW_LEN_M = 90;
  const FLOW_ARROW_COLOR = 0x111111;
  const RAIN_MAX = 2200;
  const RAIN_FALL_S = 3.4;
  const RAIN_COLOR = 0x0b4f9c;
  const RAIN_DROP_MIN = 0.55;
  const RAIN_DROP_MAX = 2.85;

  let payload = null;
  let overlayGroup = null;
  let volumeMesh = null;
  let volumePos = null;
  let volumeGeo = null;
  let reservoirLive = [];
  let surfaceMesh = null;
  let surfaceGeo = null;
  let flowArrows = [];
  let channel = null;
  let reaches = [];
  let activeHGrid = null;
  let nAcross = N_ACROSS;
  let elevGrid = null;
  let liveVerts = null;
  let timeIndex = 0;
  let timeFrac = 0;
  let playing = false;
  let hydroTimer = null;
  let busy = false;
  let rainPoints = null;
  let rainPos = null;
  let rainAlive = null;
  let rainGround = null;
  let rainScale = null;
  let rainJitter = null;
  let rainDummy = null;
  let rainRaf = null;
  let rainLastT = 0;
  let showRain = true;
  let showFlow = true;
  let reachKind = "main";

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
    const el = $("dem3dHint");
    if (el) el.textContent = text || "";
  }

  function applyLayerVisibility() {
    if (!overlayGroup) return;
    overlayGroup.traverse(function (obj) {
      const name = obj && obj.name;
      if (name === "flowRunRain") {
        obj.visible = !!showRain;
      } else if (name === "flowRunVolume" || name === "flowRunSurface") {
        obj.visible = !!showFlow;
      } else if (name === "flowRunArrow") {
        if (!showFlow) obj.visible = false;
      }
    });
  }

  function setPanel(show) {
    const panel = $("flowRunPanel");
    if (panel) panel.hidden = !show;
  }

  function fileId() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (ctx && ctx.terrain && ctx.terrain.file_id) return ctx.terrain.file_id;
    if (global.Dem3D && global.Dem3D.pickFileIdFor3D) return global.Dem3D.pickFileIdFor3D();
    return null;
  }

  function num(v, fallback) {
    const n = Number(v);
    return isFinite(n) ? n : fallback;
  }

  function gaussSmooth(arr, sigma) {
    const n = arr.length;
    if (n < 3 || !(sigma > 0)) return arr.slice();
    const out = new Array(n);
    const r = Math.max(1, Math.ceil(sigma * 3));
    const twoS2 = 2 * sigma * sigma;
    for (let i = 0; i < n; i++) {
      let s = 0;
      let w = 0;
      for (let k = -r; k <= r; k++) {
        const j = i + k;
        if (j < 0 || j >= n) continue;
        const v = arr[j];
        if (v == null || !isFinite(v)) continue;
        const ww = Math.exp(-(k * k) / twoS2);
        s += v * ww;
        w += ww;
      }
      out[i] = w > 0 ? s / w : arr[i];
    }
    return out;
  }

  function terrain() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    return ctx && ctx.terrain;
  }

  function xzPoint(lon, lat) {
    return global.Dem3D.lonLatToWorld(lon, lat);
  }

  function yElev(z) {
    return global.Dem3D.elevToWorldY(z);
  }

  function metersToWorld(m, lon, lat) {
    const a = xzPoint(lon, lat);
    const b = xzPoint(lon + 0.001, lat);
    const worldPerDeg = Math.hypot(b.x - a.x, b.z - a.z) / 0.001;
    const mPerDeg = 111320 * Math.cos((lat * Math.PI) / 180);
    if (!isFinite(worldPerDeg) || worldPerDeg < 1e-8 || !isFinite(mPerDeg) || mPerDeg < 1) {
      return m * 0.008;
    }
    return m * (worldPerDeg / mPerDeg);
  }

  function ensureElevGrid() {
    const t = terrain();
    if (!t) return null;
    if (elevGrid && elevGrid.key === t.elevations_b64) return elevGrid;
    const w = t.width;
    const h = t.height;
    const raw = atob(t.elevations_b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const elev = new Float32Array(bytes.buffer, bytes.byteOffset, (bytes.byteLength / 4) | 0);
    let mask = null;
    if (t.mask_b64) {
      const mr = atob(t.mask_b64);
      mask = new Uint8Array(mr.length);
      for (let i = 0; i < mr.length; i++) mask[i] = mr.charCodeAt(i);
    }
    elevGrid = { key: t.elevations_b64, w: w, h: h, elev: elev, mask: mask, t: t };
    return elevGrid;
  }

  function meshLayout() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    const t = ctx && ctx.terrain;
    const mesh = ctx && ctx.mesh;
    if (!t || !global.Dem3D.rasterUVToWorld) return null;
    const w = t.width;
    const h = t.height;
    const pos = mesh && mesh.geometry && mesh.geometry.attributes && mesh.geometry.attributes.position;
    if (pos && pos.count >= w * h) {
      return {
        w: w,
        h: h,
        x0: pos.getX(0),
        x1: pos.getX(w - 1),
        z0: pos.getZ(0),
        z1: pos.getZ((h - 1) * w),
        pos: pos
      };
    }
    const p0 = global.Dem3D.rasterUVToWorld(0, 0);
    const p1 = global.Dem3D.rasterUVToWorld(1, 1);
    return { w: w, h: h, x0: p0.x, x1: p1.x, z0: p0.z, z1: p1.z, pos: null };
  }

  function bilinear4(v00, v10, v01, v11, u, v) {
    let sum = 0;
    let wsum = 0;
    function acc(val, ww) {
      if (!isFinite(val) || !(ww > 0)) return;
      sum += val * ww;
      wsum += ww;
    }
    acc(v00, (1 - u) * (1 - v));
    acc(v10, u * (1 - v));
    acc(v01, (1 - u) * v);
    acc(v11, u * v);
    return wsum > 1e-9 ? sum / wsum : v00;
  }

  /** Cao do DEM + Y mesh tai (x,z) the gioi — cung UV voi mat dia hinh 3D. */
  function sampleTerrainXZ(wx, wz) {
    const g = ensureElevGrid();
    const layout = meshLayout();
    if (!g || !layout || layout.x1 === layout.x0 || layout.z1 === layout.z0) {
      return { zDem: NaN, yBed: NaN };
    }
    let col = ((wx - layout.x0) / (layout.x1 - layout.x0)) * (layout.w - 1);
    let row = ((wz - layout.z0) / (layout.z1 - layout.z0)) * (layout.h - 1);
    col = Math.max(0, Math.min(layout.w - 1, col));
    row = Math.max(0, Math.min(layout.h - 1, row));
    const c0 = Math.floor(col);
    const r0 = Math.floor(row);
    const c1 = Math.min(layout.w - 1, c0 + 1);
    const r1 = Math.min(layout.h - 1, r0 + 1);
    const u = col - c0;
    const v = row - r0;
    function zAt(r, c) {
      const i = r * layout.w + c;
      if (g.mask && !g.mask[i]) return NaN;
      return g.elev[i];
    }
    const zDem = bilinear4(zAt(r0, c0), zAt(r0, c1), zAt(r1, c0), zAt(r1, c1), u, v);
    let yBed;
    if (layout.pos) {
      function yAt(r, c) {
        if (g.mask && !g.mask[r * layout.w + c]) return NaN;
        return layout.pos.getY(r * layout.w + c);
      }
      yBed = bilinear4(yAt(r0, c0), yAt(r0, c1), yAt(r1, c0), yAt(r1, c1), u, v);
    }
    if (!isFinite(yBed) && isFinite(zDem)) yBed = yElev(zDem);
    return { zDem: zDem, yBed: yBed };
  }

  /** Bam dung dinh mesh DEM — day nuoc trung khop tam giac dia hinh. */
  function nearestVertex(wx, wz) {
    const g = ensureElevGrid();
    const layout = meshLayout();
    if (!g || !layout || layout.x1 === layout.x0 || layout.z1 === layout.z0) {
      return { x: wx, z: wz, zDem: NaN, yBed: NaN };
    }
    let col = Math.round(((wx - layout.x0) / (layout.x1 - layout.x0)) * (layout.w - 1));
    let row = Math.round(((wz - layout.z0) / (layout.z1 - layout.z0)) * (layout.h - 1));
    col = Math.max(0, Math.min(layout.w - 1, col));
    row = Math.max(0, Math.min(layout.h - 1, row));
    function at(r, c) {
      if (r < 0 || c < 0 || r >= layout.h || c >= layout.w) return null;
      const i = r * layout.w + c;
      if (g.mask && !g.mask[i]) return null;
      const zDem = g.elev[i];
      if (!isFinite(zDem)) return null;
      let x = wx;
      let z = wz;
      let yBed;
      if (layout.pos) {
        x = layout.pos.getX(i);
        z = layout.pos.getZ(i);
        yBed = layout.pos.getY(i);
      }
      if (!isFinite(yBed)) yBed = yElev(zDem);
      return { x: x, z: z, zDem: zDem, yBed: yBed };
    }
    let s = at(row, col);
    if (s) return s;
    for (let d = 1; d <= 3; d++) {
      for (let dr = -d; dr <= d; dr++) {
        for (let dc = -d; dc <= d; dc++) {
          if (Math.abs(dr) !== d && Math.abs(dc) !== d) continue;
          s = at(row + dr, col + dc);
          if (s) return s;
        }
      }
    }
    return { x: wx, z: wz, zDem: NaN, yBed: NaN };
  }

  function hAtIndex(i, t) {
    const grid = activeHGrid || (payload && payload.h_wse_m);
    if (!grid || !grid.length) return null;
    const nSta = (grid[0] || []).length;
    if (!nSta) return null;
    const t0 = Math.max(0, Math.min(grid.length - 1, Math.floor(t)));
    const t1 = Math.max(0, Math.min(grid.length - 1, t0 + 1));
    const f = Math.max(0, Math.min(1, t - t0));
    const i0 = Math.max(0, Math.min(nSta - 1, Math.floor(i)));
    const i1 = Math.max(0, Math.min(nSta - 1, i0 + 1));
    const u = Math.max(0, Math.min(1, i - i0));
    function cell(tt, ii) {
      const v = grid[tt] && grid[tt][ii];
      return v == null ? null : Number(v);
    }
    function along(tt) {
      const a = cell(tt, i0);
      const b = cell(tt, i1);
      if (a == null || !isFinite(a)) return null;
      if (b == null || !isFinite(b) || i1 === i0) return a;
      return a + (b - a) * u;
    }
    const ha = along(t0);
    const hb = along(t1);
    if (ha == null) return null;
    if (hb == null || t1 === t0) return ha;
    return ha + (hb - ha) * f;
  }

  function hAt(i, t) {
    return hAtIndex(i, t);
  }

  function disposeOverlay() {
    stopHydro();
    stopRain();
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (overlayGroup && ctx && ctx.scene) ctx.scene.remove(overlayGroup);
    if (overlayGroup) {
      overlayGroup.traverse(function (obj) {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(function (m) { m.dispose(); });
        }
      });
    }
    overlayGroup = null;
    volumeMesh = null;
    volumePos = null;
    volumeGeo = null;
    reservoirLive = [];
    surfaceMesh = null;
    surfaceGeo = null;
    flowArrows = [];
    channel = null;
    reaches = [];
    activeHGrid = null;
    liveVerts = null;
    rainPoints = null;
    rainPos = null;
    rainAlive = null;
    rainGround = null;
    rainScale = null;
    rainJitter = null;
  }

  function addOverlay(obj) {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (!ctx || !ctx.scene) return;
    if (!overlayGroup) {
      overlayGroup = new THREE.Group();
      overlayGroup.name = "flowRunOverlay";
      overlayGroup.renderOrder = 22;
      ctx.scene.add(overlayGroup);
    }
    overlayGroup.add(obj);
  }

  function pixelWorld() {
    const layout = meshLayout();
    if (layout && layout.w > 1) {
      return Math.max(1e-4, Math.abs(layout.x1 - layout.x0) / (layout.w - 1));
    }
    return 0.06;
  }

  const TRIB_BANK_Z = 7.85;

  function tribHalfM(p) {
    const s = p.station != null ? Number(p.station) : 0;
    if (s < 200) return 130;
    if (s < 420) return 190;
    return 250;
  }

  function isOfftakePt(p) {
    if (!p || reachKind !== "trib") return false;
    return (p.station != null ? Number(p.station) : 0) < 700;
  }

  function xsHalfW(p) {
    const halfM = reachKind === "trib"
      ? tribHalfM(p)
      : Math.min(Math.max(p.width * 1.5, MAIN_HALF_WIDTH_MIN_M), 2500);
    return metersToWorld(halfM, p.lon, p.lat);
  }

  function pickNAcross(pts) {
    const pix = pixelWorld();
    let halfW = 0;
    for (let i = 0; i < pts.length; i++) halfW = Math.max(halfW, xsHalfW(pts[i]));
    let n = Math.round((2 * halfW) / Math.max(pix, 0.02)) + 1;
    if (n % 2 === 0) n += 1;
    return Math.max(97, Math.min(N_ACROSS_MAX, n));
  }

  function thalwegOffset(p, halfW, pix) {
    if (reachKind === "trib") return 0;
    const nMax = Math.max(6, Math.round(halfW / Math.max(pix, 1e-4)));
    let bestO = 0;
    let bestZ = Infinity;
    for (let k = -nMax; k <= nMax; k++) {
      const o = k * pix;
      const s = nearestVertex(p.x + p.nx * o, p.z + p.nz * o);
      if (!isFinite(s.zDem)) continue;
      if (s.zDem < bestZ) {
        bestZ = s.zDem;
        bestO = o;
      }
    }
    return bestO;
  }

  function sampleXs(p) {
    const halfW = xsHalfW(p);
    const ox = p.ox != null ? p.ox : p.x;
    const oz = p.oz != null ? p.oz : p.z;
    const xs = [];
    const mid = (nAcross - 1) / 2;
    const trib = reachKind === "trib";
    for (let j = 0; j < nAcross; j++) {
      const u = nAcross === 1 ? 0 : (j - mid) / mid;
      const dens = trib || u === 0 ? u : Math.sign(u) * Math.pow(Math.abs(u), 1.45);
      const o = dens * halfW;
      const sx = ox + p.nx * o;
      const sz = oz + p.nz * o;
      const line = sampleTerrainXZ(sx, sz);
      const snap = nearestVertex(sx, sz);
      let zDem = line.zDem;
      if (!isFinite(zDem)) zDem = snap.zDem;
      if (!isFinite(zDem)) zDem = p.zDem;
      let yBed = line.yBed;
      if (!isFinite(yBed)) yBed = snap.yBed;
      if (!isFinite(yBed)) yBed = yElev(zDem);
      xs.push({
        x: trib || !isFinite(snap.x) ? sx : snap.x,
        z: trib || !isFinite(snap.z) ? sz : snap.z,
        sx: sx,
        sz: sz,
        zDem: zDem,
        yBed: yBed
      });
    }
    return xs;
  }

  function lerpChannelPt(a, b, f) {
    return {
      i: a.i + (b.i - a.i) * f,
      lon: a.lon + (b.lon - a.lon) * f,
      lat: a.lat + (b.lat - a.lat) * f,
      station: (a.station != null && b.station != null)
        ? a.station + (b.station - a.station) * f
        : (a.station != null ? a.station : 0),
      zDem: a.zDem + (b.zDem - a.zDem) * f,
      zBed: a.zBed + (b.zBed - a.zBed) * f,
      width: a.width + (b.width - a.width) * f,
      x: a.x + (b.x - a.x) * f,
      z: a.z + (b.z - a.z) * f
    };
  }

  function densifyPts(pts) {
    if (pts.length < 2) return pts;
    const layout = meshLayout();
    let pix = 0.1;
    if (layout && layout.w > 1) {
      pix = Math.abs(layout.x1 - layout.x0) / (layout.w - 1);
    }
    const maxSeg = Math.max(pix * 0.95, 0.04);
    let est = 1;
    for (let i = 0; i < pts.length - 1; i++) {
      const dist = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].z - pts[i].z);
      est += Math.max(1, Math.ceil(dist / maxSeg));
    }
    const step = est > N_ALONG_MAX ? maxSeg * (est / N_ALONG_MAX) : maxSeg;
    const out = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i];
      const b = pts[i + 1];
      const dist = Math.hypot(b.x - a.x, b.z - a.z);
      const n = Math.max(1, Math.ceil(dist / step));
      for (let k = 0; k < n; k++) out.push(lerpChannelPt(a, b, k / n));
    }
    out.push(pts[pts.length - 1]);
    return out;
  }

  function buildChannel(src) {
    ensureElevGrid();
    const lon = src.lon || [];
    const lat = src.lat || [];
    const zDem = src.elev || [];
    const zBed = src.z_bed || zDem;
    const width = src.width_m || [];
    const sta = src.station_m || [];
    const raw = [];
    for (let i = 0; i < lon.length; i++) {
      if (lon[i] == null || lat[i] == null) continue;
      const xz = xzPoint(lon[i], lat[i]);
      raw.push({
        i: i,
        lon: lon[i],
        lat: lat[i],
        station: num(sta[i], 0),
        zDem: num(zDem[i], 0),
        zBed: num(zBed[i], zDem[i]),
        width: num(width[i], 200),
        x: xz.x,
        z: xz.z
      });
    }
    const pts = densifyPts(raw);
    nAcross = pickNAcross(pts);
    const n = pts.length;
    const txA = new Array(n);
    const tzA = new Array(n);
    for (let i = 0; i < n; i++) {
      let tx = 0;
      let tz = 1;
      if (i < n - 1) {
        tx = pts[i + 1].x - pts[i].x;
        tz = pts[i + 1].z - pts[i].z;
      } else if (i > 0) {
        tx = pts[i].x - pts[i - 1].x;
        tz = pts[i].z - pts[i - 1].z;
      }
      txA[i] = tx;
      tzA[i] = tz;
    }
    const stx = gaussSmooth(txA, 8);
    const stz = gaussSmooth(tzA, 8);
    const pix = pixelWorld();
    const off = new Array(n);
    for (let i = 0; i < n; i++) {
      const len = Math.hypot(stx[i], stz[i]) || 1;
      pts[i].nx = -stz[i] / len;
      pts[i].nz = stx[i] / len;
      pts[i].fx = stx[i] / len;
      pts[i].fz = stz[i] / len;
      off[i] = thalwegOffset(pts[i], xsHalfW(pts[i]), pix);
    }
    const soff = gaussSmooth(off, 4);
    for (let i = 0; i < n; i++) {
      const o = isFinite(soff[i]) ? soff[i] : off[i];
      pts[i].ox = pts[i].x + pts[i].nx * o;
      pts[i].oz = pts[i].z + pts[i].nz * o;
      pts[i].xs = sampleXs(pts[i]);
    }
    return pts;
  }

  function hFromOfficialXs(stationM, t) {
    const xs = payload && payload.xs;
    if (!xs || !xs.station_m || !xs.h_m || !xs.h_m.length) return null;
    const st = xs.station_m;
    const n = st.length;
    if (!n) return null;
    const t0 = Math.max(0, Math.min(xs.h_m.length - 1, Math.floor(t)));
    const t1 = Math.max(0, Math.min(xs.h_m.length - 1, t0 + 1));
    const f = Math.max(0, Math.min(1, t - t0));
    let k = 0;
    let best = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(Number(st[i]) - stationM);
      if (d < best) {
        best = d;
        k = i;
      }
    }
    const spacing = n > 1 ? Math.abs(Number(st[1]) - Number(st[0])) : 500;
    const tol = Math.min(200, 0.2 * Math.max(spacing, 1));
    function atXs(tt, ii) {
      const v = xs.h_m[tt] && xs.h_m[tt][ii];
      return v == null ? null : Number(v);
    }
    if (best <= tol) {
      const a = atXs(t0, k);
      const b = atXs(t1, k);
      if (a == null) return b;
      if (b == null || t1 === t0) return a;
      return a + (b - a) * f;
    }
    let i0 = k;
    if (k > 0 && Number(st[k]) > stationM) i0 = k - 1;
    const i1 = Math.min(n - 1, i0 + 1);
    const s0 = Number(st[i0]);
    const s1 = Number(st[i1]);
    const u = (i1 === i0 || s1 === s0) ? 0 : (stationM - s0) / (s1 - s0);
    function along(tt) {
      const a = atXs(tt, i0);
      const b = atXs(tt, i1);
      if (a == null) return b;
      if (b == null || i1 === i0) return a;
      return a + (b - a) * u;
    }
    const ha = along(t0);
    const hb = along(t1);
    if (ha == null) return hb;
    if (hb == null || t1 === t0) return ha;
    return ha + (hb - ha) * f;
  }

  function hSta(i) {
    const p = channel[i];
    return hAt(p.i, timeFrac);
  }

  function isWet(zDem, h, loose) {
    if (h == null || !isFinite(h) || !isFinite(zDem)) return false;
    if (h > zDem + MIN_DEPTH) return true;
    return !!(loose && zDem < h + 2.6);
  }

  function mergeWetRuns(xs, h, runs) {
    if (runs.length < 2) return runs;
    const out = [{ L: runs[0].L, R: runs[0].R }];
    for (let i = 1; i < runs.length; i++) {
      const prev = out[out.length - 1];
      const cur = runs[i];
      const gap = cur.L - prev.R - 1;
      let join = gap >= 0 && gap <= 10;
      if (join) {
        let zmax = -Infinity;
        for (let j = prev.R + 1; j < cur.L; j++) {
          if (isFinite(xs[j].zDem) && xs[j].zDem > zmax) zmax = xs[j].zDem;
        }
        join = zmax <= h + 2.6;
      }
      if (join) prev.R = cur.R;
      else out.push({ L: cur.L, R: cur.R });
    }
    return out;
  }

  function mainWetSpan(xs, h, loose) {
    const n = xs.length;
    const runs = [];
    let a = -1;
    for (let j = 0; j <= n; j++) {
      const wet = j < n && isWet(xs[j].zDem, h, loose);
      if (wet) {
        if (a < 0) a = j;
      } else if (a >= 0) {
        runs.push({ L: a, R: j - 1 });
        a = -1;
      }
    }
    if (!runs.length) return null;
    const merged = loose ? mergeWetRuns(xs, h, runs) : runs;
    const mid = (n / 2) | 0;
    let pick = null;
    for (let r = 0; r < merged.length; r++) {
      if (merged[r].L <= mid && merged[r].R >= mid) {
        pick = merged[r];
        break;
      }
    }
    function runMinZ(run) {
      let z = Infinity;
      for (let j = run.L; j <= run.R; j++) {
        if (isFinite(xs[j].zDem) && xs[j].zDem < z) z = xs[j].zDem;
      }
      return z;
    }
    if (!pick) {
      pick = merged[0];
      for (let r = 1; r < merged.length; r++) {
        if (runMinZ(merged[r]) < runMinZ(pick)) pick = merged[r];
      }
    }
    return pick;
  }

  function tribBankSpan(xs, h) {
    const n = xs.length;
    if (n < 3) return n ? { L: 0, R: n - 1 } : null;
    const mid = (n / 2) | 0;
    const cap = Math.min(isFinite(h) ? h : TRIB_BANK_Z, TRIB_BANK_Z);
    function inChan(j) {
      const z = xs[j] && xs[j].zDem;
      return isFinite(z) && z <= cap + 0.12;
    }
    let L = mid;
    while (L > 0 && inChan(L - 1)) L--;
    let R = mid;
    while (R < n - 1 && inChan(R + 1)) R++;
    if (!inChan(mid) && L === mid && R === mid) {
      return mainWetSpan(xs, h, false);
    }
    return { L: L, R: R };
  }

  function sampleAcross(xs, t) {
    const n = xs.length;
    const tt = Math.max(0, Math.min(n - 1, t));
    const j0 = Math.floor(tt);
    const j1 = Math.min(n - 1, j0 + 1);
    const u = tt - j0;
    const a = xs[j0];
    const b = xs[j1];
    return {
      x: a.x + (b.x - a.x) * u,
      z: a.z + (b.z - a.z) * u,
      sx: (a.sx != null ? a.sx : a.x) + ((b.sx != null ? b.sx : b.x) - (a.sx != null ? a.sx : a.x)) * u,
      sz: (a.sz != null ? a.sz : a.z) + ((b.sz != null ? b.sz : b.z) - (a.sz != null ? a.sz : a.z)) * u
    };
  }

  function snapToWaterline(xs, jWet, jDry, h) {
    const a = xs[jWet];
    const b = xs[jDry];
    const den = b.zDem - a.zDem;
    let t = 1;
    if (Math.abs(den) > 1e-6) t = (h - a.zDem) / den;
    t = Math.max(0, Math.min(1, t));
    return {
      x: a.x + (b.x - a.x) * t,
      z: a.z + (b.z - a.z) * t,
      t: jWet + (jDry - jWet) * t
    };
  }

  function applyWetSpan(vs, xs, tL, tR, yH) {
    const n = vs.length;
    if (!(tR > tL + 0.2) || yH == null) return;
    for (let j = 0; j < n; j++) {
      const s = xs[j];
      const yb = (isFinite(s.yBed) ? s.yBed : yElev(s.zDem)) + BED_LIFT;
      vs[j].x = s.x;
      vs[j].z = s.z;
      vs[j].sx = s.sx != null ? s.sx : s.x;
      vs[j].sz = s.sz != null ? s.sz : s.z;
      vs[j].yb = yb;
      vs[j].ys = yb;
      vs[j].wet = false;
    }
    const jL0 = Math.max(0, Math.floor(tL));
    const jR1 = Math.min(n - 1, Math.ceil(tR));
    for (let j = jL0; j <= jR1; j++) {
      vs[j].wet = true;
      vs[j].ys = yH;
      if (vs[j].yb > yH) vs[j].yb = yH;
    }
    const pL = sampleAcross(xs, tL);
    vs[jL0].sx = pL.sx;
    vs[jL0].sz = pL.sz;
    vs[jL0].ys = yH;
    vs[jL0].wet = true;
    const bedL = sampleTerrainXZ(pL.sx, pL.sz);
    if (isFinite(bedL.yBed)) {
      vs[jL0].yb = bedL.yBed + BED_LIFT;
      if (vs[jL0].yb > yH) vs[jL0].yb = yH;
    }
    const pR = sampleAcross(xs, tR);
    vs[jR1].sx = pR.sx;
    vs[jR1].sz = pR.sz;
    vs[jR1].ys = yH;
    vs[jR1].wet = true;
    const bedR = sampleTerrainXZ(pR.sx, pR.sz);
    if (isFinite(bedR.yBed)) {
      vs[jR1].yb = bedR.yBed + BED_LIFT;
      if (vs[jR1].yb > yH) vs[jR1].yb = yH;
    }
  }

  function stationVerts(i) {
    const h = hSta(i);
    const xs = channel[i].xs;
    let yH = h != null && isFinite(h) ? yElev(h) + BED_LIFT : null;
    if (yH != null && reachKind === "trib" && isOfftakePt(channel[i])) {
      const midXs = xs[(xs.length / 2) | 0];
      if (midXs && isFinite(midXs.yBed)) yH = Math.max(yH, midXs.yBed + 0.18);
    }
    const n = xs.length;
    const verts = new Array(n);
    for (let j = 0; j < n; j++) {
      const s = xs[j];
      const yb = (isFinite(s.yBed) ? s.yBed : yElev(s.zDem)) + BED_LIFT;
      verts[j] = {
        x: s.x,
        z: s.z,
        sx: s.sx != null ? s.sx : s.x,
        sz: s.sz != null ? s.sz : s.z,
        yb: yb,
        ys: yb,
        wet: false
      };
    }
    verts.yH = yH;
    verts.tL = NaN;
    verts.tR = NaN;
    if (yH == null) return verts;
    const span = reachKind === "trib"
      ? tribBankSpan(xs, h)
      : mainWetSpan(xs, h, false);
    if (!span) return verts;
    const edgeH = reachKind === "trib"
      ? Math.min(isFinite(h) ? h : TRIB_BANK_Z, TRIB_BANK_Z)
      : h;
    let tL = span.L;
    let tR = span.R;
    if (span.L > 0) {
      const p = snapToWaterline(xs, span.L, span.L - 1, edgeH);
      tL = p.t;
    }
    if (span.R < n - 1) {
      const p = snapToWaterline(xs, span.R, span.R + 1, edgeH);
      tR = p.t;
    }
    verts.tL = tL;
    verts.tR = tR;
    applyWetSpan(verts, xs, tL, tR, yH);
    return verts;
  }

  function smoothBankRows(rows) {
    const n = rows.length;
    if (n < 3) return;
    const left = new Array(n);
    const right = new Array(n);
    for (let i = 0; i < n; i++) {
      left[i] = rows[i].tL;
      right[i] = rows[i].tR;
    }
    const sL = gaussSmooth(left, 3);
    const sR = gaussSmooth(right, 3);
    for (let i = 0; i < n; i++) {
      if (!isFinite(left[i]) || !isFinite(right[i])) continue;
      if (!isFinite(sL[i]) || !isFinite(sR[i]) || rows[i].yH == null) continue;
      applyWetSpan(rows[i], channel[i].xs, sL[i], sR[i], rows[i].yH);
    }
  }

  function vertId(i, j, layer) {
    return (i * nAcross + j) * 2 + layer;
  }

  function writeGrid(arr) {
    liveVerts = [];
    for (let i = 0; i < channel.length; i++) liveVerts.push(stationVerts(i));
    smoothBankRows(liveVerts);
    let k = 0;
    for (let i = 0; i < channel.length; i++) {
      const vs = liveVerts[i];
      for (let j = 0; j < nAcross; j++) {
        const s = vs[j];
        arr[k] = s.x;
        arr[k + 1] = s.yb;
        arr[k + 2] = s.z;
        arr[k + 3] = s.sx != null ? s.sx : s.x;
        arr[k + 4] = s.ys;
        arr[k + 5] = s.sz != null ? s.sz : s.z;
        k += 6;
      }
    }
  }

  function buildIndex(kind) {
    const idx = [];
    function pushQuad(a, b, c, d) {
      idx.push(a, b, c, b, d, c);
    }
    const rows = liveVerts;
    if (!rows || rows.length !== channel.length) return idx;
    const surf = kind === "surface";
    for (let i = 0; i < channel.length - 1; i++) {
      for (let j = 0; j < nAcross - 1; j++) {
        const w00 = rows[i][j].wet;
        const w10 = rows[i][j + 1].wet;
        const w01 = rows[i + 1][j].wet;
        const w11 = rows[i + 1][j + 1].wet;
        const nWet = (w00 ? 1 : 0) + (w10 ? 1 : 0) + (w01 ? 1 : 0) + (w11 ? 1 : 0);
        if (nWet < 2) continue;
        const b00 = vertId(i, j, 0);
        const s00 = vertId(i, j, 1);
        const b10 = vertId(i, j + 1, 0);
        const s10 = vertId(i, j + 1, 1);
        const b01 = vertId(i + 1, j, 0);
        const s01 = vertId(i + 1, j, 1);
        const b11 = vertId(i + 1, j + 1, 0);
        const s11 = vertId(i + 1, j + 1, 1);
        if (surf) {
          if (nWet === 4) pushQuad(s00, s10, s01, s11);
          else if (nWet === 3) {
            const ss = [];
            if (w00) ss.push(s00);
            if (w10) ss.push(s10);
            if (w01) ss.push(s01);
            if (w11) ss.push(s11);
            idx.push(ss[0], ss[1], ss[2]);
          }
          continue;
        }
        if (nWet === 4) pushQuad(b00, b01, b10, b11);
        else if (nWet === 3) {
          const sb = [];
          if (w00) sb.push(b00);
          if (w10) sb.push(b10);
          if (w01) sb.push(b01);
          if (w11) sb.push(b11);
          idx.push(sb[0], sb[2], sb[1]);
        }
        const leftWet = j > 0 && rows[i][j - 1].wet && rows[i + 1][j - 1].wet;
        const rightWet = j < nAcross - 2 && rows[i][j + 2].wet && rows[i + 1][j + 2].wet;
        if (!leftWet && w00 && w01) pushQuad(b00, s00, b01, s01);
        if (!rightWet && w10 && w11) pushQuad(b10, b11, s10, s11);
      }
    }
    return idx;
  }

  function setUpwardNormals(geo, nVert) {
    const nrm = new Float32Array(nVert * 3);
    for (let i = 0; i < nVert; i++) nrm[i * 3 + 1] = 1;
    geo.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
  }

  function updateVolume() {
    if (!reaches.length) {
      if (!volumePos || !channel || !volumeGeo) {
        updateReservoirVolumes();
        return;
      }
      writeGrid(volumePos.array);
      volumePos.needsUpdate = true;
      volumeGeo.setIndex(buildIndex("bed"));
      volumeGeo.computeVertexNormals();
      if (surfaceGeo) surfaceGeo.setIndex(buildIndex("surface"));
      updateFlowArrows();
      updateReservoirVolumes();
      return;
    }
    for (let r = 0; r < reaches.length; r++) applyReachVolume(reaches[r]);
    updateReservoirVolumes();
  }

  function applyReachVolume(reach) {
    reachKind = reach.kind || "main";
    channel = reach.channel;
    nAcross = reach.nAcross;
    activeHGrid = reach.hGrid;
    volumePos = reach.volumePos;
    volumeGeo = reach.volumeGeo;
    surfaceGeo = reach.surfaceGeo;
    flowArrows = reach.flowArrows || [];
    if (!volumePos || !channel || !volumeGeo) return;
    writeGrid(volumePos.array);
    reach.liveVerts = liveVerts;
    volumePos.needsUpdate = true;
    volumeGeo.setIndex(buildIndex("bed"));
    volumeGeo.computeVertexNormals();
    if (surfaceGeo) surfaceGeo.setIndex(buildIndex("surface"));
    updateFlowArrows();
  }

  function buildVolume() {
    if (!channel || channel.length < 2) return;
    const nVert = channel.length * nAcross * 2;
    const pos = new Float32Array(nVert * 3);
    volumePos = new THREE.BufferAttribute(pos, 3);
    writeGrid(pos);
    const bedGeo = new THREE.BufferGeometry();
    bedGeo.setAttribute("position", volumePos);
    bedGeo.setIndex(buildIndex("bed"));
    bedGeo.computeVertexNormals();
    volumeGeo = bedGeo;
    const bedMat = new THREE.MeshPhongMaterial({
      color: WATER,
      emissive: 0x042c5c,
      emissiveIntensity: 0.12,
      transparent: true,
      opacity: 0.62,
      shininess: 40,
      side: THREE.DoubleSide,
      depthWrite: true,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -2
    });
    volumeMesh = new THREE.Mesh(bedGeo, bedMat);
    volumeMesh.name = "flowRunVolume";
    volumeMesh.renderOrder = 20;
    addOverlay(volumeMesh);

    const surfGeo = new THREE.BufferGeometry();
    surfGeo.setAttribute("position", volumePos);
    surfGeo.setIndex(buildIndex("surface"));
    setUpwardNormals(surfGeo, nVert);
    surfaceGeo = surfGeo;
    const surfMat = new THREE.MeshPhongMaterial({
      color: 0x2b86f0,
      emissive: 0x063a70,
      emissiveIntensity: 0.18,
      specular: 0xc8e6ff,
      transparent: true,
      opacity: 0.9,
      shininess: 140,
      side: THREE.FrontSide,
      depthWrite: true,
      flatShading: false,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -4
    });
    surfaceMesh = new THREE.Mesh(surfGeo, surfMat);
    surfaceMesh.name = "flowRunSurface";
    surfaceMesh.renderOrder = 21;
    addOverlay(surfaceMesh);
  }

  function buildReservoirVolumes(list) {
    reservoirLive = [];
    if (!Array.isArray(list) || !list.length) return;
    for (let i = 0; i < list.length; i++) {
      try {
        buildOneReservoirVolume(list[i]);
      } catch (err) {
        console.warn("[flow-run] reservoir volume", list[i] && list[i].id, err);
      }
    }
    updateReservoirVolumes();
  }

  function syncReservoirLevelsWithMain(list, mainSource, mainHGrid) {
    if (!Array.isArray(list) || !mainSource) return;
    const lons = mainSource.lon || [];
    const lats = mainSource.lat || [];
    const grid = mainHGrid || [];
    if (!lons.length || !lats.length || !grid.length) return;
    for (let r = 0; r < list.length; r++) {
      const item = list[r];
      const lon = Number(item && item.lon);
      const lat = Number(item && item.lat);
      if (!item || !isFinite(lon) || !isFinite(lat)) continue;
      let nearest = -1;
      let best = Infinity;
      for (let i = 0; i < lons.length; i++) {
        const dx = Number(lons[i]) - lon;
        const dy = Number(lats[i]) - lat;
        const d = dx * dx + dy * dy;
        if (d < best) {
          best = d;
          nearest = i;
        }
      }
      if (nearest < 0) continue;
      const levels = [];
      for (let t = 0; t < grid.length; t++) {
        const value = Number(grid[t] && grid[t][nearest]);
        levels.push(isFinite(value) ? value : null);
      }
      item.h_level_m = levels;
      item.level_m = levels[0];
    }
  }

  function buildOneReservoirVolume(res) {
    if (!res || !global.THREE || !global.Dem3D) return;
    const THREE = global.THREE;
    const ws = res.water_surface || res;
    // Muc nuoc: uu tien chuoi dien toan Model 1D (h_level_m), roi initial.
    const hSeries = Array.isArray(res.h_level_m) ? res.h_level_m : null;
    let level = num(hSeries && hSeries[0], NaN);
    if (!isFinite(level)) level = Number(res.level_m);
    if (!isFinite(level)) level = Number(res.initial_level_m);
    if (!isFinite(level) && ws) level = Number(ws.level_m);
    if (!isFinite(level)) level = Number(res.crest_m);
    if (!isFinite(level)) return;
    // Flood-fill mesh da ve o muc max — dung level_m mesh neu cao hon.
    let meshLevel = Number(ws && ws.level_m);
    if (!isFinite(meshLevel)) meshLevel = level;
    if (hSeries && hSeries.length) {
      for (let i = 0; i < hSeries.length; i++) {
        const v = Number(hSeries[i]);
        if (isFinite(v) && v > meshLevel) meshLevel = v;
      }
    }
    const lift = BED_LIFT;
    const ySurfFlat = yElev(meshLevel) + lift;

    const lons = ws.lons;
    const lats = ws.lats;
    const zs = ws.z;
    if (Array.isArray(lons) && Array.isArray(lats) && Array.isArray(zs) &&
        lons.length >= 2 && Array.isArray(lons[0]) && lons[0].length >= 2) {
      if (buildReservoirGridVolume(lons, lats, zs, meshLevel, ySurfFlat, lift, res.id, hSeries)) return;
    }
    if (Array.isArray(ws.rings) && ws.rings.length) {
      for (let r = 0; r < ws.rings.length; r++) {
        buildReservoirRingVolume(ws.rings[r], meshLevel, ySurfFlat, lift, hSeries, res.id);
      }
    }
  }

  function reservoirLevelAt(hSeries, fallback) {
    if (!hSeries || !hSeries.length) return fallback;
    const n = hSeries.length;
    const t = Math.max(0, Math.min(n - 1, Number(timeFrac) || 0));
    const i0 = Math.max(0, Math.min(n - 1, Math.floor(t)));
    const i1 = Math.min(n - 1, i0 + 1);
    let a = Number(hSeries[i0]);
    let b = Number(hSeries[i1]);
    if (!isFinite(a)) {
      for (let k = i0; k >= 0; k--) {
        const u = Number(hSeries[k]);
        if (isFinite(u)) { a = u; break; }
      }
    }
    if (!isFinite(b)) b = a;
    if (!isFinite(a)) return fallback;
    if (i0 === i1 || !isFinite(b)) return a;
    const w = t - i0;
    return a + (b - a) * w;
  }

  function updateReservoirVolumes() {
    if (!reservoirLive.length) return;
    let shown = null;
    for (let i = 0; i < reservoirLive.length; i++) {
      const live = reservoirLive[i];
      if (!live || !live.pos || !live.zb) continue;
      const level = reservoirLevelAt(live.hSeries, live.level0);
      if (!isFinite(level)) continue;
      shown = level;
      const yS = yElev(level) + live.lift;
      const arr = live.pos.array;
      const zb = live.zb;
      for (let k = 0; k < zb.length; k++) {
        const is = k * 2 + 1;
        const yb = arr[(k * 2) * 3 + 1];
        if (!(zb[k] < level - MIN_DEPTH)) {
          arr[is * 3 + 1] = yb;
        } else {
          arr[is * 3 + 1] = yS > yb + 1e-5 ? yS : yb + 1e-4;
        }
      }
      live.pos.needsUpdate = true;
      if (live.surfMesh && live.surfMesh.geometry) {
        live.surfMesh.geometry.computeBoundingSphere();
      }
      if (live.bedMesh && live.bedMesh.geometry) {
        live.bedMesh.geometry.computeBoundingSphere();
      }
    }
    const hEl = $("flowRunHLabel");
    if (hEl && shown != null) {
      const riverH = currentH();
      hEl.textContent =
        (riverH == null ? "H — m" : ("H " + riverH.toFixed(2) + " m")) +
        " · Hồ " + shown.toFixed(2) + " m";
    }
  }

  function reservoirBedMat() {
    return new THREE.MeshPhongMaterial({
      color: WATER,
      emissive: 0x042c5c,
      emissiveIntensity: 0.12,
      transparent: true,
      opacity: 0.62,
      shininess: 40,
      side: THREE.DoubleSide,
      depthWrite: true,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -2
    });
  }

  function reservoirSurfMat() {
    return new THREE.MeshPhongMaterial({
      color: 0x2b86f0,
      emissive: 0x063a70,
      emissiveIntensity: 0.18,
      specular: 0xc8e6ff,
      transparent: true,
      opacity: 0.9,
      shininess: 140,
      side: THREE.FrontSide,
      depthWrite: true,
      flatShading: false,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -4
    });
  }

  function buildReservoirGridVolume(lons, lats, zs, level, ySurfFlat, lift, resId, hSeries) {
    const THREE = global.THREE;
    const nr = lons.length;
    const nc = lons[0].length;
    const bedIdx = [];
    const surfIdx = [];
    const positions = [];
    const zbList = [];
    let nVert = 0;
    for (let i = 0; i < nr; i++) {
      bedIdx[i] = [];
      surfIdx[i] = [];
      for (let j = 0; j < nc; j++) {
        const lon = lons[i] && lons[i][j];
        const lat = lats[i] && lats[i][j];
        let zb = zs[i] && zs[i][j];
        if (lon == null || lat == null) {
          bedIdx[i][j] = -1;
          surfIdx[i][j] = -1;
          continue;
        }
        zb = zb != null && isFinite(Number(zb)) ? Number(zb) : NaN;
        if (!isFinite(zb)) {
          if (global.Dem3D.sampleElevation) zb = Number(global.Dem3D.sampleElevation(lon, lat));
        }
        if (!isFinite(zb) || zb > level - MIN_DEPTH) {
          bedIdx[i][j] = -1;
          surfIdx[i][j] = -1;
          continue;
        }
        const xz = xzPoint(lon, lat);
        if (!xz || !isFinite(xz.x) || !isFinite(xz.z)) {
          bedIdx[i][j] = -1;
          surfIdx[i][j] = -1;
          continue;
        }
        const yb = yElev(zb) + lift;
        // Mat nuoc phang dung cao trinh ho — khong nang theo DEM.
        const ys = ySurfFlat;
        if (!(ys > yb + 1e-5)) {
          bedIdx[i][j] = -1;
          surfIdx[i][j] = -1;
          continue;
        }
        positions.push(xz.x, yb, xz.z);
        bedIdx[i][j] = nVert++;
        positions.push(xz.x, ys, xz.z);
        surfIdx[i][j] = nVert++;
        zbList.push(zb);
      }
    }
    if (nVert < 6) return false;

    const bedFaces = [];
    const surfFaces = [];
    const sideFaces = [];
    function pushQuad(idxArr, a, b, c, d) {
      idxArr.push(a, c, b, b, c, d);
    }
    for (let i = 0; i < nr - 1; i++) {
      for (let j = 0; j < nc - 1; j++) {
        const b00 = bedIdx[i][j];
        const b10 = bedIdx[i][j + 1];
        const b01 = bedIdx[i + 1][j];
        const b11 = bedIdx[i + 1][j + 1];
        const s00 = surfIdx[i][j];
        const s10 = surfIdx[i][j + 1];
        const s01 = surfIdx[i + 1][j];
        const s11 = surfIdx[i + 1][j + 1];
        if (b00 >= 0 && b10 >= 0 && b01 >= 0 && b11 >= 0) {
          pushQuad(bedFaces, b00, b01, b10, b11);
          pushQuad(surfFaces, s00, s10, s01, s11);
        } else {
          if (b00 >= 0 && b10 >= 0 && b01 >= 0) {
            bedFaces.push(b00, b10, b01);
            surfFaces.push(s00, s01, s10);
          }
          if (b10 >= 0 && b11 >= 0 && b01 >= 0) {
            bedFaces.push(b10, b11, b01);
            surfFaces.push(s10, s01, s11);
          }
        }
      }
    }
    for (let i = 0; i < nr; i++) {
      for (let j = 0; j < nc - 1; j++) {
        const aWet = bedIdx[i][j] >= 0;
        const bWet = bedIdx[i][j + 1] >= 0;
        if (aWet === bWet) continue;
        const jb = aWet ? j : j + 1;
        if (i + 1 >= nr) continue;
        if (bedIdx[i][jb] < 0 || bedIdx[i + 1][jb] < 0) continue;
        pushQuad(
          sideFaces,
          bedIdx[i][jb], bedIdx[i + 1][jb],
          surfIdx[i][jb], surfIdx[i + 1][jb]
        );
      }
    }
    for (let i = 0; i < nr - 1; i++) {
      for (let j = 0; j < nc; j++) {
        const aWet = bedIdx[i][j] >= 0;
        const bWet = bedIdx[i + 1][j] >= 0;
        if (aWet === bWet) continue;
        const ib = aWet ? i : i + 1;
        if (j + 1 >= nc) continue;
        if (bedIdx[ib][j] < 0 || bedIdx[ib][j + 1] < 0) continue;
        pushQuad(
          sideFaces,
          bedIdx[ib][j], bedIdx[ib][j + 1],
          surfIdx[ib][j], surfIdx[ib][j + 1]
        );
      }
    }
    if (!surfFaces.length && !bedFaces.length) return false;

    const posArr = new Float32Array(positions);
    const posAttr = new THREE.BufferAttribute(posArr, 3);
    const bedGeo = new THREE.BufferGeometry();
    bedGeo.setAttribute("position", posAttr);
    bedGeo.setIndex(bedFaces.concat(sideFaces));
    bedGeo.computeVertexNormals();
    const bedMesh = new THREE.Mesh(bedGeo, reservoirBedMat());
    bedMesh.name = "flowRunReservoirBed";
    bedMesh.renderOrder = 20;
    bedMesh.userData = { kind: "reservoir", id: resId };
    addOverlay(bedMesh);

    const surfGeo = new THREE.BufferGeometry();
    surfGeo.setAttribute("position", posAttr);
    surfGeo.setIndex(surfFaces);
    setUpwardNormals(surfGeo, nVert);
    const surfMesh = new THREE.Mesh(surfGeo, reservoirSurfMat());
    surfMesh.name = "flowRunReservoirSurface";
    surfMesh.renderOrder = 21;
    surfMesh.userData = { kind: "reservoir", id: resId };
    addOverlay(surfMesh);
    reservoirLive.push({
      pos: posAttr,
      zb: zbList,
      hSeries: hSeries || null,
      level0: Number(hSeries && hSeries[0]) || level,
      lift: lift,
      surfMesh: surfMesh,
      bedMesh: bedMesh
    });
    return true;
  }

  function buildReservoirRingVolume(ring, level, ySurfFlat, lift, hSeries, resId) {
    const THREE = global.THREE;
    if (!Array.isArray(ring) || ring.length < 3) return false;
    const top2 = [];
    const positions = [];
    const zbList = [];
    let n = 0;
    for (let i = 0; i < ring.length; i++) {
      const lon = Number(ring[i][0]);
      const lat = Number(ring[i][1]);
      if (!isFinite(lon) || !isFinite(lat)) continue;
      const xz = xzPoint(lon, lat);
      if (!xz || !isFinite(xz.x) || !isFinite(xz.z)) continue;
      let zb = NaN;
      if (global.Dem3D.sampleElevation) zb = Number(global.Dem3D.sampleElevation(lon, lat));
      if (!isFinite(zb)) zb = level - 2;
      // Day khong duoc cao hon mat ho.
      if (zb > level - MIN_DEPTH) zb = level - MIN_DEPTH;
      const yb = yElev(zb) + lift;
      const ys = ySurfFlat;
      positions.push(xz.x, yb, xz.z);
      positions.push(xz.x, ys, xz.z);
      zbList.push(zb);
      top2.push(new THREE.Vector2(xz.x, xz.z));
      n++;
    }
    if (n < 3) return false;
    const f = top2[0];
    const l = top2[n - 1];
    if (Math.hypot(f.x - l.x, f.y - l.y) > 1e-3) {
      positions.push(positions[0], positions[1], positions[2]);
      positions.push(positions[3], positions[4], positions[5]);
      zbList.push(zbList[0]);
      top2.push(f.clone());
      n++;
    }
    let faces = null;
    try {
      if (THREE.ShapeUtils && THREE.ShapeUtils.triangulateShape) {
        faces = THREE.ShapeUtils.triangulateShape(top2, []);
      }
    } catch (e) {
      faces = null;
    }
    if (!faces || !faces.length) {
      faces = [];
      for (let i = 1; i < n - 1; i++) faces.push([0, i, i + 1]);
    }
    const bedIdx = [];
    const surfIdx = [];
    const surfFaces = [];
    const bedFaces = [];
    const sideFaces = [];
    for (let i = 0; i < n; i++) {
      bedIdx.push(i * 2);
      surfIdx.push(i * 2 + 1);
    }
    faces.forEach(function (tri) {
      if (!tri || tri.length < 3) return;
      surfFaces.push(surfIdx[tri[0]], surfIdx[tri[1]], surfIdx[tri[2]]);
      bedFaces.push(bedIdx[tri[0]], bedIdx[tri[2]], bedIdx[tri[1]]);
    });
    for (let i = 0; i < n - 1; i++) {
      const b0 = bedIdx[i];
      const b1 = bedIdx[i + 1];
      const s0 = surfIdx[i];
      const s1 = surfIdx[i + 1];
      sideFaces.push(b0, b1, s0, s0, b1, s1);
    }
    const posArr = new Float32Array(positions);
    const posAttr = new THREE.BufferAttribute(posArr, 3);
    const bedGeo = new THREE.BufferGeometry();
    bedGeo.setAttribute("position", posAttr);
    bedGeo.setIndex(bedFaces.concat(sideFaces));
    bedGeo.computeVertexNormals();
    const bedMesh = new THREE.Mesh(bedGeo, reservoirBedMat());
    bedMesh.name = "flowRunReservoirBed";
    bedMesh.renderOrder = 20;
    bedMesh.userData = { kind: "reservoir", id: resId };
    addOverlay(bedMesh);

    const surfGeo = new THREE.BufferGeometry();
    surfGeo.setAttribute("position", posAttr);
    surfGeo.setIndex(surfFaces);
    setUpwardNormals(surfGeo, n * 2);
    const surfMesh = new THREE.Mesh(surfGeo, reservoirSurfMat());
    surfMesh.name = "flowRunReservoirSurface";
    surfMesh.renderOrder = 21;
    surfMesh.userData = { kind: "reservoir", id: resId };
    addOverlay(surfMesh);
    reservoirLive.push({
      pos: posAttr,
      zb: zbList,
      hSeries: hSeries || null,
      level0: Number(hSeries && hSeries[0]) || level,
      lift: lift,
      surfMesh: surfMesh,
      bedMesh: bedMesh
    });
    return true;
  }

  function distM(a, b) {
    if (a.lon == null || b.lon == null) {
      return Math.hypot(b.x - a.x, b.z - a.z);
    }
    const lat1 = a.lat * Math.PI / 180;
    const lat2 = b.lat * Math.PI / 180;
    const dlat = lat2 - lat1;
    const dlon = (b.lon - a.lon) * Math.PI / 180;
    const x = dlon * Math.cos((lat1 + lat2) / 2);
    return Math.hypot(x, dlat) * 6371000;
  }

  function pickArrowStations() {
    const n = channel.length;
    if (n < 2) return [];
    const idx = [];
    let acc = 0;
    let nextAt = FLOW_ARROW_SPACING_M * 0.5;
    for (let i = 1; i < n - 1; i++) {
      acc += distM(channel[i - 1], channel[i]);
      if (acc >= nextAt) {
        idx.push(i);
        nextAt += FLOW_ARROW_SPACING_M;
      }
    }
    return idx;
  }

  function stationSurfacePoint(i) {
    const vs = liveVerts && liveVerts[i];
    const p = channel[i];
    if (!vs || !p) return null;
    const mid = (nAcross / 2) | 0;
    let j = mid;
    if (!vs[j] || !vs[j].wet) {
      j = -1;
      for (let k = 0; k < nAcross; k++) {
        if (vs[k] && vs[k].wet) {
          j = k;
          break;
        }
      }
      if (j < 0) return null;
    }
    const s = vs[j];
    const yH = vs.yH != null ? vs.yH : s.ys;
    return {
      x: s.sx != null ? s.sx : (p.ox != null ? p.ox : p.x),
      y: yH + Math.max(pixelWorld() * 0.35, 0.03),
      z: s.sz != null ? s.sz : (p.oz != null ? p.oz : p.z),
      fx: p.fx != null ? p.fx : p.nz,
      fz: p.fz != null ? p.fz : -p.nx
    };
  }

  function arrowLength(p) {
    const lon = p.lon != null ? p.lon : 0;
    const lat = p.lat != null ? p.lat : 0;
    return Math.max(metersToWorld(FLOW_ARROW_LEN_M, lon, lat), pixelWorld() * 1.6);
  }

  function addFlowArrows() {
    flowArrows = [];
    const stations = pickArrowStations();
    const dir = new THREE.Vector3(0, 0, 1);
    const origin = new THREE.Vector3();
    for (let a = 0; a < stations.length; a++) {
      const i = stations[a];
      const len = arrowLength(channel[i]);
      const arrow = new THREE.ArrowHelper(dir, origin, len, FLOW_ARROW_COLOR, len * 0.45, len * 0.22);
      arrow.name = "flowRunArrow";
      arrow.renderOrder = 28;
      arrow.userData.sta = i;
      if (arrow.cone && arrow.cone.material) {
        arrow.cone.material.color.setHex(FLOW_ARROW_COLOR);
        arrow.cone.material.depthTest = true;
      }
      if (arrow.line && arrow.line.material) {
        arrow.line.material.color.setHex(FLOW_ARROW_COLOR);
        arrow.line.material.depthTest = true;
      }
      addOverlay(arrow);
      flowArrows.push(arrow);
    }
    updateFlowArrows();
  }

  function updateFlowArrows() {
    if (!flowArrows.length || !channel) return;
    const dir = new THREE.Vector3();
    for (let a = 0; a < flowArrows.length; a++) {
      const arrow = flowArrows[a];
      const i = arrow.userData.sta;
      const pt = stationSurfacePoint(i);
      if (!pt) {
        arrow.visible = false;
        continue;
      }
      const flen = Math.hypot(pt.fx, pt.fz) || 1;
      dir.set(pt.fx / flen, 0, pt.fz / flen);
      arrow.visible = !!showFlow;
      arrow.position.set(pt.x, pt.y, pt.z);
      arrow.setDirection(dir);
      arrow.setLength(arrowLength(channel[i]), arrowLength(channel[i]) * 0.45, arrowLength(channel[i]) * 0.22);
    }
  }

  function addXsMarkers() {
    addXsMarkersFor(payload && payload.xs, 0xfbbf24);
  }

  function addXsMarkersFor(xs, color) {
    if (!xs || !xs.lon) return;
    const col = color == null ? 0xfbbf24 : color;
    for (let i = 0; i < xs.lon.length; i++) {
      const lon = xs.lon[i];
      const lat = xs.lat[i];
      if (lon == null || lat == null) continue;
      const xz = xzPoint(lon, lat);
      const hRow = (xs.h_m && xs.h_m[Math.round(timeFrac)]) || [];
      const h = num(hRow[i], xs.z_bed && xs.z_bed[i]);
      const geo = new THREE.SphereGeometry(0.16, 8, 8);
      const mat = new THREE.MeshBasicMaterial({ color: col, depthTest: false });
      const m = new THREE.Mesh(geo, mat);
      m.position.set(xz.x, yElev(h) + 0.15, xz.z);
      m.renderOrder = 32;
      addOverlay(m);
    }
  }

  function nHours() {
    return (payload && payload.hours && payload.hours.length) || 0;
  }

  function rainStepMin() {
    return Math.max(1, Math.round(num(rainParams().step_min, RAIN_STEP_MIN_DEFAULT)));
  }

  function stepsPerHour() {
    return Math.max(1, Math.round(MIN_PER_HOUR / rainStepMin()));
  }

  function nMinutes() {
    return Math.max(1, nHours() * stepsPerHour());
  }

  function minuteIndex() {
    return Math.max(0, Math.min(nMinutes() - 1, Math.round(timeFrac * stepsPerHour())));
  }

  function setMinute(minIdx) {
    const n = nHours();
    const tot = nMinutes();
    const m = Math.max(0, Math.min(tot - 1, Math.round(Number(minIdx) || 0)));
    timeFrac = m / stepsPerHour();
    timeIndex = n ? Math.min(n - 1, Math.floor(timeFrac + 1e-9)) : 0;
  }

  function rainMmHourAt() {
    const arr = payload && payload.rainfall_mm;
    if (!arr || !arr.length) return 0;
    const i = Math.max(0, Math.min(arr.length - 1, Math.floor(timeFrac + 1e-9)));
    return num(arr[i], 0);
  }

  function chicagoHanoiWeights() {
    const rp = rainParams();
    const n1 = MIN_PER_HOUR;
    const step = rainStepMin();
    const n = Math.max(1, Math.round(n1 / step));
    const b = num(rp.idf_b_min, 9);
    const exp = num(rp.idf_n, 0.633);
    const r = num(rp.chicago_r, 0.38);
    const tp = r * n1;
    function inten(dt, before) {
      const t = Math.max(dt, 0);
      if (before) {
        const den = t / Math.max(r, 1e-6) + b;
        return ((1 - exp) * t / Math.max(r, 1e-6) + b) / Math.pow(den, exp + 1);
      }
      const omr = Math.max(1 - r, 1e-6);
      const den = t / omr + b;
      return ((1 - exp) * t / omr + b) / Math.pow(den, exp + 1);
    }
    const fine = new Array(n1);
    let s = 0;
    for (let k = 0; k < n1; k++) {
      const tMid = k + 0.5;
      fine[k] = tMid <= tp ? inten(tp - tMid, true) : inten(tMid - tp, false);
      s += fine[k];
    }
    const w = new Array(n);
    let s2 = 0;
    for (let i = 0; i < n; i++) {
      let acc = 0;
      for (let k = 0; k < step; k++) acc += fine[i * step + k] || 0;
      w[i] = s > 0 ? acc / s : 1 / n;
      s2 += w[i];
    }
    if (s2 > 0) {
      for (let i = 0; i < n; i++) w[i] /= s2;
    }
    return w;
  }

  function rainMinuteWeights() {
    const raw = rainParams().minute_weights;
    const n = stepsPerHour();
    if (raw && raw.length === n) return raw;
    return chicagoHanoiWeights();
  }

  function rainMinuteWeightAt() {
    const w = rainMinuteWeights();
    const m = minuteIndex() % stepsPerHour();
    return num(w[m], 1 / stepsPerHour());
  }

  function rainMmMinuteAt() {
    return rainMmHourAt() * rainMinuteWeightAt();
  }

  function rainMmAt() {
    return rainMmMinuteAt();
  }

  function rainPeakMinuteMm() {
    const listed = num(payload && payload.rain_peak_minute_mm, 0);
    if (listed > 0) return listed;
    const wmax = num(rainParams().minute_weight_max, 0);
    const peakH = num(payload && payload.rain_peak_mm, 0);
    if (wmax > 0) return peakH * wmax;
    let mx = 0;
    const w = rainMinuteWeights();
    for (let i = 0; i < w.length; i++) if (w[i] > mx) mx = w[i];
    return peakH * mx;
  }

  function rainParams() {
    return (payload && payload.rain) || {};
  }

  function rainCloudY() {
    const t = terrain();
    const z0 = t && isFinite(Number(t.zmin)) ? Number(t.zmin) : 0;
    const bounds = t && t.bounds_wgs84;
    const lon = bounds ? (bounds.west + bounds.east) / 2 : 105.9;
    const lat = bounds ? (bounds.south + bounds.north) / 2 : 21.0;
    const h = num(rainParams().cloud_height_m, 5000);
    return yElev(z0) + metersToWorld(h, lon, lat);
  }

  function rainWindWorld(dt) {
    const bounds = terrain() && terrain().bounds_wgs84;
    const lon = bounds ? (bounds.west + bounds.east) / 2 : 105.9;
    const lat = bounds ? (bounds.south + bounds.north) / 2 : 21.0;
    const rp = rainParams();
    return {
      x: metersToWorld(num(rp.wind_east_ms, 1.6) * dt, lon, lat),
      z: -metersToWorld(num(rp.wind_north_ms, 0.4) * dt, lon, lat)
    };
  }

  function rainIntensityFrac() {
    if (rainMmHourAt() <= 0.02) return 0;
    const peak = Math.max(rainPeakMinuteMm(), 1e-6);
    return Math.max(0, Math.min(1, rainMmMinuteAt() / peak));
  }

  function rainTargetCount() {
    if (rainMmHourAt() <= 0.02) return 0;
    return RAIN_MAX;
  }

  function rainDropBase() {
    const f = rainIntensityFrac();
    return RAIN_DROP_MIN + (RAIN_DROP_MAX - RAIN_DROP_MIN) * Math.sqrt(f);
  }

  function teardropGeometry() {
    const pts = [
      new THREE.Vector2(0.00, 1.20),
      new THREE.Vector2(0.10, 0.92),
      new THREE.Vector2(0.22, 0.55),
      new THREE.Vector2(0.36, 0.12),
      new THREE.Vector2(0.44, -0.22),
      new THREE.Vector2(0.40, -0.52),
      new THREE.Vector2(0.24, -0.74),
      new THREE.Vector2(0.00, -0.82)
    ];
    const geo = new THREE.LatheGeometry(pts, 10);
    geo.computeVertexNormals();
    return geo;
  }

  function writeRainInstance(i) {
    if (!rainPoints || !rainDummy) return;
    const s = rainScale[i] || 1;
    rainDummy.position.set(rainPos[i * 3], rainPos[i * 3 + 1], rainPos[i * 3 + 2]);
    rainDummy.scale.set(s, s, s);
    rainDummy.rotation.set(0, 0, 0);
    rainDummy.updateMatrix();
    rainPoints.setMatrixAt(i, rainDummy.matrix);
  }

  function spawnRainDrop(i) {
    const layout = meshLayout();
    if (!layout || !rainPos) return;
    const u = Math.random();
    const v = Math.random();
    const x = layout.x0 + u * (layout.x1 - layout.x0);
    const z = layout.z0 + v * (layout.z1 - layout.z0);
    const bed = sampleTerrainXZ(x, z);
    const yG = isFinite(bed.yBed) ? bed.yBed : 0;
    const yC = rainCloudY();
    rainPos[i * 3] = x;
    rainPos[i * 3 + 1] = yG + Math.random() * Math.max(yC - yG, 0.2);
    rainPos[i * 3 + 2] = z;
    rainGround[i] = yG;
    rainJitter[i] = 0.82 + Math.random() * 0.36;
    rainScale[i] = rainDropBase() * rainJitter[i];
    rainAlive[i] = 1;
  }

  function buildRain() {
    stopRain();
    rainPos = new Float32Array(RAIN_MAX * 3);
    rainAlive = new Uint8Array(RAIN_MAX);
    rainGround = new Float32Array(RAIN_MAX);
    rainScale = new Float32Array(RAIN_MAX);
    rainJitter = new Float32Array(RAIN_MAX);
    rainDummy = new THREE.Object3D();
    for (let i = 0; i < RAIN_MAX; i++) {
      rainPos[i * 3 + 1] = -999;
    }
    const geo = teardropGeometry();
    const mat = new THREE.MeshPhongMaterial({
      color: RAIN_COLOR,
      emissive: 0x041f4a,
      emissiveIntensity: 0.22,
      specular: 0x9ecfff,
      shininess: 110,
      transparent: true,
      opacity: 0.94,
      flatShading: false
    });
    rainPoints = new THREE.InstancedMesh(geo, mat, RAIN_MAX);
    rainPoints.name = "flowRunRain";
    rainPoints.frustumCulled = false;
    rainPoints.renderOrder = 26;
    rainPoints.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    rainPoints.count = 0;
    addOverlay(rainPoints);
    const want = rainTargetCount();
    for (let i = 0; i < want; i++) {
      spawnRainDrop(i);
      writeRainInstance(i);
    }
    rainPoints.count = want;
    rainPoints.instanceMatrix.needsUpdate = true;
    startRain();
  }

  function updateRain(dt) {
    if (!rainPoints || !rainPos || !payload) return;
    const want = rainTargetCount();
    const yC = rainCloudY();
    const fall = Math.max(yC - 0.2, 1) / RAIN_FALL_S;
    const wind = rainWindWorld(dt);
    const layout = meshLayout();
    const zLo = layout ? Math.min(layout.z0, layout.z1) : 0;
    const zHi = layout ? Math.max(layout.z0, layout.z1) : 0;
    let live = 0;
    for (let i = 0; i < RAIN_MAX; i++) {
      if (!rainAlive[i]) continue;
      rainPos[i * 3] += wind.x;
      rainPos[i * 3 + 1] -= fall * dt;
      rainPos[i * 3 + 2] += wind.z;
      const x = rainPos[i * 3];
      const z = rainPos[i * 3 + 2];
      const out = layout && (x < layout.x0 || x > layout.x1 || z < zLo || z > zHi);
      if (out || rainPos[i * 3 + 1] <= rainGround[i]) {
        rainAlive[i] = 0;
        continue;
      }
      live++;
    }
    for (let i = 0; i < RAIN_MAX && live < want; i++) {
      if (rainAlive[i]) continue;
      spawnRainDrop(i);
      rainPos[i * 3 + 1] = yC;
      live++;
    }
    for (let i = 0; i < RAIN_MAX && live > want; i++) {
      if (!rainAlive[i]) continue;
      rainAlive[i] = 0;
      live--;
    }
    const base = rainDropBase();
    let w = 0;
    for (let i = 0; i < RAIN_MAX; i++) {
      if (!rainAlive[i]) continue;
      if (w !== i) {
        rainPos[w * 3] = rainPos[i * 3];
        rainPos[w * 3 + 1] = rainPos[i * 3 + 1];
        rainPos[w * 3 + 2] = rainPos[i * 3 + 2];
        rainGround[w] = rainGround[i];
        rainJitter[w] = rainJitter[i];
        rainAlive[i] = 0;
        rainAlive[w] = 1;
      }
      rainScale[w] = base * (rainJitter[w] || 1);
      writeRainInstance(w);
      w++;
    }
    rainPoints.count = w;
    rainPoints.instanceMatrix.needsUpdate = true;
    rainPoints.visible = !!showRain && w > 0;
  }

  function rainFrame(now) {
    if (!rainPoints) {
      rainRaf = null;
      return;
    }
    const dt = rainLastT ? Math.min(0.05, (now - rainLastT) / 1000) : 0.016;
    rainLastT = now;
    updateRain(dt);
    rainRaf = requestAnimationFrame(rainFrame);
  }

  function startRain() {
    if (rainRaf != null) return;
    rainLastT = 0;
    rainRaf = requestAnimationFrame(rainFrame);
  }

  function stopRain() {
    if (rainRaf != null) {
      cancelAnimationFrame(rainRaf);
      rainRaf = null;
    }
    rainLastT = 0;
  }

  function stopHydro() {
    playing = false;
    if (hydroTimer) {
      clearInterval(hydroTimer);
      hydroTimer = null;
    }
    const btn = $("flowRunPlayBtn");
    if (btn) btn.textContent = "Phát";
  }

  function currentH() {
    const n = channel && channel.length ? channel.length : 0;
    let mx = null;
    if (n) {
      const step = n > 80 ? Math.max(1, Math.round(n / 80)) : 1;
      for (let i = 0; i < n; i += step) {
        const h = hAt(i, timeFrac);
        if (h == null || !isFinite(h)) continue;
        if (mx == null || h > mx) mx = h;
      }
    }
    if (mx != null) return mx;
    const grid = activeHGrid || (payload && payload.h_wse_m);
    if (!grid || !grid.length) return null;
    const t0 = Math.max(0, Math.min(grid.length - 1, Math.floor(timeFrac)));
    const row = grid[t0] || [];
    for (let i = 0; i < row.length; i++) {
      const v = Number(row[i]);
      if (!isFinite(v)) continue;
      if (mx == null || v > mx) mx = v;
    }
    return mx;
  }

  function syncHydroUi() {
    if (!payload) return;
    const slider = $("flowRunTime");
    const label = $("flowRunTimeLabel");
    const minIdx = minuteIndex();
    if (slider) {
      slider.max = String(Math.max(0, nMinutes() - 1));
      slider.step = "1";
      slider.value = String(minIdx);
    }
    const hourVal = (payload.hours || [])[timeIndex];
    const step = rainStepMin();
    const slot = minIdx % stepsPerHour();
    const t0 = slot * step;
    const t1 = t0 + step;
    if (label) {
      label.textContent =
        "Giờ " + num(hourVal, timeIndex).toFixed(0) +
        " · " + String(t0).padStart(2, "0") + "–" + String(t1).padStart(2, "0") + " phút";
    }
    const rainEl = $("flowRunRainLabel");
    if (rainEl) {
      rainEl.textContent = "Mưa " + rainMmMinuteAt().toFixed(2) + " mm/15 phút";
    }
    const hEl = $("flowRunHLabel");
    if (hEl) {
      const h = currentH();
      hEl.textContent = h == null ? "H — m" : "H " + h.toFixed(2) + " m";
    }
    updateVolume();
    applyLayerVisibility();
  }

  function stepTime() {
    if (!nHours()) return;
    const next = minuteIndex() + 1;
    if (next >= nMinutes()) setMinute(0);
    else setMinute(next);
    syncHydroUi();
  }

  function toggleHydro() {
    if (!payload) return;
    if (playing) {
      stopHydro();
      return;
    }
    playing = true;
    const btn = $("flowRunPlayBtn");
    if (btn) btn.textContent = "Dừng";
    hydroTimer = setInterval(stepTime, PLAY_MS);
  }

  function buildOverlay(data) {
    disposeOverlay();
    payload = data;
    timeIndex = 0;
    timeFrac = 0;
    reaches = [];
    const specs = [{ id: "main", channel: data.channel || {}, hGrid: data.h_wse_m, xs: data.xs }];
    const brs = data.branches || [];
    for (let i = 0; i < brs.length; i++) {
      specs.push({
        id: brs[i].id || ("trib_" + (i + 1)),
        kind: brs[i].kind || "outlet",
        channel: brs[i].channel || {},
        hGrid: brs[i].h_wse_m,
        xs: brs[i].xs
      });
    }
    for (let s = 0; s < specs.length; s++) {
      const spec = specs[s];
      reachKind = spec.id === "main" ? "main" : "trib";
      activeHGrid = spec.hGrid;
      const pts = buildChannel(spec.channel);
      if (pts.length < 2) continue;
      channel = pts;
      const reach = {
        id: spec.id,
        kind: reachKind,
        channel: pts,
        nAcross: nAcross,
        hGrid: spec.hGrid,
        xs: spec.xs,
        volumePos: null,
        volumeGeo: null,
        surfaceGeo: null,
        liveVerts: null,
        flowArrows: []
      };
      buildVolume();
      reach.volumePos = volumePos;
      reach.volumeGeo = volumeGeo;
      reach.surfaceGeo = surfaceGeo;
      reach.liveVerts = liveVerts;
      addFlowArrows();
      reach.flowArrows = flowArrows.slice();
      addXsMarkersFor(spec.xs, spec.id === "main" ? 0xfbbf24 : 0xf97316);
      reaches.push(reach);
    }
    if (!reaches.length) {
      setHint("Không có lòng sông / mặt cắt Saint-Venant.");
      return;
    }
    channel = reaches[0].channel;
    nAcross = reaches[0].nAcross;
    activeHGrid = reaches[0].hGrid;
    volumePos = reaches[0].volumePos;
    volumeGeo = reaches[0].volumeGeo;
    surfaceGeo = reaches[0].surfaceGeo;
    liveVerts = reaches[0].liveVerts;
    const rainCb = $("flowRunShowRain");
    const flowCb = $("flowRunShowFlow");
    if (rainCb) showRain = !!rainCb.checked;
    if (flowCb) showFlow = !!flowCb.checked;
    syncReservoirLevelsWithMain(data.reservoirs || [], data.channel || {}, data.h_wse_m || []);
    buildRain();
    buildReservoirVolumes(data.reservoirs || []);
    setPanel(true);
    syncHydroUi();
    setHint("");
    if (!playing) toggleHydro();
  }

  async function runSimulate() {
    if (busy) return;
    if (!global.Dem3D || !global.Dem3D.getContext || !global.Dem3D.getContext().terrain) {
      alert("Hãy mở DEM 3D trước.");
      return;
    }
    busy = true;
    const btn = $("flowRunBtn");
    if (btn) btn.disabled = true;
    setHint("Đang dựng khối nước theo mặt cắt địa hình…");
    try {
      const res = await fetch(API + "/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file_id: fileId(), water_source: selectedWaterSource() })
      });
      const data = await res.json();
      if (!res.ok || !data || data.ok === false) {
        throw new Error((data && data.error) || ("HTTP " + res.status));
      }
      if (!data.h_wse_m || !data.h_wse_m.length) {
        throw new Error(
          "Chưa có H mặt cắt. Hãy chạy Model 1D (" +
          waterSourceLabel(selectedWaterSource()) +
          ") trước."
        );
      }
      buildOverlay(data);
    } catch (err) {
      setHint(err.message || String(err));
      alert(err.message || String(err));
    } finally {
      busy = false;
      if (btn) btn.disabled = false;
    }
  }

  function clearAll() {
    stopHydro();
    disposeOverlay();
    payload = null;
    elevGrid = null;
    setPanel(false);
    const btn = $("flowRunBtn");
    if (btn) btn.classList.remove("is-active");
  }

  function redrawOverlay() {
    if (!payload) return;
    const playingWas = playing;
    const tKeep = timeFrac;
    buildOverlay(payload);
    timeFrac = tKeep;
    setMinute(Math.round(timeFrac * stepsPerHour()));
    if (!playingWas) stopHydro();
    syncHydroUi();
  }

  function wireUi() {
    const btn = $("flowRunBtn");
    if (btn) {
      btn.addEventListener("click", function () {
        btn.classList.add("is-active");
        runSimulate();
      });
    }
    const play = $("flowRunPlayBtn");
    if (play) play.addEventListener("click", toggleHydro);
    const slider = $("flowRunTime");
    if (slider) {
      slider.addEventListener("input", function () {
        if (!payload) return;
        setMinute(slider.value);
        stopHydro();
        syncHydroUi();
      });
    }
    const closeBtn = $("flowRunCloseBtn");
    if (closeBtn) closeBtn.addEventListener("click", clearAll);
    const rainCb = $("flowRunShowRain");
    if (rainCb) {
      rainCb.addEventListener("change", function () {
        showRain = !!rainCb.checked;
        applyLayerVisibility();
      });
    }
    const flowCb = $("flowRunShowFlow");
    if (flowCb) {
      flowCb.addEventListener("change", function () {
        showFlow = !!flowCb.checked;
        if (showFlow) updateVolume();
        applyLayerVisibility();
      });
    }
  }

  global.FlowRun = {
    run: runSimulate,
    clear: clearAll,
    teardown: clearAll,
    onTerrainReady: function () {},
    redrawOverlay: redrawOverlay
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireUi);
  } else {
    wireUi();
  }
})(window);

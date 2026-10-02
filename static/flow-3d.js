/**
 * Ve mat cat doc tren DEM 3D va mo ta muc nuoc q_surface_mm.
 */
(function (global) {
  "use strict";

  const API = (window.floodUrl ? window.floodUrl("/api/flow-3d") : "/api/flow-3d");
    const LINE_COLOR = 0xffdd44;
    const TRIB_LINE_COLOR = 0xf97316;
    const WATER_COLOR = 0x1d6fe8;
  const MARKER_COLOR = 0xfff3a0;

  let drawing = false;
  let coordPicking = false;
  let coordHit = null;
  let coordGroup = null;
  let coordMapMarker = null;
  let coordMapPopup = null;
  let coordReqId = 0;
  let measureActive = false;
  let measurePts = [];
  let measureGroup = null;
  let measureMapLayer = null;
  let sketch = [];
  let payload = null;
  let timeIndex = 0;
  let playTimer = null;
  let overlayGroup = null;
  let networkOverlayGroup = null;
  let networkOverlayData = null;
  let networkOverlayLoading = false;
  let networkShowXs = false;
  let networkShowStructures = false;
  let selectedXsKey = "";
  let xsProfileLoading = false;
  let downPt = null;
  let lastRequest = null;
  let simPollTimer = null;
  let rrHydroData = null;
  let rrHydroResizeObs = null;
  let namHydroData = null;
  let namHydroResizeObs = null;
  let svEvalData = null;
  let svEvalObs = null;
  let svEvalResizeObs = null;
  let lastExtractedXs = null;
  let lastManningRows = [];
  let xsExtractTimer = null;
  let xsExtractGen = 0;
  let xsExtractBusy = false;
  let constructionHasRows = [];
  let constructionHasLoaded = false;

  function $(id) {
    return document.getElementById(id);
  }

  function selectedWaterSource() {
    return window.pickWaterSource ? window.pickWaterSource() : "saint-venant";
  }

  function waterSourceHint(data) {
    const xs = data.route_xs;
    if (data.water_source === "saint-venant-1d") {
      const reach = data.route_reach && data.route_reach !== "main" ? " " + data.route_reach : "";
      const off = data.official_xs ? " offset/z mô hình" : "";
      return " · mực nước Saint-venant-1D" + (xs ? " (XS" + xs + off + reach + ")" : " (nội suy" + reach + ")");
    }
    if (data.water_source === "saint-venant") {
      const reach = data.route_reach && data.route_reach !== "main" ? " " + data.route_reach : "";
      const off = data.official_xs ? " offset/z mô hình" : "";
      return " · mực nước Saint-Venant 1D" + (xs ? " (XS" + xs + off + reach + ")" : " (nội suy" + reach + ")");
    }
    return "";
  }

  function fileId() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (ctx && ctx.terrain && ctx.terrain.file_id) return ctx.terrain.file_id;
    if (global.Dem3D && global.Dem3D.pickFileIdFor3D) return global.Dem3D.pickFileIdFor3D();
    return null;
  }

  function setHint(text) {
    const el = $("dem3dHint");
    if (el) el.textContent = text || "";
  }

  function setPanelVisible(show) {
    const panel = $("flow3dPanel");
    if (panel) panel.hidden = !show;
  }

  function disposeOverlay() {
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
  }

  function addOverlay(obj) {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (!ctx || !ctx.scene) return;
    if (!overlayGroup) {
      overlayGroup = new THREE.Group();
      overlayGroup.name = "flow3dOverlay";
      ctx.scene.add(overlayGroup);
    }
    overlayGroup.add(obj);
  }

  function worldPoint(lon, lat, elev, lift) {
    const xz = global.Dem3D.lonLatToWorld(lon, lat);
    const y = global.Dem3D.elevToWorldY(elev) + (lift || 0);
    return new THREE.Vector3(xz.x, y, xz.z);
  }

  function disposeNetworkOverlay() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (networkOverlayGroup && ctx && ctx.scene) ctx.scene.remove(networkOverlayGroup);
    if (networkOverlayGroup) {
      networkOverlayGroup.traverse(function (obj) {
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
    networkOverlayGroup = null;
  }

  function addNetworkOverlay(obj) {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (!ctx || !ctx.scene || !global.THREE) return;
    if (!networkOverlayGroup) {
      networkOverlayGroup = new THREE.Group();
      networkOverlayGroup.name = "xsConstructionOverlay";
      ctx.scene.add(networkOverlayGroup);
    }
    networkOverlayGroup.add(obj);
  }

  function sampleElevOr(lon, lat, fallback) {
    let elev = null;
    if (global.Dem3D && global.Dem3D.sampleElevation) {
      elev = global.Dem3D.sampleElevation(lon, lat);
    }
    if (elev == null || !isFinite(Number(elev))) {
      elev = fallback != null && isFinite(Number(fallback)) ? Number(fallback) : 0;
    }
    return Number(elev);
  }

  function makeNetworkLabel(text, colorHex) {
    const THREE = global.THREE;
    if (!THREE) return null;
    const canvas = document.createElement("canvas");
    canvas.width = 256;
    canvas.height = 64;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = "bold 28px Arial, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 5;
    ctx.strokeStyle = "rgba(10,16,24,0.85)";
    ctx.strokeText(text, 128, 32);
    ctx.fillStyle = colorHex || "#dff3ff";
    ctx.fillText(text, 128, 32);
    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    const mat = new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      sizeAttenuation: true
    });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(5.5, 1.4, 1);
    sprite.renderOrder = 40;
    return sprite;
  }

  const STRUCTURE_COLORS = {
    dike: 0x34d399,
    weir: 0xf59e0b,
    reservoir: 0x38bdf8,
    gate: 0xfacc15,
    culvert: 0xc084fc,
    pump: 0xf87171
  };

  function sectionsForOverlayDisplay(sections) {
    // Ve day du tat ca mat cat (vd. 500 m -> ~107 XS), khong thua bot.
    return Array.isArray(sections) ? sections : [];
  }

  function demWgsBounds() {
    if (global.Dem3D && typeof global.Dem3D.getLonLatBounds === "function") {
      return global.Dem3D.getLonLatBounds();
    }
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    const t = ctx && ctx.terrain;
    if (!t) return null;
    const raw = t.bounds_wgs84 || t.bounds;
    if (!raw) return null;
    if (Array.isArray(raw) && raw.length >= 4) {
      return { west: +raw[0], south: +raw[1], east: +raw[2], north: +raw[3] };
    }
    if (raw.west != null) {
      return {
        west: +raw.west,
        south: +raw.south,
        east: +raw.east,
        north: +raw.north
      };
    }
    return null;
  }

  function pointOnDem(lon, lat) {
    const lo = Number(lon);
    const la = Number(lat);
    if (!isFinite(lo) || !isFinite(la)) return false;
    if (global.Dem3D && typeof global.Dem3D.pointInDem === "function") {
      return !!global.Dem3D.pointInDem(lo, la);
    }
    const b = demWgsBounds();
    if (!b) return true;
    return lo >= b.west && lo <= b.east && la >= b.south && la <= b.north;
  }

  function findDemMaskEdge(lonIn, latIn, lonOut, latOut) {
    // Binary search diem cuoi cung con nam trong mask DEM tren doan In->Out.
    let aLon = Number(lonIn);
    let aLat = Number(latIn);
    let bLon = Number(lonOut);
    let bLat = Number(latOut);
    for (let k = 0; k < 16; k++) {
      const mLon = (aLon + bLon) * 0.5;
      const mLat = (aLat + bLat) * 0.5;
      if (pointOnDem(mLon, mLat)) {
        aLon = mLon;
        aLat = mLat;
      } else {
        bLon = mLon;
        bLat = mLat;
      }
    }
    return { lon: aLon, lat: aLat };
  }

  function clipPolylineToDem(lon, lat, zArr, zFallback) {
    // Cat polyline theo mask DEM, tra ve cac doan hop le.
    const srcLon = lon || [];
    const srcLat = lat || [];
    if (srcLon.length < 2 || srcLat.length < 2) {
      return [{ lon: srcLon, lat: srcLat, z: zArr || [] }];
    }
    function zAt(i) {
      if (zArr && zArr[i] != null && isFinite(Number(zArr[i]))) return Number(zArr[i]);
      if (zFallback != null && isFinite(Number(zFallback))) return Number(zFallback);
      return null;
    }
    function lerpZ(i0, i1, t) {
      const z0 = zAt(i0);
      const z1 = zAt(i1);
      if (z0 != null && z1 != null) return z0 + t * (z1 - z0);
      if (z0 != null) return z0;
      return z1;
    }
    const parts = [];
    let curLon = [];
    let curLat = [];
    let curZ = [];
    function flush() {
      if (curLon.length >= 2) {
        parts.push({ lon: curLon, lat: curLat, z: curZ });
      }
      curLon = [];
      curLat = [];
      curZ = [];
    }
    function pushPt(lo, la, zz) {
      const n = curLon.length;
      if (n && Math.abs(curLon[n - 1] - lo) < 1e-10 && Math.abs(curLat[n - 1] - la) < 1e-10) {
        return;
      }
      curLon.push(lo);
      curLat.push(la);
      curZ.push(zz);
    }

    for (let i = 0; i < srcLon.length - 1; i++) {
      const aLon = Number(srcLon[i]);
      const aLat = Number(srcLat[i]);
      const bLon = Number(srcLon[i + 1]);
      const bLat = Number(srcLat[i + 1]);
      if (![aLon, aLat, bLon, bLat].every(isFinite)) {
        flush();
        continue;
      }
      const inA = pointOnDem(aLon, aLat);
      const inB = pointOnDem(bLon, bLat);
      if (inA && inB) {
        pushPt(aLon, aLat, zAt(i));
        pushPt(bLon, bLat, zAt(i + 1));
        continue;
      }
      if (inA && !inB) {
        pushPt(aLon, aLat, zAt(i));
        const edge = findDemMaskEdge(aLon, aLat, bLon, bLat);
        const t =
          Math.abs(bLon - aLon) + Math.abs(bLat - aLat) > 1e-12
            ? (Math.abs(edge.lon - aLon) + Math.abs(edge.lat - aLat)) /
              (Math.abs(bLon - aLon) + Math.abs(bLat - aLat))
            : 0;
        pushPt(edge.lon, edge.lat, lerpZ(i, i + 1, Math.max(0, Math.min(1, t))));
        flush();
        continue;
      }
      if (!inA && inB) {
        const edge = findDemMaskEdge(bLon, bLat, aLon, aLat);
        const t =
          Math.abs(bLon - aLon) + Math.abs(bLat - aLat) > 1e-12
            ? (Math.abs(edge.lon - aLon) + Math.abs(edge.lat - aLat)) /
              (Math.abs(bLon - aLon) + Math.abs(bLat - aLat))
            : 1;
        pushPt(edge.lon, edge.lat, lerpZ(i, i + 1, Math.max(0, Math.min(1, t))));
        pushPt(bLon, bLat, zAt(i + 1));
        continue;
      }
      flush();
    }
    flush();
    if (!parts.length) return [{ lon: [], lat: [], z: [] }];
    return parts;
  }

  function drawNetworkOverlay(data) {
    disposeNetworkOverlay();
    if (!global.THREE || !global.Dem3D || !global.Dem3D.lonLatToWorld) return;
    const THREE = global.THREE;
    const showXs = networkShowXs;
    const showSt = networkShowStructures;
    if (!showXs && !showSt) return;
    if (!data) return;

    if (showXs && data.sections && data.sections.length) {
      const displaySecs = sectionsForOverlayDisplay(data.sections);
      displaySecs.forEach(function (sec) {
        const isTrib = String(sec.kind || "") !== "main" && String(sec.reach || "") !== "main";
        const key = String(sec.reach || "main") + ":" + String(sec.xs_id);
        const selected = key === selectedXsKey;
        const lineColor = selected ? 0xfff27a : (isTrib ? 0xf97316 : 0x7dd3fc);
        const parts = clipPolylineToDem(sec.lon || [], sec.lat || [], sec.z || [], sec.z_bed);
        const meta = {
          kind: "xs",
          pickRole: "marker",
          xs_id: sec.xs_id,
          reach: sec.reach || "main",
          station_km: sec.station_km,
          manning_n: sec.manning_n,
          center_lon: sec.center_lon,
          center_lat: sec.center_lat
        };
        parts.forEach(function (clipped) {
          const lon = clipped.lon;
          const lat = clipped.lat;
          const zArr = clipped.z;
          if (lon.length < 2 || lon.length !== lat.length) return;
          const verts = [];
          for (let i = 0; i < lon.length; i++) {
            if (!pointOnDem(lon[i], lat[i])) continue;
            const elev = sampleElevOr(lon[i], lat[i], zArr[i] != null ? zArr[i] : sec.z_bed);
            if (!isFinite(elev)) continue;
            const v = worldPoint(lon[i], lat[i], elev, 0.35);
            verts.push(v.x, v.y, v.z);
          }
          if (verts.length < 6) return;
          const geo = new THREE.BufferGeometry();
          geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
          const mat = new THREE.LineBasicMaterial({ color: lineColor, depthTest: false });
          const line = new THREE.Line(geo, mat);
          line.renderOrder = 28;
          line.userData = {
            kind: "xs",
            pickRole: "line",
            xs_id: sec.xs_id,
            reach: sec.reach || "main",
            station_km: sec.station_km,
            manning_n: sec.manning_n
          };
          addNetworkOverlay(line);
        });
        const clon = sec.center_lon;
        const clat = sec.center_lat;
        if (clon == null || clat == null) return;
        if (!pointOnDem(clon, clat)) return;
        const ce = sampleElevOr(clon, clat, sec.z_bed);
        if (!isFinite(ce)) return;
        const marker = new THREE.Mesh(
          new THREE.SphereGeometry(selected ? 0.5 : (isTrib ? 0.28 : 0.36), 10, 10),
          new THREE.MeshBasicMaterial({ color: lineColor, depthTest: false })
        );
        marker.position.copy(worldPoint(clon, clat, ce, 0.55));
        marker.renderOrder = 29;
        marker.userData = meta;
        addNetworkOverlay(marker);
        const hit = new THREE.Mesh(
          new THREE.SphereGeometry(2.4, 8, 8),
          new THREE.MeshBasicMaterial({
            color: 0xffffff,
            transparent: true,
            opacity: 0.01,
            depthTest: false
          })
        );
        hit.position.copy(marker.position);
        hit.renderOrder = 30;
        hit.userData = meta;
        addNetworkOverlay(hit);

        const xsId = sec.xs_id;
        // Nhan: XS dang chon, hoac moi 5 mat cat (tranh chen chu khi 100+ XS).
        if (xsId != null && (selected || Number(xsId) % 5 === 1 || Number(xsId) === 1)) {
          let label = "XS" + xsId;
          if (sec.manning_n != null && isFinite(Number(sec.manning_n))) {
            label += " n=" + Number(sec.manning_n).toFixed(3);
          }
          const spr = makeNetworkLabel(label, selected ? "#fff7a0" : (isTrib ? "#fdba74" : "#e0f2fe"));
          if (spr) {
            spr.position.copy(worldPoint(clon, clat, ce, selected ? 3.0 : 2.2));
            spr.userData = meta;
            addNetworkOverlay(spr);
          }
        }
      });
    }

    if (showSt && data.structures && data.structures.length) {
      data.structures.forEach(function (st) {
        const water = makeReservoirWaterMesh(st);
        if (water) addNetworkOverlay(water);
        const mesh = makeStructureMesh(st);
        if (!mesh) return;
        addNetworkOverlay(mesh);
        const t = String(st.type || "").toLowerCase();
        const crest = Number(st.crest_m);
        const demZ = sampleElevOr(st.lon, st.lat, NaN);
        // Gan nhan sat dinh cong trinh (crest / DEM), khong lay (crest-invert) gay bay xa.
        const damCrestLabel = Number(st.dam_crest_m);
        let elevLabel = ((t === "reservoir" || t === "weir") && isFinite(damCrestLabel))
          ? damCrestLabel
          : (isFinite(crest) ? crest : demZ);
        if (!isFinite(elevLabel)) elevLabel = Number(st.invert_m);
        if (!isFinite(elevLabel)) return;
        const label = (st.id || st.name || st.type || "CT").toString();
        const spr = makeNetworkLabel(label, t === "reservoir" ? "#e0f2fe" : "#fff7ed");
        if (spr) {
          const liftM = (t === "weir" || t === "gate" || t === "reservoir") ? 2.5 : 3.5;
          const liftY = global.Dem3D.metersToWorldY
            ? global.Dem3D.metersToWorldY(liftM)
            : liftM * 0.01;
          spr.scale.set(5.5, 1.35, 1);
          spr.position.copy(worldPoint(st.lon, st.lat, elevLabel, liftY));
          spr.userData = { kind: "structure", id: st.id, type: st.type, record: st };
          addNetworkOverlay(spr);
        }
      });
    }
  }

  function makeReservoirWaterMesh(st) {
    const THREE = global.THREE;
    const ws = st && st.water_surface;
    if (!THREE || !global.Dem3D || !ws) return null;

    // Giong Dòng chảy 3D: mat phang theo mực nước ban đầu, day bam DEM.
    let level = Number(st.initial_level_m);
    if (!isFinite(level)) level = Number(ws.level_m);
    if (!isFinite(level)) level = Number(st.crest_m);
    if (!isFinite(level)) return null;

    const MIN_D = 0.05;
    const liftY = global.Dem3D.metersToWorldY ? global.Dem3D.metersToWorldY(0.35) : 0.004;
    const ySurfFlat = global.Dem3D.elevToWorldY(level) + liftY;
    const meta = {
      kind: "structure",
      pickRole: "water",
      id: st.id,
      type: st.type,
      record: st
    };

    const bedMat = new THREE.MeshPhongMaterial({
      color: 0x1d6fe8,
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

    function pushQuad(idxArr, a, b, c, d) {
      idxArr.push(a, c, b, b, c, d);
    }

    function addVolumeMeshes(positions, bedFaces, surfFaces, sideFaces, nVert) {
      if ((!surfFaces.length && !bedFaces.length) || nVert < 6) return false;
      const group = new THREE.Group();
      group.name = "reservoirWater";
      group.userData = meta;
      const posArr = new Float32Array(positions);
      const bedGeo = new THREE.BufferGeometry();
      bedGeo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));
      bedGeo.setIndex(bedFaces.concat(sideFaces));
      bedGeo.computeVertexNormals();
      const bedMesh = new THREE.Mesh(bedGeo, bedMat);
      bedMesh.renderOrder = 24;
      bedMesh.userData = meta;
      group.add(bedMesh);

      const surfGeo = new THREE.BufferGeometry();
      surfGeo.setAttribute("position", new THREE.BufferAttribute(posArr.slice(), 3));
      surfGeo.setIndex(surfFaces);
      const nrm = new Float32Array(nVert * 3);
      for (let i = 0; i < nVert; i++) nrm[i * 3 + 1] = 1;
      surfGeo.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
      const surfMesh = new THREE.Mesh(surfGeo, surfMat);
      surfMesh.renderOrder = 25;
      surfMesh.userData = meta;
      group.add(surfMesh);
      return group;
    }

    // 1) Luoi flood-fill (uu tien — giong flow-run).
    const lons = ws.lons;
    const lats = ws.lats;
    const zs = ws.z;
    if (Array.isArray(lons) && Array.isArray(lats) && Array.isArray(zs) &&
        lons.length >= 2 && Array.isArray(lons[0]) && lons[0].length >= 2) {
      const nr = lons.length;
      const nc = lons[0].length;
      const bedIdx = [];
      const surfIdx = [];
      const positions = [];
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
          if (!isFinite(zb)) zb = sampleElevOr(lon, lat, NaN);
          if (!isFinite(zb) || zb > level - MIN_D) {
            bedIdx[i][j] = -1;
            surfIdx[i][j] = -1;
            continue;
          }
          const p = global.Dem3D.lonLatToWorld(lon, lat);
          if (!p || !isFinite(p.x) || !isFinite(p.z)) {
            bedIdx[i][j] = -1;
            surfIdx[i][j] = -1;
            continue;
          }
          const yb = global.Dem3D.elevToWorldY(zb) + liftY;
          if (!(ySurfFlat > yb + 1e-5)) {
            bedIdx[i][j] = -1;
            surfIdx[i][j] = -1;
            continue;
          }
          positions.push(p.x, yb, p.z);
          bedIdx[i][j] = nVert++;
          positions.push(p.x, ySurfFlat, p.z);
          surfIdx[i][j] = nVert++;
        }
      }
      const bedFaces = [];
      const surfFaces = [];
      const sideFaces = [];
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
          pushQuad(sideFaces, bedIdx[i][jb], bedIdx[i + 1][jb], surfIdx[i][jb], surfIdx[i + 1][jb]);
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
          pushQuad(sideFaces, bedIdx[ib][j], bedIdx[ib][j + 1], surfIdx[ib][j], surfIdx[ib][j + 1]);
        }
      }
      const g = addVolumeMeshes(positions, bedFaces, surfFaces, sideFaces, nVert);
      if (g) return g;
    }

    // 2) Fallback rings → khối nước (day DEM + mặt phẳng level).
    if (!Array.isArray(ws.rings) || !ws.rings.length) return null;
    const groupAll = new THREE.Group();
    groupAll.name = "reservoirWater";
    groupAll.userData = meta;
    let any = false;
    ws.rings.forEach(function (ring) {
      if (!Array.isArray(ring) || ring.length < 3) return;
      const top2 = [];
      const positions = [];
      let n = 0;
      for (let i = 0; i < ring.length; i++) {
        const lon = Number(ring[i][0]);
        const lat = Number(ring[i][1]);
        if (!isFinite(lon) || !isFinite(lat)) continue;
        const p = global.Dem3D.lonLatToWorld(lon, lat);
        if (!p || !isFinite(p.x) || !isFinite(p.z)) continue;
        let zb = sampleElevOr(lon, lat, NaN);
        if (!isFinite(zb)) zb = level - 2;
        if (zb > level - MIN_D) zb = level - MIN_D;
        const yb = global.Dem3D.elevToWorldY(zb) + liftY;
        positions.push(p.x, yb, p.z);
        positions.push(p.x, ySurfFlat, p.z);
        top2.push(new THREE.Vector2(p.x, p.z));
        n++;
      }
      if (n < 3) return;
      const f = top2[0];
      const l = top2[n - 1];
      if (Math.hypot(f.x - l.x, f.y - l.y) > 1e-3) {
        positions.push(positions[0], positions[1], positions[2]);
        positions.push(positions[3], positions[4], positions[5]);
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
        sideFaces.push(bedIdx[i], bedIdx[i + 1], surfIdx[i], surfIdx[i], bedIdx[i + 1], surfIdx[i + 1]);
      }
      const g = addVolumeMeshes(positions, bedFaces, surfFaces, sideFaces, n * 2);
      if (g) {
        while (g.children.length) groupAll.add(g.children[0]);
        any = true;
      }
    });
    return any ? groupAll : null;
  }

  function structureDimsMeters(st) {
    const t = String(st.type || "").toLowerCase();
    const w = Number(st.width_m);
    const L = Number(st.length_m);
    const hIn = Number(st.height_m);
    const crest = Number(st.crest_m);
    const invert = Number(st.invert_m);
    const demZ = (st.lon != null && st.lat != null && global.Dem3D && global.Dem3D.sampleElevation)
      ? Number(global.Dem3D.sampleElevation(st.lon, st.lat))
      : NaN;

    let height = NaN;
    if (isFinite(crest) && isFinite(invert) && crest > invert) height = crest - invert;
    else if (isFinite(hIn) && hIn > 0) height = hIn;
    else if (isFinite(crest) && isFinite(demZ)) height = Math.max(crest - demZ, 1.0);
    else if (isFinite(hIn)) height = Math.max(hIn, 1.0);

    const defaults = {
      dike: { across: 12, along: 200, height: 4 },
      weir: { across: 40, along: 8, height: 3 },
      gate: { across: 10, along: 4, height: 4 },
      culvert: { across: 3, along: 20, height: 2.5 },
      reservoir: { across: 60, along: 60, height: 8 },
      pump: { across: 8, along: 8, height: 5 }
    };
    const d0 = defaults[t] || { across: 10, along: 10, height: 4 };
    if (!isFinite(height) || height <= 0) height = d0.height;

    let across;
    let along;
    if (t === "dike") {
      // length_m: chieu dai de theo song; width_m: be rong tran (cung theo song) hoac be day.
      const alongCand = (isFinite(L) && L > 0) ? L : ((isFinite(w) && w > 0) ? w : d0.along);
      along = alongCand;
      if (isFinite(w) && w > 0 && isFinite(L) && L > 0 && Math.min(w, L) / Math.max(w, L) > 0.7) {
        // Ca hai ~ bang nhau (vd 2500/2500): coi la chieu dai tran, be day mong.
        across = Math.min(30, Math.max(8, along * 0.008));
      } else if (isFinite(w) && w > 0 && w < along * 0.35) {
        across = w;
      } else {
        across = d0.across;
      }
    } else if (t === "weir" || t === "gate" || t === "reservoir") {
      across = (isFinite(w) && w > 0) ? w : d0.across;
      // Be day theo dong chay: giu mong (ho chua ve nhu dap ngang long).
      along = (isFinite(L) && L > 0)
        ? Math.min(L, t === "reservoir" ? 60 : 40)
        : (t === "reservoir" ? 20 : d0.along);
    } else if (t === "culvert") {
      across = (isFinite(w) && w > 0) ? w : d0.across;
      along = (isFinite(L) && L > 0) ? L : d0.along;
      if (isFinite(hIn) && hIn > 0) height = hIn;
    } else {
      across = (isFinite(w) && w > 0) ? w : d0.across;
      along = (isFinite(L) && L > 0) ? L : d0.along;
    }

    return {
      across: Math.max(across, 0.5),
      along: Math.max(along, 0.5),
      height: Math.max(height, 0.5),
      zBottom: isFinite(invert) ? invert : (isFinite(demZ) ? demZ : (isFinite(crest) ? crest - height : 0)),
      zTop: isFinite(crest) ? crest : (isFinite(invert) ? invert + height : (isFinite(demZ) ? demZ + height : height))
    };
  }

  function makeStructurePathMesh(st, dims, col) {
    const THREE = global.THREE;
    const lons0 = st.path_lon;
    const lats0 = st.path_lat;
    if (!THREE || !Array.isArray(lons0) || !Array.isArray(lats0) || lons0.length < 2) return null;
    const zPath0 = Array.isArray(st.path_z) ? st.path_z : null;
    const toXZ = global.Dem3D.metersToWorldXZ
      ? function (m) { return global.Dem3D.metersToWorldXZ(m); }
      : function (m) { return m * 0.01; };
    const toY = global.Dem3D.metersToWorldY
      ? function (m) { return global.Dem3D.metersToWorldY(m); }
      : toXZ;
    const t = String(st.type || "").toLowerCase();
    const pathKind = String(st.path_kind || "").toLowerCase();
    const acrossBed = pathKind === "across_bed" || t === "weir" || t === "gate" || t === "reservoir";
    // De: be day = across; dap/tran ngang long: be day theo dong chay = along (mong).
    const thickM = acrossBed ? Math.min(Math.max(dims.along, 4), 16) : dims.across;
    const halfW = toXZ(thickM) * 0.5;
    const crest = Number(st.crest_m);
    const hasCrest = isFinite(crest);
    const yCrest = hasCrest ? global.Dem3D.elevToWorldY(crest) : NaN;
    const damCrest = Number(st.dam_crest_m);
    const hasDamCrest = (t === "reservoir" || t === "weir") && isFinite(damCrest);
    const yDamCrest = hasDamCrest ? global.Dem3D.elevToWorldY(damCrest) : NaN;
    const minHy = toY(1.0);
    // Đê chỉ hạ rất nhẹ để tránh z-fighting; hạ sâu 0.8 m làm chân đê cắm
    // vào sườn DEM. Đập/tràn ngang lòng vẫn cần chìm sâu để khép kín đáy.
    const sinkY = toY(acrossBed ? 2.5 : (t === "dike" ? 0.08 : 0.8));

    // Densify + noi suy path_z de giu khac sau long (khong mat diem day).
    const densLon = [];
    const densLat = [];
    const densZ = [];
    const maxSegM = acrossBed ? 5 : 25;
    const n0 = Math.min(lons0.length, lats0.length);
    function zAtSrc(i) {
      if (!zPath0 || zPath0[i] == null) return NaN;
      const v = Number(zPath0[i]);
      return isFinite(v) ? v : NaN;
    }
    for (let i = 0; i < n0; i++) {
      const lon = Number(lons0[i]);
      const lat = Number(lats0[i]);
      if (!isFinite(lon) || !isFinite(lat)) continue;
      const zi = zAtSrc(i);
      if (!densLon.length) {
        densLon.push(lon);
        densLat.push(lat);
        densZ.push(zi);
        continue;
      }
      const pLon = densLon[densLon.length - 1];
      const pLat = densLat[densLat.length - 1];
      const pZ = densZ[densZ.length - 1];
      const segM = haversineM(pLon, pLat, lon, lat);
      const steps = Math.max(1, Math.ceil(segM / maxSegM));
      for (let s = 1; s <= steps; s++) {
        const u = s / steps;
        densLon.push(pLon + (lon - pLon) * u);
        densLat.push(pLat + (lat - pLat) * u);
        if (isFinite(pZ) && isFinite(zi)) densZ.push(pZ + (zi - pZ) * u);
        else densZ.push(isFinite(zi) ? zi : pZ);
      }
    }
    if (densLon.length > 500) {
      const keep = [];
      const keepLat = [];
      const keepZ = [];
      const step = (densLon.length - 1) / 499;
      for (let k = 0; k < 500; k++) {
        const i = Math.min(densLon.length - 1, Math.round(k * step));
        keep.push(densLon[i]);
        keepLat.push(densLat[i]);
        keepZ.push(densZ[i]);
      }
      densLon.length = 0;
      densLat.length = 0;
      densZ.length = 0;
      for (let i = 0; i < keep.length; i++) {
        densLon.push(keep[i]);
        densLat.push(keepLat[i]);
        densZ.push(keepZ[i]);
      }
    }

    const pts = [];
    for (let i = 0; i < densLon.length; i++) {
      const lon = densLon[i];
      const lat = densLat[i];
      // Bam mat DEM 3D dang hien (long -> mep bo); path_z bo sung khi sample loi.
      let elevBed = sampleElevOr(lon, lat, NaN);
      if (!isFinite(elevBed) && isFinite(densZ[i])) elevBed = densZ[i];
      if (!isFinite(elevBed)) continue;
      if (!pointOnDem(lon, lat) && !isFinite(densZ[i])) continue;
      const xz = global.Dem3D.lonLatToWorld(lon, lat);
      if (!xz || !isFinite(xz.x) || !isFinite(xz.z)) continue;
      // Day bam DEM/path_z (long -> mep bo -> dinh); dinh = crest ngang.
      // invert_m chi dung thuy luc, KHONG ve day phang (tranh khoi hop).
      const yBed = global.Dem3D.elevToWorldY(elevBed) - sinkY;
      let yb = yBed;
      let yt;
      if (t === "dike" && hasCrest) {
        // Đê là một polygon có mép đỉnh cùng cao trình thiết kế. Nếu DEM cục
        // bộ cao hơn crest, nhấn đáy xuống thay vì đẩy đỉnh lên thành răng cưa.
        yb = Math.min(yBed, yCrest - minHy);
        yt = yCrest;
      } else if (hasDamCrest) {
        // Đỉnh đập là một cao trình phẳng, đúng bằng cao trình đỉnh đập.
        yb = Math.min(yBed, yDamCrest - minHy);
        yt = yDamCrest;
      } else if (acrossBed && hasCrest) {
        if (elevBed >= crest - 0.1) {
          // Gan dinh bo: chi con lop mong tren mat dat.
          yb = yBed;
          yt = yBed + minHy + sinkY;
        } else {
          // Day theo dia hinh, con mat dinh de/nguong la mot cao trinh crest
          // lien tuc; khong cong chieu cao vao tung o DEM gay canh rang cua.
          yb = yBed;
          yt = Math.max(yCrest, yBed + minHy);
        }
      } else {
        let hy = toY(dims.height);
        if (!(hy > 1e-6)) {
          const y0 = global.Dem3D.elevToWorldY(dims.zBottom);
          const y1 = global.Dem3D.elevToWorldY(dims.zTop);
          hy = Math.abs(y1 - y0);
        }
        hy = Math.max(hy, minHy);
        yb = yBed;
        yt = yb + hy + sinkY;
      }
      if (!(yt > yb + minHy * 0.5)) yt = yb + minHy;
      pts.push({ x: xz.x, yb: yb, yt: yt, z: xz.z, elev: elevBed, lon: lon, lat: lat });
    }
    if (pts.length < 2) return null;

    // De doc bo can la mot dai lien mach. Track mep bo tu DEM co the dao qua lai
    // tung o raster; lam muot trong mat phang truoc khi tao hai canh cua polygon.
    // Khong ap dung cho dap/tran ngang long de van giu dung hinh dang mat cat.
    if (!acrossBed) {
      for (let pass = 0; pass < 2; pass++) {
        const xs = pts.map(function (p) { return p.x; });
        const zs = pts.map(function (p) { return p.z; });
        for (let i = 1; i < pts.length - 1; i++) {
          pts[i].x = xs[i] * 0.5 + (xs[i - 1] + xs[i + 1]) * 0.25;
          pts[i].z = zs[i] * 0.5 + (zs[i - 1] + zs[i + 1]) * 0.25;
        }
      }
      for (let pass = 0; pass < 2; pass++) {
        const ybs = pts.map(function (p) { return p.yb; });
        for (let i = 1; i < pts.length - 1; i++) {
          pts[i].yb = ybs[i] * 0.5 + (ybs[i - 1] + ybs[i + 1]) * 0.25;
          const hy = Math.max(pts[i].yt - ybs[i], minHy);
          pts[i].yt = pts[i].yb + hy;
        }
      }
      if (t === "dike" && hasCrest) {
        pts.forEach(function (p) {
          p.yb = Math.min(p.yb, yCrest - minHy);
          p.yt = yCrest;
        });
      }
    }

    const positions = [];
    const indices = [];
    for (let i = 0; i < pts.length; i++) {
      const i0 = Math.max(0, i - 1);
      const i1 = Math.min(pts.length - 1, i + 1);
      let tx0 = pts[i].x - pts[i0].x;
      let tz0 = pts[i].z - pts[i0].z;
      let tx1 = pts[i1].x - pts[i].x;
      let tz1 = pts[i1].z - pts[i].z;
      if (i === 0) {
        tx0 = tx1;
        tz0 = tz1;
      } else if (i === pts.length - 1) {
        tx1 = tx0;
        tz1 = tz0;
      }
      const len0 = Math.hypot(tx0, tz0) || 1;
      const len1 = Math.hypot(tx1, tz1) || 1;
      tx0 /= len0;
      tz0 /= len0;
      tx1 /= len1;
      tz1 /= len1;

      // Miter join: hai canh chung dung mot dinh tai moi station, khong tao khe
      // hay cac tam giac thua o cho duong de doi huong.
      const n0x = -tz0;
      const n0z = tx0;
      const n1x = -tz1;
      const n1z = tx1;
      let nx = n0x + n1x;
      let nz = n0z + n1z;
      const nlen = Math.hypot(nx, nz);
      if (nlen > 1e-6) {
        nx /= nlen;
        nz /= nlen;
      } else {
        nx = n1x;
        nz = n1z;
      }
      const denom = Math.max(0.35, Math.abs(nx * n1x + nz * n1z));
      const joinW = Math.min(halfW / denom, halfW * 2.25);
      let ybL = pts[i].yb;
      let ybR = pts[i].yb;
      const px = nx * joinW;
      const pz = nz * joinW;
      const yt = pts[i].yt;
      // Sample DEM ở cả hai cạnh để đáy polygon bám đúng sườn bờ. Với đê,
      // không dùng cao độ tại tim cho cả bề rộng vì cạnh phía bãi sẽ cắm vào DEM.
      if ((acrossBed || t === "dike") && global.Dem3D.worldToLonLat) {
        const llL = global.Dem3D.worldToLonLat(pts[i].x - px, pts[i].z - pz);
        const llR = global.Dem3D.worldToLonLat(pts[i].x + px, pts[i].z + pz);
        if (llL && pointOnDem(llL.lon, llL.lat)) {
          const e = sampleElevOr(llL.lon, llL.lat, pts[i].elev);
          if (isFinite(e)) {
            const edgeY = global.Dem3D.elevToWorldY(e) - sinkY;
            ybL = t === "dike" ? Math.min(edgeY, yt - minHy) : Math.min(pts[i].yb, edgeY);
          }
        }
        if (llR && pointOnDem(llR.lon, llR.lat)) {
          const e = sampleElevOr(llR.lon, llR.lat, pts[i].elev);
          if (isFinite(e)) {
            const edgeY = global.Dem3D.elevToWorldY(e) - sinkY;
            ybR = t === "dike" ? Math.min(edgeY, yt - minHy) : Math.min(pts[i].yb, edgeY);
          }
        }
      }
      positions.push(pts[i].x - px, ybL, pts[i].z - pz);
      positions.push(pts[i].x + px, ybR, pts[i].z + pz);
      positions.push(pts[i].x - px, yt, pts[i].z - pz);
      positions.push(pts[i].x + px, yt, pts[i].z + pz);
    }

    function quad(a, b, c, d) {
      indices.push(a, b, c, a, c, d);
    }
    for (let i = 0; i < pts.length - 1; i++) {
      const a = i * 4;
      const b = (i + 1) * 4;
      quad(a + 0, a + 1, b + 1, b + 0);
      quad(a + 2, b + 2, b + 3, a + 3);
      quad(a + 0, b + 0, b + 2, a + 2);
      quad(a + 1, a + 3, b + 3, b + 1);
    }
    const last = (pts.length - 1) * 4;
    quad(0, 2, 3, 1);
    quad(last + 0, last + 1, last + 3, last + 2);

    const geo = new THREE.BufferGeometry();
    const posArr = new Float32Array(positions);
    if (THREE.Float32BufferAttribute) {
      geo.setAttribute("position", new THREE.Float32BufferAttribute(posArr, 3));
    } else {
      geo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));
    }
    geo.setIndex(indices);
    geo.computeVertexNormals();

    const mat = new THREE.MeshBasicMaterial({
      color: col,
      // Đê là polygon đặc; đường tim đã được làm trơn nên có thể kiểm tra depth
      // bình thường để phần chân khuất đúng sau DEM, không xuyên vào địa hình.
      transparent: t !== "dike",
      opacity: t === "dike" ? 1.0 : (t === "reservoir" ? 0.55 : 0.92),
      depthTest: true,
      depthWrite: true,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -1,
      polygonOffsetUnits: -1
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 35;
    mesh.userData = { kind: "structure", id: st.id, type: st.type, record: st };
    appendReservoirOutletGates(mesh, st, pts, halfW);
    return mesh;
  }

  function outletGatePlan(st) {
    const t = String(st && st.type || "").toLowerCase();
    if (t !== "reservoir" && t !== "weir") return null;
    const sill = Number(st.outlet_sill_m);
    const top = Number(st.spillway_crest_m);
    if (!isFinite(sill) || !isFinite(top) || !(top > sill)) return null;
    const widths = parseOutletGateWidths(st.outlet_gate_widths_m).map(function (v) {
      return Number(v);
    }).filter(function (v) { return isFinite(v) && v > 0; });
    let count = Math.floor(Number(st.outlet_gate_count));
    if (!isFinite(count) || count <= 0) count = widths.length;
    if (!widths.length || !count) return null;
    const used = widths.slice(0, count);
    while (used.length < count) used.push(used[used.length - 1]);
    const left = Number(st.gate_left_offset_m);
    const gap = Number(st.gate_spacing_m);
    const leftM = isFinite(left) && left > 0 ? left : 0;
    const gapM = isFinite(gap) && gap > 0 ? gap : 0;
    const gates = [];
    let cursor = leftM;
    used.forEach(function (width) {
      gates.push({ s0: cursor, s1: cursor + width });
      cursor += width + gapM;
    });
    return { sill: sill, top: top, gates: gates };
  }

  function appendReservoirOutletGates(parent, st, pts, halfW) {
    const THREE = global.THREE;
    const plan = outletGatePlan(st);
    if (!THREE || !plan || !parent || !pts || pts.length < 2 || !global.Dem3D) return;
    if (!isFinite(pts[0].lon) || !isFinite(pts[0].lat)) return;
    const chain = [0];
    for (let i = 1; i < pts.length; i++) {
      chain.push(chain[i - 1] + haversineM(pts[i - 1].lon, pts[i - 1].lat, pts[i].lon, pts[i].lat));
    }
    const total = chain[chain.length - 1];
    if (!(total > 1)) return;
    const ySill = global.Dem3D.elevToWorldY(plan.sill);
    const yTop = global.Dem3D.elevToWorldY(plan.top);
    if (!isFinite(ySill) || !isFinite(yTop) || !(Math.abs(yTop - ySill) > 1e-6)) return;
    const yb = Math.min(ySill, yTop);
    const yt = Math.max(ySill, yTop);
    const gateHalf = Math.max(halfW * 1.2, halfW + 0.15);
    const positions = [];
    const indices = [];

    function at(s) {
      let i = 1;
      while (i < pts.length - 1 && chain[i] < s) i++;
      const a = pts[i - 1];
      const b = pts[i];
      const span = (chain[i] - chain[i - 1]) || 1;
      const u = Math.max(0, Math.min(1, (s - chain[i - 1]) / span));
      let tx = b.x - a.x;
      let tz = b.z - a.z;
      const len = Math.hypot(tx, tz) || 1;
      tx /= len;
      tz /= len;
      return {
        x: a.x + (b.x - a.x) * u,
        z: a.z + (b.z - a.z) * u,
        nx: -tz,
        nz: tx
      };
    }

    function pushSection(p) {
      const px = p.nx * gateHalf;
      const pz = p.nz * gateHalf;
      const base = positions.length / 3;
      positions.push(p.x - px, yb, p.z - pz);
      positions.push(p.x + px, yb, p.z + pz);
      positions.push(p.x - px, yt, p.z - pz);
      positions.push(p.x + px, yt, p.z + pz);
      return base;
    }

    function quad(a, b, c, d) {
      indices.push(a, b, c, a, c, d);
    }

    plan.gates.forEach(function (g) {
      if (g.s0 >= total - 0.15) return;
      const s0 = Math.max(0, g.s0);
      const s1 = Math.min(total, g.s1);
      if (s1 - s0 < 0.25) return;
      const marks = [s0];
      for (let s = s0 + 4; s < s1 - 0.2; s += 4) marks.push(s);
      marks.push(s1);
      const bases = marks.map(function (s) { return pushSection(at(s)); });
      for (let i = 0; i < bases.length - 1; i++) {
        const a = bases[i];
        const b = bases[i + 1];
        quad(a + 0, a + 1, b + 1, b + 0);
        quad(a + 2, b + 2, b + 3, a + 3);
        quad(a + 0, b + 0, b + 2, a + 2);
        quad(a + 1, a + 3, b + 3, b + 1);
      }
      const a0 = bases[0];
      const a1 = bases[bases.length - 1];
      quad(a0 + 0, a0 + 2, a0 + 3, a0 + 1);
      quad(a1 + 0, a1 + 1, a1 + 3, a1 + 2);
    });
    if (positions.length < 12) return;

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(new Float32Array(positions), 3));
    geo.setIndex(indices);
    geo.computeVertexNormals();
    const mat = new THREE.MeshBasicMaterial({
      color: 0x000000,
      depthTest: true,
      depthWrite: true,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4,
      polygonOffsetUnits: -4
    });
    const gate = new THREE.Mesh(geo, mat);
    gate.name = "outletGates";
    gate.renderOrder = 42;
    gate.userData = parent.userData;
    parent.add(gate);
  }

  function makeStructureMesh(st) {
    const THREE = global.THREE;
    if (!THREE || !global.Dem3D || st.lon == null || st.lat == null) return null;
    const dims = structureDimsMeters(st);
    const t = String(st.type || "").toLowerCase();
    const col = STRUCTURE_COLORS[t] || 0xffffff;
    const hasPath = Array.isArray(st.path_lon) && st.path_lon.length >= 2 &&
      (t === "dike" || t === "weir" || t === "gate" || t === "reservoir" || st.path_kind);

    if (hasPath) {
      try {
        const curved = makeStructurePathMesh(st, dims, col);
        if (curved) return curved;
      } catch (err) {
        console.warn("[structure-path]", st && st.id, err);
      }
      // Khong bao gio ve hop phang cho dap/tran/ho (gay ho day so voi DEM).
      if (t === "weir" || t === "gate" || t === "reservoir" || String(st.path_kind || "") === "across_bed") return null;
    }

    // Weir/gate/reservoir khong co path: dung phap tuyen + sample DEM, khong BoxGeometry.
    if (t === "weir" || t === "gate" || t === "reservoir") {
      const fe = Number(st.flow_east);
      const fn = Number(st.flow_north);
      let nx = 1;
      let ny = 0;
      if (isFinite(fe) && isFinite(fn) && Math.abs(fe) + Math.abs(fn) > 1e-9) {
        const L = Math.hypot(fe, fn) || 1;
        nx = -fn / L;
        ny = fe / L;
      }
      const half = Math.max(Number(dims.across) || 40, 20) * 0.5;
      const synLon = [];
      const synLat = [];
      const synZ = [];
      const steps = Math.max(24, Math.min(160, Math.round(half / 2.5)));
      const toRad = Math.PI / 180;
      const cosLat = Math.cos(Number(st.lat) * toRad);
      for (let i = 0; i <= steps; i++) {
        const o = -half + (2 * half * i) / steps;
        const lo = Number(st.lon) + (nx * o) / Math.max(111320 * cosLat, 1e-6);
        const la = Number(st.lat) + (ny * o) / 110540;
        const e = sampleElevOr(lo, la, NaN);
        if (!isFinite(e)) continue;
        synLon.push(lo);
        synLat.push(la);
        synZ.push(e);
      }
      if (synLon.length >= 3) {
        const mesh = makeStructurePathMesh({
          id: st.id,
          type: st.type,
          path_lon: synLon,
          path_lat: synLat,
          path_z: synZ,
          path_kind: "across_bed",
          crest_m: st.crest_m,
          dam_crest_m: st.dam_crest_m,
          invert_m: st.invert_m,
          width_m: st.width_m,
          length_m: st.length_m,
          height_m: st.height_m,
          outlet_sill_m: st.outlet_sill_m,
          spillway_crest_m: st.spillway_crest_m,
          gate_left_offset_m: st.gate_left_offset_m,
          gate_spacing_m: st.gate_spacing_m,
          outlet_gate_count: st.outlet_gate_count,
          outlet_gate_widths_m: st.outlet_gate_widths_m
        }, dims, col);
        if (mesh) {
          mesh.userData = { kind: "structure", id: st.id, type: st.type, record: st };
          return mesh;
        }
      }
      return null;
    }

    if (!pointOnDem(st.lon, st.lat)) return null;

    const toXZ = global.Dem3D.metersToWorldXZ
      ? function (m) { return global.Dem3D.metersToWorldXZ(m); }
      : function (m) { return m * 0.01; };
    const wx = toXZ(dims.across);
    const wz = toXZ(dims.along);
    const y0 = global.Dem3D.elevToWorldY(dims.zBottom);
    const y1 = global.Dem3D.elevToWorldY(dims.zTop);
    let hy = Math.abs(y1 - y0);
    if (!(hy > 1e-6) && global.Dem3D.metersToWorldY) {
      hy = global.Dem3D.metersToWorldY(dims.height);
    }
    hy = Math.max(hy, toXZ(0.5));

    let geo;
    if (t === "culvert") {
      geo = new THREE.CylinderGeometry(Math.max(wx, toXZ(dims.height)) * 0.5, Math.max(wx, toXZ(dims.height)) * 0.5, wz, 16, 1, false);
    } else if (t === "pump") {
      geo = new THREE.CylinderGeometry(Math.min(wx, wz) * 0.5, Math.min(wx, wz) * 0.45, hy, 12);
    } else {
      geo = new THREE.BoxGeometry(wx, hy, wz);
    }
    const mat = new THREE.MeshBasicMaterial({
      color: col,
      transparent: true,
      opacity: t === "reservoir" ? 0.45 : 0.88,
      depthTest: true,
      depthWrite: false
    });
    const mesh = new THREE.Mesh(geo, mat);
    const xz = global.Dem3D.lonLatToWorld(st.lon, st.lat);
    mesh.position.set(xz.x, Math.min(y0, y1) + hy * 0.5, xz.z);

    // Quay theo huong dong chay: local Z = along, local X = across.
    const fe = Number(st.flow_east);
    const fn = Number(st.flow_north);
    const yaw = (isFinite(fe) && isFinite(fn) && (Math.abs(fe) + Math.abs(fn)) > 1e-9)
      ? Math.atan2(fe, -fn)
      : 0;
    if (t === "culvert") {
      // Cylinder truc Y -> nam ngang theo dong chay (truc ~ local Z).
      mesh.rotation.order = "YXZ";
      mesh.rotation.x = Math.PI / 2;
      mesh.rotation.y = yaw;
    } else if (t !== "pump") {
      mesh.rotation.y = yaw;
    }
    mesh.renderOrder = 35;
    mesh.userData = {
      kind: "structure",
      id: st.id,
      type: st.type,
      record: st
    };
    return mesh;
  }

  async function loadNetworkOverlay(force) {
    if (networkOverlayLoading) return;
    if (!global.Dem3D || !global.Dem3D.getContext || !global.Dem3D.getContext()) return;
    networkOverlayLoading = true;
    try {
      const qs = "?water_source=" + encodeURIComponent(selectedWaterSource());
      const res = await fetch(API + "/network-overlay" + qs, {
        headers: { "Accept": "application/json" }
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không tải được lớp mặt cắt / công trình.");
      }
      networkOverlayData = data;
      drawNetworkOverlay(data);
    } catch (err) {
      console.warn("[network-overlay]", err && err.message ? err.message : err);
      if (force) setHint(err.message || String(err));
    } finally {
      networkOverlayLoading = false;
    }
  }

  function refreshNetworkOverlayVisibility() {
    const xsEl = $("demXsToggle");
    const stEl = $("demStructuresToggle");
    networkShowXs = !xsEl || !!xsEl.checked;
    networkShowStructures = !stEl || !!stEl.checked;
    if (!networkShowStructures) closeStructurePopup();
    if (networkOverlayData) drawNetworkOverlay(networkOverlayData);
    else loadNetworkOverlay(false);
  }

  function haversineM(lon1, lat1, lon2, lat2) {
    const toRad = Math.PI / 180;
    const r = 6371000;
    const dLat = (lat2 - lat1) * toRad;
    const dLon = (lon2 - lon1) * toRad;
    const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
      Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * r * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  function pickNetworkXsByRay(clientX, clientY) {
    const THREE = global.THREE;
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    const canvas = canvasEl();
    if (!THREE || !ctx || !ctx.camera || !networkOverlayGroup || !canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const mouse = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    if (raycaster.params && raycaster.params.Line) raycaster.params.Line.threshold = 0.35;
    raycaster.setFromCamera(mouse, ctx.camera);
    const hits = raycaster.intersectObjects(networkOverlayGroup.children, true);
    let bestMarker = null;
    let bestMarkerDist = Infinity;
    let bestAny = null;
    let bestAnyDist = Infinity;
    for (let i = 0; i < hits.length; i++) {
      let obj = hits[i].object;
      let meta = null;
      while (obj) {
        if (obj.userData && obj.userData.kind === "xs" && obj.userData.xs_id != null) {
          meta = obj.userData;
          break;
        }
        obj = obj.parent;
      }
      if (!meta) continue;
      const d = hits[i].distance;
      if (meta.pickRole === "marker" && d < bestMarkerDist) {
        bestMarkerDist = d;
        bestMarker = meta;
      }
      if (d < bestAnyDist) {
        bestAnyDist = d;
        bestAny = meta;
      }
    }
    return bestMarker || bestAny;
  }

  function pickNetworkXsNearest(lon, lat, maxM, onlyDisplayed) {
    if (!networkOverlayData || !networkOverlayData.sections) return null;
    const limit = maxM != null ? maxM : 250;
    const source = onlyDisplayed
      ? sectionsForOverlayDisplay(networkOverlayData.sections)
      : networkOverlayData.sections;
    let best = null;
    let bestD = Infinity;
    source.forEach(function (sec) {
      const clon = sec.center_lon;
      const clat = sec.center_lat;
      if (clon == null || clat == null) return;
      const d = haversineM(lon, lat, clon, clat);
      if (d < bestD) {
        bestD = d;
        best = {
          kind: "xs",
          xs_id: sec.xs_id,
          reach: sec.reach || "main",
          station_km: sec.station_km,
          manning_n: sec.manning_n,
          center_lon: clon,
          center_lat: clat,
          pickRole: "marker",
          _dist_m: d
        };
      }
    });
    if (!best || best._dist_m > limit) return null;
    return best;
  }

  async function requestXsProfile(xsId, reach, extra) {
    if (xsProfileLoading) return;
    xsProfileLoading = true;
    const ex = extra || {};
    selectedXsKey = String(reach || "main") + ":" + String(xsId);
    if (networkOverlayData) drawNetworkOverlay(networkOverlayData);
    try {
      let qs =
        "?xs_id=" + encodeURIComponent(xsId) +
        "&reach=" + encodeURIComponent(reach || "main") +
        "&water_source=" + encodeURIComponent(selectedWaterSource()) +
        (fileId() ? "&file_id=" + encodeURIComponent(fileId()) : "");
      if (ex.station_km != null && isFinite(Number(ex.station_km))) {
        qs += "&station_km=" + encodeURIComponent(ex.station_km);
      }
      const res = await fetch(API + "/xs-profile" + qs, { headers: { "Accept": "application/json" } });
      const text = await res.text();
      let data = null;
      try {
        data = text ? JSON.parse(text.replace(/^\uFEFF/, "")) : null;
      } catch (_e) {
        throw new Error(messageFromHttpBody(text, res.status));
      }
      if (!res.ok || !data || data.ok === false) {
        throw new Error(
          (data && (data.error || data.message)) ||
          messageFromHttpBody(text, res.status) ||
          "Không tải được profile mặt cắt"
        );
      }
      // Bao dam tieu de / highlight dung XS vua click (khong de route_xs ghi de).
      data.xs_id = Number(xsId);
      data.reach = reach || data.reach || "main";
      data.source = "xs_click";
      if (ex.manning_n != null && data.manning_n == null) data.manning_n = ex.manning_n;
      if (ex.station_km != null && data.station_km == null) data.station_km = ex.station_km;
      lastRequest = { type: "xs_profile", xs_id: xsId, reach: reach || "main" };
      applyPayload(data);
    } catch (err) {
      setHint(err.message || String(err));
    } finally {
      xsProfileLoading = false;
    }
  }

  function pickNetworkStructureByRay(clientX, clientY) {
    const THREE = global.THREE;
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    const canvas = canvasEl();
    if (!THREE || !ctx || !ctx.camera || !networkOverlayGroup || !canvas) return null;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const mouse = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(mouse, ctx.camera);
    const hits = raycaster.intersectObjects(networkOverlayGroup.children, true);
    let best = null;
    let bestD = Infinity;
    for (let i = 0; i < hits.length; i++) {
      let obj = hits[i].object;
      let rec = null;
      while (obj) {
        if (obj.userData && obj.userData.kind === "structure" && obj.userData.record) {
          rec = obj.userData.record;
          break;
        }
        obj = obj.parent;
      }
      if (!rec) continue;
      if (hits[i].distance < bestD) {
        bestD = hits[i].distance;
        best = rec;
      }
    }
    return best;
  }

  function pickNetworkStructureNearest(lon, lat, maxM) {
    if (!networkOverlayData || !networkOverlayData.structures) return null;
    const limit = maxM != null ? maxM : 180;
    let best = null;
    let bestD = Infinity;
    networkOverlayData.structures.forEach(function (st) {
      if (st.lon == null || st.lat == null) return;
      const d = haversineM(lon, lat, st.lon, st.lat);
      if (d < bestD) {
        bestD = d;
        best = st;
      }
    });
    if (!best || bestD > limit) return null;
    return best;
  }

  function fmtStructureVal(v) {
    if (v == null || v === "") return "—";
    if (typeof v === "number" && isFinite(v)) {
      const a = Math.abs(v);
      if (a >= 1000) return String(Math.round(v * 10) / 10);
      if (a >= 10) return String(Math.round(v * 100) / 100);
      return String(Math.round(v * 1000) / 1000);
    }
    return String(v);
  }

  function storageAreaM2ToKm2(m2) {
    const n = Number(m2);
    if (!isFinite(n) || String(m2).trim() === "") return "";
    const km2 = n / 1e6;
    if (Math.abs(km2) >= 1) return String(Math.round(km2 * 1000) / 1000);
    if (Math.abs(km2) >= 0.01) return String(Math.round(km2 * 1e4) / 1e4);
    return String(Math.round(km2 * 1e6) / 1e6);
  }

  function storageAreaKm2ToM2(km2) {
    const n = Number(km2);
    if (!isFinite(n) || String(km2).trim() === "") return "";
    return String(Math.round(n * 1e6 * 10) / 10);
  }

  function placementLabel(v) {
    const k = String(v || "").toLowerCase();
    if (k === "inline") return "Trên dòng (inline)";
    if (k === "lateral") return "Bên / tràn bãi (lateral)";
    if (k === "auto") return "Tự động";
    return v || "—";
  }

  function bankSideLabel(v) {
    const k = String(v || "").toLowerCase();
    if (k === "left") return "Tả ngạn (trái)";
    if (k === "right") return "Hữu ngạn (phải)";
    return v || "—";
  }

  function valveLabel(v) {
    const k = String(v || "").toLowerCase();
    if (k === "both") return "Hai chiều";
    if (k === "positive") return "Thuận dòng";
    if (k === "negative") return "Ngược dòng";
    return v || "—";
  }

  function controlLabel(v) {
    const k = String(v || "").toLowerCase();
    if (k === "free") return "Tự do";
    if (k === "controlled") return "Điều khiển";
    if (k === "closed") return "Đóng";
    return v || "—";
  }

  function closeStructurePopup() {
    const card = $("structureInfoCard");
    if (card) card.hidden = true;
  }

  function escapeHtmlText(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showStructurePopup(st) {
    if (!st) return;
    const card = $("structureInfoCard");
    const body = $("structureInfoBody");
    const title = $("structureInfoTitle");
    if (!card || !body) return;
    const name = st.name || st.id || st.type_label || "Công trình";
    if (title) title.textContent = (st.id ? st.id + " · " : "") + name;
    const rows = [
      ["Loại", st.type_label || st.type],
      ["Sông / nhánh", st.reach],
      ["Lý trình (km)", st.station_km],
      ["XS", st.xs_id],
      ["Kinh độ", st.lon != null ? Number(st.lon).toFixed(6) : null],
      ["Vĩ độ", st.lat != null ? Number(st.lat).toFixed(6) : null],
      ["Đỉnh / ngưỡng Hw (m)", st.crest_m],
      ["Cao trình đáy (m)", st.invert_m],
      ["Bề rộng W (m)", st.width_m],
      ["Chiều dài (m)", st.length_m],
      ["Chiều cao (m)", st.height_m],
      ["Độ mở cửa (m)", st.gate_opening_m],
      ["Hệ số C / Cd", st.cd],
      ["Số mũ k", st.submerged_exp],
      ["Công thức", st.formula_label || st.formula],
      ["Vị trí trên lưới", placementLabel(st.placement)],
      ["Bờ", bankSideLabel(st.bank_side)],
      ["Lệch bờ (m)", st.bank_offset_m],
      ["Van / chiều chảy", valveLabel(st.valve)],
      ["Chế độ", controlLabel(st.control)],
      ["Q lớn nhất (m³/s)", st.q_max_m3s],
      ["Q nhỏ nhất (m³/s)", st.q_min_m3s],
      ["Diện tích hồ A (km²)", storageAreaM2ToKm2(st.storage_area_m2)],
      ["Diện tích mặt nước DEM (km²)", st.water_surface && st.water_surface.area_m2 != null
        ? storageAreaM2ToKm2(st.water_surface.area_m2) : null],
      ["Mực nước mặt hồ (m)", st.water_surface && st.water_surface.level_m],
      ["H ban đầu (m)", st.initial_level_m],
      ["Cao trình đỉnh đập (m)", st.dam_crest_m],
      ["Cao trình đáy cửa xả (m)", st.outlet_sill_m],
      ["Cao trình đỉnh tràn (m)", st.spillway_crest_m],
      ["Khoảng cách từ mép trái (m)", st.gate_left_offset_m],
      ["Khoảng cách các cửa xả (m)", st.gate_spacing_m],
      ["Số cửa xả", st.outlet_gate_count],
      ["Độ rộng mỗi cửa (m)", formatOutletGateWidths(st.outlet_gate_widths_m)],
      ["Thượng lưu", st.upstream_reach],
      ["Hạ lưu", st.downstream_reach],
      ["File dữ liệu", st.file],
      ["Cột dữ liệu", st.value_col],
      ["Đơn vị", st.unit],
      ["Ghi chú", st.note]
    ];
    let html = "";
    rows.forEach(function (pair) {
      const val = pair[1];
      if (val == null || val === "") return;
      html +=
        "<div><dt>" + escapeHtmlText(pair[0]) + "</dt><dd>" +
        escapeHtmlText(fmtStructureVal(val)) + "</dd></div>";
    });
    if (!html) html = "<div><dt>—</dt><dd>Không có thông tin</dd></div>";
    body.innerHTML = html;
    card.hidden = false;
  }

  function tryOpenStructureAtPointer(clientX, clientY) {
    if (!networkShowStructures) return false;
    let hit = pickNetworkStructureByRay(clientX, clientY);
    if (!hit && global.Dem3D && global.Dem3D.pickTerrain) {
      const terrain = global.Dem3D.pickTerrain(clientX, clientY);
      if (terrain && terrain.lon != null && terrain.lat != null) {
        hit = pickNetworkStructureNearest(terrain.lon, terrain.lat, 180);
      }
    }
    if (!hit) return false;
    showStructurePopup(hit);
    return true;
  }

  function tryOpenXsAtPointer(clientX, clientY) {
    if (!networkShowXs) return false;
    let hit = pickNetworkXsByRay(clientX, clientY);
    if (!hit && global.Dem3D && global.Dem3D.pickTerrain) {
      const terrain = global.Dem3D.pickTerrain(clientX, clientY);
      if (terrain && terrain.lon != null && terrain.lat != null) {
        hit = pickNetworkXsNearest(terrain.lon, terrain.lat, 220, true);
      }
    }
    if (!hit || hit.xs_id == null) return false;
    closeStructurePopup();
    requestXsProfile(hit.xs_id, hit.reach || "main", {
      station_km: hit.station_km,
      manning_n: hit.manning_n
    });
    return true;
  }

  function drawSketchLine(points) {
    disposeOverlay();
    if (!points.length) return;
    points.forEach(function (p) {
      const geo = new THREE.SphereGeometry(0.45, 10, 10);
      const mat = new THREE.MeshBasicMaterial({ color: MARKER_COLOR, depthTest: false });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.copy(worldPoint(p.lon, p.lat, p.elev, 0.2));
      mesh.renderOrder = 20;
      addOverlay(mesh);
    });
    if (points.length < 2) return;
    const verts = [];
    points.forEach(function (p) {
      const v = worldPoint(p.lon, p.lat, p.elev, 0.15);
      verts.push(v.x, v.y, v.z);
    });
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
    const mat = new THREE.LineBasicMaterial({ color: LINE_COLOR, linewidth: 2, depthTest: false });
    const line = new THREE.Line(geo, mat);
    line.renderOrder = 19;
    addOverlay(line);
  }

  function dhAt(data, t) {
    const hArr = data.h_display_m || [];
    return Math.max(Number(hArr[t] || 0) - Number(hArr[0] || 0), 0);
  }

  function hMat(data, t) {
    const row = data.h_wse_m || data.h_display_m;
    if (row && row[t] != null && isFinite(Number(row[t]))) return Number(row[t]);
    if (data.wse_by_time && data.wse_by_time[t]) {
      const vals = data.wse_by_time[t].filter(function (v) { return v != null && isFinite(Number(v)); });
      if (vals.length) {
        return vals.reduce(function (a, b) { return a + Number(b); }, 0) / vals.length;
      }
    }
    const zmin = Number(data.zmin);
    const zmax = Number(data.zmax);
    const mid = isFinite(zmin) && isFinite(zmax) ? 0.5 * (zmax + zmin) : 0;
    return dhAt(data, t) + mid;
  }

  function waterElev(data, t, i) {
    if (data.wse_by_time && data.wse_by_time[t] && data.wse_by_time[t][i] != null) {
      const v = Number(data.wse_by_time[t][i]);
      if (isFinite(v)) return v;
    }
    if (data.h_wse_m && data.h_wse_m[t] != null) {
      const v = Number(data.h_wse_m[t]);
      if (isFinite(v)) return v;
    }
    const zg = data.z_dem[i];
    const zb = data.z_bed[i];
    if (zg == null && zb == null) return null;
    const ground = zg != null ? Number(zg) : Number(zb);
    const bed = zb != null ? Number(zb) : ground;
    if (data.water_mode !== "along_bed") {
      return hMat(data, t);
    }
    return Math.max(ground, bed + dhAt(data, t));
  }

  function drawProfileOverlay(data, t) {
    disposeOverlay();
    if (!data || !data.lon || data.lon.length < 2) return;
    const THREE = global.THREE;
    if (!THREE) return;

    function addLine(arr, color, order) {
      if (arr.length < 6) return;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(arr, 3));
      const mat = new THREE.LineBasicMaterial({ color: color, depthTest: false });
      const line = new THREE.Line(geo, mat);
      line.renderOrder = order;
      addOverlay(line);
    }

    function addBaiRibbon(br) {
      const llon = br && br.bank_left_lon;
      const llat = br && br.bank_left_lat;
      const lz = br && br.bank_left_z;
      const rlon = br && br.bank_right_lon;
      const rlat = br && br.bank_right_lat;
      const rz = br && br.bank_right_z;
      if (!llon || !rlon || llon.length < 2 || llon.length !== rlon.length) return;
      const verts = [];
      const idx = [];
      let lastL = null;
      let lastR = null;
      for (let i = 0; i < llon.length; i++) {
        const zl = lz && lz[i] != null && isFinite(Number(lz[i])) ? Number(lz[i]) : null;
        const zr = rz && rz[i] != null && isFinite(Number(rz[i])) ? Number(rz[i]) : null;
        if (zl == null || zr == null) {
          lastL = lastR = null;
          continue;
        }
        const L = worldPoint(llon[i], llat[i], zl, 0.16);
        const R = worldPoint(rlon[i], rlat[i], zr, 0.16);
        const base = verts.length / 3;
        verts.push(L.x, L.y, L.z, R.x, R.y, R.z);
        if (lastL && lastR) {
          idx.push(base - 2, base - 1, base);
          idx.push(base - 1, base + 1, base);
        }
        lastL = L;
        lastR = R;
      }
      if (idx.length < 3) return;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
      geo.setIndex(idx);
      const mat = new THREE.MeshBasicMaterial({
        color: 0x22d3ee,
        transparent: true,
        opacity: 0.38,
        side: THREE.DoubleSide,
        depthWrite: false,
        depthTest: true
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.renderOrder = 17;
      addOverlay(mesh);
    }

    function addReach(lonArr, latArr, zArr, wseArr, pathColor) {
      if (!lonArr || lonArr.length < 2) return;
      let useLon = lonArr;
      let useLat = latArr;
      let useZ = zArr;
      let useWse = wseArr;
      if (!isAlongRiver(data) && latArr) {
        const parts = clipPolylineToDem(lonArr, latArr, zArr || [], null);
        const clipped = parts && parts[0] ? parts[0] : { lon: [], lat: [], z: [] };
        // Noi cac doan hop le bang gap (khong ve chord qua nodata): dung part dai nhat.
        let best = clipped;
        for (let pi = 1; pi < (parts || []).length; pi++) {
          if ((parts[pi].lon || []).length > (best.lon || []).length) best = parts[pi];
        }
        if (!best.lon || best.lon.length < 2) return;
        useLon = best.lon;
        useLat = best.lat;
        useZ = best.z;
        if (wseArr && wseArr.length === lonArr.length) {
          useWse = useLon.map(function (lo, i) {
            let bestW = null;
            let bestD = Infinity;
            for (let j = 0; j < lonArr.length; j++) {
              if (lonArr[j] == null || latArr[j] == null) continue;
              const d = Math.abs(Number(lonArr[j]) - lo) + Math.abs(Number(latArr[j]) - useLat[i]);
              if (d < bestD) {
                bestD = d;
                bestW = wseArr[j];
              }
            }
            return bestW;
          });
        }
      }
      const n = useLon.length;
      const pathVerts = [];
      const waterVerts = [];
      const ribbon = [];
      const ribbonIdx = [];
      let prevWet = false;
      for (let i = 0; i < n; i++) {
        const lon = useLon[i];
        const lat = useLat[i];
        if (lon == null || lat == null) continue;
        const zg = useZ ? useZ[i] : null;
        let we = null;
        if (useWse && useWse[i] != null) {
          const v = Number(useWse[i]);
          if (isFinite(v)) we = v;
        }
        if (zg == null || !isFinite(Number(zg))) {
          prevWet = false;
          continue;
        }
        const g = worldPoint(lon, lat, Number(zg), 0.12);
        pathVerts.push(g.x, g.y, g.z);
        if (we == null || we <= Number(zg) + 0.01) {
          prevWet = false;
          continue;
        }
        const w = worldPoint(lon, lat, we, 0.18);
        waterVerts.push(w.x, w.y, w.z);
        const base = ribbon.length / 3;
        ribbon.push(g.x, g.y, g.z, w.x, w.y, w.z);
        if (prevWet && base >= 2) {
          ribbonIdx.push(base - 2, base - 1, base);
          ribbonIdx.push(base - 1, base + 1, base);
        }
        prevWet = true;
      }
      addLine(pathVerts, pathColor, 19);
      addLine(waterVerts, WATER_COLOR, 21);
      if (ribbonIdx.length) {
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(ribbon, 3));
        geo.setIndex(ribbonIdx);
        const mat = new THREE.MeshBasicMaterial({
          color: WATER_COLOR,
          transparent: true,
          opacity: 0.42,
          side: THREE.DoubleSide,
          depthWrite: false,
          depthTest: true
        });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.renderOrder = 18;
        addOverlay(mesh);
      }
    }

    const mainWse = (data.lon || []).map(function (_v, i) { return waterElev(data, t, i); });
    addReach(data.lon, data.lat, data.z_dem, mainWse, LINE_COLOR);
    const brs = data.branches || [];
    for (let b = 0; b < brs.length; b++) {
      const br = brs[b];
      addBaiRibbon(br);
      const wse = (br.wse_by_time && br.wse_by_time[t]) ? br.wse_by_time[t] : [];
      addReach(br.lon, br.lat, br.z_dem, wse, TRIB_LINE_COLOR);
    }
  }

  function isAlongRiver(data) {
    return data.chart_x === "along_river" || data.source === "thalweg" || data.water_mode === "along_bed";
  }

  function chartDistKm(data) {
    const along = isAlongRiver(data);
    const raw = data.distance_m;
    const km = (raw || []).map(function (v) {
      const n = Number(v);
      return v == null || !isFinite(n) ? null : n / 1000;
    });
    if (along || !km.length) return km;
    let x0 = Infinity;
    km.forEach(function (v) {
      if (v != null && isFinite(v)) x0 = Math.min(x0, v);
    });
    if (!isFinite(x0)) return km;
    return km.map(function (v) { return v == null ? null : v - x0; });
  }

  function chartXLabel(data) {
    if (isAlongRiver(data)) return "Khoảng cách dọc sông (km)";
    return "Khoảng cách (km)";
  }

  function drawChart(data, t) {
    const canvas = $("flow3dChart");
    if (!canvas || !data) return;
    const parent = canvas.parentElement;
    const w = Math.max(320, (parent && parent.clientWidth) || 480);
    const h = 210;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pad = { l: 48, r: 16, t: 14, b: 42 };
    const dist = chartDistKm(data);
    const along = isAlongRiver(data);
    const zDem = data.z_dem || [];
    const zBed = data.z_bed || [];
    const z = zDem.map(function (zg, i) {
      if (along && zBed[i] != null && isFinite(Number(zBed[i]))) return Number(zBed[i]);
      return zg;
    });
    const wse = z.map(function (_v, i) { return waterElev(data, t, i); });
    const finiteZ = [];
    z.forEach(function (v) {
      if (v != null && isFinite(v)) finiteZ.push(v);
    });
    wse.forEach(function (v) {
      if (v != null && isFinite(v)) finiteZ.push(v);
    });
    if (!finiteZ.length) return;
    finiteZ.sort(function (a, b) { return a - b; });
    const lo = finiteZ[Math.floor((finiteZ.length - 1) * 0.02)];
    const hi = finiteZ[Math.floor((finiteZ.length - 1) * 0.98)];
    let z0 = lo;
    let z1 = hi;
    if (z1 <= z0) {
      z0 = finiteZ[0];
      z1 = finiteZ[finiteZ.length - 1];
    }
    let xMin = Infinity;
    let xMax = -Infinity;
    dist.forEach(function (d) {
      if (d != null && isFinite(d)) {
        xMin = Math.min(xMin, d);
        xMax = Math.max(xMax, d);
      }
    });
    if (!isFinite(xMin) || !isFinite(xMax)) return;
    if (xMax <= xMin) xMax = xMin + 0.001;
    const yPad = Math.max(1, (z1 - z0) * 0.08);
    z0 -= yPad;
    z1 += yPad;

    function X(d) {
      return pad.l + ((d - xMin) / (xMax - xMin)) * (w - pad.l - pad.r);
    }
    function Y(elev) {
      return pad.t + (1 - (elev - z0) / (z1 - z0)) * (h - pad.t - pad.b);
    }
    function fmtKm(v) {
      const span = xMax - xMin;
      if (span < 0.2) return v.toFixed(3);
      if (span < 2) return v.toFixed(2);
      if (span < 20) return v.toFixed(1);
      return v.toFixed(0);
    }

    ctx.fillStyle = "#101820";
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1;
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.fillStyle = "#c5d0dc";
    for (let k = 0; k < 5; k++) {
      const ev = z0 + (z1 - z0) * k / 4;
      const yy = Y(ev);
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(w - pad.r, yy);
      ctx.stroke();
      ctx.fillText(ev.toFixed(1), 6, yy + 4);
    }

    ctx.beginPath();
    let started = false;
    let prevX = -Infinity;
    for (let i = 0; i < z.length; i++) {
      if (z[i] == null || dist[i] == null || !isFinite(z[i]) || !isFinite(dist[i])) continue;
      const x = X(dist[i]);
      if (started && x + 1e-6 < prevX) continue;
      const y = Y(z[i]);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
      prevX = x;
    }
    ctx.lineTo(X(xMax), h - pad.b);
    ctx.lineTo(X(xMin), h - pad.b);
    ctx.closePath();
    ctx.fillStyle = "#8b6b3d";
    ctx.globalAlpha = 0.88;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = "#5c4324";
    ctx.lineWidth = 1.2;
    ctx.stroke();

    function bankHit(ia, ib) {
      const za = z[ia];
      const zb = z[ib];
      const ha = wse[ia];
      const hb = wse[ib];
      const da = dist[ia];
      const db = dist[ib];
      const den = (zb - za) - (hb - ha);
      let t = 0.5;
      if (Math.abs(den) > 1e-12) t = (ha - za) / den;
      t = Math.min(1, Math.max(0, t));
      const elev = za + t * (zb - za);
      return { d: da + t * (db - da), elev: elev };
    }

    ctx.fillStyle = "rgba(37, 99, 235, 0.55)";
    let i = 0;
    while (i < z.length) {
      while (i < z.length && (z[i] == null || wse[i] == null || wse[i] <= z[i] + 0.01)) i++;
      if (i >= z.length) break;
      const i0 = i;
      while (i < z.length && z[i] != null && wse[i] != null && wse[i] > z[i] + 0.01) i++;
      const i1 = i - 1;
      ctx.beginPath();
      let left = { d: dist[i0], elev: z[i0] };
      if (i0 > 0 && z[i0 - 1] != null && wse[i0 - 1] != null) left = bankHit(i0 - 1, i0);
      let right = { d: dist[i1], elev: z[i1] };
      if (i1 + 1 < z.length && z[i1 + 1] != null && wse[i1 + 1] != null) right = bankHit(i1, i1 + 1);
      ctx.moveTo(X(left.d), Y(left.elev));
      let lastWx = -Infinity;
      for (let k = i0; k <= i1; k++) {
        if (dist[k] == null || wse[k] == null || !isFinite(dist[k]) || !isFinite(wse[k])) continue;
        const xk = X(dist[k]);
        if (xk + 1e-6 < lastWx) continue;
        ctx.lineTo(xk, Y(wse[k]));
        lastWx = xk;
      }
      ctx.lineTo(X(right.d), Y(right.elev));
      for (let k = i1; k >= i0; k--) {
        if (dist[k] == null || z[k] == null || !isFinite(dist[k]) || !isFinite(z[k])) continue;
        const xk = X(dist[k]);
        ctx.lineTo(xk, Y(z[k]));
      }
      ctx.closePath();
      ctx.fill();
    }

    ctx.beginPath();
    started = false;
    for (let j = 0; j < wse.length; j++) {
      if (wse[j] == null || z[j] == null || dist[j] == null || wse[j] <= z[j] + 0.01) {
        started = false;
        continue;
      }
      const x = X(dist[j]);
      const y = Y(wse[j]);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = "#60a5fa";
    ctx.lineWidth = 1.8;
    ctx.stroke();

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#c5d0dc";
    ctx.textAlign = "center";
    ctx.font = "11px Segoe UI, sans-serif";
    for (let k = 0; k < 5; k++) {
      const xv = xMin + (xMax - xMin) * k / 4;
      const xx = X(xv);
      ctx.beginPath();
      ctx.moveTo(xx, pad.t);
      ctx.lineTo(xx, h - pad.b);
      ctx.stroke();
      ctx.fillText(fmtKm(xv), xx, h - pad.b + 14);
    }
    ctx.fillStyle = "#dbe7f3";
    ctx.fillText(chartXLabel(data), (pad.l + w - pad.r) / 2, h - 6);
    ctx.font = "10px Segoe UI, sans-serif";
    ctx.fillStyle = "#93a4b5";
    if (along) {
      ctx.textAlign = "left";
      ctx.fillText("Thượng lưu", pad.l, h - pad.b + 26);
      ctx.textAlign = "right";
      ctx.fillText("Hạ lưu", w - pad.r, h - pad.b + 26);
    } else {
      ctx.textAlign = "left";
      ctx.fillText("Mép trái", pad.l, h - pad.b + 26);
      ctx.textAlign = "right";
      ctx.fillText("Mép phải", w - pad.r, h - pad.b + 26);
    }
    ctx.textAlign = "left";
    const hour = data.hours[t];
    ctx.fillStyle = "#93c5fd";
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.fillText("t = " + Number(hour).toFixed(0) + " h", pad.l, 12);
  }

  function qAt(data, t) {
    const q = data.q_cut_m3s;
    if (!q || q[t] == null || !isFinite(Number(q[t]))) return null;
    return Number(q[t]);
  }

  function drawQChart(data, t) {
    const canvas = $("flow3dQChart");
    if (!canvas) return;
    const q = data.q_cut_m3s;
    const hours = data.hours || [];
    const show = !isAlongRiver(data) && q && q.length && hours.length;
    canvas.hidden = !show;
    if (!show) return;

    const parent = canvas.parentElement;
    const w = Math.max(320, (parent && parent.clientWidth) || 480);
    const h = 150;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const pad = { l: 52, r: 12, t: 18, b: 28 };
    const vals = q.map(function (v) { return v == null ? null : Number(v); });
    let q0 = Infinity;
    let q1 = -Infinity;
    vals.forEach(function (v) {
      if (v != null && isFinite(v)) {
        q0 = Math.min(q0, v);
        q1 = Math.max(q1, v);
      }
    });
    if (!isFinite(q0) || !isFinite(q1)) return;
    if (q1 <= q0) q1 = q0 + 1;
    const yPad = Math.max(1, (q1 - q0) * 0.08);
    q0 = Math.max(0, q0 - yPad);
    q1 += yPad;
    const t0 = Number(hours[0]) || 0;
    const t1 = Number(hours[hours.length - 1]) || t0 + 1;

    function X(hr) {
      return pad.l + ((hr - t0) / Math.max(t1 - t0, 1e-6)) * (w - pad.l - pad.r);
    }
    function Y(qv) {
      return pad.t + (1 - (qv - q0) / (q1 - q0)) * (h - pad.t - pad.b);
    }

    ctx.fillStyle = "#101820";
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1;
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.fillStyle = "#c5d0dc";
    ctx.textAlign = "left";
    for (let k = 0; k < 4; k++) {
      const qv = q0 + (q1 - q0) * k / 3;
      const yy = Y(qv);
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(w - pad.r, yy);
      ctx.stroke();
      ctx.fillText(qv.toFixed(0), 4, yy + 4);
    }

    ctx.beginPath();
    let started = false;
    for (let i = 0; i < vals.length; i++) {
      if (vals[i] == null || !isFinite(vals[i]) || hours[i] == null) {
        started = false;
        continue;
      }
      const x = X(Number(hours[i]));
      const y = Y(vals[i]);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = "#f59e0b";
    ctx.lineWidth = 1.8;
    ctx.stroke();

    const hrNow = Number(hours[t]);
    if (isFinite(hrNow)) {
      const xNow = X(hrNow);
      ctx.strokeStyle = "rgba(147, 197, 253, 0.85)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xNow, pad.t);
      ctx.lineTo(xNow, h - pad.b);
      ctx.stroke();
      if (vals[t] != null && isFinite(vals[t])) {
        ctx.fillStyle = "#fbbf24";
        ctx.beginPath();
        ctx.arc(xNow, Y(vals[t]), 3.5, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    ctx.fillStyle = "#c5d0dc";
    ctx.textAlign = "center";
    for (let k = 0; k < 5; k++) {
      const hr = t0 + (t1 - t0) * k / 4;
      ctx.fillText(hr.toFixed(0), X(hr), h - 12);
    }
    ctx.fillStyle = "#dbe7f3";
    ctx.fillText("Q tại mặt cắt (m³/s)", (pad.l + w - pad.r) / 2, 12);
    ctx.fillText("Giờ", (pad.l + w - pad.r) / 2, h - 2);
    ctx.textAlign = "left";
  }

  function syncTimeUi() {
    if (!payload) return;
    const slider = $("flow3dTime");
    const label = $("flow3dTimeLabel");
    const rain = $("flow3dRainLabel");
    if (slider) {
      slider.max = String(Math.max(0, payload.n_time - 1));
      slider.value = String(timeIndex);
    }
    const hour = payload.hours[timeIndex];
    const hm = hMat(payload, timeIndex);
    const p = payload.rainfall_mm[timeIndex];
    const qv = qAt(payload, timeIndex);
    if (label) {
      let txt = "Giờ " + Number(hour).toFixed(0) + "  ·  H mặt " + hm.toFixed(2) + " m";
      if (qv != null) txt += "  ·  Q " + qv.toFixed(1) + " m³/s";
      label.textContent = txt;
    }
    if (rain) {
      rain.textContent = "Mưa " + Number(p).toFixed(2) + " mm";
    }
    drawChart(payload, timeIndex);
    drawQChart(payload, timeIndex);
    drawProfileOverlay(payload, timeIndex);
  }

  function applyPayload(data) {
    payload = data;
    timeIndex = data.initial_time_index != null ? Number(data.initial_time_index) : 0;
    setPanelVisible(true);
    syncTimeUi();
    const title = $("flow3dPanelTitle");
    if (title) {
      if (data && data.source === "xs_click" && data.xs_id != null) {
        const bits = ["XS " + data.xs_id];
        if (data.reach_label || data.reach) bits.push(data.reach_label || data.reach);
        if (data.station_km != null && isFinite(Number(data.station_km))) {
          bits.push(Number(data.station_km).toFixed(2) + " km");
        }
        if (data.manning_n != null && isFinite(Number(data.manning_n))) {
          bits.push("n = " + Number(data.manning_n).toFixed(4));
        }
        title.textContent = bits.join(" · ");
      } else if (data && data.source === "drawn") {
        title.textContent = "Mặt cắt vẽ tay · mực nước · Q";
      } else {
        title.textContent = isAlongRiver(data) ? "Dọc sông · mực nước" : "Mặt cắt ngang · mực nước · Q";
      }
    }
    const lenKm = (data.length_m || 0) / 1000;
    const brs = data.branches || [];
    let extra = "";
    if (isAlongRiver(data) && brs.length) {
      const brKm = brs.reduce(function (s, b) { return s + (Number(b.length_m) || 0); }, 0) / 1000;
      extra = " + " + brs.length + " nhánh (" + brKm.toFixed(2) + " km)";
      const a = brs[0] && brs[0].lon && brs[0].lon.length ? brs[0].lon[0] : null;
      if (a != null) extra += ", vào Hồng " + Number(a).toFixed(3);
    }
    let nHint = "";
    if (data && data.manning_n != null && isFinite(Number(data.manning_n))) {
      nHint = " · Manning n = " + Number(data.manning_n).toFixed(4);
    }
    if (data && data.source === "xs_click") {
      setHint("");
    } else {
      setHint(
        (isAlongRiver(data) ? "Dọc sông " : "Mặt cắt ") +
          lenKm.toFixed(2) + " km" + extra + nHint +
          waterSourceHint(data) +
          ". Kéo thanh thời gian để xem nước dâng."
      );
    }
  }

  function messageFromHttpBody(text, status) {
    const trimmed = String(text || "").replace(/^\uFEFF/, "").trim();
    if (!trimmed) {
      return "Máy chủ không trả dữ liệu mặt cắt (HTTP " + status + "). Hãy khởi động lại app.py rồi thử lại.";
    }
    try {
      const data = JSON.parse(trimmed);
      return (data && (data.error || data.message)) || ("Lỗi HTTP " + status);
    } catch (_eParse) {
      if (trimmed.charAt(0) === "<") {
        return (
          "API mặt cắt trả về HTML thay vì JSON (HTTP " + status + "). " +
          "Khởi động lại Flask (app.py) rồi tải lại trang."
        );
      }
      return trimmed.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 220);
    }
  }

  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
      },
      body: JSON.stringify(body)
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text.replace(/^\uFEFF/, "")) : null;
    } catch (_eParse) {
      throw new Error(messageFromHttpBody(text, res.status));
    }
    if (!res.ok || !data || data.ok === false) {
      throw new Error(
        (data && (data.error || data.message)) ||
        messageFromHttpBody(text, res.status) ||
        "Không tính được mặt cắt"
      );
    }
    return data;
  }

  async function requestProfile(coords) {
    // Mat cat ve tay: bo highlight XS he thong, giu profile theo polyline vua ve.
    selectedXsKey = "";
    if (networkOverlayData) drawNetworkOverlay(networkOverlayData);
    lastRequest = { type: "profile", coords: coords };
    setHint("Đang lấy cao độ DEM và mực nước…");
    const data = await postJson(API + "/profile", {
      coordinates: coords,
      file_id: fileId(),
      water_source: selectedWaterSource()
    });
    data.source = "drawn";
    data.official_xs = false;
    applyPayload(data);
  }

  async function requestThalweg() {
    lastRequest = { type: "thalweg" };
    setHint("Đang lần theo lòng sông chính và nhánh…");
    const data = await postJson(API + "/thalweg", {
      file_id: fileId(),
      water_source: selectedWaterSource()
    });
    applyPayload(data);
  }

  function waterSourceLabel(_src) {
    return _src === "saint-venant-1d" ? "Saint-venant-1D" : "Saint-venant";
  }

  function simKind(job) {
    if (job && job.kind === "rr") return "rr";
    if (job && job.kind === "nam") return "nam";
    if (job && job.kind === "manning-dem") return "manning-dem";
    return "1d";
  }

  function setHydroBusy(busy, kind) {
    const rr = $("hydroRrRunBtn");
    const nam = $("hydroNamRunBtn");
    const one = $("hydro1dRunBtn");
    if (rr) {
      rr.disabled = !!busy;
      rr.textContent = (busy && kind === "rr") ? "Đang chạy…" : "Model RR";
    }
    if (nam) {
      nam.disabled = !!busy;
      nam.textContent = (busy && kind === "nam") ? "Đang chạy…" : "Model NAM";
    }
    if (one) {
      one.disabled = !!busy;
      one.textContent = (busy && kind === "1d") ? "Đang chạy…" : "Model 1D";
    }
    const demN = $("svManningFromDem");
    if (demN) {
      demN.disabled = !!busy;
      demN.textContent = (busy && kind === "manning-dem") ? "Đang lấy n…" : "Hệ số nhám từ DEM";
    }
  }

  function setSimBusy(busy) {
    setHydroBusy(busy, "1d");
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
      titleEl.textContent = title || (kind === "rr" ? "Model RR" : kind === "nam" ? "Model NAM" : kind === "manning-dem" ? "Hệ số nhám từ DEM" : "Model 1D");
    }
    modal.hidden = false;
    const closeBtn = $("hydro1dProgressClose");
    if (closeBtn) closeBtn.disabled = true;
    updateProgressModal(job || {
      status: "running",
      kind: kind || "1d",
      message: kind === "rr" ? "Đang chạy Model RR…" : kind === "nam" ? "Đang chạy Model NAM…" : kind === "manning-dem" ? "Đang lấy hệ số nhám từ DEM…" : "Đang chạy Model 1D…",
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
      const label = job.label || (job.kind === "rr" ? "TANK mưa-dòng chảy" : job.kind === "nam" ? "MIKE NAM mưa-dòng chảy" : job.kind === "manning-dem" ? "Hệ số nhám từ DEM" : waterSourceLabel(job.water_source));
      const elapsed = job.elapsed_s != null ? fmtElapsed(job.elapsed_s) : "0 s";
      metaEl.textContent = (label || (job.kind === "rr" ? "Model RR" : job.kind === "nam" ? "Model NAM" : job.kind === "manning-dem" ? "Hệ số nhám từ DEM" : "Model 1D")) + " · " + elapsed;
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
    const kind = simKind(job);
    if (job.status === "running") {
      setHydroBusy(true, kind);
      if (kind === "rr") setRrActionBusy(true);
      if (kind === "nam") setNamActionBusy(true);
      if (kind === "manning-dem") setSvActionBusy(true, "dem");
      return;
    }
    stopSimPoll();
    setHydroBusy(false, kind);
    if (kind === "rr") setRrActionBusy(false);
    if (kind === "nam") setNamActionBusy(false);
    if (kind === "manning-dem") setSvActionBusy(false);
    if (job.status === "ok") {
      if (global.Flood3D && global.Flood3D.invalidate) global.Flood3D.invalidate();
      if (kind === "1d" || kind === "manning-dem") loadNetworkOverlay(false);
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
      if (kind === "nam") {
        fetch(API + "/nam-params", { headers: { "Accept": "application/json" } })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (!data || !data.ok) return;
            namHydroData = data.hydrograph || null;
            const modal = namParamsModal();
            if (modal && !modal.hidden) {
              drawNamHydrograph(namHydroData || {});
              closeProgressModal();
            }
          })
          .catch(function () {});
      }
      if (kind === "manning-dem") {
        loadManningTable().then(function () {
          closeProgressModal();
        }).catch(function () {
          closeProgressModal();
        });
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

  async function startHydro1d(opts) {
    opts = opts || {};
    const src = selectedWaterSource();
    const label = waterSourceLabel(src);
    stopSimPoll();
    setHydroBusy(true, "1d");
    setHint("");
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
        body: JSON.stringify(Object.assign({
          water_source: src,
          file_id: fileId(),
          xs_spacing_m: opts.xs_spacing_m != null ? opts.xs_spacing_m : selectedXsSpacingM(),
          dt_hours: opts.dt_hours != null ? opts.dt_hours : selectedDtHours()
        }, collectHdParams(), opts.hd || {}))
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

  function selectedXsSpacingM() {
    const el = $("svXsSpacing");
    const n = Number(el && el.value);
    if (Number.isFinite(n) && n > 0) return n;
    return 1500;
  }

  function setXsSpacingInput(meters) {
    const el = $("svXsSpacing");
    if (!el) return;
    const n = Number(meters);
    if (Number.isFinite(n) && n > 0) el.value = String(Math.round(n));
  }

  function applyExtractedXs(data) {
    if (!data) return;
    if (data.xs_spacing_m != null) setXsSpacingInput(data.xs_spacing_m);
    if (data.dt_hours != null) setDtHoursInput(data.dt_hours);
    applyHdParams(data);
    if (data.rows) renderManningRows(data.rows);
    lastExtractedXs = selectedXsSpacingM();
  }

  async function extractXsForSpacing() {
    const xs = selectedXsSpacingM();
    if (!Number.isFinite(xs) || xs < 50 || xs > 20000) {
      setManningError("Khoảng XS phải từ 50 đến 20000 m.");
      return null;
    }
    const existing = $("svManningBody") && $("svManningBody").querySelector("input[data-n]");
    if (existing && lastExtractedXs != null && Math.abs(xs - lastExtractedXs) < 0.5 && !xsExtractBusy) {
      return { skipped: true, n_xs: null };
    }
    const gen = ++xsExtractGen;
    xsExtractBusy = true;
    setManningError("");
    setSvActionBusy(true, "xs");
    const body = $("svManningBody");
    if (body) body.innerHTML = "<tr><td colspan=\"7\">Đang cắt mặt cắt theo khoảng " + Math.round(xs) + " m…</td></tr>";
    try {
      const res = await fetch(API + "/extract-xs", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({
          xs_spacing_m: xs,
          water_source: selectedWaterSource(),
          file_id: fileId()
        })
      });
      const data = await res.json();
      if (gen !== xsExtractGen) return null;
      if (!res.ok || !data || data.ok === false) {
        throw new Error((data && (data.error || data.message)) || "Không cắt được mặt cắt.");
      }
      applyExtractedXs(data);
      loadNetworkOverlay(false);
      return data;
    } catch (err) {
      if (gen !== xsExtractGen) return null;
      if (body) body.innerHTML = "";
      setManningError(err.message || String(err));
      throw err;
    } finally {
      if (gen === xsExtractGen) {
        xsExtractBusy = false;
        setSvActionBusy(false);
      }
    }
  }

  function scheduleExtractXs() {
    clearTimeout(xsExtractTimer);
    xsExtractTimer = setTimeout(function () {
      extractXsForSpacing().catch(function () {});
    }, 700);
  }

  function selectedDtHours() {
    const el = $("svDtHours");
    const n = Number(el && el.value);
    if (Number.isFinite(n) && n >= 0.05 && n <= 24) return n;
    return 1;
  }

  function setDtHoursInput(hours) {
    const el = $("svDtHours");
    if (!el) return;
    const n = Number(hours);
    if (Number.isFinite(n) && n > 0) {
      const rounded = Math.round(n * 1000) / 1000;
      el.value = String(rounded);
    }
  }

  function numParam(id, lo, hi, fallback) {
    const el = $(id);
    const n = Number(el && el.value);
    if (Number.isFinite(n) && n >= lo && n <= hi) return n;
    return fallback;
  }

  function setNumParam(id, value, digits) {
    const el = $(id);
    if (!el || value == null) return;
    const n = Number(value);
    if (!Number.isFinite(n)) return;
    if (digits == null) {
      el.value = String(n);
      return;
    }
    const f = Math.pow(10, digits);
    el.value = String(Math.round(n * f) / f);
  }

  function collectHdParams() {
    const src = selectedWaterSource();
    return {
      cfl: numParam("svCfl", 0.05, 10, src === "saint-venant-1d" ? 1 : 0.45),
      dt_hydro_max_s: numParam("svDtHydro", 1, 3600, 60),
      theta: numParam("svTheta", 0.5, 1, 1),
      n_picard: Math.round(numParam("svPicard", 1, 10, 3)),
      convective: numParam("svConvective", 0, 1, 0.15),
      manning_blend: numParam("svManningBlend", 0, 1, 0.25),
      q_relax: numParam("svQRelax", 0.1, 1, 0.35)
    };
  }

  function applyHdParams(data) {
    if (!data) return;
    setNumParam("svCfl", data.cfl, 4);
    setNumParam("svDtHydro", data.dt_hydro_max_s, 0);
    setNumParam("svTheta", data.theta, 4);
    setNumParam("svPicard", data.n_picard, 0);
    setNumParam("svConvective", data.convective, 4);
    setNumParam("svManningBlend", data.manning_blend, 4);
    setNumParam("svQRelax", data.q_relax, 4);
  }

  function setWaterSource(src) {
    if (window.HYDRO1D_FORCE_MIKE) src = "saint-venant-1d";
    const mike = src === "saint-venant-1d";
    const localEl = $("svSolverLocal");
    const mikeEl = $("svSolverMike");
    if (localEl) localEl.checked = !mike;
    if (mikeEl) mikeEl.checked = mike;
    syncSolverParamsUi(false);
  }

  function syncSolverParamsUi(applyDefaults) {
    const mike = selectedWaterSource() === "saint-venant-1d";
    const row = $("svMikeParams");
    if (row) row.hidden = !mike;
    const hint = $("svManningHint") || document.querySelector("#svManningModal .sv-manning-hint");
    if (hint) {
      hint.textContent = mike
        ? "Saint-venant-1D (Abbott–Ionescu) · khoảng XS · Δt xuất · CFL · θ / Picard"
        : "Saint-venant (sóng khuếch tán) · khoảng XS · Δt xuất · CFL";
    }
    if (!applyDefaults) return;
    const cfl = $("svCfl");
    if (!cfl) return;
    const v = Number(cfl.value);
    const otherDefault = mike ? 0.45 : 1;
    const selfDefault = mike ? 1 : 0.45;
    if (!Number.isFinite(v) || Math.abs(v - otherDefault) < 1e-9) {
      cfl.value = String(selfDefault);
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
    closeSvInputModal();
    closeEvalModal();
  }

  function setEvalError(text) {
    const el = $("svEvalError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeEvalModal() {
    const modal = $("svEvalModal");
    if (modal) modal.hidden = true;
    setEvalError("");
    svEvalObs = null;
    clearEvalObsCols();
    setEvalNse(null);
  }

  function evalKind() {
    const el = document.querySelector('input[name="svEvalKind"]:checked');
    return (el && el.value) === "h" ? "h" : "q";
  }

  function setEvalKind(kind) {
    document.querySelectorAll('input[name="svEvalKind"]').forEach(function (el) {
      el.checked = el.value === kind;
    });
  }

  function setEvalNse(nse) {
    setHydroNse($("svEvalNse"), nse);
  }

  function attachEvalObs(data, obs) {
    if (!data || !obs) return data;
    data.hours_obs = obs.hours;
    data.ts_obs = obs.ts;
    data.q_obs_m3s = obs.q_m3s;
    data.h_obs_m = obs.h_m;
    data.has_q_obs = !!obs.has_q;
    data.has_h_obs = !!obs.has_h;
    data.obs_path = obs.path;
    data.obs_n = obs.n;
    data.obs_column = obs.column || "";
    data.obs_kind = obs.kind || (obs.has_h ? "h" : "q");
    return data;
  }

  function clearEvalObsCols() {
    const wrap = $("svEvalObsColWrap");
    const sel = $("svEvalObsCol");
    if (sel) sel.innerHTML = "";
    if (wrap) wrap.hidden = true;
  }

  function fillEvalObsCols(columns, selected) {
    const wrap = $("svEvalObsColWrap");
    const sel = $("svEvalObsCol");
    if (!sel) return;
    let pick = 0;
    sel.innerHTML = "";
    (columns || []).forEach(function (c, i) {
      if (!c || !c.name) return;
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = c.name;
      sel.appendChild(opt);
      if (selected != null && (c.name === selected || String(i) === String(selected))) {
        pick = sel.options.length - 1;
      }
    });
    if (sel.options.length) sel.selectedIndex = pick;
    if (wrap) wrap.hidden = sel.options.length === 0;
  }

  function selectedObsColumn() {
    const sel = $("svEvalObsCol");
    const cols = (svEvalObs && svEvalObs.columns) || [];
    if (!cols.length) return null;
    let idx = sel ? Number(sel.value) : 0;
    if (!isFinite(idx) || idx < 0 || idx >= cols.length) {
      idx = sel && sel.selectedIndex >= 0 ? sel.selectedIndex : 0;
    }
    if (idx >= 0 && idx < cols.length && cols[idx] && cols[idx].name) return cols[idx];
    return cols[0];
  }

  function obsSliceFromColumn(col) {
    if (!svEvalObs || !col) return null;
    const kind = col.kind === "q" ? "q" : "h";
    return {
      hours: svEvalObs.hours,
      ts: svEvalObs.ts,
      path: svEvalObs.path,
      n: svEvalObs.n,
      column: col.name,
      kind: kind,
      q_m3s: kind === "q" ? (col.values || col.series || []) : [],
      h_m: kind === "h" ? (col.values || col.series || []) : [],
      has_q: kind === "q",
      has_h: kind === "h"
    };
  }

  function applyEvalObsColumn() {
    const slice = obsSliceFromColumn(selectedObsColumn());
    if (!slice) return;
    if (svEvalData) attachEvalObs(svEvalData, slice);
    else svEvalData = attachEvalObs({}, slice);
    setEvalKind(slice.kind);
    drawEvalHydrograph(svEvalData);
  }

  function selectedEvalReach() {
    const sel = $("svEvalReach");
    return sel && sel.value ? String(sel.value) : "main";
  }

  function fillEvalReaches(reaches, selectedId) {
    const sel = $("svEvalReach");
    if (!sel) return;
    const prev = selectedId != null && selectedId !== "" ? String(selectedId) : sel.value;
    sel.innerHTML = "";
    (reaches || []).forEach(function (r) {
      const id = String((r && (r.id || r.reach)) || "main");
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = ((r && r.label) || id) + " (" + id + ")";
      sel.appendChild(opt);
    });
    if (!sel.options.length) {
      const opt = document.createElement("option");
      opt.value = "main";
      opt.textContent = "Sông chính 1 (main)";
      sel.appendChild(opt);
    }
    if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
    else sel.selectedIndex = 0;
  }

  function fillEvalXs(sections, selectedId, reach) {
    const sel = $("svEvalXs");
    if (!sel) return;
    const want = String(reach || selectedEvalReach() || "main");
    const filtered = (sections || []).filter(function (s) {
      if (!s || s.reach == null || s.reach === "") return want === "main";
      return String(s.reach) === want;
    });
    const list = filtered.length ? filtered : (sections || []);
    const prev = selectedId != null ? String(selectedId) : sel.value;
    sel.innerHTML = "";
    list.forEach(function (s) {
      const opt = document.createElement("option");
      const id = s.xs_id;
      const km = s.station_km != null && isFinite(Number(s.station_km))
        ? Number(s.station_km).toFixed(3)
        : "—";
      opt.value = String(id);
      opt.textContent = "XS " + id + " · " + km + " km";
      sel.appendChild(opt);
    });
    if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
    else if (sel.options.length) sel.selectedIndex = 0;
  }

  function seriesHasValues(arr) {
    if (!arr || !arr.length) return false;
    for (let i = 0; i < arr.length; i++) {
      if (isFinite(Number(arr[i]))) return true;
    }
    return false;
  }

  function drawEvalHydrograph(data) {
    const isH = evalKind() === "h";
    const hasObs = isH
      ? !!(data && data.has_h_obs && seriesHasValues(data.h_obs_m))
      : !!(data && data.has_q_obs && seriesHasValues(data.q_obs_m3s));
    const hydro = {
      hours_sim: data && data.hours_sim,
      ts_sim: data && data.ts_sim,
      q_sim_m3s: isH ? (data && data.h_m) : (data && data.q_m3s),
      hours_obs: hasObs ? (data && data.hours_obs) : [],
      ts_obs: hasObs ? (data && data.ts_obs) : [],
      q_obs_m3s: hasObs ? (isH ? (data && data.h_obs_m) : (data && data.q_obs_m3s)) : []
    };
    drawHydrographOn(
      $("svEvalChart"),
      $("svEvalMeta"),
      setEvalNse,
      hydro,
      isH
        ? "Chưa có mực nước tại mặt cắt. Chạy Model 1D trước."
        : "Chưa có lưu lượng tại mặt cắt. Chạy Model 1D trước.",
      {
        yLabel: isH ? "H (m)" : "Q (m³/s)",
        unit: isH ? "m" : "m³/s",
        simLabel: isH ? "H tính toán" : "Q tính toán",
        obsLabel: (data && data.obs_column) || (isH ? "H thực đo" : "Q thực đo"),
        noZero: isH,
        hideNse: !hasObs
      }
    );
    const meta = $("svEvalMeta");
    if (meta && data && data.xs_id != null) {
      const km = data.station_km != null && isFinite(Number(data.station_km))
        ? Number(data.station_km).toFixed(3) + " km"
        : "";
      const zb = data.z_bed_m != null && isFinite(Number(data.z_bed_m))
        ? "z đáy " + Number(data.z_bed_m).toFixed(2) + " m"
        : "";
      const prefix = [data.reach_label || data.reach, "XS " + data.xs_id, km, zb].filter(Boolean).join(" · ");
      const obsCol = data.obs_column ? "thực đo " + data.obs_column : "";
      meta.textContent = [prefix, obsCol, meta.textContent].filter(Boolean).join(" · ");
    }
    if (data && (data.has_h_obs || data.has_q_obs)) {
      if (data.has_h_obs && !data.has_q_obs && !isH) {
        setEvalError("Cột thực đo là mực nước. Chọn H (m) để so sánh Tính toán và Thực đo.");
      } else if (data.has_q_obs && !data.has_h_obs && isH) {
        setEvalError("Cột thực đo là lưu lượng. Chọn Q (m³/s) để so sánh Tính toán và Thực đo.");
      } else {
        setEvalError("");
      }
    }
  }

  function bindEvalHydroResize() {
    const canvas = $("svEvalChart");
    const wrap = canvas && canvas.parentElement;
    if (!wrap || typeof ResizeObserver === "undefined") return;
    if (svEvalResizeObs) return;
    svEvalResizeObs = new ResizeObserver(function () {
      if (svEvalData) drawEvalHydrograph(svEvalData);
    });
    svEvalResizeObs.observe(wrap);
  }

  async function loadEvalHydrograph() {
    const sel = $("svEvalXs");
    const xsId = sel && sel.value ? sel.value : "1";
    const reach = selectedEvalReach();
    setEvalError("");
    try {
      const res = await fetch(
        API + "/xs-hydrograph?xs_id=" + encodeURIComponent(xsId) +
        "&reach=" + encodeURIComponent(reach) +
        "&water_source=" + encodeURIComponent(selectedWaterSource()),
        { headers: { "Accept": "application/json" } }
      );
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Chưa có kết quả Model 1D.");
      }
      fillEvalReaches(data.reaches, data.reach);
      fillEvalXs(data.sections, data.xs_id, data.reach);
      svEvalData = data;
      bindEvalHydroResize();
      if (svEvalObs) applyEvalObsColumn();
      else {
        requestAnimationFrame(function () {
          drawEvalHydrograph(svEvalData);
        });
      }
    } catch (err) {
      svEvalData = svEvalObs ? attachEvalObs({}, obsSliceFromColumn(selectedObsColumn()) || svEvalObs) : null;
      setEvalError(err.message || String(err));
      drawEvalHydrograph(svEvalData || {});
    }
  }

  function onEvalReachChange() {
    const sel = $("svEvalXs");
    if (sel) sel.selectedIndex = 0;
    loadEvalHydrograph();
  }

  async function openEvalModal() {
    const modal = $("svEvalModal");
    if (!modal) return;
    setEvalError("");
    const rows = collectManningRows();
    fillEvalReaches(uniqueManningReaches(rows));
    fillEvalXs(rows.map(function (row) {
      return { xs_id: row.xs_id, station_km: row.station_km, reach: row.reach || "main" };
    }), null, selectedEvalReach());
    modal.hidden = false;
    await loadEvalHydrograph();
    await tryLoadEvalObsFromDb();
  }

  async function tryLoadEvalObsFromDb() {
    try {
      const res = await fetch(API + "/xs-obs", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) return;
      const cols = data.columns || [];
      if (!cols.length) return;
      svEvalObs = data;
      fillEvalObsCols(cols, data.column);
      bindEvalHydroResize();
      applyEvalObsColumn();
    } catch (_) {}
  }

  function pickEvalObsFile() {
    const input = $("svEvalObsFile");
    if (input) input.click();
  }

  async function onEvalObsFilePicked(ev) {
    const input = ev && ev.target;
    const file = input && input.files && input.files[0];
    if (input) input.value = "";
    if (!file) return;
    const btn = $("svEvalLoad");
    setEvalError("");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Đang tải…";
    }
    try {
      const body = new FormData();
      body.append("file", file, file.name || "thucdo.csv");
      const res = await fetch(API + "/xs-obs", { method: "POST", body: body });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được file thực đo.");
      }
      const cols = data.columns || [];
      if (!cols.length) {
        throw new Error("File thực đo không có cột giá trị (ngoài Gio, Time).");
      }
      svEvalObs = data;
      fillEvalObsCols(cols, data.column);
      bindEvalHydroResize();
      applyEvalObsColumn();
    } catch (err) {
      setEvalError(err.message || String(err));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Load file...";
      }
    }
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
        reach: tr.getAttribute("data-reach") || "main",
        reach_label: tr.getAttribute("data-reach-label") || "",
        xs_id: id,
        station_km: stEl ? stEl.getAttribute("data-station") : null,
        manning_n: nEl.value
      });
    });
    return rows;
  }

  function uniqueManningReaches(rows) {
    const seen = [];
    (rows || []).forEach(function (row) {
      const id = String(row.reach || "main");
      if (seen.some(function (item) { return item.reach === id; })) return;
      seen.push({
        reach: id,
        label: row.reach_label || (id === "main" ? "Sông chính 1" : id)
      });
    });
    return seen;
  }

  function fillManningReachSelect(rows) {
    const sel = $("svManningReach");
    if (!sel) return;
    const prev = sel.value;
    const reaches = uniqueManningReaches(rows);
    sel.innerHTML = "";
    reaches.forEach(function (item) {
      const opt = document.createElement("option");
      opt.value = item.reach;
      opt.textContent = item.label + " (" + item.reach + ")";
      sel.appendChild(opt);
    });
    if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
  }

  function setManningFillRangeForReach(rows, reach) {
    const ids = (rows || [])
      .filter(function (row) { return String(row.reach || "main") === String(reach || "main"); })
      .map(function (row) { return Number(row.xs_id); })
      .filter(Number.isFinite);
    const fromEl = $("svManningFrom");
    const toEl = $("svManningTo");
    if (fromEl && ids.length) fromEl.value = String(Math.min.apply(null, ids));
    if (toEl && ids.length) toEl.value = String(Math.max.apply(null, ids));
  }

  function fillManningRange() {
    const fromEl = $("svManningFrom");
    const toEl = $("svManningTo");
    const nEl = $("svManningFillN");
    const reachEl = $("svManningReach");
    const from = Number(fromEl && fromEl.value);
    const to = Number(toEl && toEl.value);
    const n = Number(nEl && nEl.value);
    const reach = reachEl ? String(reachEl.value || "") : "";
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
      if (reach && (tr.getAttribute("data-reach") || "main") !== reach) return;
      const id = Number(tr.getAttribute("data-xs-id"));
      if (id < lo || id > hi) return;
      const input = tr.querySelector("input[data-n]");
      if (input) {
        input.value = n.toFixed(4);
        count += 1;
      }
    });
    setManningError(count ? "" : "Không có mặt cắt trong khoảng đã chọn trên sông này.");
  }

  function renderManningRows(rows) {
    const body = $("svManningBody");
    if (!body) return;
    lastManningRows = rows || [];
    body.innerHTML = "";
    lastManningRows.forEach(function (row) {
      const tr = document.createElement("tr");
      const xs = Number(row.xs_id);
      tr.setAttribute("data-xs-id", String(xs));
      const reach = row.reach || "main";
      tr.setAttribute("data-reach", reach);
      const reachLabel = row.reach_label || (reach === "main" ? "Sông chính 1" : "Sông nhánh 1");
      tr.setAttribute("data-reach-label", reachLabel);
      const km = row.station_km != null && Number.isFinite(Number(row.station_km))
        ? Number(row.station_km).toFixed(2)
        : "—";
      const n = Number(row.manning_n);
      function fmtXy(v) {
        const num = Number(v);
        return Number.isFinite(num) ? num.toFixed(6) : "—";
      }
      tr.innerHTML =
        "<td>" + reachLabel + "</td>" +
        "<td data-reach-id>" + reach + "</td>" +
        "<td>XS" + xs + "</td>" +
        "<td data-station=\"" + (row.station_km != null ? row.station_km : "") + "\">" + km + "</td>" +
        "<td data-xy>" + fmtXy(row.x != null ? row.x : row.lon) + "</td>" +
        "<td data-xy>" + fmtXy(row.y != null ? row.y : row.lat) + "</td>" +
        "<td><input data-n type=\"number\" min=\"0.001\" max=\"0.2\" step=\"0.001\" value=\"" +
        (Number.isFinite(n) ? n.toFixed(4) : "0.0300") + "\" /></td>";
      body.appendChild(tr);
    });
    fillManningReachSelect(lastManningRows);
    const reachEl = $("svManningReach");
    setManningFillRangeForReach(lastManningRows, reachEl ? reachEl.value : "main");
  }

  async function loadManningTable() {
    const body = $("svManningBody");
    const res = await fetch(API + "/manning-n", { headers: { "Accept": "application/json" } });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Không đọc được hệ số nhám.");
    }
    if (!data.rows || !data.rows.length) {
      renderManningRows([]);
      setWaterSource(data.water_source || "saint-venant");
      setXsSpacingInput(data.xs_spacing_m);
      setDtHoursInput(data.dt_hours);
      applyHdParams(data);
      lastExtractedXs = null;
      setManningError("Chưa có mặt cắt. Đổi khoảng XS để cắt từ DEM, hoặc chạy Model 1D.");
      return data;
    }
    setWaterSource(data.water_source || "saint-venant");
    setXsSpacingInput(data.xs_spacing_m);
    setDtHoursInput(data.dt_hours);
    applyHdParams(data);
    renderManningRows(data.rows);
    lastExtractedXs = selectedXsSpacingM();
    return data;
  }

  async function openManningModal() {
    const modal = manningModal();
    if (!modal) {
      startHydro1d();
      return;
    }
    setManningError("");
    setSvActionBusy(false);
    const body = $("svManningBody");
    if (body) body.innerHTML = "<tr><td colspan=\"7\">Đang tải…</td></tr>";
    modal.hidden = false;
    try {
      await loadManningTable();
    } catch (err) {
      if (body) body.innerHTML = "";
      setManningError(err.message || String(err));
    }
  }

  async function saveManningParams() {
    setManningError("");
    const rows = collectManningRows();
    if (!rows.length) {
      throw new Error("Không có mặt cắt để lưu.");
    }
    const dtEl = $("svDtHours");
    const dt = Number(dtEl && dtEl.value);
    if (!Number.isFinite(dt) || dt < 0.05 || dt > 24) {
      throw new Error("Δt (giờ) phải từ 0.05 đến 24. Ví dụ 0.25, 0.5, 1.");
    }
    const xs = selectedXsSpacingM();
    const hd = collectHdParams();
    const res = await fetch(API + "/manning-n", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
      },
      body: JSON.stringify(Object.assign({
        rows: rows,
        xs_spacing_m: xs,
        dt_hours: dt,
        water_source: selectedWaterSource()
      }, hd))
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Không lưu được thông số.");
    }
    if (data.xs_spacing_m != null) setXsSpacingInput(data.xs_spacing_m);
    if (data.dt_hours != null) setDtHoursInput(data.dt_hours);
    applyHdParams(data);
    return data;
  }

  function setSvActionBusy(busy, mode) {
    const saveBtn = $("svManningSave");
    const calBtn = $("svManningCalibrate");
    const inBtn = $("svManningInput");
    const evalBtn = $("svManningEval");
    const demBtn = $("svManningFromDem");
    const xsEl = $("svXsSpacing");
    if (xsEl) xsEl.disabled = !!busy;
    if (inBtn) inBtn.disabled = !!busy;
    if (evalBtn) evalBtn.disabled = !!busy;
    if (demBtn) {
      demBtn.disabled = !!busy;
      demBtn.textContent = busy && mode === "dem" ? "Đang lấy n…" : "Hệ số nhám từ DEM";
    }
    if (saveBtn) {
      saveBtn.disabled = !!busy;
      if (mode === "save") saveBtn.textContent = busy ? "Đang lưu…" : "Lưu thông số";
      else if (!busy) saveBtn.textContent = "Lưu thông số";
    }
    if (calBtn) {
      calBtn.disabled = !!busy;
      calBtn.textContent = busy && mode === "run" ? "Đang hiệu chỉnh…" : "Hiệu chỉnh";
    }
  }

  async function saveManningParamsOnly() {
    const saveBtn = $("svManningSave");
    const calBtn = $("svManningCalibrate");
    setManningError("");
    if (saveBtn) saveBtn.disabled = true;
    if (calBtn) calBtn.disabled = true;
    try {
      await saveManningParams();
      if (saveBtn) saveBtn.textContent = "Đã lưu";
      setTimeout(function () {
        if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu thông số";
      }, 1400);
    } catch (err) {
      setManningError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (calBtn) calBtn.disabled = false;
    }
  }

  async function calibrateSvModel() {
    setManningError("");
    setSvActionBusy(true, "run");
    try {
      const xs = selectedXsSpacingM();
      const dt = selectedDtHours();
      const hd = collectHdParams();
      await saveManningParams();
      closeManningModal();
      setSvActionBusy(false);
      startHydro1d({ xs_spacing_m: xs, dt_hours: dt, hd: hd });
    } catch (err) {
      setSvActionBusy(false);
      setManningError(err.message || String(err));
    }
  }

  async function startManningFromDem() {
    setManningError("");
    setSvActionBusy(true, "dem");
    try {
      const xs = selectedXsSpacingM();
      const hasRows = $("svManningBody") && $("svManningBody").querySelector("input[data-n]");
      if (!hasRows || lastExtractedXs == null || Math.abs(xs - lastExtractedXs) > 0.5) {
        lastExtractedXs = null;
        await extractXsForSpacing();
        setSvActionBusy(true, "dem");
      }
      const res = await fetch(API + "/manning-n-dem", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({
          water_source: selectedWaterSource(),
          file_id: fileId()
        })
      });
      const job = await res.json();
      if (res.status === 409) {
        const label = job.label
          || (job.kind === "1d" ? "Model 1D"
            : job.kind === "rr" ? "Model RR"
              : job.kind === "nam" ? "Model NAM"
                : "mô phỏng khác");
        throw new Error("Đang chạy " + label + ". Chờ xong rồi lấy hệ số nhám từ DEM.");
      }
      if (!res.ok) {
        throw new Error((job && (job.error || job.message)) || "Không lấy được hệ số nhám từ DEM.");
      }
      stopSimPoll();
      setHydroBusy(true, "manning-dem");
      openProgressModal(Object.assign({
        status: "running",
        kind: "manning-dem",
        label: "Hệ số nhám từ DEM",
        elapsed_s: 0,
        message: "Đang lấy hệ số nhám từ DEM quanh từng mặt cắt…",
        log: ["Đang chạy mainning.py…"]
      }, job || {}), "Hệ số nhám từ DEM");
      applySimStatus(job);
      if (job.status === "running") {
        simPollTimer = setInterval(pollHydro1d, 1000);
      }
    } catch (err) {
      setSvActionBusy(false);
      setManningError(err.message || String(err));
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

  function hydroTsMap(hydro) {
    const map = {};
    function add(hours, labels) {
      const hs = hours || [];
      const ls = labels || [];
      const n = Math.min(hs.length, ls.length);
      for (let i = 0; i < n; i++) {
        const x = Number(hs[i]);
        if (!isFinite(x) || ls[i] == null || ls[i] === "") continue;
        map[x] = String(ls[i]);
      }
    }
    add(hydro && hydro.hours_sim, hydro && hydro.ts_sim);
    add(hydro && hydro.hours_obs, hydro && hydro.ts_obs);
    add(hydro && hydro.hours_rain, hydro && hydro.ts_rain);
    return map;
  }

  function tsAtHour(hour, lookup) {
    if (lookup && lookup[hour]) return lookup[hour];
    const rounded = Math.round(hour);
    if (lookup && lookup[rounded]) return lookup[rounded];
    let best = "";
    let bestD = Infinity;
    Object.keys(lookup || {}).forEach(function (k) {
      const d = Math.abs(Number(k) - hour);
      if (d < bestD) {
        bestD = d;
        best = lookup[k];
      }
    });
    return best || "";
  }

  function fmtTsTick(hour, lookup) {
    const ts = tsAtHour(hour, lookup);
    if (!ts) return String(Math.round(hour));
    return ts;
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

  function setHydroNse(el, nse) {
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

  function setRrNse(nse) {
    setHydroNse($("rrHydroNse"), nse);
  }

  function setNamNse(nse) {
    setHydroNse($("namHydroNse"), nse);
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

  function bindHydroHover(canvas) {
    if (!canvas || canvas._hydroHoverBound) return;
    canvas._hydroHoverBound = true;
    canvas.style.cursor = "crosshair";
    canvas.addEventListener("mousemove", function (ev) {
      const rect = canvas.getBoundingClientRect();
      canvas._hoverPx = {
        x: ev.clientX - rect.left,
        y: ev.clientY - rect.top
      };
      if (canvas._hoverRaf) return;
      canvas._hoverRaf = requestAnimationFrame(function () {
        canvas._hoverRaf = 0;
        if (canvas._redrawHydro) canvas._redrawHydro();
      });
    });
    canvas.addEventListener("mouseleave", function () {
      canvas._hoverPx = null;
      if (canvas._redrawHydro) canvas._redrawHydro();
    });
  }

  function drawHydroHover(ctx, canvas, pad, w, h, plotH, xMin, xMax, X, Y, sim, obs, rain, showRain, tsMap) {
    const hover = canvas && canvas._hoverPx;
    if (!hover) return;
    if (hover.x < pad.l || hover.x > w - pad.r) return;
    const span = w - pad.l - pad.r;
    if (span <= 0) return;
    const hour = xMin + ((hover.x - pad.l) / span) * (xMax - xMin);
    const xx = X(Math.min(xMax, Math.max(xMin, hour)));
    ctx.save();
    ctx.strokeStyle = "rgba(232, 241, 250, 0.45)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(xx, pad.t);
    ctx.lineTo(xx, pad.t + plotH);
    ctx.stroke();
    ctx.setLineDash([]);

    function mark(pts, color) {
      if (!pts.length) return;
      const yv = interpY(pts, hour);
      if (!isFinite(yv)) return;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(xx, Y(yv), 4.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#101820";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    mark(obs, "#e8c547");
    mark(sim, "#3d9cf0");

    const items = [];
    items.push({ text: tsAtHour(hour, tsMap) || ("giờ " + hour.toFixed(1)), color: "#e8f1fa" });
    const qS = sim.length ? interpY(sim, hour) : NaN;
    const qO = obs.length ? interpY(obs, hour) : NaN;
    const qR = rain.length ? interpY(rain, hour) : NaN;
    const labels = (canvas && canvas._hydroLabels) || {};
    const simName = labels.simLabel || "Q tính toán";
    const obsName = labels.obsLabel || "Q thực đo";
    const unit = labels.unit || "m³/s";
    if (isFinite(qS)) items.push({ text: simName + "  " + fmtQ(qS) + " " + unit, color: "#7ec8ff" });
    if (isFinite(qO)) items.push({ text: obsName + "    " + fmtQ(qO) + " " + unit, color: "#e8c547" });
    if (showRain && isFinite(qR)) items.push({ text: "Mưa          " + fmtQ(qR) + " mm", color: "#8ec5f2" });

    ctx.font = "12px Segoe UI, Be Vietnam Pro, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const padBox = 8;
    const lineH = 17;
    let boxW = 0;
    items.forEach(function (it) {
      boxW = Math.max(boxW, ctx.measureText(it.text).width);
    });
    boxW += padBox * 2;
    const boxH = items.length * lineH + padBox;
    let bx = xx + 12;
    let by = pad.t + 8;
    if (bx + boxW > w - 8) bx = xx - 12 - boxW;
    if (bx < 8) bx = 8;
    if (by + boxH > pad.t + plotH) by = pad.t + plotH - boxH;
    ctx.fillStyle = "rgba(18, 24, 32, 0.94)";
    ctx.strokeStyle = "rgba(61, 156, 240, 0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (typeof ctx.roundRect === "function") ctx.roundRect(bx, by, boxW, boxH, 6);
    else ctx.rect(bx, by, boxW, boxH);
    ctx.fill();
    ctx.stroke();
    items.forEach(function (it, i) {
      ctx.fillStyle = it.color;
      ctx.fillText(it.text, bx + padBox, by + padBox / 2 + i * lineH);
    });
    ctx.restore();
  }

  function drawHydrographOn(canvas, meta, setNse, hydro, emptyHint, opts) {
    if (!canvas) return;
    const showRain = !!(opts && opts.showRain);
    const yLabel = (opts && opts.yLabel) || "Q (m³/s)";
    const unit = (opts && opts.unit) || "m³/s";
    const simLabel = (opts && opts.simLabel) || "Q tính toán";
    const obsLabel = (opts && opts.obsLabel) || "Q thực đo";
    const forceZero = !(opts && opts.noZero);
    const hideNse = !!(opts && opts.hideNse);
    canvas._hydroLabels = { simLabel: simLabel, obsLabel: obsLabel, unit: unit };
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
    const rainHours = (hydro && hydro.hours_rain && hydro.hours_rain.length)
      ? hydro.hours_rain
      : (hydro && hydro.hours_sim);
    const rain = showRain ? zipSeries(rainHours, hydro && hydro.rainfall_mm) : [];
    if (!sim.length && !obs.length && !rain.length) {
      ctx.fillStyle = "#aeb9c6";
      ctx.font = "12px Segoe UI, Be Vietnam Pro, sans-serif";
      ctx.fillText(emptyHint || "Chưa có Q tính toán hoặc Q thực đo.", 16, h / 2);
      if (meta) meta.textContent = emptyHint || "";
      setNse(null);
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
    acc(rain);
    if (!isFinite(xMin) || !isFinite(xMax) || xMax <= xMin) {
      xMin = 0;
      xMax = 1;
    }
    if (!sim.length && !obs.length) {
      yMin = 0;
      yMax = 1;
    } else {
      if (yMax <= yMin) yMax = yMin + 1;
      if (forceZero) {
        yMin = Math.min(0, yMin);
        yMax += Math.max(1, (yMax - yMin) * 0.08);
      } else {
        const padY = Math.max(0.05, (yMax - yMin) * 0.08);
        yMin -= padY;
        yMax += padY;
      }
    }
    const pad = { l: 52, r: showRain && rain.length ? 46 : 14, t: 12, b: 54 };
    const plotH = h - pad.t - pad.b;
    const tsMap = hydroTsMap(hydro);

    function X(x) {
      return pad.l + ((x - xMin) / (xMax - xMin)) * (w - pad.l - pad.r);
    }
    function Y(y) {
      return pad.t + (1 - (y - yMin) / (yMax - yMin)) * plotH;
    }

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 1;
    ctx.font = "11px Segoe UI, sans-serif";
    ctx.fillStyle = "#c5d0dc";
    for (let k = 0; k <= 4; k++) {
      const frac = k / 4;
      const val = yMin + (yMax - yMin) * (1 - frac);
      const yy = pad.t + frac * plotH;
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(w - pad.r, yy);
      ctx.stroke();
      ctx.fillText(fmtQ(val), 6, yy + 4);
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let k = 0; k <= 4; k++) {
      const val = xMin + (xMax - xMin) * (k / 4);
      const xx = X(val);
      ctx.save();
      ctx.translate(xx, h - pad.b + 6);
      ctx.rotate(-Math.PI / 6);
      ctx.fillText(fmtTsTick(val, tsMap), 0, 0);
      ctx.restore();
    }
    ctx.textBaseline = "alphabetic";
    ctx.fillText("Timeseries", (pad.l + w - pad.r) / 2, h - 4);
    ctx.textAlign = "left";
    ctx.save();
    ctx.translate(12, h / 2 + 28);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText(yLabel, 0, 0);
    ctx.restore();

    const stRain = seriesStats(rain);
    if (rain.length) {
      let rainMax = 0;
      rain.forEach(function (p) { if (p.y > rainMax) rainMax = p.y; });
      rainMax = Math.max(rainMax * 1.15, 1);
      const rainBand = plotH * 0.34;
      function Yrain(mm) {
        return pad.t + (mm / rainMax) * rainBand;
      }
      let dt = 1;
      if (rain.length >= 2) {
        const diffs = [];
        for (let i = 1; i < rain.length; i++) diffs.push(rain[i].x - rain[i - 1].x);
        diffs.sort(function (a, b) { return a - b; });
        dt = diffs[Math.floor(diffs.length / 2)] || 1;
      }
      const barW = Math.max(1.2, (X(xMin + dt) - X(xMin)) * 0.82);
      ctx.fillStyle = "rgba(74, 144, 217, 0.55)";
      rain.forEach(function (p) {
        if (p.y <= 0) return;
        const bh = Yrain(p.y) - pad.t;
        ctx.fillRect(X(p.x) - barW / 2, pad.t, barW, bh);
      });
      ctx.fillStyle = "#8ec5f2";
      ctx.textAlign = "right";
      for (let k = 0; k <= 3; k++) {
        const val = rainMax * (k / 3);
        ctx.fillText(fmtQ(val), w - 6, Yrain(val) + 3);
      }
      ctx.textAlign = "left";
      ctx.save();
      ctx.fillStyle = "#8ec5f2";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.translate(w - 22, pad.t + rainBand / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText("Mưa (mm)", 0, 0);
      ctx.restore();
    }

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
    if (!hideNse) setNse(nse);
    else if (setNse) setNse(null);
    if (meta) {
      const parts = [];
      if (!hideNse && nse != null && isFinite(nse)) parts.push("Nash–Sutcliffe NSE = " + nse.toFixed(3));
      if (stSim) parts.push(simLabel + " đỉnh " + fmtQ(stSim.peak) + " " + unit + " (" + (tsAtHour(stSim.tPeak, tsMap) || ("giờ " + Math.round(stSim.tPeak))) + ")");
      if (stObs) parts.push(obsLabel + " đỉnh " + fmtQ(stObs.peak) + " " + unit + " (" + (tsAtHour(stObs.tPeak, tsMap) || ("giờ " + Math.round(stObs.tPeak))) + ")");
      if (stRain) {
        let sumR = 0;
        rain.forEach(function (p) { sumR += p.y; });
        parts.push("Mưa tổng " + fmtQ(sumR) + " mm, đỉnh " + fmtQ(stRain.peak) + " mm (" + (tsAtHour(stRain.tPeak, tsMap) || ("giờ " + Math.round(stRain.tPeak))) + ")");
      }
      meta.textContent = parts.join(" · ");
    }
    drawHydroHover(ctx, canvas, pad, w, h, plotH, xMin, xMax, X, Y, sim, obs, rain, showRain, tsMap);
    canvas._redrawHydro = function () {
      drawHydrographOn(canvas, meta, setNse, hydro, emptyHint, opts);
    };
    bindHydroHover(canvas);
  }

  function drawRrHydrograph(hydro) {
    drawHydrographOn(
      $("rrHydroChart"),
      $("rrHydroMeta"),
      setRrNse,
      hydro,
      "Thiếu tank_result.csv hoặc demo_flow.csv (cột q_m3/s)."
    );
  }

  function drawNamHydrograph(hydro) {
    drawHydrographOn(
      $("namHydroChart"),
      $("namHydroMeta"),
      setNamNse,
      hydro,
      "Thiếu mike_nam_result.csv. Bấm Hiệu chỉnh để chạy NAM.",
      { showRain: true }
    );
  }

  function bindRrHydroResize() {
    const canvas = $("rrHydroChart");
    const wrap = canvas && canvas.parentElement;
    if (!wrap || typeof ResizeObserver === "undefined") return;
    if (rrHydroResizeObs) return;
    rrHydroResizeObs = new ResizeObserver(function () {
      if (rrHydroData) drawRrHydrograph(rrHydroData);
    });
    rrHydroResizeObs.observe(wrap);
  }

  function bindNamHydroResize() {
    const canvas = $("namHydroChart");
    const wrap = canvas && canvas.parentElement;
    if (!wrap || typeof ResizeObserver === "undefined") return;
    if (namHydroResizeObs) return;
    namHydroResizeObs = new ResizeObserver(function () {
      if (namHydroData) drawNamHydrograph(namHydroData);
    });
    namHydroResizeObs.observe(wrap);
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
    openRrInputModal();
  }

  async function openNamInputCsv() {
    openRrInputModal();
  }

  function rrInputModal() {
    return $("rrInputModal");
  }

  function setRrInputError(text) {
    const el = $("rrInputError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeRrInputModal() {
    const modal = rrInputModal();
    if (modal) modal.hidden = true;
    setRrInputError("");
  }

  function fmtInputNum(v) {
    if (v == null || v === "") return "";
    const n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    if (Math.abs(n - Math.round(n)) < 1e-9) return String(Math.round(n));
    return String(Math.round(n * 10000) / 10000);
  }

  function rrInputRowHtml(row) {
    const r = row || {};
    return (
      "<td><input data-k=\"hour\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.hour) + "\" /></td>" +
      "<td><input data-k=\"timeseries\" type=\"text\" value=\"" + String(r.timeseries || "").replace(/"/g, "&quot;") + "\" /></td>" +
      "<td><input data-k=\"rainfall_mm\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.rainfall_mm) + "\" /></td>" +
      "<td><input data-k=\"pet_mm\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.pet_mm) + "\" /></td>" +
      "<td><input data-k=\"q_m3s\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.q_m3s) + "\" /></td>" +
      "<td><button type=\"button\" class=\"rr-input-del\" title=\"Xóa dòng\">×</button></td>"
    );
  }

  function renderRrInputRows(rows) {
    const body = $("rrInputBody");
    if (!body) return;
    body.innerHTML = "";
    const list = rows && rows.length ? rows : [{ hour: 0, timeseries: "", rainfall_mm: "", pet_mm: "", q_m3s: "" }];
    list.forEach(function (row) {
      const tr = document.createElement("tr");
      tr.innerHTML = rrInputRowHtml(row);
      body.appendChild(tr);
    });
  }

  function collectRrInputRows() {
    const body = $("rrInputBody");
    if (!body) return [];
    const rows = [];
    body.querySelectorAll("tr").forEach(function (tr) {
      const get = function (k) {
        const el = tr.querySelector("input[data-k=\"" + k + "\"]");
        return el ? el.value : "";
      };
      rows.push({
        hour: get("hour"),
        timeseries: get("timeseries"),
        rainfall_mm: get("rainfall_mm"),
        pet_mm: get("pet_mm"),
        q_m3s: get("q_m3s")
      });
    });
    return rows;
  }

  function addRrInputRow() {
    const body = $("rrInputBody");
    if (!body) return;
    const last = body.querySelector("tr:last-child input[data-k=\"hour\"]");
    const lastHour = last ? Number(last.value) : NaN;
    const hour = Number.isFinite(lastHour) ? lastHour + 1 : body.children.length;
    const tr = document.createElement("tr");
    tr.innerHTML = rrInputRowHtml({ hour: hour, timeseries: "", rainfall_mm: 0, pet_mm: 0, q_m3s: "" });
    body.appendChild(tr);
    tr.scrollIntoView({ block: "nearest" });
  }

  function fillRrInputRange() {
    const from = Number($("rrInputFrom") && $("rrInputFrom").value);
    const to = Number($("rrInputTo") && $("rrInputTo").value);
    const rainEl = $("rrInputFillRain");
    const petEl = $("rrInputFillPet");
    const rain = rainEl && rainEl.value !== "" ? Number(rainEl.value) : null;
    const pet = petEl && petEl.value !== "" ? Number(petEl.value) : null;
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      setRrInputError("Nhập khoảng giờ hợp lệ.");
      return;
    }
    if (rain != null && !Number.isFinite(rain)) {
      setRrInputError("Mưa không hợp lệ.");
      return;
    }
    if (pet != null && !Number.isFinite(pet)) {
      setRrInputError("PET không hợp lệ.");
      return;
    }
    if (rain == null && pet == null) {
      setRrInputError("Nhập mưa hoặc PET để áp dụng.");
      return;
    }
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    const body = $("rrInputBody");
    if (!body) return;
    let count = 0;
    body.querySelectorAll("tr").forEach(function (tr) {
      const hourEl = tr.querySelector("input[data-k=\"hour\"]");
      const hour = Number(hourEl && hourEl.value);
      if (!Number.isFinite(hour) || hour < lo || hour > hi) return;
      if (rain != null) {
        const el = tr.querySelector("input[data-k=\"rainfall_mm\"]");
        if (el) el.value = fmtInputNum(rain);
      }
      if (pet != null) {
        const el = tr.querySelector("input[data-k=\"pet_mm\"]");
        if (el) el.value = fmtInputNum(pet);
      }
      count += 1;
    });
    setRrInputError(count ? "" : "Không có dòng trong khoảng giờ đã chọn.");
  }

  async function refreshRrHydroIfOpen() {
    const modal = rrParamsModal();
    if (!modal || modal.hidden) return;
    try {
      const res = await fetch(API + "/runoff-params", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) return;
      rrHydroData = data.hydrograph || null;
      drawRrHydrograph(rrHydroData || {});
    } catch (_err) {}
  }

  async function refreshNamHydroIfOpen() {
    const modal = namParamsModal();
    if (!modal || modal.hidden) return;
    try {
      const res = await fetch(API + "/nam-params", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) return;
      namHydroData = data.hydrograph || null;
      drawNamHydrograph(namHydroData || {});
    } catch (_err) {}
  }

  async function openRrInputModal() {
    const modal = rrInputModal();
    if (!modal) return;
    setRrInputError("");
    const body = $("rrInputBody");
    if (body) body.innerHTML = "<tr><td colspan=\"6\">Đang tải…</td></tr>";
    modal.hidden = false;
    try {
      const res = await fetch(API + "/runoff-input", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được dữ liệu đầu vào.");
      }
      renderRrInputRows(data.rows || []);
    } catch (err) {
      if (body) body.innerHTML = "";
      setRrInputError(err.message || String(err));
    }
  }

  async function saveRrInputRows() {
    const saveBtn = $("rrInputSave");
    setRrInputError("");
    const rows = collectRrInputRows();
    if (!rows.length) {
      setRrInputError("Không có dòng để lưu.");
      return;
    }
    if (saveBtn) saveBtn.disabled = true;
    try {
      const res = await fetch(API + "/runoff-input", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({ rows: rows })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không lưu được dữ liệu đầu vào.");
      }
      renderRrInputRows(data.rows || rows);
      await refreshRrHydroIfOpen();
      await refreshNamHydroIfOpen();
      if (saveBtn) saveBtn.textContent = "Đã lưu";
      setTimeout(function () {
        if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu dữ liệu";
      }, 1400);
    } catch (err) {
      setRrInputError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  function svInputModal() {
    return $("svInputModal");
  }

  function setSvInputError(text) {
    const el = $("svInputError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeSvInputModal() {
    const modal = svInputModal();
    if (modal) modal.hidden = true;
    setSvInputError("");
  }

  function svInputRowHtml(row) {
    const r = row || {};
    return (
      "<td><input data-k=\"hour\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.hour) + "\" /></td>" +
      "<td><input data-k=\"timeseries\" type=\"text\" value=\"" + String(r.timeseries || "").replace(/"/g, "&quot;") + "\" /></td>" +
      "<td><input data-k=\"q_m3s\" type=\"number\" step=\"any\" min=\"0\" value=\"" + fmtInputNum(r.q_m3s) + "\" /></td>" +
      "<td><input data-k=\"h_down_m\" type=\"number\" step=\"any\" value=\"" + fmtInputNum(r.h_down_m) + "\" /></td>" +
      "<td><input data-k=\"h_na1_m\" type=\"number\" step=\"any\" value=\"" + fmtInputNum(r.h_na1_m) + "\" /></td>" +
      "<td><button type=\"button\" class=\"rr-input-del\" title=\"Xóa dòng\">×</button></td>"
    );
  }

  function renderSvInputRows(rows) {
    const body = $("svInputBody");
    if (!body) return;
    body.innerHTML = "";
    const list = rows && rows.length ? rows : [{ hour: 0, timeseries: "", q_m3s: "", h_down_m: "", h_na1_m: "" }];
    list.forEach(function (row) {
      const tr = document.createElement("tr");
      tr.innerHTML = svInputRowHtml(row);
      body.appendChild(tr);
    });
  }

  function collectSvInputRows() {
    const body = $("svInputBody");
    if (!body) return [];
    const rows = [];
    body.querySelectorAll("tr").forEach(function (tr) {
      const get = function (k) {
        const el = tr.querySelector("input[data-k=\"" + k + "\"]");
        return el ? el.value : "";
      };
      rows.push({
        hour: get("hour"),
        timeseries: get("timeseries"),
        q_m3s: get("q_m3s"),
        h_down_m: get("h_down_m"),
        h_na1_m: get("h_na1_m")
      });
    });
    return rows;
  }

  function addSvInputRow() {
    const body = $("svInputBody");
    if (!body) return;
    const last = body.querySelector("tr:last-child input[data-k=\"hour\"]");
    const lastHour = last ? Number(last.value) : NaN;
    const hour = Number.isFinite(lastHour) ? lastHour + 1 : body.children.length;
    const tr = document.createElement("tr");
    tr.innerHTML = svInputRowHtml({ hour: hour, timeseries: "", q_m3s: "", h_down_m: "", h_na1_m: "" });
    body.appendChild(tr);
    tr.scrollIntoView({ block: "nearest" });
  }

  function fillSvInputRange() {
    const from = Number($("svInputFrom") && $("svInputFrom").value);
    const to = Number($("svInputTo") && $("svInputTo").value);
    const qEl = $("svInputFillQ");
    const hEl = $("svInputFillH");
    const nEl = $("svInputFillHna1");
    const q = qEl && qEl.value !== "" ? Number(qEl.value) : null;
    const h = hEl && hEl.value !== "" ? Number(hEl.value) : null;
    const hna1 = nEl && nEl.value !== "" ? Number(nEl.value) : null;
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      setSvInputError("Nhập khoảng giờ hợp lệ.");
      return;
    }
    if (q != null && !Number.isFinite(q)) {
      setSvInputError("Q không hợp lệ.");
      return;
    }
    if (h != null && !Number.isFinite(h)) {
      setSvInputError("H hạ lưu không hợp lệ.");
      return;
    }
    if (hna1 != null && !Number.isFinite(hna1)) {
      setSvInputError("H hạ lưu nhánh không hợp lệ.");
      return;
    }
    if (q == null && h == null && hna1 == null) {
      setSvInputError("Nhập Q, H hạ lưu hoặc H hạ lưu nhánh để áp dụng.");
      return;
    }
    const lo = Math.min(from, to);
    const hi = Math.max(from, to);
    const body = $("svInputBody");
    if (!body) return;
    let count = 0;
    body.querySelectorAll("tr").forEach(function (tr) {
      const hourEl = tr.querySelector("input[data-k=\"hour\"]");
      const hour = Number(hourEl && hourEl.value);
      if (!Number.isFinite(hour) || hour < lo || hour > hi) return;
      if (q != null) {
        const el = tr.querySelector("input[data-k=\"q_m3s\"]");
        if (el) el.value = fmtInputNum(q);
      }
      if (h != null) {
        const el = tr.querySelector("input[data-k=\"h_down_m\"]");
        if (el) el.value = fmtInputNum(h);
      }
      if (hna1 != null) {
        const el = tr.querySelector("input[data-k=\"h_na1_m\"]");
        if (el) el.value = fmtInputNum(hna1);
      }
      count += 1;
    });
    setSvInputError(count ? "" : "Không có dòng trong khoảng giờ đã chọn.");
  }

  async function openSvInputModal() {
    const modal = svInputModal();
    if (!modal) return;
    setSvInputError("");
    const body = $("svInputBody");
    if (body) body.innerHTML = "<tr><td colspan=\"6\">Đang tải…</td></tr>";
    modal.hidden = false;
    try {
      const res = await fetch(API + "/sv-input", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được dữ liệu biên 1D.");
      }
      renderSvInputRows(data.rows || []);
    } catch (err) {
      if (body) body.innerHTML = "";
      setSvInputError(err.message || String(err));
    }
  }

  async function saveSvInputRows() {
    const saveBtn = $("svInputSave");
    setSvInputError("");
    const rows = collectSvInputRows();
    if (!rows.length) {
      setSvInputError("Không có dòng để lưu.");
      return;
    }
    if (saveBtn) saveBtn.disabled = true;
    try {
      const res = await fetch(API + "/sv-input", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({ rows: rows })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không lưu được dữ liệu biên 1D.");
      }
      renderSvInputRows(data.rows || rows);
      if (saveBtn) saveBtn.textContent = "Đã lưu";
      setTimeout(function () {
        if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu dữ liệu";
      }, 1400);
    } catch (err) {
      setSvInputError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  let boundaryReachOptions = [
    { id: "basin", kind: "basin", label: "Lưu vực" },
    { id: "main", kind: "main", label: "Sông chính 1" },
    { id: "trib_1", kind: "trib", label: "Sông nhánh 1" }
  ];

  function boundaryStationsModal() {
    return $("boundaryStationsModal");
  }

  function setBoundaryStationsError(text) {
    const el = $("boundaryStationsError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeBoundaryStationsModal() {
    const modal = boundaryStationsModal();
    if (modal) modal.hidden = true;
    setBoundaryStationsError("");
  }

  function escAttr(v) {
    return String(v == null ? "" : v).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  }

  function boundaryReachKind(id) {
    const key = String(id || "").toLowerCase();
    if (key === "basin") return "basin";
    if (key === "main" || key.indexOf("main") === 0) return "main";
    return "trib";
  }

  function fillBoundaryReachSelects(reaches) {
    if (reaches && reaches.length) boundaryReachOptions = reaches.slice();
    const opts = boundaryReachOptions;
    const reachSel = $("boundaryStationsReach");
    if (reachSel) {
      const prev = reachSel.value;
      reachSel.innerHTML = "";
      opts.forEach(function (item) {
        const opt = document.createElement("option");
        opt.value = item.id;
        opt.textContent = (item.label || item.id) + " (" + item.id + ")";
        opt.setAttribute("data-kind", item.kind || boundaryReachKind(item.id));
        reachSel.appendChild(opt);
      });
      if (prev && reachSel.querySelector('option[value="' + prev + '"]')) {
        reachSel.value = prev;
      } else {
        const mainOpt = reachSel.querySelector('option[value="main"]');
        if (mainOpt) reachSel.value = "main";
      }
    }
  }

  function boundaryReachSelectHtml(selected) {
    const want = String(selected || "main");
    const opts = boundaryReachOptions.slice();
    if (want && !opts.some(function (item) { return item.id === want; })) {
      opts.push({
        id: want,
        kind: boundaryReachKind(want),
        label: want === "main" ? "Sông chính 1" : want
      });
    }
    return opts.map(function (item) {
      return "<option value=\"" + escAttr(item.id) + "\"" +
        (item.id === want ? " selected" : "") +
        " data-kind=\"" + escAttr(item.kind || boundaryReachKind(item.id)) + "\">" +
        escAttr((item.label || item.id) + " (" + item.id + ")") +
        "</option>";
    }).join("");
  }

  function boundaryStationRowHtml(row) {
    const r = row || {};
    const kind = String(r.kind || "Q").toUpperCase();
    const kinds = ["P", "PET", "Q", "H"];
    const opts = kinds.map(function (k) {
      return "<option value=\"" + k + "\"" + (k === kind ? " selected" : "") + ">" + k + "</option>";
    }).join("");
    const ok = r.file_ok ? "<span class=\"boundary-file-ok\">Có</span>" : "<span class=\"boundary-file-miss\">Thiếu</span>";
    const reach = String(r.reach || "main");
    return (
      "<td><input data-k=\"id\" type=\"text\" value=\"" + escAttr(r.id) + "\" /></td>" +
      "<td><input data-k=\"name\" type=\"text\" value=\"" + escAttr(r.name) + "\" /></td>" +
      "<td><select data-k=\"kind\">" + opts + "</select></td>" +
      "<td><select data-k=\"reach\">" + boundaryReachSelectHtml(reach) + "</select></td>" +
      "<td><input data-k=\"lon\" type=\"number\" step=\"0.000001\" value=\"" + escAttr(r.lon) + "\" /></td>" +
      "<td><input data-k=\"lat\" type=\"number\" step=\"0.000001\" value=\"" + escAttr(r.lat) + "\" /></td>" +
      "<td><input data-k=\"station_km\" type=\"number\" step=\"0.001\" value=\"" + escAttr(r.station_km) + "\" /></td>" +
      "<td><div class=\"boundary-file-cell\">" +
        "<input data-k=\"file\" type=\"text\" value=\"" + escAttr(r.file) + "\" />" +
        "<button type=\"button\" class=\"boundary-file-browse\" title=\"Chọn file trong thư mục\">Browse...</button>" +
      "</div></td>" +
      "<td><input data-k=\"value_col\" type=\"text\" value=\"" + escAttr(r.value_col) + "\" /></td>" +
      "<td><input data-k=\"unit\" type=\"text\" value=\"" + escAttr(r.unit) + "\" /></td>" +
      "<td>" + ok + "</td>" +
      "<td><button type=\"button\" class=\"rr-input-del\" title=\"Xóa điểm\">×</button></td>"
    );
  }

  function applyBoundaryReachFilter() {
    const filterEl = $("boundaryStationsReachFilter");
    const filter = filterEl ? String(filterEl.value || "") : "";
    const body = $("boundaryStationsBody");
    if (!body) return;
    Array.from(body.querySelectorAll("tr")).forEach(function (tr) {
      const reachEl = tr.querySelector("[data-k=\"reach\"]");
      const rid = reachEl ? String(reachEl.value || "") : "";
      const kind = boundaryReachKind(rid);
      tr.hidden = !!(filter && kind !== filter);
    });
  }

  function renderBoundaryStationRows(rows) {
    const body = $("boundaryStationsBody");
    if (!body) return;
    body.innerHTML = "";
    const list = rows && rows.length ? rows : [{
      id: "Q_us", name: "Q thượng lưu sông chính 1", kind: "Q", reach: "main",
      lon: "", lat: "", station_km: "", file: "", value_col: "q_m3s", unit: "m3/s"
    }];
    list.forEach(function (row) {
      const tr = document.createElement("tr");
      tr.setAttribute("data-xs-id", row.xs_id || "");
      tr.setAttribute("data-reach-kind", row.reach_kind || boundaryReachKind(row.reach));
      tr.innerHTML = boundaryStationRowHtml(row);
      body.appendChild(tr);
    });
    applyBoundaryReachFilter();
  }

  function collectBoundaryStationRows() {
    const body = $("boundaryStationsBody");
    if (!body) return [];
    const keys = ["id", "name", "kind", "reach", "lon", "lat", "station_km", "file", "value_col", "unit"];
    return Array.from(body.querySelectorAll("tr")).map(function (tr) {
      const rec = {};
      keys.forEach(function (key) {
        const el = tr.querySelector("[data-k=\"" + key + "\"]");
        rec[key] = el ? String(el.value || "").trim() : "";
      });
      rec.xs_id = tr.getAttribute("data-xs-id") || "";
      return rec;
    }).filter(function (rec) {
      return rec.id || rec.name || rec.file;
    });
  }

  async function openBoundaryStationsModal() {
    const modal = boundaryStationsModal();
    if (!modal) return;
    setBoundaryStationsError("");
    modal.hidden = false;
    try {
      const res = await fetch(API + "/boundary-stations", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không tải được khai báo biên.");
      }
      fillBoundaryReachSelects(data.reaches || []);
      renderBoundaryStationRows(data.rows || []);
    } catch (err) {
      fillBoundaryReachSelects([]);
      renderBoundaryStationRows([]);
      setBoundaryStationsError(err.message || String(err));
    }
  }

  function addBoundaryStationRow() {
    const body = $("boundaryStationsBody");
    if (!body) return;
    const n = body.querySelectorAll("tr").length + 1;
    const reachSel = $("boundaryStationsReach");
    const reach = reachSel && reachSel.value ? reachSel.value : "main";
    const kind = boundaryReachKind(reach);
    const tr = document.createElement("tr");
    tr.innerHTML = boundaryStationRowHtml({
      id: "bc_" + n,
      name: "",
      kind: kind === "basin" ? "P" : (kind === "trib" ? "H" : "Q"),
      reach: reach,
      lon: "",
      lat: "",
      station_km: "",
      file: "",
      value_col: kind === "trib" ? "h_na1_m" : (kind === "basin" ? "rainfall_mm" : "q_m3s"),
      unit: kind === "trib" || kind === "main" ? (kind === "trib" ? "m" : "m3/s") : "mm",
      file_ok: false
    });
    body.appendChild(tr);
    applyBoundaryReachFilter();
  }

  let boundaryBrowseTarget = null;
  let boundaryBrowseRow = null;
  let boundaryBrowseCwd = "";
  let boundaryBrowsePendingFile = "";

  function setBoundaryBrowseError(text) {
    const el = $("boundaryBrowseError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function clearBoundaryBrowseColumns() {
    boundaryBrowsePendingFile = "";
    const wrap = $("boundaryBrowseColumnsWrap");
    const cols = $("boundaryBrowseColumns");
    const nameEl = $("boundaryBrowseFileName");
    if (wrap) wrap.hidden = true;
    if (cols) cols.innerHTML = "";
    if (nameEl) nameEl.textContent = "";
  }

  function closeBoundaryBrowseModal() {
    const modal = $("boundaryBrowseModal");
    if (modal) modal.hidden = true;
    boundaryBrowseTarget = null;
    boundaryBrowseRow = null;
    clearBoundaryBrowseColumns();
    setBoundaryBrowseError("");
  }

  function parentDirOf(path) {
    const parts = String(path || "").replace(/\\/g, "/").split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  }

  function renderBoundaryBrowseList(data) {
    const list = $("boundaryBrowseList");
    const cwdEl = $("boundaryBrowseCwd");
    if (cwdEl) cwdEl.textContent = "/" + (data.cwd || "");
    if (!list) return;
    list.innerHTML = "";
    clearBoundaryBrowseColumns();
    const dirs = data.dirs || [];
    const files = data.files || [];
    if (!dirs.length && !files.length) {
      const empty = document.createElement("div");
      empty.className = "boundary-browse-empty";
      empty.textContent = "Thư mục trống (không có CSV).";
      list.appendChild(empty);
      return;
    }
    dirs.forEach(function (item) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "boundary-browse-item is-dir";
      btn.setAttribute("data-kind", "dir");
      btn.setAttribute("data-path", item.path || "");
      btn.textContent = "[DIR] " + (item.name || item.path);
      list.appendChild(btn);
    });
    files.forEach(function (item) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "boundary-browse-item is-file";
      btn.setAttribute("data-kind", "file");
      btn.setAttribute("data-path", item.path || "");
      btn.textContent = (item.name || item.path);
      list.appendChild(btn);
    });
  }

  function renderBoundaryBrowseColumns(filePath, columns) {
    const wrap = $("boundaryBrowseColumnsWrap");
    const box = $("boundaryBrowseColumns");
    const nameEl = $("boundaryBrowseFileName");
    if (!wrap || !box) return;
    boundaryBrowsePendingFile = filePath || "";
    if (nameEl) {
      const parts = String(filePath || "").split("/");
      nameEl.textContent = parts[parts.length - 1] || filePath || "";
    }
    box.innerHTML = "";
    (columns || []).forEach(function (name) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "boundary-browse-col";
      btn.setAttribute("data-col", name);
      btn.textContent = name;
      box.appendChild(btn);
    });
    wrap.hidden = !(columns && columns.length);
  }

  async function loadBoundaryBrowseDir(path) {
    setBoundaryBrowseError("");
    const list = $("boundaryBrowseList");
    if (list) list.innerHTML = "<div class=\"boundary-browse-empty\">Đang tải…</div>";
    try {
      const q = path ? ("?path=" + encodeURIComponent(path)) : "";
      const res = await fetch(API + "/browse-data" + q, { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không duyệt được thư mục.");
      }
      boundaryBrowseCwd = data.cwd || "";
      renderBoundaryBrowseList(data);
    } catch (err) {
      if (list) list.innerHTML = "";
      setBoundaryBrowseError(err.message || String(err));
    }
  }

  async function loadBoundaryBrowseColumns(filePath) {
    setBoundaryBrowseError("");
    const wrap = $("boundaryBrowseColumnsWrap");
    const box = $("boundaryBrowseColumns");
    if (wrap) wrap.hidden = false;
    if (box) box.innerHTML = "<div class=\"boundary-browse-empty\">Đang đọc cột…</div>";
    try {
      const res = await fetch(
        API + "/csv-columns?path=" + encodeURIComponent(filePath),
        { headers: { "Accept": "application/json" } }
      );
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được cột CSV.");
      }
      const cols = data.columns || [];
      if (!cols.length) {
        throw new Error("File không có tên cột.");
      }
      renderBoundaryBrowseColumns(filePath, cols);
      Array.from(document.querySelectorAll(".boundary-browse-item.is-file")).forEach(function (el) {
        el.classList.toggle("is-selected", el.getAttribute("data-path") === filePath);
      });
    } catch (err) {
      clearBoundaryBrowseColumns();
      setBoundaryBrowseError(err.message || String(err));
    }
  }

  function applyBoundaryBrowseSelection(filePath, column) {
    if (boundaryBrowseTarget) boundaryBrowseTarget.value = filePath || "";
    const row = boundaryBrowseRow ||
      (boundaryBrowseTarget && boundaryBrowseTarget.closest(".construction-item, tr"));
    if (row && column) {
      const colEl = row.querySelector("[data-k=\"value_col\"]");
      if (colEl) colEl.value = column;
      const mark = row.querySelector(".boundary-file-ok, .boundary-file-miss");
      if (mark) {
        mark.className = "boundary-file-ok";
        mark.textContent = "Có";
      }
    }
    closeBoundaryBrowseModal();
  }

  function openBoundaryBrowseForRow(tr) {
    if (!tr) return;
    const input = tr.querySelector("[data-k=\"file\"]");
    boundaryBrowseTarget = input || null;
    boundaryBrowseRow = tr;
    const modal = $("boundaryBrowseModal");
    if (!modal) return;
    modal.hidden = false;
    clearBoundaryBrowseColumns();
    const current = input ? String(input.value || "").replace(/\\/g, "/") : "";
    const start = current.indexOf("/") >= 0 ? parentDirOf(current) : "";
    loadBoundaryBrowseDir(start || "").then(function () {
      if (current && current.toLowerCase().endsWith(".csv")) {
        loadBoundaryBrowseColumns(current);
      }
    });
  }

  function onBoundaryBrowseListClick(ev) {
    const btn = ev.target && ev.target.closest ? ev.target.closest(".boundary-browse-item") : null;
    if (!btn) return;
    const kind = btn.getAttribute("data-kind");
    const path = btn.getAttribute("data-path") || "";
    if (kind === "dir") {
      loadBoundaryBrowseDir(path);
      return;
    }
    if (kind === "file") {
      loadBoundaryBrowseColumns(path);
    }
  }

  function onBoundaryBrowseColumnsClick(ev) {
    const btn = ev.target && ev.target.closest ? ev.target.closest(".boundary-browse-col") : null;
    if (!btn) return;
    const col = btn.getAttribute("data-col") || "";
    if (!col || !boundaryBrowsePendingFile) return;
    applyBoundaryBrowseSelection(boundaryBrowsePendingFile, col);
  }

  async function saveBoundaryStations(reset) {
    const saveBtn = $("boundaryStationsSave");
    const resetBtn = $("boundaryStationsResetBtn");
    setBoundaryStationsError("");
    const rows = reset ? null : collectBoundaryStationRows();
    if (!reset && !rows.length) {
      setBoundaryStationsError("Không có điểm biên để lưu.");
      return;
    }
    if (saveBtn) saveBtn.disabled = true;
    if (resetBtn) resetBtn.disabled = true;
    try {
      const res = await fetch(API + "/boundary-stations", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify(reset ? { reset: true } : { rows: rows })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không lưu được khai báo biên.");
      }
      fillBoundaryReachSelects(data.reaches || []);
      renderBoundaryStationRows(data.rows || []);
      if (saveBtn && !reset) {
        saveBtn.textContent = "Đã lưu";
        setTimeout(function () {
          if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu khai báo";
        }, 1400);
      }
    } catch (err) {
      setBoundaryStationsError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (resetBtn) resetBtn.disabled = false;
    }
  }

  let constructionReachOptions = [
    { id: "main", kind: "main", label: "Sông chính 1" },
    { id: "trib_1", kind: "trib", label: "Sông nhánh 1" }
  ];
  let constructionTypeLabels = {
    dike: "Đê / Tràn bãi",
    weir: "Đập / Tràn",
    reservoir: "Đập / Hồ chứa",
    gate: "Cống điều tiết",
    culvert: "Cống hộp",
    pump: "Trạm bơm"
  };
  let constructionFormulaLabels = {
    villemonte: "Villemonte (Công thức 1)",
    honma: "Honma (Công thức 2)",
    honma_ext: "Honma mở rộng (Công thức 3)",
    broad_crested: "Đỉnh rộng"
  };

  function constructionsModal() {
    return $("constructionsModal");
  }

  function setConstructionsError(text) {
    const el = $("constructionsError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeConstructionsModal() {
    const modal = constructionsModal();
    if (modal) modal.hidden = true;
    setConstructionsError("");
  }

  function fillConstructionReachSelects(reaches) {
    if (reaches && reaches.length) constructionReachOptions = reaches.slice();
    const sel = $("constructionsReach");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = "";
    constructionReachOptions.forEach(function (item) {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = (item.label || item.id) + " (" + item.id + ")";
      sel.appendChild(opt);
    });
    if (prev && sel.querySelector('option[value="' + prev + '"]')) sel.value = prev;
    else if (sel.querySelector('option[value="main"]')) sel.value = "main";
  }

  function constructionReachSelectHtml(selected) {
    const want = String(selected || "main");
    const opts = constructionReachOptions.slice();
    if (want && !opts.some(function (item) { return item.id === want; })) {
      opts.push({ id: want, kind: boundaryReachKind(want), label: want });
    }
    return opts.map(function (item) {
      return "<option value=\"" + escAttr(item.id) + "\"" +
        (item.id === want ? " selected" : "") + ">" +
        escAttr((item.label || item.id) + " (" + item.id + ")") +
        "</option>";
    }).join("");
  }

  function constructionTypeSelectHtml(selected) {
    const want = String(selected || "weir");
    const keys = Object.keys(constructionTypeLabels);
    return keys.map(function (k) {
      return "<option value=\"" + k + "\"" + (k === want ? " selected" : "") + ">" +
        escAttr(constructionTypeLabels[k]) + "</option>";
    }).join("");
  }

  function constructionControlSelectHtml(selected) {
    const want = String(selected || "free");
    const opts = [
      ["free", "Tự do"],
      ["controlled", "Điều khiển"],
      ["closed", "Đóng"]
    ];
    return opts.map(function (pair) {
      return "<option value=\"" + pair[0] + "\"" + (pair[0] === want ? " selected" : "") + ">" +
        pair[1] + "</option>";
    }).join("");
  }

  function constructionFormulaSelectHtml(selected) {
    let want = String(selected || "villemonte").toLowerCase();
    if (want === "free") want = "broad_crested";
    const keys = Object.keys(constructionFormulaLabels);
    const val = keys.indexOf(want) >= 0 ? want : "villemonte";
    return keys.map(function (k) {
      return "<option value=\"" + k + "\"" + (k === val ? " selected" : "") + ">" +
        escAttr(constructionFormulaLabels[k]) + "</option>";
    }).join("");
  }

  function constructionPlacementSelectHtml(selected, type) {
    let want = String(selected || "auto").toLowerCase();
    if (!want || want === "auto") {
      const t = String(type || "").toLowerCase();
      want = (t === "dike" || t === "pump") ? "lateral" : "inline";
    }
    const opts = [
      ["auto", "Tự động"],
      ["inline", "Điểm Q (trên sông)"],
      ["lateral", "Bên / nhập bên"]
    ];
    return opts.map(function (pair) {
      return "<option value=\"" + pair[0] + "\"" + (pair[0] === want ? " selected" : "") + ">" +
        pair[1] + "</option>";
    }).join("");
  }

  function constructionValveSelectHtml(selected) {
    const want = String(selected || "both").toLowerCase();
    const opts = [
      ["both", "Hai chiều"],
      ["positive", "Chỉ xuôi dòng"],
      ["negative", "Chỉ ngược dòng"]
    ];
    const val = (want === "positive" || want === "negative") ? want : "both";
    return opts.map(function (pair) {
      return "<option value=\"" + pair[0] + "\"" + (pair[0] === val ? " selected" : "") + ">" +
        pair[1] + "</option>";
    }).join("");
  }

  function constructionField(label, innerHtml, extraClass, showFor) {
    const showAttr = showFor ? " data-show-for=\"" + escAttr(showFor) + "\"" : "";
    return (
      "<label class=\"construction-field" + (extraClass ? " " + extraClass : "") + "\"" + showAttr + ">" +
        "<span>" + label + "</span>" +
        innerHtml +
      "</label>"
    );
  }

  function parseOutletGateWidths(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!text) return [];
    const parts = text.indexOf(";") >= 0 ? text.split(";") : text.split(",");
    return parts.map(function (part) { return String(part).trim(); }).filter(function (part) {
      return part !== "";
    });
  }

  function formatOutletGateWidths(raw) {
    const parts = parseOutletGateWidths(raw);
    return parts.length ? parts.join(" · ") : "";
  }

  function outletGateCountAttr(row) {
    const r = row || {};
    const raw = String(r.outlet_gate_count == null ? "" : r.outlet_gate_count).trim();
    if (raw !== "") return raw;
    const widths = parseOutletGateWidths(r.outlet_gate_widths_m);
    return widths.length ? String(widths.length) : "";
  }

  function renderOutletGateWidths(item, preset) {
    if (!item) return;
    const box = item.querySelector("[data-k=\"outlet_gate_widths\"]");
    const countEl = item.querySelector("[data-k=\"outlet_gate_count\"]");
    if (!box) return;
    let widths = Array.isArray(preset)
      ? preset.slice()
      : Array.from(box.querySelectorAll("[data-gate-width]")).map(function (el) {
        return String(el.value || "").trim();
      });
    let count = 0;
    if (countEl && String(countEl.value).trim() !== "") {
      count = Math.max(0, Math.floor(Number(countEl.value) || 0));
      if (count > 60) count = 60;
      if (String(countEl.value) !== String(count)) countEl.value = String(count);
    } else {
      count = widths.length;
    }
    while (widths.length < count) widths.push("");
    widths = widths.slice(0, count);
    if (!count) {
      box.innerHTML = "<span class=\"outlet-gate-empty\">Nhập số cửa để khai độ rộng từng cửa</span>";
      return;
    }
    box.innerHTML = (
      "<table class=\"outlet-gate-table\" aria-label=\"Độ rộng mỗi cửa\">" +
        "<thead><tr><th>Cửa</th><th>Độ rộng (m)</th></tr></thead>" +
        "<tbody>" +
        widths.map(function (width, index) {
          return (
            "<tr><td class=\"outlet-gate-no\">" + (index + 1) + "</td>" +
            "<td><input data-gate-width type=\"number\" min=\"0\" step=\"0.01\" value=\"" + escAttr(width) + "\" aria-label=\"Độ rộng cửa " + (index + 1) + "\" /></td></tr>"
          );
        }).join("") +
        "</tbody></table>"
    );
  }

  function constructionItemHtml(row) {
    const r = row || {};
    return (
      "<div class=\"construction-item-head\">" +
        constructionField("Mã", "<input data-k=\"id\" type=\"text\" value=\"" + escAttr(r.id) + "\" />", "cf-id") +
        constructionField("Tên", "<input data-k=\"name\" type=\"text\" value=\"" + escAttr(r.name) + "\" />", "cf-name") +
        constructionField("Loại", "<select data-k=\"type\">" + constructionTypeSelectHtml(r.type) + "</select>", "cf-type") +
        constructionField("Sông / nhánh", "<select data-k=\"reach\">" + constructionReachSelectHtml(r.reach) + "</select>", "cf-reach") +
        constructionField("Lý trình (km)", "<input data-k=\"station_km\" type=\"number\" step=\"0.001\" value=\"" + escAttr(r.station_km) + "\" />", "cf-km") +
        "<button type=\"button\" class=\"rr-input-del\" title=\"Xóa\">×</button>" +
      "</div>" +
      "<div class=\"construction-grid\">" +
        constructionField("Công thức", "<select data-k=\"formula\">" + constructionFormulaSelectHtml(r.formula) + "</select>", "", "weir,reservoir,dike") +
        constructionField("Vị trí trên lưới", "<select data-k=\"placement\">" + constructionPlacementSelectHtml(r.placement, r.type) + "</select>") +
        constructionField("Van / chiều chảy", "<select data-k=\"valve\">" + constructionValveSelectHtml(r.valve) + "</select>", "", "weir,culvert,gate,reservoir") +
        constructionField("Chế độ", "<select data-k=\"control\">" + constructionControlSelectHtml(r.control) + "</select>") +
        constructionField("Đỉnh / ngưỡng Hw (m)", "<input data-k=\"crest_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.crest_m) + "\" />", "", "weir,reservoir,dike") +
        constructionField("Cao trình đáy (m)", "<input data-k=\"invert_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.invert_m) + "\" />", "", "gate,culvert,reservoir,weir") +
        constructionField("Bề rộng W (m)", "<input data-k=\"width_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.width_m) + "\" />", "", "weir,gate,culvert,reservoir,dike") +
        constructionField("Chiều dài (m)", "<input data-k=\"length_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.length_m) + "\" />", "", "dike,culvert") +
        constructionField("Chiều cao (m)", "<input data-k=\"height_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.height_m) + "\" />", "", "gate,culvert,weir") +
        constructionField("Độ mở cửa (m)", "<input data-k=\"gate_opening_m\" type=\"number\" step=\"0.01\" min=\"0\" value=\"" + escAttr(r.gate_opening_m) + "\" />", "", "gate,reservoir,weir") +
        constructionField("Hệ số C / Cd", "<input data-k=\"cd\" type=\"number\" step=\"0.001\" value=\"" + escAttr(r.cd) + "\" />") +
        constructionField("Số mũ k", "<input data-k=\"submerged_exp\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.submerged_exp || "1.5") + "\" />", "", "weir,reservoir,dike") +
        constructionField("Q lớn nhất (m³/s)", "<input data-k=\"q_max_m3s\" type=\"number\" step=\"0.1\" value=\"" + escAttr(r.q_max_m3s) + "\" />", "", "reservoir,pump,weir,gate,culvert") +
        constructionField(
          "File điều khiển",
          "<div class=\"boundary-file-cell\">" +
            "<input data-k=\"file\" type=\"text\" value=\"" + escAttr(r.file) + "\" />" +
            "<button type=\"button\" class=\"boundary-file-browse\" title=\"Chọn file\">Chọn…</button>" +
          "</div>",
          "cf-file",
          "gate,pump,reservoir"
        ) +
        constructionField("Cột dữ liệu", "<input data-k=\"value_col\" type=\"text\" value=\"" + escAttr(r.value_col) + "\" />", "cf-col", "gate,pump,reservoir") +
      "</div>" +
      "<div class=\"res-layout\" data-show-for=\"weir,reservoir\">" +
        "<section class=\"res-card\">" +
          "<h3>Đập</h3>" +
          "<div class=\"res-grid\">" +
            constructionField("Diện tích hồ A (km²)", "<input data-k=\"storage_area_km2\" type=\"number\" step=\"0.0001\" min=\"0\" value=\"" + escAttr(storageAreaM2ToKm2(r.storage_area_m2)) + "\" />", "", "reservoir") +
            constructionField("H ban đầu (m)", "<input data-k=\"initial_level_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.initial_level_m) + "\" />", "", "reservoir") +
            constructionField("Cao trình đỉnh đập (m)", "<input data-k=\"dam_crest_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.dam_crest_m) + "\" />") +
          "</div>" +
        "</section>" +
        "<section class=\"res-card res-card-gates\">" +
          "<h3>Cửa xả</h3>" +
          "<div class=\"res-grid\">" +
            constructionField("Đáy cửa xả (m)", "<input data-k=\"outlet_sill_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.outlet_sill_m) + "\" />") +
            constructionField("Đỉnh tràn (m)", "<input data-k=\"spillway_crest_m\" type=\"number\" step=\"0.01\" value=\"" + escAttr(r.spillway_crest_m) + "\" />") +
            constructionField("Từ mép trái (m)", "<input data-k=\"gate_left_offset_m\" type=\"number\" step=\"0.01\" min=\"0\" value=\"" + escAttr(r.gate_left_offset_m) + "\" />") +
            constructionField("Cách các cửa (m)", "<input data-k=\"gate_spacing_m\" type=\"number\" step=\"0.01\" min=\"0\" value=\"" + escAttr(r.gate_spacing_m) + "\" />") +
            constructionField("Số cửa xả", "<input data-k=\"outlet_gate_count\" type=\"number\" min=\"0\" max=\"60\" step=\"1\" value=\"" + escAttr(outletGateCountAttr(r)) + "\" />", "cf-count") +
          "</div>" +
          "<label class=\"construction-field cf-gates\">" +
            "<span>Độ rộng mỗi cửa (m)</span>" +
            "<div class=\"outlet-gate-widths\" data-k=\"outlet_gate_widths\"></div>" +
          "</label>" +
        "</section>" +
      "</div>"
    );
  }

  function applyConstructionFieldVisibility(item) {
    if (!item) return;
    const typeEl = item.querySelector("[data-k=\"type\"]");
    const t = typeEl ? String(typeEl.value || "").toLowerCase() : "";
    Array.from(item.querySelectorAll("[data-show-for]")).forEach(function (field) {
      const allow = String(field.getAttribute("data-show-for") || "")
        .split(",")
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      const ok = !allow.length || allow.indexOf(t) >= 0;
      field.hidden = !ok;
    });
    const invertEl = item.querySelector("[data-k=\"invert_m\"]");
    if (invertEl) {
      const autoDem = t === "weir";
      const autoHas = t === "reservoir";
      invertEl.readOnly = autoDem || autoHas;
      invertEl.title = autoHas
        ? "Giống Cao trình đáy của hồ chứa: H thấp nhất trong quan hệ H–A–S"
        : (autoDem ? "Tự động lấy điểm thấp nhất của mặt cắt DEM đi qua đập" : "");
    }
    const openingEl = item.querySelector("[data-k=\"gate_opening_m\"]");
    if (openingEl) {
      const field = openingEl.closest(".construction-field");
      const controlEl = item.querySelector("[data-k=\"control\"]");
      const control = controlEl ? String(controlEl.value || "").toLowerCase() : "free";
      const span = field ? field.querySelector("span") : null;
      if (t === "gate") {
        if (field) field.hidden = false;
        if (span) span.textContent = "Độ mở cửa (m)";
      } else if (t === "reservoir" || t === "weir") {
        if (field) field.hidden = control !== "controlled";
        if (span) span.textContent = "Độ mở của tràn (m)";
      }
    }
    const areaEl = item.querySelector("[data-k=\"storage_area_km2\"]");
    if (areaEl) {
      const autoArea = t === "reservoir";
      areaEl.readOnly = autoArea;
      areaEl.title = autoArea
        ? "Nội suy từ bảng H–A–S theo H ban đầu, giống hồ chứa"
        : "";
    }
  }

  function reservoirHasUrl() {
    return window.floodUrl ? window.floodUrl("/reservoir/api/has") : "/reservoir/api/has";
  }

  async function loadConstructionHas() {
    if (constructionHasLoaded) return constructionHasRows;
    constructionHasLoaded = true;
    try {
      const res = await fetch(reservoirHasUrl(), { headers: { "Accept": "application/json" } });
      const data = await res.json();
      const table = data && data.has;
      if (!res.ok || !data.ok || !table || !table.level_m || table.level_m.length < 2) {
        constructionHasRows = [];
        return constructionHasRows;
      }
      constructionHasRows = table.level_m.map(function (level, i) {
        return {
          level: Number(level),
          area: Number(table.area_m2[i] || 0)
        };
      }).filter(function (row) { return row.area > 0; });
      if (constructionHasRows.length < 2) constructionHasRows = [];
    } catch (err) {
      constructionHasRows = [];
      constructionHasLoaded = false;
    }
    return constructionHasRows;
  }

  function interpConstructionArea(level) {
    const rows = constructionHasRows;
    if (rows.length < 2 || !isFinite(level)) return null;
    if (level <= rows[0].level) return rows[0].area;
    const last = rows[rows.length - 1];
    if (level >= last.level) return last.area;
    for (let i = 1; i < rows.length; i++) {
      const lo = rows[i - 1];
      const hi = rows[i];
      if (level <= hi.level) {
        const span = hi.level - lo.level;
        const t = Math.abs(span) < 1e-12 ? 0 : (level - lo.level) / span;
        return lo.area + t * (hi.area - lo.area);
      }
    }
    return last.area;
  }

  function syncConstructionAreaFromLevel(item) {
    if (!item) return;
    const typeEl = item.querySelector("[data-k=\"type\"]");
    if (!typeEl || String(typeEl.value || "").toLowerCase() !== "reservoir") return;
    const levelEl = item.querySelector("[data-k=\"initial_level_m\"]");
    const areaEl = item.querySelector("[data-k=\"storage_area_km2\"]");
    if (!levelEl || !areaEl) return;
    const area = interpConstructionArea(Number(levelEl.value));
    if (area != null && area > 0) areaEl.value = (area / 1e6).toFixed(3);
  }

  function syncAllConstructionAreas() {
    const body = $("constructionsBody");
    if (!body) return;
    Array.from(body.querySelectorAll(".construction-item")).forEach(syncConstructionAreaFromLevel);
  }

  function reservoirHasBedText() {
    if (!constructionHasRows.length) return "";
    const bed = constructionHasRows.reduce(function (lowest, row) {
      return Math.min(lowest, row.level);
    }, Infinity);
    return isFinite(bed) ? bed.toFixed(2) : "";
  }

  function syncReservoirInvertFromHas(item) {
    if (!item) return;
    const typeEl = item.querySelector("[data-k=\"type\"]");
    if (!typeEl || String(typeEl.value || "").toLowerCase() !== "reservoir") return;
    const invertEl = item.querySelector("[data-k=\"invert_m\"]");
    const bed = reservoirHasBedText();
    if (invertEl && bed) invertEl.value = bed;
  }

  let reservoirHasRebuild = Promise.resolve();
  let reservoirHasDirty = false;

  function applyHasTable(table) {
    const levels = table && table.level_m;
    if (!levels || levels.length < 2) return false;
    const rows = levels.map(function (level, i) {
      return {
        level: Number(level),
        area: Number(table.area_m2[i] || 0)
      };
    }).filter(function (row) { return row.area > 0; });
    if (rows.length < 2) return false;
    constructionHasLoaded = true;
    constructionHasRows = rows;
    return true;
  }

  function rebuildReservoirHasFromStation(item, writeFile) {
    if (!item) return reservoirHasRebuild;
    const typeEl = item.querySelector("[data-k=\"type\"]");
    if (!typeEl || String(typeEl.value || "").toLowerCase() !== "reservoir") return reservoirHasRebuild;
    const stationEl = item.querySelector("[data-k=\"station_km\"]");
    const reachEl = item.querySelector("[data-k=\"reach\"]");
    const station = stationEl ? String(stationEl.value || "").trim() : "";
    if (!station) return reservoirHasRebuild;
    const reach = reachEl ? String(reachEl.value || "main") : "main";
    const persist = writeFile !== false;
    reservoirHasRebuild = reservoirHasRebuild.then(async function () {
      if (stationEl) stationEl.disabled = true;
      setConstructionsError("Đang lập lại H–A–S theo lý trình " + station + " km…");
      try {
        const res = await fetch(reservoirHasUrl().replace(/\/api\/has$/, "/api/has-from-dem"), {
          method: "POST",
          headers: { "Content-Type": "application/json", "Accept": "application/json" },
          body: JSON.stringify({ station_km: Number(station), reach: reach, step_m: 0.25, write: persist })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error((data && data.error) || "Không lập lại được H–A–S");
        if (!applyHasTable(data.has)) throw new Error("Bảng H–A–S mới không có đủ mốc");
        syncReservoirInvertFromHas(item);
        syncConstructionAreaFromLevel(item);
        if (!persist) reservoirHasDirty = true;
        else reservoirHasDirty = false;
        setConstructionsError("");
      } catch (err) {
        setConstructionsError(err.message || String(err));
      } finally {
        if (stationEl) stationEl.disabled = false;
      }
    }).catch(function () {});
    return reservoirHasRebuild;
  }

  async function fillWeirInvertFromDem(item) {
    if (!item) return;
    const typeEl = item.querySelector("[data-k=\"type\"]");
    const structureType = typeEl ? String(typeEl.value || "").toLowerCase() : "";
    if (structureType === "reservoir") {
      await loadConstructionHas();
      syncReservoirInvertFromHas(item);
      return;
    }
    if (structureType !== "weir") return;
    const invertEl = item.querySelector("[data-k=\"invert_m\"]");
    const reachEl = item.querySelector("[data-k=\"reach\"]");
    const stationEl = item.querySelector("[data-k=\"station_km\"]");
    if (!invertEl || !stationEl || String(stationEl.value || "").trim() === "") return;
    try {
      const widthEl = item.querySelector("[data-k=\"width_m\"]");
      const qs = "reach=" + encodeURIComponent(reachEl ? reachEl.value : "main") +
        "&station_km=" + encodeURIComponent(stationEl.value) +
        "&width_m=" + encodeURIComponent(widthEl ? widthEl.value : "");
      const res = await fetch(API + "/weir-bed?" + qs, { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (res.ok && data && data.ok && data.invert_m != null && data.invert_m !== "") {
        invertEl.value = data.invert_m;
      }
    } catch (err) {
      /* Giữ giá trị đang có nếu chưa đọc được DEM. */
    }
  }

  function applyConstructionsTypeFilter() {
    const filterEl = $("constructionsTypeFilter");
    const filter = filterEl ? String(filterEl.value || "") : "";
    const body = $("constructionsBody");
    if (!body) return;
    Array.from(body.querySelectorAll(".construction-item")).forEach(function (item) {
      const typeEl = item.querySelector("[data-k=\"type\"]");
      const t = typeEl ? String(typeEl.value || "") : "";
      item.hidden = !!(filter && t !== filter);
      applyConstructionFieldVisibility(item);
    });
  }

  function bindConstructionItemMeta(el, row) {
    const r = row || {};
    el.setAttribute("data-xs-id", r.xs_id || "");
    el.setAttribute("data-lon", r.lon || "");
    el.setAttribute("data-lat", r.lat || "");
    el.setAttribute("data-qmin", r.q_min_m3s || "0");
    el.setAttribute("data-note", r.note || "");
    el.setAttribute("data-us", r.upstream_reach || "");
    el.setAttribute("data-ds", r.downstream_reach || "");
    el.setAttribute("data-unit", r.unit || "");
  }

  function renderConstructionRows(rows) {
    const body = $("constructionsBody");
    if (!body) return;
    body.innerHTML = "";
    const list = Array.isArray(rows) ? rows : [];
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "constructions-empty";
      empty.textContent =
        "Chưa có công trình. Model 1D chạy sông tự do (không cấu trúc). Bấm «Thêm công trình» hoặc «Mặc định».";
      body.appendChild(empty);
      return;
    }
    list.forEach(function (row) {
      const item = document.createElement("article");
      item.className = "construction-item";
      bindConstructionItemMeta(item, row);
      item.innerHTML = constructionItemHtml(row);
      renderOutletGateWidths(item, parseOutletGateWidths(row.outlet_gate_widths_m));
      body.appendChild(item);
      applyConstructionFieldVisibility(item);
    });
    applyConstructionsTypeFilter();
  }

  function collectConstructionRows() {
    const body = $("constructionsBody");
    if (!body) return [];
    const keys = [
      "id", "name", "type", "reach", "station_km", "formula", "placement", "valve",
      "crest_m", "width_m", "length_m", "height_m", "invert_m", "gate_opening_m",
      "cd", "submerged_exp", "initial_level_m",
      "dam_crest_m",
      "outlet_sill_m", "spillway_crest_m", "gate_left_offset_m", "gate_spacing_m",
      "outlet_gate_count",
      "q_max_m3s",
      "control", "file", "value_col"
    ];
    return Array.from(body.querySelectorAll(".construction-item")).map(function (item) {
      const rec = {};
      keys.forEach(function (key) {
        const el = item.querySelector("[data-k=\"" + key + "\"]");
        rec[key] = el ? String(el.value || "").trim() : "";
      });
      const km2El = item.querySelector("[data-k=\"storage_area_km2\"]");
      rec.storage_area_m2 = km2El ? storageAreaKm2ToM2(km2El.value) : "";
      const gateWidths = Array.from(item.querySelectorAll("[data-gate-width]")).map(function (el) {
        return String(el.value || "").trim();
      });
      rec.outlet_gate_widths_m = gateWidths.join(";");
      if (!rec.outlet_gate_count && gateWidths.length) {
        rec.outlet_gate_count = String(gateWidths.length);
      }
      rec.xs_id = item.getAttribute("data-xs-id") || "";
      rec.lon = item.getAttribute("data-lon") || "";
      rec.lat = item.getAttribute("data-lat") || "";
      rec.q_min_m3s = item.getAttribute("data-qmin") || "0";
      rec.note = item.getAttribute("data-note") || "";
      rec.upstream_reach = item.getAttribute("data-us") || "";
      rec.downstream_reach = item.getAttribute("data-ds") || "";
      rec.unit = item.getAttribute("data-unit") || "";
      if (!rec.submerged_exp) rec.submerged_exp = "1.5";
      if (!rec.valve) rec.valve = "both";
      return rec;
    }).filter(function (rec) {
      return rec.id || rec.name;
    });
  }

  async function openConstructionsModal() {
    const modal = constructionsModal();
    if (!modal) return;
    setConstructionsError("");
    modal.hidden = false;
    try {
      const res = await fetch(API + "/constructions", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không tải được công trình.");
      }
      if (data.type_labels) constructionTypeLabels = data.type_labels;
      if (data.formula_labels) constructionFormulaLabels = data.formula_labels;
      fillConstructionReachSelects(data.reaches || []);
      await loadConstructionHas();
      renderConstructionRows(data.rows || []);
      syncAllConstructionAreas();
      Array.from(document.querySelectorAll("#constructionsBody .construction-item")).forEach(syncReservoirInvertFromHas);
    } catch (err) {
      fillConstructionReachSelects([]);
      renderConstructionRows([]);
      setConstructionsError(err.message || String(err));
    }
  }

  function addConstructionRow() {
    const body = $("constructionsBody");
    if (!body) return;
    const emptyHint = body.querySelector(".constructions-empty");
    if (emptyHint) emptyHint.remove();
    const n = body.querySelectorAll(".construction-item").length + 1;
    const reachSel = $("constructionsReach");
    const reach = reachSel && reachSel.value ? reachSel.value : "main";
    const filterEl = $("constructionsTypeFilter");
    const type = (filterEl && filterEl.value) ? filterEl.value : "weir";
    const item = document.createElement("article");
    item.className = "construction-item";
    const row = {
      id: "ST_" + n,
      name: "",
      type: type,
      reach: reach,
      station_km: "",
      formula: "villemonte",
      placement: (type === "dike" || type === "pump") ? "lateral" : "inline",
      valve: "both",
      crest_m: "",
      width_m: "",
      length_m: "",
      height_m: "",
      invert_m: "",
      gate_opening_m: "",
      cd: type === "gate" ? "0.63" : (type === "culvert" ? "0.60" : "1.838"),
      submerged_exp: "1.5",
      storage_area_m2: "",
      initial_level_m: "",
      dam_crest_m: "",
      outlet_sill_m: "",
      spillway_crest_m: "",
      gate_left_offset_m: "",
      gate_spacing_m: "",
      outlet_gate_count: "",
      outlet_gate_widths_m: "",
      q_max_m3s: "",
      control: "free",
      file: "",
      value_col: ""
    };
    bindConstructionItemMeta(item, { q_min_m3s: "0" });
    item.innerHTML = constructionItemHtml(row);
    renderOutletGateWidths(item, []);
    body.appendChild(item);
    applyConstructionFieldVisibility(item);
    applyConstructionsTypeFilter();
    syncConstructionAreaFromLevel(item);
    item.scrollIntoView({ block: "nearest" });
  }

  async function saveConstructions(mode) {
    const saveBtn = $("constructionsSave");
    const resetBtn = $("constructionsResetBtn");
    const isReset = mode === true || mode === "reset";
    setConstructionsError("");
    if (!isReset) syncAllConstructionAreas();
    const rows = isReset ? null : collectConstructionRows();
    if (saveBtn) saveBtn.disabled = true;
    if (resetBtn) resetBtn.disabled = true;
    try {
      const body = isReset ? { reset: true } : { rows: rows || [] };
      const res = await fetch(API + "/constructions", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không lưu được công trình.");
      }
      if (data.type_labels) constructionTypeLabels = data.type_labels;
      if (data.formula_labels) constructionFormulaLabels = data.formula_labels;
      fillConstructionReachSelects(data.reaches || []);
      renderConstructionRows(data.rows || []);
      syncAllConstructionAreas();
      if (reservoirHasDirty) {
        Array.from(document.querySelectorAll("#constructionsBody .construction-item")).forEach(function (item) {
          rebuildReservoirHasFromStation(item, true);
        });
      } else {
        Array.from(document.querySelectorAll("#constructionsBody .construction-item")).forEach(syncReservoirInvertFromHas);
      }
      loadNetworkOverlay(false);
      if (saveBtn && !isReset) {
        saveBtn.textContent = "Đã lưu";
        setTimeout(function () {
          if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu công trình";
        }, 1400);
      }
    } catch (err) {
      setConstructionsError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (resetBtn) resetBtn.disabled = false;
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
    setHint("");
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

  function namParamsModal() {
    return $("namParamsModal");
  }

  function setNamParamsError(text) {
    const el = $("namParamsError");
    if (!el) return;
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = text;
  }

  function closeNamParamsModal() {
    const modal = namParamsModal();
    if (modal) modal.hidden = true;
    setNamParamsError("");
  }

  function renderNamParams(data) {
    const body = $("namParamsBody");
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
        inp.setAttribute("data-nam-key", field.key);
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

  function collectNamParams() {
    const body = $("namParamsBody");
    const values = {};
    if (!body) return values;
    body.querySelectorAll("input[data-nam-key]").forEach(function (inp) {
      values[inp.getAttribute("data-nam-key")] = inp.value;
    });
    return values;
  }

  async function openNamParamsModal() {
    const modal = namParamsModal();
    if (!modal) {
      startNamRunoff();
      return;
    }
    setNamParamsError("");
    const body = $("namParamsBody");
    if (body) body.innerHTML = "<p class=\"sv-manning-hint\">Đang tải thông số…</p>";
    modal.hidden = false;
    try {
      const res = await fetch(API + "/nam-params", { headers: { "Accept": "application/json" } });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error((data && data.error) || "Không đọc được thông số NAM.");
      }
      renderNamParams(data);
      namHydroData = data.hydrograph || null;
      bindNamHydroResize();
      requestAnimationFrame(function () {
        drawNamHydrograph(namHydroData || {});
      });
    } catch (err) {
      if (body) body.innerHTML = "";
      setNamParamsError(err.message || String(err));
    }
  }

  function setNamActionBusy(busy) {
    const saveBtn = $("namParamsSave");
    const calBtn = $("namParamsCalibrate");
    if (saveBtn) saveBtn.disabled = !!busy;
    if (calBtn) {
      calBtn.disabled = !!busy;
      calBtn.textContent = busy ? "Đang hiệu chỉnh…" : "Hiệu chỉnh";
    }
  }

  async function saveNamParams() {
    setNamParamsError("");
    const values = collectNamParams();
    if (!Object.keys(values).length) {
      throw new Error("Không có thông số để lưu.");
    }
    const res = await fetch(API + "/nam-params", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json"
      },
      body: JSON.stringify({ values: values })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error((data && data.error) || "Không lưu được thông số NAM.");
    }
    return data;
  }

  async function saveNamParamsOnly() {
    const saveBtn = $("namParamsSave");
    setNamParamsError("");
    if (saveBtn) saveBtn.disabled = true;
    try {
      await saveNamParams();
      if (saveBtn) saveBtn.textContent = "Đã lưu";
      setTimeout(function () {
        if (saveBtn && saveBtn.textContent === "Đã lưu") saveBtn.textContent = "Lưu thông số";
      }, 1400);
    } catch (err) {
      setNamParamsError(err.message || String(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function startNamRunoff(opts) {
    const keepModal = !!(opts && opts.keepModal);
    stopSimPoll();
    setHydroBusy(true, "nam");
    setNamActionBusy(true);
    setHint("");
    openProgressModal({
      status: "running",
      kind: "nam",
      label: "MIKE NAM mưa-dòng chảy",
      water_source: "nam",
      elapsed_s: 0,
      message: "Đang hiệu chỉnh Model NAM…",
      log: ["Đang hiệu chỉnh Model NAM…"]
    }, "Model NAM");
    try {
      const res = await fetch(API + "/simulate-nam", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json"
        },
        body: JSON.stringify({})
      });
      const job = await res.json();
      if (!res.ok && res.status !== 409) {
        throw new Error((job && (job.error || job.message)) || "Không chạy được Model NAM");
      }
      if (res.status === 409 && job.kind && job.kind !== "nam") {
        throw new Error((job && (job.error || job.message)) || "Đang có mô phỏng khác chạy.");
      }
      applySimStatus(job);
      if (job.status === "running") {
        simPollTimer = setInterval(pollHydro1d, 1000);
      } else if (job.status !== "running") {
        setNamActionBusy(false);
      }
    } catch (err) {
      setHydroBusy(false, "nam");
      setNamActionBusy(false);
      updateProgressModal({
        status: "error",
        kind: "nam",
        label: "MIKE NAM mưa-dòng chảy",
        message: err.message || String(err),
        log: [err.message || String(err)]
      });
    }
    return keepModal;
  }

  async function calibrateNamModel() {
    setNamParamsError("");
    setNamActionBusy(true);
    try {
      await saveNamParams();
      await startNamRunoff({ keepModal: true });
    } catch (err) {
      setNamActionBusy(false);
      setNamParamsError(err.message || String(err));
    }
  }

  function stopPlay() {
    if (playTimer) {
      clearInterval(playTimer);
      playTimer = null;
    }
    const btn = $("flow3dPlayBtn");
    if (btn) btn.textContent = "Phát";
  }

  function togglePlay() {
    if (!payload) return;
    if (playTimer) {
      stopPlay();
      return;
    }
    const btn = $("flow3dPlayBtn");
    if (btn) btn.textContent = "Dừng";
    playTimer = setInterval(function () {
      timeIndex = (timeIndex + 1) % payload.n_time;
      syncTimeUi();
    }, 120);
  }

  function canvasEl() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    return ctx && ctx.renderer ? ctx.renderer.domElement : null;
  }

  function disposeCoordOverlay() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (coordGroup && ctx && ctx.scene) ctx.scene.remove(coordGroup);
    if (coordGroup) {
      coordGroup.traverse(function (obj) {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(function (m) { m.dispose(); });
        }
      });
    }
    coordGroup = null;
    if (global.map) {
      if (coordMapMarker) global.map.removeLayer(coordMapMarker);
      if (coordMapPopup) global.map.closePopup(coordMapPopup);
    }
    coordMapMarker = null;
    coordMapPopup = null;
  }

  function drawCoordPin(hit) {
    disposeCoordOverlay();
    if (!hit) return;
    const THREE = global.THREE;
    if (THREE && global.Dem3D && typeof hit.elev === "number" && isFinite(hit.elev)) {
      const ctx = global.Dem3D.getContext && global.Dem3D.getContext();
      if (ctx && ctx.scene) {
        coordGroup = new THREE.Group();
        coordGroup.name = "coordPickOverlay";
        const geo = new THREE.SphereGeometry(0.7, 12, 12);
        const mat = new THREE.MeshBasicMaterial({ color: 0x38bdf8, depthTest: false });
        const mesh = new THREE.Mesh(geo, mat);
        mesh.position.copy(worldPoint(hit.lon, hit.lat, hit.elev, 0.25));
        mesh.renderOrder = 30;
        coordGroup.add(mesh);
        ctx.scene.add(coordGroup);
      }
    }
    if (global.map && typeof L !== "undefined") {
      coordMapMarker = L.circleMarker([hit.lat, hit.lon], {
        radius: 8,
        color: "#0ea5e9",
        weight: 2,
        fillColor: "#38bdf8",
        fillOpacity: 0.9
      }).addTo(global.map);
    }
  }

  function fmtCoord(v, digits) {
    const n = Number(v);
    return isFinite(n) ? n.toFixed(digits) : "—";
  }

  function setCoordCard(hit, geo) {
    const card = $("coordPickCard");
    if (!card) return;
    card.hidden = false;
    const lonEl = $("coordPickLon");
    const latEl = $("coordPickLat");
    const elevEl = $("coordPickElev");
    const placeEl = $("coordPickPlace");
    if (lonEl) lonEl.textContent = hit ? fmtCoord(hit.lon, 6) : "—";
    if (latEl) latEl.textContent = hit ? fmtCoord(hit.lat, 6) : "—";
    if (elevEl) {
      if (hit && hit.elev != null && isFinite(Number(hit.elev))) {
        elevEl.textContent = Number(hit.elev).toFixed(2) + " m";
      } else {
        elevEl.textContent = "—";
      }
    }
    if (placeEl) {
      if (!hit) placeEl.textContent = "Click một điểm trên bản đồ";
      else if (geo && geo.place) placeEl.textContent = geo.place;
      else placeEl.textContent = "Đang tra địa danh…";
    }
    if (hit && geo && geo.place && global.map && typeof L !== "undefined") {
      const html = "<b>" + geo.place + "</b><br>Kinh độ " + fmtCoord(hit.lon, 6) +
        "<br>Vĩ độ " + fmtCoord(hit.lat, 6);
      if (coordMapPopup) global.map.closePopup(coordMapPopup);
      coordMapPopup = L.popup({ maxWidth: 280 })
        .setLatLng([hit.lat, hit.lon])
        .setContent(html);
      if (document.body.classList.contains("mode-3d")) {
        /* 3D: chi hien the card */
      } else {
        coordMapPopup.openOn(global.map);
      }
    }
  }

  function bindMapClick() {
    const map = global.map;
    if (!map) {
      if (coordPicking || measureActive) setTimeout(bindMapClick, 250);
      return;
    }
    if (map._coordPickBound) return;
    map._coordPickBound = true;
    map.on("click", function (ev) {
      if ((!coordPicking && !measureActive) || !ev || !ev.latlng) return;
      const lat = ev.latlng.lat;
      const lon = ev.latlng.lng;
      const elev = global.Dem3D && global.Dem3D.sampleElevation
        ? global.Dem3D.sampleElevation(lon, lat)
        : null;
      if (measureActive) {
        applyMeasurePoint({ lon: lon, lat: lat, elev: elev });
        return;
      }
      applyCoordPick({ lon: lon, lat: lat, elev: elev });
    });
  }

  function applyCoordPick(hit) {
    if (!hit) return;
    coordHit = hit;
    drawCoordPin(hit);
    setCoordCard(hit, null);
    setHint("Kinh độ " + fmtCoord(hit.lon, 6) + " · vĩ độ " + fmtCoord(hit.lat, 6) + " · đang tra địa danh…");
    const req = ++coordReqId;
    const url = API + "/reverse-geocode?lon=" + encodeURIComponent(hit.lon) +
      "&lat=" + encodeURIComponent(hit.lat);
    fetch(url, { headers: { "Accept": "application/json" } })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        if (req !== coordReqId || !coordHit) return;
        if (!data || data.ok === false) {
          setCoordCard(coordHit, { place: "Không tra được địa danh" });
          setHint("Kinh độ " + fmtCoord(coordHit.lon, 6) + " · vĩ độ " + fmtCoord(coordHit.lat, 6));
          return;
        }
        setCoordCard(coordHit, data);
        setHint("Kinh độ " + fmtCoord(coordHit.lon, 6) + " · vĩ độ " + fmtCoord(coordHit.lat, 6) +
          (data.place ? " · " + data.place : ""));
      })
      .catch(function () {
        if (req !== coordReqId || !coordHit) return;
        setCoordCard(coordHit, { place: "Không tra được địa danh" });
        setHint("Kinh độ " + fmtCoord(coordHit.lon, 6) + " · vĩ độ " + fmtCoord(coordHit.lat, 6));
      });
  }

  function setCoordBtnActive(on) {
    const btn = $("coordPickBtn");
    if (btn) btn.classList.toggle("is-active", !!on);
  }

  function stopCoordPick(keepCard) {
    coordPicking = false;
    setCoordBtnActive(false);
    const canvas = canvasEl();
    if (canvas && !drawing) canvas.style.cursor = "";
    if (global.map && global.map.getContainer) global.map.getContainer().style.cursor = "";
    if (!keepCard) {
      coordHit = null;
      coordReqId += 1;
      disposeCoordOverlay();
      const card = $("coordPickCard");
      if (card) card.hidden = true;
    }
  }

  function startCoordPick() {
    if (coordPicking) {
      stopCoordPick(false);
      setHint("");
      return;
    }
    if (measureActive) stopMeasure(false);
    if (drawing) {
      drawing = false;
      sketch = [];
      const canvas = canvasEl();
      if (canvas) canvas.style.cursor = "";
    }
    coordPicking = true;
    setCoordBtnActive(true);
    bindCanvas();
    bindMapClick();
    const canvas = canvasEl();
    if (canvas) canvas.style.cursor = "crosshair";
    if (global.map && global.map.getContainer) global.map.getContainer().style.cursor = "crosshair";
    const card = $("coordPickCard");
    if (card) {
      card.hidden = false;
      setCoordCard(coordHit, coordHit ? { place: $("coordPickPlace") && $("coordPickPlace").textContent } : null);
      if (!coordHit) setCoordCard(null, null);
    }
    setHint("Chế độ Tọa độ: click một điểm trên DEM hoặc bản đồ 2D.");
  }

  function copyCoordText() {
    if (!coordHit) return;
    const text = fmtCoord(coordHit.lon, 6) + ", " + fmtCoord(coordHit.lat, 6);
    const done = function () {
      const btn = $("coordPickCopyBtn");
      if (!btn) return;
      const old = btn.textContent;
      btn.textContent = "Đã sao chép";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        window.prompt("Sao chép tọa độ", text);
      });
    } else {
      window.prompt("Sao chép tọa độ", text);
    }
  }

  function fmtLengthM(m) {
    const n = Number(m);
    if (!isFinite(n) || n < 0) return "—";
    if (n >= 1000) return (n / 1000).toFixed(3) + " km";
    return n.toFixed(1) + " m";
  }

  function measureTotals(pts) {
    let d2 = 0;
    let d3 = 0;
    let lastSeg2 = 0;
    let lastSeg3 = 0;
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      const h = haversineM(a.lon, a.lat, b.lon, b.lat);
      let s = h;
      if (a.elev != null && b.elev != null && isFinite(Number(a.elev)) && isFinite(Number(b.elev))) {
        const dz = Number(b.elev) - Number(a.elev);
        s = Math.sqrt(h * h + dz * dz);
      }
      d2 += h;
      d3 += s;
      lastSeg2 = h;
      lastSeg3 = s;
    }
    return { d2: d2, d3: d3, lastSeg2: lastSeg2, lastSeg3: lastSeg3 };
  }

  function disposeMeasureOverlay() {
    const ctx = global.Dem3D && global.Dem3D.getContext && global.Dem3D.getContext();
    if (measureGroup && ctx && ctx.scene) ctx.scene.remove(measureGroup);
    if (measureGroup) {
      measureGroup.traverse(function (obj) {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(function (m) { m.dispose(); });
        }
      });
    }
    measureGroup = null;
    if (measureMapLayer && global.map) {
      try { global.map.removeLayer(measureMapLayer); } catch (e) { /* ignore */ }
    }
    measureMapLayer = null;
  }

  function drawMeasureOverlay(pts) {
    disposeMeasureOverlay();
    if (!pts || !pts.length) return;
    const THREE = global.THREE;
    if (THREE && global.Dem3D) {
      const ctx = global.Dem3D.getContext && global.Dem3D.getContext();
      if (ctx && ctx.scene) {
        measureGroup = new THREE.Group();
        measureGroup.name = "measureLengthOverlay";
        const worldPts = [];
        for (let i = 0; i < pts.length; i++) {
          const p = pts[i];
          const elev = (p.elev != null && isFinite(Number(p.elev)))
            ? Number(p.elev)
            : sampleElevOr(p.lon, p.lat, 0);
          const wp = worldPoint(p.lon, p.lat, elev, 0.35);
          worldPts.push(wp);
          const geo = new THREE.SphereGeometry(i === 0 || i === pts.length - 1 ? 0.85 : 0.55, 10, 10);
          const mat = new THREE.MeshBasicMaterial({
            color: i === 0 ? 0x4ade80 : (i === pts.length - 1 ? 0xf87171 : 0xfbbf24),
            depthTest: false
          });
          const mesh = new THREE.Mesh(geo, mat);
          mesh.position.copy(wp);
          mesh.renderOrder = 32;
          measureGroup.add(mesh);
        }
        if (worldPts.length >= 2) {
          const lineGeo = new THREE.BufferGeometry().setFromPoints(worldPts);
          const lineMat = new THREE.LineBasicMaterial({
            color: 0xfbbf24,
            linewidth: 2,
            depthTest: false
          });
          const line = new THREE.Line(lineGeo, lineMat);
          line.renderOrder = 31;
          measureGroup.add(line);
        }
        ctx.scene.add(measureGroup);
      }
    }
    if (global.map && typeof L !== "undefined") {
      const latlngs = pts.map(function (p) { return [p.lat, p.lon]; });
      measureMapLayer = L.layerGroup();
      if (latlngs.length >= 2) {
        L.polyline(latlngs, { color: "#f59e0b", weight: 3, opacity: 0.95 }).addTo(measureMapLayer);
      }
      for (let i = 0; i < pts.length; i++) {
        L.circleMarker(latlngs[i], {
          radius: i === 0 || i === pts.length - 1 ? 7 : 5,
          color: i === 0 ? "#16a34a" : (i === pts.length - 1 ? "#dc2626" : "#d97706"),
          weight: 2,
          fillColor: i === 0 ? "#4ade80" : (i === pts.length - 1 ? "#f87171" : "#fbbf24"),
          fillOpacity: 0.95
        }).addTo(measureMapLayer);
      }
      measureMapLayer.addTo(global.map);
    }
  }

  function setMeasureCard() {
    const card = $("measureLengthCard");
    if (!card) return;
    card.hidden = false;
    const tot = measureTotals(measurePts);
    const nEl = $("measureLengthCount");
    const d2El = $("measureLength2d");
    const d3El = $("measureLength3d");
    const segEl = $("measureLengthSeg");
    if (nEl) nEl.textContent = String(measurePts.length);
    if (d2El) d2El.textContent = measurePts.length >= 2 ? fmtLengthM(tot.d2) : "—";
    if (d3El) d3El.textContent = measurePts.length >= 2 ? fmtLengthM(tot.d3) : "—";
    if (segEl) {
      segEl.textContent = measurePts.length >= 2
        ? fmtLengthM(tot.lastSeg2) + " / " + fmtLengthM(tot.lastSeg3)
        : "—";
    }
  }

  function setMeasureBtnActive(on) {
    const btn = $("measureLengthBtn");
    if (btn) btn.classList.toggle("is-active", !!on);
  }

  function stopMeasure(keepOverlay) {
    measureActive = false;
    setMeasureBtnActive(false);
    const canvas = canvasEl();
    if (canvas && !drawing && !coordPicking) canvas.style.cursor = "";
    if (global.map && global.map.getContainer && !coordPicking) {
      global.map.getContainer().style.cursor = "";
    }
    if (!keepOverlay) {
      measurePts = [];
      disposeMeasureOverlay();
      const card = $("measureLengthCard");
      if (card) card.hidden = true;
    }
  }

  function startMeasure() {
    if (measureActive) {
      stopMeasure(false);
      setHint("");
      return;
    }
    if (coordPicking) stopCoordPick(false);
    if (drawing) {
      drawing = false;
      sketch = [];
      const canvas = canvasEl();
      if (canvas) canvas.style.cursor = "";
    }
    measureActive = true;
    setMeasureBtnActive(true);
    bindCanvas();
    bindMapClick();
    const canvas = canvasEl();
    if (canvas) canvas.style.cursor = "crosshair";
    if (global.map && global.map.getContainer) global.map.getContainer().style.cursor = "crosshair";
    setMeasureCard();
    drawMeasureOverlay(measurePts);
    setHint("Đo dài: click các điểm trên DEM/bản đồ · Right-click xóa điểm cuối · Esc đóng.");
  }

  function undoMeasurePoint() {
    if (!measurePts.length) return;
    measurePts.pop();
    drawMeasureOverlay(measurePts);
    setMeasureCard();
    if (measurePts.length >= 2) {
      const tot = measureTotals(measurePts);
      setHint("Đo dài: " + measurePts.length + " điểm · mặt bằng " + fmtLengthM(tot.d2) +
        " · theo địa hình " + fmtLengthM(tot.d3));
    } else {
      setHint("Đo dài: còn " + measurePts.length + " điểm · click thêm.");
    }
  }

  function clearMeasurePoints() {
    measurePts = [];
    drawMeasureOverlay(measurePts);
    setMeasureCard();
    setHint("Đo dài: đã xóa hết điểm · click điểm đầu.");
  }

  function applyMeasurePoint(hit) {
    if (!hit || !measureActive) return;
    measurePts.push({
      lon: Number(hit.lon),
      lat: Number(hit.lat),
      elev: hit.elev != null && isFinite(Number(hit.elev)) ? Number(hit.elev) : null
    });
    drawMeasureOverlay(measurePts);
    setMeasureCard();
    if (measurePts.length >= 2) {
      const tot = measureTotals(measurePts);
      setHint("Đo dài: " + measurePts.length + " điểm · mặt bằng " + fmtLengthM(tot.d2) +
        " · theo địa hình " + fmtLengthM(tot.d3));
    } else {
      setHint("Đo dài: điểm 1 · click điểm tiếp theo.");
    }
  }

  function onPointerDown(e) {
    downPt = { x: e.clientX, y: e.clientY };
  }

  function onPointerUp(e) {
    if (!downPt) return;
    const dx = e.clientX - downPt.x;
    const dy = e.clientY - downPt.y;
    downPt = null;
    // Kéo chuột = xoay/pan OrbitControls, không coi là click.
    if (dx * dx + dy * dy > 36) return;
    if (coordPicking) {
      if (e.button === 2) return;
      const hit = global.Dem3D.pickTerrain(e.clientX, e.clientY);
      if (!hit) return;
      applyCoordPick(hit);
      return;
    }
    if (measureActive) {
      if (e.button === 2) {
        undoMeasurePoint();
        return;
      }
      if (e.button !== 0) return;
      const hit = global.Dem3D.pickTerrain(e.clientX, e.clientY);
      if (!hit) return;
      applyMeasurePoint(hit);
      return;
    }
    if (drawing) {
      if (e.button === 2) {
        sketch.pop();
        drawSketchLine(sketch);
        return;
      }
      const hit = global.Dem3D.pickTerrain(e.clientX, e.clientY);
      if (!hit) return;
      sketch.push({ lon: hit.lon, lat: hit.lat, elev: hit.elev });
      drawSketchLine(sketch);
      setHint("Điểm " + sketch.length + " · click thêm, Enter/double-click kết thúc, Right-click xóa điểm.");
      return;
    }
    if (e.button === 0) {
      if (tryOpenStructureAtPointer(e.clientX, e.clientY)) return;
      tryOpenXsAtPointer(e.clientX, e.clientY);
    }
  }

  function finishDraw() {
    if (!drawing) return;
    drawing = false;
    const canvas = canvasEl();
    if (canvas) canvas.style.cursor = "";
    if (sketch.length < 2) {
      setHint("Cần ít nhất 2 điểm.");
      return;
    }
    const coords = sketch.map(function (p) { return [p.lon, p.lat]; });
    requestProfile(coords).catch(function (err) {
      setHint(err.message || String(err));
      alert(err.message || String(err));
    });
  }

  function onDblClick(e) {
    if (!drawing) return;
    e.preventDefault();
    finishDraw();
  }

  function onKey(e) {
    if (measureActive && e.key === "Escape") {
      stopMeasure(false);
      setHint("");
      return;
    }
    if (coordPicking && e.key === "Escape") {
      stopCoordPick(false);
      setHint("");
      return;
    }
    if (!drawing) return;
    if (e.key === "Enter") finishDraw();
    if (e.key === "Escape") {
      drawing = false;
      const canvas = canvasEl();
      if (canvas) canvas.style.cursor = "";
      sketch = [];
      disposeOverlay();
      setHint("");
    }
  }

  function bindCanvas() {
    const canvas = canvasEl();
    if (!canvas || canvas._flow3dBound) return;
    canvas._flow3dBound = true;
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("dblclick", onDblClick);
    canvas.addEventListener("contextmenu", function (e) {
      if (drawing || measureActive) e.preventDefault();
    });
    window.addEventListener("keydown", onKey);
  }

  function startDraw() {
    stopCoordPick(false);
    stopMeasure(false);
    stopPlay();
    payload = null;
    setPanelVisible(false);
    sketch = [];
    drawing = true;
    disposeOverlay();
    bindCanvas();
    const canvas = canvasEl();
    if (canvas) canvas.style.cursor = "crosshair";
    setHint("Click trên DEM 3D để vẽ mặt cắt dọc. Enter hoặc double-click để tính mực nước.");
  }

  function clearAll() {
    stopPlay();
    stopCoordPick(false);
    stopMeasure(false);
    drawing = false;
    sketch = [];
    payload = null;
    disposeOverlay();
    setPanelVisible(false);
    const canvas = canvasEl();
    if (canvas) canvas.style.cursor = "";
    setHint("");
    lastRequest = null;
  }

  function hideChart() {
    stopPlay();
    setPanelVisible(false);
  }

  function wireUi() {
    const drawBtn = $("flow3dDrawBtn");
    const coordBtn = $("coordPickBtn");
    const coordClose = $("coordPickCloseBtn");
    const coordCopy = $("coordPickCopyBtn");
    const measureBtn = $("measureLengthBtn");
    const measureClose = $("measureLengthCloseBtn");
    const measureUndo = $("measureLengthUndoBtn");
    const measureClear = $("measureLengthClearBtn");
    const thalwegBtn = $("flow3dThalwegBtn");
    const clearBtn = $("flow3dClearBtn");
    const closeBtn = $("flow3dCloseBtn");
    const playBtn = $("flow3dPlayBtn");
    const slider = $("flow3dTime");
    if (drawBtn) drawBtn.addEventListener("click", startDraw);
    if (coordBtn) coordBtn.addEventListener("click", startCoordPick);
    if (coordClose) coordClose.addEventListener("click", function () { stopCoordPick(false); setHint(""); });
    if (coordCopy) coordCopy.addEventListener("click", copyCoordText);
    if (measureBtn) measureBtn.addEventListener("click", startMeasure);
    if (measureClose) measureClose.addEventListener("click", function () { stopMeasure(false); setHint(""); });
    if (measureUndo) measureUndo.addEventListener("click", undoMeasurePoint);
    if (measureClear) measureClear.addEventListener("click", clearMeasurePoints);
    const stInfoClose = $("structureInfoCloseBtn");
    if (stInfoClose) stInfoClose.addEventListener("click", closeStructurePopup);
    if (thalwegBtn) {
      thalwegBtn.addEventListener("click", function () {
        stopPlay();
        drawing = false;
        requestThalweg().catch(function (err) {
          setHint(err.message || String(err));
          alert(err.message || String(err));
        });
      });
    }
    if (clearBtn) clearBtn.addEventListener("click", clearAll);
    if (closeBtn) closeBtn.addEventListener("click", hideChart);
    if (playBtn) playBtn.addEventListener("click", togglePlay);
    if (slider) {
      slider.addEventListener("input", function () {
        if (!payload) return;
        timeIndex = Number(slider.value) || 0;
        stopPlay();
        syncTimeUi();
      });
    }
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
    const namBtn = $("hydroNamRunBtn");
    if (namBtn) namBtn.addEventListener("click", openNamParamsModal);
    const namCancel = $("namParamsCancel");
    const namInput = $("namParamsInput");
    const namSave = $("namParamsSave");
    const namCalibrate = $("namParamsCalibrate");
    const namModalEl = namParamsModal();
    if (namCancel) namCancel.addEventListener("click", closeNamParamsModal);
    if (namInput) namInput.addEventListener("click", openNamInputCsv);
    if (namSave) namSave.addEventListener("click", saveNamParamsOnly);
    if (namCalibrate) namCalibrate.addEventListener("click", calibrateNamModel);
    if (namModalEl) {
      namModalEl.addEventListener("click", function (ev) {
        if (ev.target === namModalEl) closeNamParamsModal();
      });
    }
    const rrInCancel = $("rrInputCancel");
    const rrInSave = $("rrInputSave");
    const rrInFill = $("rrInputFillBtn");
    const rrInAdd = $("rrInputAddBtn");
    const rrInModalEl = rrInputModal();
    const rrInBody = $("rrInputBody");
    if (rrInCancel) rrInCancel.addEventListener("click", closeRrInputModal);
    if (rrInSave) rrInSave.addEventListener("click", saveRrInputRows);
    if (rrInFill) rrInFill.addEventListener("click", fillRrInputRange);
    if (rrInAdd) rrInAdd.addEventListener("click", addRrInputRow);
    if (rrInBody) {
      rrInBody.addEventListener("click", function (ev) {
        const btn = ev.target && ev.target.closest ? ev.target.closest(".rr-input-del") : null;
        if (!btn) return;
        const tr = btn.closest("tr");
        if (tr && rrInBody.children.length > 1) tr.remove();
      });
    }
    if (rrInModalEl) {
      rrInModalEl.addEventListener("click", function (ev) {
        if (ev.target === rrInModalEl) closeRrInputModal();
      });
    }
    const manningCancel = $("svManningCancel");
    const manningInput = $("svManningInput");
    const manningSave = $("svManningSave");
    const manningCalibrate = $("svManningCalibrate");
    const manningEval = $("svManningEval");
    const manningFromDem = $("svManningFromDem");
    const manningFill = $("svManningFillBtn");
    const manningModalEl = manningModal();
    if (manningCancel) manningCancel.addEventListener("click", closeManningModal);
    if (manningInput) manningInput.addEventListener("click", openSvInputModal);
    if (manningSave) manningSave.addEventListener("click", saveManningParamsOnly);
    if (manningCalibrate) manningCalibrate.addEventListener("click", calibrateSvModel);
    if (manningEval) manningEval.addEventListener("click", openEvalModal);
    if (manningFromDem) manningFromDem.addEventListener("click", startManningFromDem);
    if (manningFill) manningFill.addEventListener("click", fillManningRange);
    const manningReach = $("svManningReach");
    if (manningReach) {
      manningReach.addEventListener("change", function () {
        setManningFillRangeForReach(lastManningRows, manningReach.value);
      });
    }
    const xsSpacingEl = $("svXsSpacing");
    if (xsSpacingEl) {
      xsSpacingEl.addEventListener("change", scheduleExtractXs);
      xsSpacingEl.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          clearTimeout(xsExtractTimer);
          extractXsForSpacing().catch(function () {});
        }
      });
    }
    document.querySelectorAll('input[name="svSolver"]').forEach(function (el) {
      el.addEventListener("change", function () { syncSolverParamsUi(true); });
    });
    syncSolverParamsUi(false);
    if (manningModalEl) {
      manningModalEl.addEventListener("click", function (ev) {
        if (ev.target === manningModalEl) closeManningModal();
      });
    }
    const svEvalModalEl = $("svEvalModal");
    const svEvalClose = $("svEvalClose");
    const svEvalXs = $("svEvalXs");
    if (svEvalClose) svEvalClose.addEventListener("click", closeEvalModal);
    if (svEvalXs) svEvalXs.addEventListener("change", loadEvalHydrograph);
    const svEvalReach = $("svEvalReach");
    if (svEvalReach) svEvalReach.addEventListener("change", onEvalReachChange);
    const svEvalLoad = $("svEvalLoad");
    if (svEvalLoad) svEvalLoad.addEventListener("click", pickEvalObsFile);
    const svEvalObsFile = $("svEvalObsFile");
    if (svEvalObsFile) svEvalObsFile.addEventListener("change", onEvalObsFilePicked);
    const svEvalObsCol = $("svEvalObsCol");
    if (svEvalObsCol) svEvalObsCol.addEventListener("change", applyEvalObsColumn);
    document.querySelectorAll('input[name="svEvalKind"]').forEach(function (el) {
      el.addEventListener("change", function () {
        if (svEvalData) drawEvalHydrograph(svEvalData);
      });
    });
    if (svEvalModalEl) {
      svEvalModalEl.addEventListener("click", function (ev) {
        if (ev.target === svEvalModalEl) closeEvalModal();
      });
    }
    const svInCancel = $("svInputCancel");
    const svInSave = $("svInputSave");
    const svInFill = $("svInputFillBtn");
    const svInAdd = $("svInputAddBtn");
    const svInModalEl = svInputModal();
    const svInBody = $("svInputBody");
    if (svInCancel) svInCancel.addEventListener("click", closeSvInputModal);
    if (svInSave) svInSave.addEventListener("click", saveSvInputRows);
    if (svInFill) svInFill.addEventListener("click", fillSvInputRange);
    if (svInAdd) svInAdd.addEventListener("click", addSvInputRow);
    if (svInBody) {
      svInBody.addEventListener("click", function (ev) {
        const btn = ev.target && ev.target.closest ? ev.target.closest(".rr-input-del") : null;
        if (!btn) return;
        const tr = btn.closest("tr");
        if (tr && svInBody.children.length > 1) tr.remove();
      });
    }
    if (svInModalEl) {
      svInModalEl.addEventListener("click", function (ev) {
        if (ev.target === svInModalEl) closeSvInputModal();
      });
    }
    const bcBtn = $("boundaryStationsBtn");
    const bcModalEl = boundaryStationsModal();
    const bcBody = $("boundaryStationsBody");
    if (bcBtn) bcBtn.addEventListener("click", openBoundaryStationsModal);
    if ($("boundaryStationsCancel")) $("boundaryStationsCancel").addEventListener("click", closeBoundaryStationsModal);
    if ($("boundaryStationsSave")) $("boundaryStationsSave").addEventListener("click", function () { saveBoundaryStations(false); });
    if ($("boundaryStationsAddBtn")) $("boundaryStationsAddBtn").addEventListener("click", addBoundaryStationRow);
    if ($("boundaryStationsResetBtn")) $("boundaryStationsResetBtn").addEventListener("click", function () { saveBoundaryStations(true); });
    const bcFilter = $("boundaryStationsReachFilter");
    if (bcFilter) bcFilter.addEventListener("change", applyBoundaryReachFilter);
    if (bcBody) {
      bcBody.addEventListener("click", function (ev) {
        const browseBtn = ev.target && ev.target.closest ? ev.target.closest(".boundary-file-browse") : null;
        if (browseBtn) {
          openBoundaryBrowseForRow(browseBtn.closest("tr"));
          return;
        }
        const btn = ev.target && ev.target.closest ? ev.target.closest(".rr-input-del") : null;
        if (!btn) return;
        const tr = btn.closest("tr");
        if (tr && bcBody.children.length > 1) tr.remove();
      });
    }
    if (bcModalEl) {
      bcModalEl.addEventListener("click", function (ev) {
        if (ev.target === bcModalEl) closeBoundaryStationsModal();
      });
    }
    const browseModal = $("boundaryBrowseModal");
    const browseList = $("boundaryBrowseList");
    if ($("boundaryBrowseCancel")) $("boundaryBrowseCancel").addEventListener("click", closeBoundaryBrowseModal);
    if ($("boundaryBrowseUp")) {
      $("boundaryBrowseUp").addEventListener("click", function () {
        loadBoundaryBrowseDir(parentDirOf(boundaryBrowseCwd));
      });
    }
    if (browseList) browseList.addEventListener("click", onBoundaryBrowseListClick);
    const browseCols = $("boundaryBrowseColumns");
    if (browseCols) browseCols.addEventListener("click", onBoundaryBrowseColumnsClick);
    if (browseModal) {
      browseModal.addEventListener("click", function (ev) {
        if (ev.target === browseModal) closeBoundaryBrowseModal();
      });
    }
    const stBtn = $("constructionsBtn");
    const stModalEl = constructionsModal();
    const stBody = $("constructionsBody");
    if (stBtn) stBtn.addEventListener("click", openConstructionsModal);
    if ($("constructionsCancel")) $("constructionsCancel").addEventListener("click", closeConstructionsModal);
    if ($("constructionsSave")) $("constructionsSave").addEventListener("click", function () { saveConstructions(false); });
    if ($("constructionsAddBtn")) $("constructionsAddBtn").addEventListener("click", addConstructionRow);
    if ($("constructionsResetBtn")) $("constructionsResetBtn").addEventListener("click", function () { saveConstructions("reset"); });
    const xsToggle = $("demXsToggle");
    const stToggle = $("demStructuresToggle");
    if (xsToggle) xsToggle.addEventListener("change", refreshNetworkOverlayVisibility);
    if (stToggle) stToggle.addEventListener("change", refreshNetworkOverlayVisibility);
    const stFilter = $("constructionsTypeFilter");
    if (stFilter) stFilter.addEventListener("change", applyConstructionsTypeFilter);
    if (stBody) {
      stBody.addEventListener("click", function (ev) {
        const browseBtn = ev.target && ev.target.closest ? ev.target.closest(".boundary-file-browse") : null;
        if (browseBtn) {
          openBoundaryBrowseForRow(browseBtn.closest(".construction-item"));
          return;
        }
        const btn = ev.target && ev.target.closest ? ev.target.closest(".rr-input-del") : null;
        if (!btn) return;
        const item = btn.closest(".construction-item");
        if (!item) return;
        item.remove();
        if (!stBody.querySelector(".construction-item")) {
          renderConstructionRows([]);
        }
      });
      stBody.addEventListener("input", function (ev) {
        const el = ev.target;
        if (!el) return;
        const key = el.getAttribute("data-k");
        const item = el.closest(".construction-item");
        if (!item) return;
        if (key === "outlet_gate_count") renderOutletGateWidths(item);
        if (key === "initial_level_m") syncConstructionAreaFromLevel(item);
      });
      stBody.addEventListener("change", function (ev) {
        const el = ev.target;
        if (!el) return;
        const key = el.getAttribute("data-k");
        const item = el.closest(".construction-item");
        if (!item) return;
        if (key === "type") {
          const placeEl = item.querySelector("[data-k=\"placement\"]");
          if (placeEl && placeEl.value === "auto") {
            const t = String(el.value || "").toLowerCase();
            placeEl.value = (t === "dike" || t === "pump") ? "lateral" : "inline";
          }
          applyConstructionFieldVisibility(item);
          applyConstructionsTypeFilter();
          fillWeirInvertFromDem(item);
          syncConstructionAreaFromLevel(item);
          return;
        }
        if (key === "control") applyConstructionFieldVisibility(item);
        if ((key === "reach" || key === "station_km") &&
            String((item.querySelector("[data-k=\"type\"]") || {}).value || "").toLowerCase() === "reservoir") {
          rebuildReservoirHasFromStation(item, false);
          return;
        }
        if (key === "reach" || key === "station_km" || key === "width_m") fillWeirInvertFromDem(item);
      });
    }
    if (stModalEl) {
      stModalEl.addEventListener("click", function (ev) {
        if (ev.target === stModalEl) closeConstructionsModal();
      });
    }
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      const browseEl = $("boundaryBrowseModal");
      if (browseEl && !browseEl.hidden) {
        closeBoundaryBrowseModal();
        return;
      }
      if (stModalEl && !stModalEl.hidden) {
        closeConstructionsModal();
        return;
      }
      if (bcModalEl && !bcModalEl.hidden) {
        closeBoundaryStationsModal();
        return;
      }
      if (svInModalEl && !svInModalEl.hidden) {
        closeSvInputModal();
        return;
      }
      if (rrInModalEl && !rrInModalEl.hidden) {
        closeRrInputModal();
        return;
      }
      if (rrModalEl && !rrModalEl.hidden) {
        closeRrParamsModal();
        return;
      }
      if (namModalEl && !namModalEl.hidden) {
        closeNamParamsModal();
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
        return;
      }
      if (measureActive) {
        stopMeasure(false);
        setHint("");
        return;
      }
      if (coordPicking) {
        stopCoordPick(false);
        setHint("");
      }
    });
    const progressClose = $("hydro1dProgressClose");
    if (progressClose) progressClose.addEventListener("click", closeProgressModal);
  }

  global.Flow3D = {
    startDraw: startDraw,
    clear: clearAll,
    teardown: function () {
      clearAll();
    },
    onTerrainReady: function () {
      bindCanvas();
      refreshNetworkOverlayVisibility();
      loadNetworkOverlay(false);
    },
    redrawOverlay: function () {
      if (payload) drawProfileOverlay(payload, timeIndex);
      else if (sketch.length) drawSketchLine(sketch);
      if (coordHit) drawCoordPin(coordHit);
      if (measurePts.length) drawMeasureOverlay(measurePts);
      if (networkOverlayData) drawNetworkOverlay(networkOverlayData);
    },
    refreshNetworkOverlay: function () {
      return loadNetworkOverlay(true);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireUi);
  } else {
    wireUi();
  }
})(window);

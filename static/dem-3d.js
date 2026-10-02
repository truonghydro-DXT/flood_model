/**
 * DEM / GeoTIFF 3D viewer (Three.js r128 + OrbitControls).
 * Có bản đồ nền OSM dưới mesh: lớp phủ cả vùng DEM + lớp chi tiết theo zoom.
 */
(function (global) {
  function floodUrl(path) {
    return window.floodUrl ? window.floodUrl(path) : path;
  }

  let renderer = null;
  let scene = null;
  let camera = null;
  let controls = null;
  let animationId = null;
  let currentMesh = null;
  let osmMesh = null;
  let osmBaseMesh = null;
  let osmDetailMesh = null;
  let contourGroup = null;
  let resizeHandler = null;
  let exaggeration = 0.02;
  let contoursVisible = false;
  let lastTerrain = null;
  /** Ứng viên nhãn contour + sprite đang hiện theo viewport */
  let contourLabelData = [];
  let contourLabelSprites = [];
  let contourLabelLayer = null;
  let labelsNeedUpdate = true;
  const _labelFrustum = new THREE.Frustum();
  const _labelProj = new THREE.Matrix4();
  const _labelWorld = new THREE.Vector3();
  const _labelNdc = new THREE.Vector3();
  const CONTOUR_LABEL_MAX_VISIBLE = 28;
  const CONTOUR_LABEL_MIN_NDC_DIST = 0.11;

  /** Cạnh DEM trong không gian Three.js */
  const DEM_WORLD_W = 200;
  /** Zoom camera tối đa (tương đương mức ~22 bản đồ) */
  const DEM3D_MAX_ZOOM = 32;
  /** Mức zoom quy ước khi vừa fit DEM vào khung */
  const DEM3D_FIT_ZOOM = 14;
  /** OSM tile gốc chỉ tới 19; z cao hơn dùng overzoom */
  const OSM_NATIVE_MAX_ZOOM = 19;
  /** Vành đai tile OSM quanh khung nhìn */
  const OSM_MARGIN_TILES = 1;
  /** Tile lớp nền (cả vùng DEM + xung quanh, giống 2D khi thu nhỏ) */
  const OSM_MAX_BASE_TILES = 72;
  /** Tile lớp chi tiết theo viewport */
  const OSM_MAX_DETAIL_TILES = 96;
  const OSM_GROUND_Y = -0.38;
  const OSM_DETAIL_Y = -0.34;
  const OSM_TILE_VER = '4';
  /** Mỗi phía mở rộng thêm hệ số × kích thước DEM */
  const OSM_WORLD_PAD = 1.5;

  let osmTexCache = Object.create(null);
  let osmTexInflight = Object.create(null);
  let osmRefreshTimer = null;
  let osmGen = 0;
  let osmFitDist = 80;
  let osmLastSig = '';
  let osmBaseSig = '';
  let osmBaseZ = 0;
  let osmBasePromise = null;
  let osmLoading = false;

  function $(id) {
    return document.getElementById(id);
  }

  function getOpenEntries() {
    if (typeof global.getOpenGeotiffEntries === 'function') {
      return global.getOpenGeotiffEntries();
    }
    return [];
  }

  function pickFileIdFor3D() {
    return "dem";
  }

  function disposeObject3D(root) {
    if (!root) return;
    if (scene) scene.remove(root);
    root.traverse(function (obj) {
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

  function disposeOsmGroup(root, disposeTextures) {
    if (!root) return;
    if (scene) scene.remove(root);
    root.traverse(function (obj) {
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach(function (m) {
          if (disposeTextures && m.map) m.map.dispose();
          m.dispose();
        });
      }
    });
  }

  function stopLoop() {
    if (animationId != null) {
      cancelAnimationFrame(animationId);
      animationId = null;
    }
  }

  function destroyViewer() {
    stopLoop();
    if (resizeHandler) {
      window.removeEventListener('resize', resizeHandler);
      resizeHandler = null;
    }
    disposeObject3D(currentMesh);
    currentMesh = null;
    if (osmRefreshTimer) {
      clearTimeout(osmRefreshTimer);
      osmRefreshTimer = null;
    }
    osmGen += 1;
    disposeOsmGroup(osmDetailMesh, true);
    disposeOsmGroup(osmBaseMesh, true);
    osmDetailMesh = null;
    osmBaseMesh = null;
    osmMesh = null;
    osmTexCache = Object.create(null);
    osmTexInflight = Object.create(null);
    osmLastSig = '';
    osmBaseSig = '';
    osmBaseZ = 0;
    osmBasePromise = null;
    disposeObject3D(contourGroup);
    contourGroup = null;
    clearContourLabels();
    contoursVisible = false;
    lastTerrain = null;
    if (controls) {
      controls.dispose();
      controls = null;
    }
    if (renderer) {
      renderer.dispose();
      const el = $('view3d');
      if (el && renderer.domElement.parentNode === el) {
        el.removeChild(renderer.domElement);
      }
      renderer = null;
    }
    scene = null;
    camera = null;
  }

  function initViewer() {
    const container = $('view3d');
    if (!container || !global.THREE) {
      throw new Error('Thiếu container #view3d hoặc thư viện Three.js');
    }
    if (!THREE.OrbitControls) {
      throw new Error('Thiếu THREE.OrbitControls');
    }

    destroyViewer();

    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || window.innerHeight;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x87a8c4);

    camera = new THREE.PerspectiveCamera(55, w / h, 0.01, 100000);
    camera.position.set(120, 90, 120);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h);
    container.appendChild(renderer.domElement);

    const amb = new THREE.AmbientLight(0xffffff, 0.65);
    const dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(80, 120, 40);
    scene.add(amb, dir);

    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.set(0, 0, 0);
    controls.minDistance = 0.05;
    controls.maxDistance = 50000;
    controls.zoomSpeed = 1.1;
    controls.addEventListener('change', function () {
      labelsNeedUpdate = true;
      scheduleOsmRefresh();
    });

    resizeHandler = function () {
      if (!renderer || !camera || !container) return;
      const nw = container.clientWidth || window.innerWidth;
      const nh = container.clientHeight || window.innerHeight;
      camera.aspect = nw / nh;
      camera.updateProjectionMatrix();
      renderer.setSize(nw, nh);
      scheduleOsmRefresh();
    };
    window.addEventListener('resize', resizeHandler);

    const loop = function () {
      animationId = requestAnimationFrame(loop);
      if (controls) controls.update();
      if (contoursVisible && contourLabelData.length) {
        updateContourLabelsInView();
      }
      if (renderer && scene && camera) renderer.render(scene, camera);
    };
    loop();
  }

  function decodeElevations(b64, width, height) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const elev = new Float32Array(bytes.buffer);
    if (elev.length !== width * height) {
      throw new Error('Kích thước elevations không khớp');
    }
    return elev;
  }

  function decodeMask(b64, width, height) {
    if (!b64) {
      const all = new Uint8Array(width * height);
      all.fill(1);
      return all;
    }
    const binary = atob(b64);
    const mask = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) mask[i] = binary.charCodeAt(i);
    if (mask.length !== width * height) {
      throw new Error('Kích thước mask không khớp');
    }
    return mask;
  }

  /**
   * Giải màu RGB theo cao độ (thấp → cao):
   * xanh dương → cyan → xanh lá → vàng → cam → đỏ
   */
  function heightColorRgb(t) {
    t = Math.max(0, Math.min(1, t));
    const stops = [
      { t: 0.0, r: 0.05, g: 0.2, b: 0.85 },
      { t: 0.2, r: 0.05, g: 0.75, b: 0.85 },
      { t: 0.4, r: 0.15, g: 0.85, b: 0.25 },
      { t: 0.6, r: 0.95, g: 0.9, b: 0.15 },
      { t: 0.8, r: 0.95, g: 0.45, b: 0.08 },
      { t: 1.0, r: 0.9, g: 0.1, b: 0.1 }
    ];

    let a = stops[0];
    let b = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
      if (t >= stops[i].t && t <= stops[i + 1].t) {
        a = stops[i];
        b = stops[i + 1];
        break;
      }
    }
    const u = (t - a.t) / Math.max(b.t - a.t, 1e-6);
    const c = new THREE.Color();
    c.setRGB(
      a.r + (b.r - a.r) * u,
      a.g + (b.g - a.g) * u,
      a.b + (b.b - a.b) * u
    );
    return c;
  }

  function loadTexture(url) {
    return new Promise(function (resolve, reject) {
      new THREE.TextureLoader().load(
        url,
        function (tex) {
          if (THREE.sRGBEncoding !== undefined) {
            tex.encoding = THREE.sRGBEncoding;
          }
          tex.anisotropy = 4;
          resolve(tex);
        },
        undefined,
        reject
      );
    });
  }

  const WEB_MERCATOR_MAX = 20037508.342789244;

  function parseDemBounds(bounds) {
    if (!bounds) return null;
    let west;
    let south;
    let east;
    let north;
    if (Array.isArray(bounds)) {
      west = bounds[0];
      south = bounds[1];
      east = bounds[2];
      north = bounds[3];
    } else {
      west = bounds.west;
      south = bounds.south;
      east = bounds.east;
      north = bounds.north;
    }
    if (![west, south, east, north].every(function (v) { return Number.isFinite(v); })) {
      return null;
    }
    return {
      west: west,
      south: south,
      east: east,
      north: north,
      lonSpan: Math.max(east - west, 1e-9),
      latSpan: Math.max(north - south, 1e-9),
      lonCenter: (west + east) / 2,
      latCenter: (south + north) / 2
    };
  }

  /** Bounds CRS gốc raster: {left,bottom,right,top} hoặc [left,bottom,right,top]. */
  function parseNativeBounds(bounds) {
    if (!bounds) return null;
    let left;
    let bottom;
    let right;
    let top;
    if (Array.isArray(bounds)) {
      left = bounds[0];
      bottom = bounds[1];
      right = bounds[2];
      top = bounds[3];
    } else {
      left = bounds.left;
      bottom = bounds.bottom;
      right = bounds.right;
      top = bounds.top;
    }
    if (![left, bottom, right, top].every(function (v) { return Number.isFinite(Number(v)); })) {
      return null;
    }
    left = Number(left);
    bottom = Number(bottom);
    right = Number(right);
    top = Number(top);
    const xSpan = Math.max(Math.abs(right - left), 1e-9);
    const ySpan = Math.max(Math.abs(top - bottom), 1e-9);
    return {
      left: left,
      bottom: bottom,
      right: right,
      top: top,
      xSpan: xSpan,
      ySpan: ySpan,
      xCenter: (left + right) / 2,
      yCenter: (bottom + top) / 2
    };
  }

  function isWebMercatorCrs(crs, nativeBounds) {
    if (crs) {
      const s = String(crs).toUpperCase();
      if (
        s.indexOf('3857') !== -1 ||
        s.indexOf('900913') !== -1 ||
        s.indexOf('WEB MERCATOR') !== -1 ||
        s.indexOf('PSEUDO-MERCATOR') !== -1 ||
        s.indexOf('POPULAR VISUALISATION') !== -1
      ) {
        return true;
      }
    }
    // Heuristic: bounds theo mét Web Mercator (không phải độ)
    if (nativeBounds) {
      const maxAbs = Math.max(
        Math.abs(nativeBounds.left),
        Math.abs(nativeBounds.right),
        Math.abs(nativeBounds.bottom),
        Math.abs(nativeBounds.top)
      );
      return maxAbs > 180;
    }
    return false;
  }

  function isGeographicCrs(crs) {
    if (!crs) return false;
    const s = String(crs).toUpperCase().trim();
    // Tránh false-positive với WKT Web Mercator có chuỗi "WGS 84" trong base CRS
    if (isWebMercatorCrs(s, null)) return false;
    if (s === 'EPSG:4326' || s === '4326') return true;
    if (s.indexOf('EPSG:4326') !== -1) return true;
    if (s.indexOf('GEOGCRS') !== -1 || s.indexOf('GEOGCS') !== -1) return true;
    return false;
  }

  function lonLatToWebMercator(lon, lat) {
    const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
    const x = (lon * WEB_MERCATOR_MAX) / 180;
    let y = Math.log(Math.tan(((90 + clampedLat) * Math.PI) / 360)) / (Math.PI / 180);
    y = (y * WEB_MERCATOR_MAX) / 180;
    return { x: x, y: y };
  }

  function demWorldSize(terrain) {
    const width = Math.max(1, Number(terrain.width) || 1);
    const height = Math.max(1, Number(terrain.height) || 1);
    const worldW = DEM_WORLD_W;
    // Ưu tiên tỉ lệ mét CRS gốc (khớp footprint địa lý); fallback theo pixel grid
    let aspect = height / width;
    const native = parseNativeBounds(terrain.bounds);
    if (native) {
      aspect = native.ySpan / native.xSpan;
    } else {
      const wgs = parseDemBounds(terrain.bounds_wgs84);
      if (wgs && !isGeographicCrs(terrain.crs)) {
        // Ước lượng tỉ lệ mercator từ bbox WGS84 khi thiếu bounds gốc
        const sw = lonLatToWebMercator(wgs.west, wgs.south);
        const ne = lonLatToWebMercator(wgs.east, wgs.north);
        const xSpan = Math.max(Math.abs(ne.x - sw.x), 1e-9);
        const ySpan = Math.max(Math.abs(ne.y - sw.y), 1e-9);
        aspect = ySpan / xSpan;
      }
    }
    return { worldW: worldW, worldH: worldW * aspect };
  }

  function elevationScaleForTerrain(terrain, worldW) {
    const zmin = Number(terrain.zmin);
    const zmax = Number(terrain.zmax);
    const zrange = Math.max(zmax - zmin, 1e-6);
    return (worldW / zrange) * exaggeration;
  }

  /** Map toạ độ phẳng (CRS DEM hoặc Web Mercator) → thế giới Three.js. */
  function projToWorld(px, py, projBounds, worldW, worldH) {
    const x = ((px - projBounds.xCenter) / projBounds.xSpan) * worldW;
    const z = -((py - projBounds.yCenter) / projBounds.ySpan) * worldH;
    return { x: x, z: z };
  }

  /** Lon/lat → thế giới; dùng mercator khi DEM là Web Mercator / projected. */
  function lonLatToWorld(lon, lat, terrain, worldW, worldH) {
    const native = parseNativeBounds(terrain && terrain.bounds);
    const wgs = parseDemBounds(terrain && terrain.bounds_wgs84);

    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const m = lonLatToWebMercator(lon, lat);
      return projToWorld(m.x, m.y, native, worldW, worldH);
    }

    if (native && isGeographicCrs(terrain.crs)) {
      return projToWorld(lon, lat, native, worldW, worldH);
    }

    // DEM projected khác 3857 nhưng có WGS84: căn OSM theo mercator
    // của footprint WGS84 (khớp tile OSM), mesh vẫn theo pixel/native aspect.
    if (wgs && !isGeographicCrs(terrain && terrain.crs)) {
      const sw = lonLatToWebMercator(wgs.west, wgs.south);
      const ne = lonLatToWebMercator(wgs.east, wgs.north);
      const mercBounds = {
        left: Math.min(sw.x, ne.x),
        right: Math.max(sw.x, ne.x),
        bottom: Math.min(sw.y, ne.y),
        top: Math.max(sw.y, ne.y),
        xSpan: Math.max(Math.abs(ne.x - sw.x), 1e-9),
        ySpan: Math.max(Math.abs(ne.y - sw.y), 1e-9),
        xCenter: (sw.x + ne.x) / 2,
        yCenter: (sw.y + ne.y) / 2
      };
      const m = lonLatToWebMercator(lon, lat);
      return projToWorld(m.x, m.y, mercBounds, worldW, worldH);
    }

    // Fallback: nội suy tuyến tính lon/lat (DEM geographic)
    if (!wgs) return { x: 0, z: 0 };
    const x = ((lon - wgs.lonCenter) / wgs.lonSpan) * worldW;
    const z = -((lat - wgs.latCenter) / wgs.latSpan) * worldH;
    return { x: x, z: z };
  }

  function lonLatToTileXY(lon, lat, z) {
    const n = Math.pow(2, z);
    const clampedLat = Math.max(Math.min(lat, 85.05112878), -85.05112878);
    const x = ((lon + 180) / 360) * n;
    const latRad = (clampedLat * Math.PI) / 180;
    const y =
      (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n;
    return { x: x, y: y };
  }

  function tileToLonLatBounds(z, x, y) {
    const n = Math.pow(2, z);
    const west = (x / n) * 360 - 180;
    const east = ((x + 1) / n) * 360 - 180;
    const north =
      (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / n))) * 180) / Math.PI;
    const south =
      (Math.atan(Math.sinh(Math.PI * (1 - (2 * (y + 1)) / n))) * 180) / Math.PI;
    return { west: west, south: south, east: east, north: north };
  }

  function loadOsmTexture(url) {
    if (osmTexCache[url]) return Promise.resolve(osmTexCache[url]);
    if (osmTexInflight[url]) return osmTexInflight[url];
    osmTexInflight[url] = loadTexture(url).then(function (tex) {
      tex.flipY = true;
      osmTexCache[url] = tex;
      const keys = Object.keys(osmTexCache);
      if (keys.length > 420) {
        keys.slice(0, keys.length - 300).forEach(function (k) {
          const old = osmTexCache[k];
          delete osmTexCache[k];
          if (old && old.dispose) old.dispose();
        });
      }
      return tex;
    }).finally(function () {
      delete osmTexInflight[url];
    });
    return osmTexInflight[url];
  }

  function osmWorldBounds(demBounds) {
    if (!demBounds) return null;
    const lonPad = demBounds.lonSpan * OSM_WORLD_PAD;
    const latPad = demBounds.latSpan * OSM_WORLD_PAD;
    return {
      west: demBounds.west - lonPad,
      south: demBounds.south - latPad,
      east: demBounds.east + lonPad,
      north: demBounds.north + latPad,
      lonSpan: demBounds.lonSpan + 2 * lonPad,
      latSpan: demBounds.latSpan + 2 * latPad
    };
  }

  function clampBoundsTo(inner, outer) {
    if (!inner) return outer;
    if (!outer) return inner;
    const west = Math.max(inner.west, outer.west);
    const east = Math.min(inner.east, outer.east);
    const south = Math.max(inner.south, outer.south);
    const north = Math.min(inner.north, outer.north);
    if (east <= west || north <= south) return outer;
    return { west: west, south: south, east: east, north: north };
  }

  function visibleLonLatBounds(terrain) {
    const demBounds = parseDemBounds(terrain && terrain.bounds_wgs84);
    const world = osmWorldBounds(demBounds) || demBounds;
    if (!camera) return world;
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), -OSM_GROUND_Y);
    const raycaster = new THREE.Raycaster();
    const hit = new THREE.Vector3();
    const samples = [
      [-1, -1], [1, -1], [1, 1], [-1, 1],
      [0, 0], [-0.5, 0], [0.5, 0], [0, -0.5], [0, 0.5]
    ];
    const lons = [];
    const lats = [];
    for (let i = 0; i < samples.length; i++) {
      raycaster.setFromCamera(new THREE.Vector2(samples[i][0], samples[i][1]), camera);
      if (!raycaster.ray.intersectPlane(plane, hit)) continue;
      const ll = worldToLonLat(hit.x, hit.z, terrain);
      if (!Number.isFinite(ll.lon) || !Number.isFinite(ll.lat)) continue;
      lons.push(ll.lon);
      lats.push(ll.lat);
    }
    if (!lons.length) return world;
    let west = Math.min.apply(null, lons);
    let east = Math.max.apply(null, lons);
    let south = Math.min.apply(null, lats);
    let north = Math.max.apply(null, lats);
    const padLon = Math.max((east - west) * 0.08, 0.001);
    const padLat = Math.max((north - south) * 0.08, 0.001);
    const view = {
      west: west - padLon,
      east: east + padLon,
      south: south - padLat,
      north: north + padLat
    };
    return clampBoundsTo(view, world);
  }

  function zoomForView(bounds) {
    const pixelW = (renderer && renderer.domElement && renderer.domElement.clientWidth) || 960;
    const lonSpan = Math.max(bounds.east - bounds.west, 1e-9);
    let z = Math.log2((360 / lonSpan) * (pixelW / 256));
    if (camera && controls && osmFitDist > 0) {
      const d = camera.position.distanceTo(controls.target);
      const zCam = DEM3D_FIT_ZOOM + Math.log2(osmFitDist / Math.max(d, 1e-6));
      if (Number.isFinite(zCam)) z = Math.max(z || 0, zCam);
    }
    return Math.max(5, Math.min(OSM_NATIVE_MAX_ZOOM, Math.round(z)));
  }

  function chooseZoomFitting(bounds, maxTiles) {
    for (let z = OSM_NATIVE_MAX_ZOOM; z >= 5; z--) {
      const range = tileRangeForBounds(bounds, z);
      if (range.count > 0 && range.count <= maxTiles) return z;
    }
    return 5;
  }

  function tileRangeForBounds(bounds, z) {
    const n = Math.pow(2, z);
    const nw = lonLatToTileXY(bounds.west, bounds.north, z);
    const se = lonLatToTileXY(bounds.east, bounds.south, z);
    let x0 = Math.floor(nw.x) - OSM_MARGIN_TILES;
    let y0 = Math.floor(nw.y) - OSM_MARGIN_TILES;
    let x1 = Math.floor(se.x) + OSM_MARGIN_TILES;
    let y1 = Math.floor(se.y) + OSM_MARGIN_TILES;
    x0 = Math.max(0, x0);
    y0 = Math.max(0, y0);
    x1 = Math.min(n - 1, x1);
    y1 = Math.min(n - 1, y1);
    if (x1 < x0 || y1 < y0) {
      return { z: z, x0: 0, y0: 0, x1: -1, y1: -1, count: 0 };
    }
    return {
      z: z,
      x0: x0,
      y0: y0,
      x1: x1,
      y1: y1,
      count: (x1 - x0 + 1) * (y1 - y0 + 1)
    };
  }

  function scheduleOsmRefresh() {
    if (!lastTerrain || !scene) return;
    if (osmRefreshTimer) clearTimeout(osmRefreshTimer);
    osmRefreshTimer = setTimeout(function () {
      osmRefreshTimer = null;
      refreshOsmBasemap(lastTerrain, false);
    }, 160);
  }

  async function loadOsmTileGroup(terrain, range, groundY, isStale) {
    const dem = demWorldSize(terrain);
    const group = new THREE.Group();
    const jobs = [];
    for (let ty = range.y0; ty <= range.y1; ty++) {
      for (let tx = range.x0; tx <= range.x1; tx++) {
        jobs.push({ tx: tx, ty: ty, z: range.z });
      }
    }

    async function addTile(job) {
      if (isStale()) return;
      const b = tileToLonLatBounds(job.z, job.tx, job.ty);
      const sw = lonLatToWorld(b.west, b.south, terrain, dem.worldW, dem.worldH);
      const ne = lonLatToWorld(b.east, b.north, terrain, dem.worldW, dem.worldH);
      const w = Math.abs(ne.x - sw.x);
      const h = Math.abs(sw.z - ne.z);
      const url = floodUrl(
        '/api/basemap/osm/' + job.z + '/' + job.tx + '/' + job.ty + '.png?v=' + OSM_TILE_VER
      );
      let tex;
      try {
        tex = await loadOsmTexture(url);
      } catch (_e) {
        return;
      }
      if (isStale()) return;
      const geometry = new THREE.PlaneGeometry(Math.max(w, 1e-3), Math.max(h, 1e-3));
      geometry.rotateX(-Math.PI / 2);
      const material = new THREE.MeshBasicMaterial({
        map: tex,
        side: THREE.DoubleSide,
        depthWrite: false
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set((sw.x + ne.x) / 2, groundY, (sw.z + ne.z) / 2);
      group.add(mesh);
    }

    const concurrency = 8;
    for (let i = 0; i < jobs.length; i += concurrency) {
      if (isStale()) {
        disposeOsmGroup(group, false);
        return null;
      }
      await Promise.all(jobs.slice(i, i + concurrency).map(addTile));
    }
    if (isStale() || !group.children.length) {
      disposeOsmGroup(group, false);
      return null;
    }
    return group;
  }

  async function ensureOsmBase(terrain) {
    const demBounds = parseDemBounds(terrain && terrain.bounds_wgs84);
    const world = osmWorldBounds(demBounds) || demBounds;
    if (!world || !scene) return;
    const z = chooseZoomFitting(world, OSM_MAX_BASE_TILES);
    const range = tileRangeForBounds(world, z);
    const sig = ['base', range.z, range.x0, range.y0, range.x1, range.y1].join(':');
    if (osmBaseMesh && sig === osmBaseSig) return;
    if (osmBasePromise) return osmBasePromise;
    const terrainRef = terrain;
    osmBasePromise = loadOsmTileGroup(terrain, range, OSM_GROUND_Y, function () {
      return !scene || lastTerrain !== terrainRef;
    }).then(function (group) {
      if (!group || !scene || lastTerrain !== terrainRef) {
        if (group) disposeOsmGroup(group, false);
        return;
      }
      const prev = osmBaseMesh;
      osmBaseMesh = group;
      osmBaseMesh.name = 'osmBase';
      scene.add(osmBaseMesh);
      disposeOsmGroup(prev, false);
      osmBaseSig = sig;
      osmBaseZ = range.z;
      osmMesh = osmBaseMesh;
    }).finally(function () {
      osmBasePromise = null;
    });
    return osmBasePromise;
  }

  /**
   * Lớp nền phủ cả DEM + vùng quanh (như 2D).
   * Lớp chi tiết theo viewport khi zoom gần.
   */
  async function refreshOsmBasemap(terrain, force) {
    if (!terrain || !scene || !camera) return;
    await ensureOsmBase(terrain);
    const view = visibleLonLatBounds(terrain);
    if (!view) return;
    let z = zoomForView(view);
    let range = tileRangeForBounds(view, z);
    while (z > 5 && range.count > OSM_MAX_DETAIL_TILES) {
      z -= 1;
      range = tileRangeForBounds(view, z);
    }
    if (z <= osmBaseZ) {
      if (osmDetailMesh) {
        disposeOsmGroup(osmDetailMesh, false);
        osmDetailMesh = null;
      }
      osmLastSig = 'base';
      return;
    }
    const sig = [range.z, range.x0, range.y0, range.x1, range.y1].join(':');
    if (!force && sig === osmLastSig) return;
    const gen = ++osmGen;
    osmLoading = true;
    const group = await loadOsmTileGroup(terrain, range, OSM_DETAIL_Y, function () {
      return gen !== osmGen || !scene;
    });
    if (gen !== osmGen || !scene) {
      if (group) disposeOsmGroup(group, false);
      osmLoading = false;
      return;
    }
    const prev = osmDetailMesh;
    osmDetailMesh = group;
    if (osmDetailMesh) {
      osmDetailMesh.name = 'osmDetail';
      scene.add(osmDetailMesh);
    }
    disposeOsmGroup(prev, false);
    osmLastSig = sig;
    osmLoading = false;
  }

  function buildOsmBasemap(terrain) {
    return refreshOsmBasemap(terrain, true);
  }

  async function buildTerrainMesh(terrain) {
    const width = terrain.width;
    const height = terrain.height;
    const elev = decodeElevations(terrain.elevations_b64, width, height);
    const mask = decodeMask(terrain.mask_b64, width, height);
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const zrange = Math.max(zmax - zmin, 1e-6);

    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const geometry = new THREE.PlaneGeometry(worldW, worldH, width - 1, height - 1);
    geometry.rotateX(-Math.PI / 2);

    const positions = geometry.attributes.position;
    const colors = new Float32Array(positions.count * 3);
    const elevScale = elevationScaleForTerrain(terrain, worldW);

    const valid = new Uint8Array(positions.count);
    // PlaneGeometry (r128): iy=0 → +Y → sau rotateX(-π/2) thành −Z (Bắc).
    // GeoTIFF north-up: row 0 = Bắc → map thẳng iy == rasterRow (không đảo N-S).
    for (let iy = 0; iy < height; iy++) {
      for (let ix = 0; ix < width; ix++) {
        const rasterRow = iy;
        const idx = iy * width + ix;
        const rasterIdx = rasterRow * width + ix;
        const isValid = mask[rasterIdx] ? 1 : 0;
        valid[idx] = isValid;
        const z = elev[rasterIdx];
        const t = (z - zmin) / zrange;
        const y = terrain.is_dem && isValid ? (z - zmin) * elevScale : 0.12;
        positions.setY(idx, y);

        const col = heightColorRgb(t);
        colors[idx * 3] = col.r;
        colors[idx * 3 + 1] = col.g;
        colors[idx * 3 + 2] = col.b;
      }
    }
    positions.needsUpdate = true;
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const indices = [];
    for (let iy = 0; iy < height - 1; iy++) {
      for (let ix = 0; ix < width - 1; ix++) {
        const a = iy * width + ix;
        const b = a + 1;
        const c = a + width;
        const d = c + 1;
        if (valid[a] && valid[b] && valid[c] && valid[d]) {
          indices.push(a, c, b);
          indices.push(b, c, d);
        }
      }
    }
    if (indices.length) {
      geometry.setIndex(indices);
    } else {
      geometry.setIndex([]);
    }
    geometry.computeVertexNormals();

    let material;
    if (terrain.is_dem) {
      material = new THREE.MeshStandardMaterial({
        vertexColors: true,
        roughness: 0.88,
        metalness: 0.04,
        side: THREE.DoubleSide
      });
    } else {
      try {
        const tex = await loadTexture(floodUrl(terrain.texture_url));
        material = new THREE.MeshStandardMaterial({
          map: tex,
          roughness: 0.95,
          metalness: 0.05,
          side: THREE.DoubleSide,
          transparent: true,
          alphaTest: 0.05
        });
      } catch (e) {
        material = new THREE.MeshStandardMaterial({
          vertexColors: true,
          roughness: 0.92,
          metalness: 0.05,
          side: THREE.DoubleSide
        });
      }
    }

    return new THREE.Mesh(geometry, material);
  }

  /** Mục tiêu ~50 đường contour theo (zmax − zmin); cứ 5 đường → 1 nét liền + nhãn. */
  const CONTOUR_TARGET_COUNT = 50;
  const CONTOUR_MAJOR_EVERY = 5;
  /** Lưới tối đa khi trích contour — khớp độ phân giải mesh DEM (~256). */
  const CONTOUR_GRID_MAX = 256;
  /** Nét liền (major) dày hơn nét đứt */
  const CONTOUR_LINE_WIDTH = 2.5;
  /** Nét đứt: ngắn + mỏng hơn nét liền */
  const CONTOUR_DASH_LINE_WIDTH = 0.55;
  const CONTOUR_DASH_SIZE = 0.7;
  const CONTOUR_GAP_SIZE = 0.55;

  /** Chọn khoảng cao “đẹp” để số đường gần targetCount. */
  function niceContourStep(range, targetCount) {
    const rough = range / Math.max(targetCount, 1);
    if (!(rough > 0) || !Number.isFinite(rough)) return 1;
    const exp = Math.floor(Math.log10(rough));
    const pow10 = Math.pow(10, exp);
    const candidates = [1, 2, 2.5, 5, 10].map(function (m) {
      return m * pow10;
    });
    candidates.push(0.5 * pow10, 20 * pow10);
    let best = candidates[0];
    let bestScore = Infinity;
    candidates.forEach(function (c) {
      if (!(c > 0)) return;
      const n = range / c;
      const score = Math.abs(n - targetCount);
      if (score < bestScore) {
        bestScore = score;
        best = c;
      }
    });
    return best;
  }

  function contourLevels(zmin, zmax) {
    const range = Math.max(zmax - zmin, 1e-6);
    const step = niceContourStep(range, CONTOUR_TARGET_COUNT);
    const majorStep = step * CONTOUR_MAJOR_EVERY;
    const start = Math.ceil(zmin / step) * step;
    const levels = [];
    const maxLevels = CONTOUR_TARGET_COUNT * 2;
    for (let z = start; z <= zmax + step * 1e-9; z += step) {
      if (z < zmin + 1e-6 || z > zmax - 1e-6) continue;
      levels.push(Math.round(z * 1e6) / 1e6);
      if (levels.length >= maxLevels) break;
    }
    return {
      levels: levels,
      step: step,
      labelStep: majorStep,
      solidStep: majorStep
    };
  }

  function isContourLabelLevel(level, labelStep) {
    if (!(labelStep > 0)) return false;
    const q = level / labelStep;
    return Math.abs(q - Math.round(q)) < 1e-6;
  }

  /** Downsample DEM để trích contour nhanh hơn. */
  function downsampleContourGrid(elev, mask, width, height, maxDim) {
    const scale = Math.max(width, height) / Math.max(maxDim, 2);
    if (scale <= 1.01) {
      return { elev: elev, mask: mask, width: width, height: height };
    }
    const nw = Math.max(2, Math.round(width / scale));
    const nh = Math.max(2, Math.round(height / scale));
    const ne = new Float32Array(nw * nh);
    const nm = new Uint8Array(nw * nh);
    const yScale = (height - 1) / Math.max(nh - 1, 1);
    const xScale = (width - 1) / Math.max(nw - 1, 1);
    for (let r = 0; r < nh; r++) {
      const sr = Math.min(height - 1, Math.round(r * yScale));
      for (let c = 0; c < nw; c++) {
        const sc = Math.min(width - 1, Math.round(c * xScale));
        const si = sr * width + sc;
        const di = r * nw + c;
        ne[di] = elev[si];
        nm[di] = mask[si];
      }
    }
    return { elev: ne, mask: nm, width: nw, height: nh };
  }

  /**
   * Marching squares nhanh: chỉ trả đoạn cạnh (không assemble polyline).
   * Mỗi đoạn: r0,c0,r1,c1 trong mảng phẳng segs.
   */
  function findContourSegments(data, width, height, level, mask) {
    const segs = [];
    const h1 = height - 1;
    const w1 = width - 1;

    for (let r = 0; r < h1; r++) {
      const row0 = r * width;
      const row1 = (r + 1) * width;
      for (let c = 0; c < w1; c++) {
        const i00 = row0 + c;
        const i10 = row0 + c + 1;
        const i01 = row1 + c;
        const i11 = row1 + c + 1;
        if (!mask[i00] || !mask[i10] || !mask[i01] || !mask[i11]) continue;

        const v00 = data[i00];
        const v10 = data[i10];
        const v01 = data[i01];
        const v11 = data[i11];
        if (
          !Number.isFinite(v00) ||
          !Number.isFinite(v10) ||
          !Number.isFinite(v01) ||
          !Number.isFinite(v11)
        ) {
          continue;
        }

        let idx = 0;
        if (v00 > level) idx |= 1;
        if (v10 > level) idx |= 2;
        if (v11 > level) idx |= 4;
        if (v01 > level) idx |= 8;
        if (idx === 0 || idx === 15) continue;

        // Điểm trên cạnh: top=0, right=1, bottom=2, left=3
        let tr, tc, rr, rc, br, bc, lr, lc;
        {
          let t = v10 === v00 ? 0.5 : (level - v00) / (v10 - v00);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          tr = r; tc = c + t;
        }
        {
          let t = v11 === v10 ? 0.5 : (level - v10) / (v11 - v10);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          rr = r + t; rc = c + 1;
        }
        {
          let t = v11 === v01 ? 0.5 : (level - v01) / (v11 - v01);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          br = r + 1; bc = c + t;
        }
        {
          let t = v01 === v00 ? 0.5 : (level - v00) / (v01 - v00);
          t = t < 0 ? 0 : t > 1 ? 1 : t;
          lr = r + t; lc = c;
        }

        switch (idx) {
          case 1: case 14: segs.push(lr, lc, tr, tc); break;
          case 2: case 13: segs.push(tr, tc, rr, rc); break;
          case 3: case 12: segs.push(lr, lc, rr, rc); break;
          case 4: case 11: segs.push(rr, rc, br, bc); break;
          case 6: case 9: segs.push(tr, tc, br, bc); break;
          case 7: case 8: segs.push(lr, lc, br, bc); break;
          case 5:
            segs.push(tr, tc, lr, lc, rr, rc, br, bc);
            break;
          case 10:
            segs.push(tr, tc, rr, rc, lr, lc, br, bc);
            break;
          default:
            break;
        }
      }
    }
    return segs;
  }

  /** Nội suy bilinear cao độ trên lưới DEM đầy đủ (cùng công thức mesh). */
  function sampleElevBilinear(elev, mask, width, height, row, col) {
    const r0 = Math.max(0, Math.min(height - 1, Math.floor(row)));
    const c0 = Math.max(0, Math.min(width - 1, Math.floor(col)));
    const r1 = Math.min(height - 1, r0 + 1);
    const c1 = Math.min(width - 1, c0 + 1);
    const fr = Math.max(0, Math.min(1, row - r0));
    const fc = Math.max(0, Math.min(1, col - c0));

    const i00 = r0 * width + c0;
    const i10 = r0 * width + c1;
    const i01 = r1 * width + c0;
    const i11 = r1 * width + c1;

    const v00 = elev[i00];
    const v10 = elev[i10];
    const v01 = elev[i01];
    const v11 = elev[i11];
    const m00 = mask[i00];
    const m10 = mask[i10];
    const m01 = mask[i01];
    const m11 = mask[i11];

    let sum = 0;
    let wsum = 0;
    function accum(v, m, w) {
      if (!m || !Number.isFinite(v) || !(w > 0)) return;
      sum += v * w;
      wsum += w;
    }
    accum(v00, m00, (1 - fr) * (1 - fc));
    accum(v10, m10, (1 - fr) * fc);
    accum(v01, m01, fr * (1 - fc));
    accum(v11, m11, fr * fc);
    if (wsum > 1e-9) return sum / wsum;
    if (m00 && Number.isFinite(v00)) return v00;
    return NaN;
  }

  /**
   * Raster (row,col) lưới contour → world.
   * XZ theo footprint mesh; Y draping theo cao độ DEM tại điểm đó (bám sát mặt mesh).
   */
  function contourRasterToWorld(
    row, col, gridW, gridH, fullW, fullH,
    worldW, worldH, zmin, elevScale, level,
    elevFull, maskFull
  ) {
    const colFull = col * (fullW - 1) / Math.max(gridW - 1, 1);
    const rowFull = row * (fullH - 1) / Math.max(gridH - 1, 1);
    const dx = Math.max(fullW - 1, 1);
    const dy = Math.max(fullH - 1, 1);
    // row 0 = Bắc = −Z; khớp mesh + OSM (EPSG:3857)
    const x = -worldW / 2 + (colFull / dx) * worldW;
    const z = -worldH / 2 + (rowFull / dy) * worldH;

    let elevVal = sampleElevBilinear(elevFull, maskFull, fullW, fullH, rowFull, colFull);
    if (!Number.isFinite(elevVal)) elevVal = level;
    // Nhấc rất nhẹ khỏi mặt để tránh z-fighting, vẫn bám mesh
    const lift = Math.max(worldW, worldH) * 0.00012 + 0.008;
    const y = (elevVal - zmin) * elevScale + lift;
    return { x: x, y: y, z: z };
  }

  /** Cắt đoạn thành nét đứt, ghi vào mảng LineSegments. */
  function appendDashedWorldSegment(out, ax, ay, az, bx, by, bz, dashSize, gapSize) {
    const dx = bx - ax;
    const dy = by - ay;
    const dz = bz - az;
    const len = Math.sqrt(dx * dx + dy * dy + dz * dz);
    if (!(len > 1e-8)) return;
    const inv = 1 / len;
    const ux = dx * inv;
    const uy = dy * inv;
    const uz = dz * inv;
    let t = 0;
    let drawing = true;
    while (t < len) {
      const span = drawing ? dashSize : gapSize;
      const t1 = t + span < len ? t + span : len;
      if (drawing && t1 > t) {
        out.push(
          ax + ux * t, ay + uy * t, az + uz * t,
          ax + ux * t1, ay + uy * t1, az + uz * t1
        );
      }
      t = t1;
      drawing = !drawing;
    }
  }

  function makeElevLabelSprite(text) {
    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 48;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = 'bold 28px Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.strokeText(text, 64, 24);
    ctx.fillStyle = '#e30613';
    ctx.fillText(text, 64, 24);

    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    const mat = new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      // Không depth-test → DEM không che chữ; vị trí vẫn bám contour
      depthTest: false,
      depthWrite: false,
      sizeAttenuation: true
    });
    const sprite = new THREE.Sprite(mat);
    sprite.scale.set(4.5, 1.7, 1);
    sprite.center.set(0.5, 0.5);
    sprite.renderOrder = 999;
    sprite.userData.labelText = text;
    return sprite;
  }

  function formatContourLabel(z) {
    return String(Math.round(z));
  }

  function clearContourLabels() {
    if (contourLabelLayer) {
      disposeObject3D(contourLabelLayer);
      contourLabelLayer = null;
    }
    contourLabelSprites = [];
    contourLabelData = [];
    labelsNeedUpdate = true;
  }

  function setLabelSpriteText(sprite, text) {
    if (sprite.userData.labelText === text) return;
    const canvas = document.createElement('canvas');
    canvas.width = 128;
    canvas.height = 48;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = 'bold 28px Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.strokeText(text, 64, 24);
    ctx.fillStyle = '#e30613';
    ctx.fillText(text, 64, 24);
    const tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    if (sprite.material && sprite.material.map) sprite.material.map.dispose();
    if (sprite.material) {
      sprite.material.map = tex;
      sprite.material.needsUpdate = true;
    }
    sprite.userData.labelText = text;
  }

  let _lastLabelCamKey = '';

  /**
   * Chỉ hiện nhãn nằm trong khung nhìn hiện tại (zoom/pan).
   * Giới hạn số nhãn + khoảng cách màn hình để tránh chồng.
   */
  function updateContourLabelsInView() {
    if (!camera || !scene || !contourLabelData.length) return;

    // Chỉ tính lại khi camera đổi (hoặc lần đầu)
    const camKey =
      camera.position.x.toFixed(2) +
      ',' +
      camera.position.y.toFixed(2) +
      ',' +
      camera.position.z.toFixed(2) +
      ',' +
      (controls ? controls.target.x.toFixed(2) : '0') +
      ',' +
      (controls ? controls.target.y.toFixed(2) : '0') +
      ',' +
      (controls ? controls.target.z.toFixed(2) : '0');
    if (!labelsNeedUpdate && camKey === _lastLabelCamKey) return;
    _lastLabelCamKey = camKey;

    camera.updateMatrixWorld();
    _labelProj.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    _labelFrustum.setFromProjectionMatrix(_labelProj);

    const inView = [];
    for (let i = 0; i < contourLabelData.length; i++) {
      const c = contourLabelData[i];
      _labelWorld.set(c.x, c.y, c.z);
      if (!_labelFrustum.containsPoint(_labelWorld)) continue;
      _labelNdc.copy(_labelWorld).project(camera);
      if (_labelNdc.z < -1 || _labelNdc.z > 1) continue;
      if (_labelNdc.x < -1.05 || _labelNdc.x > 1.05) continue;
      if (_labelNdc.y < -1.05 || _labelNdc.y > 1.05) continue;
      inView.push({
        c: c,
        nx: _labelNdc.x,
        ny: _labelNdc.y
      });
    }

    inView.sort(function (a, b) {
      return a.nx * a.nx + a.ny * a.ny - (b.nx * b.nx + b.ny * b.ny);
    });

    const picked = [];
    const minD2 = CONTOUR_LABEL_MIN_NDC_DIST * CONTOUR_LABEL_MIN_NDC_DIST;
    for (let i = 0; i < inView.length && picked.length < CONTOUR_LABEL_MAX_VISIBLE; i++) {
      const v = inView[i];
      let ok = true;
      for (let j = 0; j < picked.length; j++) {
        const p = picked[j];
        const dx = v.nx - p.nx;
        const dy = v.ny - p.ny;
        if (dx * dx + dy * dy < minD2) {
          ok = false;
          break;
        }
      }
      if (ok) picked.push(v);
    }

    if (!contourLabelLayer) {
      contourLabelLayer = new THREE.Group();
      contourLabelLayer.name = 'contour-labels';
      scene.add(contourLabelLayer);
    }

    while (contourLabelSprites.length < picked.length) {
      const spr = makeElevLabelSprite('0');
      spr.visible = false;
      contourLabelLayer.add(spr);
      contourLabelSprites.push(spr);
    }

    for (let i = 0; i < contourLabelSprites.length; i++) {
      const spr = contourLabelSprites[i];
      if (i >= picked.length) {
        spr.visible = false;
        continue;
      }
      const item = picked[i];
      setLabelSpriteText(spr, formatContourLabel(item.c.level));
      spr.position.set(item.c.x, item.c.y, item.c.z);
      // Cỡ chữ theo khoảng cách; giữ nhỏ vừa để bám đường contour
      const dist = camera.position.distanceTo(spr.position);
      const s = Math.max(1.8, Math.min(5.5, dist * 0.028));
      spr.scale.set(s, s * 0.38, 1);
      spr.visible = true;
      spr.renderOrder = 999;
    }

    labelsNeedUpdate = false;
  }

  function buildContourGroup(terrain) {
    if (!terrain || !terrain.is_dem) return null;

    const fullW = terrain.width;
    const fullH = terrain.height;
    const elevFull = decodeElevations(terrain.elevations_b64, fullW, fullH);
    const maskFull = decodeMask(terrain.mask_b64, fullW, fullH);
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const elevScale = elevationScaleForTerrain(terrain, worldW);

    const info = contourLevels(zmin, zmax);
    if (!info.levels.length) return null;

    const grid = downsampleContourGrid(
      elevFull, maskFull, fullW, fullH, CONTOUR_GRID_MAX
    );
    const elev = grid.elev;
    const mask = grid.mask;
    const width = grid.width;
    const height = grid.height;

    const group = new THREE.Group();
    group.name = 'contours';

    const solidPositions = [];
    const dashedPositions = [];
    const labelCandidates = [];
    /** Mật hơn để khi zoom gần vẫn còn ứng viên nhãn trong vùng. */
    const labelTrack = Object.create(null);
    const LABEL_SPACING = 8;

    const levels = info.levels;
    for (let li = 0; li < levels.length; li++) {
      const level = levels[li];
      const isSolid = isContourLabelLevel(level, info.solidStep);
      const segs = findContourSegments(elev, width, height, level, mask);
      if (!segs.length) continue;

      for (let i = 0; i + 3 < segs.length; i += 4) {
        const a = contourRasterToWorld(
          segs[i], segs[i + 1], width, height, fullW, fullH,
          worldW, worldH, zmin, elevScale, level,
          elevFull, maskFull
        );
        const b = contourRasterToWorld(
          segs[i + 2], segs[i + 3], width, height, fullW, fullH,
          worldW, worldH, zmin, elevScale, level,
          elevFull, maskFull
        );
        if (isSolid) {
          solidPositions.push(a.x, a.y, a.z, b.x, b.y, b.z);
          const segLen = Math.hypot(b.x - a.x, b.z - a.z);
          let track = labelTrack[level];
          if (!track) {
            track = labelTrack[level] = { pathLen: 0, lastAt: -1e9 };
          }
          track.pathLen += segLen;
          if (track.pathLen - track.lastAt >= LABEL_SPACING) {
            labelCandidates.push({
              level: level,
              x: (a.x + b.x) * 0.5,
              y: (a.y + b.y) * 0.5 + 0.04,
              z: (a.z + b.z) * 0.5
            });
            track.lastAt = track.pathLen;
          }
        } else {
          appendDashedWorldSegment(
            dashedPositions,
            a.x, a.y, a.z, b.x, b.y, b.z,
            CONTOUR_DASH_SIZE, CONTOUR_GAP_SIZE
          );
        }
      }
    }

    if (!solidPositions.length && !dashedPositions.length) return null;

    if (solidPositions.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute(
        'position',
        new THREE.Float32BufferAttribute(new Float32Array(solidPositions), 3)
      );
      const mat = new THREE.LineBasicMaterial({
        color: 0x111111,
        linewidth: CONTOUR_LINE_WIDTH,
        depthTest: true,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2
      });
      const lines = new THREE.LineSegments(geo, mat);
      lines.renderOrder = 10;
      group.add(lines);
    }

    if (dashedPositions.length) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute(
        'position',
        new THREE.Float32BufferAttribute(new Float32Array(dashedPositions), 3)
      );
      const mat = new THREE.LineBasicMaterial({
        color: 0xe6c200,
        linewidth: CONTOUR_DASH_LINE_WIDTH,
        transparent: true,
        opacity: 0.9,
        depthTest: true,
        depthWrite: false,
        polygonOffset: true,
        polygonOffsetFactor: -2,
        polygonOffsetUnits: -2
      });
      const lines = new THREE.LineSegments(geo, mat);
      lines.renderOrder = 10;
      group.add(lines);
    }

    // Nhãn không gắn sẵn — cập nhật theo viewport khi zoom/pan
    contourLabelData = labelCandidates;
    labelsNeedUpdate = true;

    return group;
  }

  function syncContours() {
    disposeObject3D(contourGroup);
    contourGroup = null;
    clearContourLabels();
    if (!contoursVisible || !lastTerrain || !scene) return;
    contourGroup = buildContourGroup(lastTerrain);
    if (contourGroup) scene.add(contourGroup);
    updateContourLabelsInView();
  }

  function setContoursVisible(visible) {
    contoursVisible = !!visible;
    syncContours();
  }

  async function rebuildFromLast() {
    if (!lastTerrain || !scene) return;
    disposeObject3D(currentMesh);
    currentMesh = await buildTerrainMesh(lastTerrain);
    scene.add(currentMesh);
    syncContours();
    if (global.Flow3D && typeof global.Flow3D.redrawOverlay === 'function') {
      global.Flow3D.redrawOverlay();
    }
    if (global.FlowRun && typeof global.FlowRun.redrawOverlay === 'function') {
      global.FlowRun.redrawOverlay();
    }
    if (global.Flood3D && typeof global.Flood3D.redrawOverlay === 'function') {
      global.Flood3D.redrawOverlay();
    }
  }

  async function loadDem3D(fileId, maxSize) {
    maxSize = maxSize || 256;
    if (!fileId) {
      fileId = pickFileIdFor3D();
    }
    if (!fileId) {
      alert('Không tìm thấy DEM.');
      return;
    }

    const exagInput = $('demExaggeration');
    if (exagInput) {
      exaggeration = Number(exagInput.value) || 0.02;
    }

    initViewer();

    const res = await fetch(floodUrl('/api/geotiff/dem/file/' + fileId + '/terrain?max_size=' + maxSize));
    const data = await res.json();
    if (!data.success) {
      throw new Error(data.message || 'Không tải được terrain DEM');
    }

    lastTerrain = data;

    disposeObject3D(currentMesh);
    currentMesh = await buildTerrainMesh(data);
    scene.add(currentMesh);

    // Không tự bật contour khi load terrain
    syncContours();

    fitCameraToObjects(currentMesh, null);
    updateElevationLegend(data);

    osmLastSig = '';
    refreshOsmBasemap(data, true).catch(function (e) {
      console.warn('Không tải được nền basemap:', e);
    });

    if (global.Flow3D && typeof global.Flow3D.onTerrainReady === 'function') {
      global.Flow3D.onTerrainReady();
    }
    if (global.FlowRun && typeof global.FlowRun.onTerrainReady === 'function') {
      global.FlowRun.onTerrainReady();
    }
    if (global.Flood3D && typeof global.Flood3D.onTerrainReady === 'function') {
      global.Flood3D.onTerrainReady();
    }
  }

  function fitCameraToObjects(demObj, osmObj) {
    if (!controls || !camera) return;
    const focus = osmObj || demObj;
    if (!focus) return;
    const box = new THREE.Box3().setFromObject(focus);
    if (demObj && osmObj) box.expandByObject(demObj);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    controls.target.copy(center);
    const fitDist = Math.max(size.x, size.z, size.y, 1) * 1.15;
    camera.position.set(
      center.x + fitDist * 0.85,
      center.y + fitDist * 0.55,
      center.z + fitDist * 0.85
    );
    controls.update();
    // Zoom out tối đa ~mức 4; zoom in tới DEM3D_MAX_ZOOM (22)
    controls.maxDistance = fitDist * Math.pow(2, DEM3D_FIT_ZOOM - 4);
    controls.minDistance = Math.max(
      0.02,
      fitDist * Math.pow(2, DEM3D_FIT_ZOOM - DEM3D_MAX_ZOOM)
    );
    camera.near = Math.max(0.005, controls.minDistance * 0.05);
    camera.far = Math.max(100000, controls.maxDistance * 20);
    camera.updateProjectionMatrix();
    osmFitDist = camera.position.distanceTo(controls.target);
  }

  async function setExaggeration(value) {
    exaggeration = Number(value) || 0.02;
    if (lastTerrain) {
      await rebuildFromLast();
    }
  }

  function formatElev(v) {
    if (!Number.isFinite(v)) return '—';
    const abs = Math.abs(v);
    if (abs >= 1000) return v.toFixed(0);
    if (abs >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  function updateElevationLegend(terrain) {
    const legend = $('dem3dLegend');
    if (!legend) return;
    if (!terrain || !terrain.is_dem) {
      legend.hidden = true;
      return;
    }
    const zmin = terrain.zmin;
    const zmax = terrain.zmax;
    const zmid = (zmin + zmax) / 2;
    const elMax = $('dem3dLegendMax');
    const elMid = $('dem3dLegendMid');
    const elMin = $('dem3dLegendMin');
    if (elMax) elMax.textContent = formatElev(zmax);
    if (elMid) elMid.textContent = formatElev(zmid);
    if (elMin) elMin.textContent = formatElev(zmin);
    legend.hidden = false;
  }

  function show3DMode(show) {
    const mapEl = $('map');
    const view3d = $('view3d');
    const demControls = $('dem3dControls');
    const legend = $('dem3dLegend');
    if (mapEl) mapEl.style.display = show ? 'none' : '';
    if (view3d) view3d.hidden = !show;
    if (demControls) demControls.hidden = !show;
    if (!show) {
      if (legend) legend.hidden = true;
      if (global.Flow3D && typeof global.Flow3D.teardown === 'function') {
        global.Flow3D.teardown();
      }
      if (global.FlowRun && typeof global.FlowRun.teardown === 'function') {
        global.FlowRun.teardown();
      }
      if (global.Flood3D && typeof global.Flood3D.teardown === 'function') {
        global.Flood3D.teardown();
      }
      destroyViewer();
      if (typeof global.resizeLeafletMap === 'function') {
        setTimeout(function () { global.resizeLeafletMap(); }, 40);
      }
    }
  }

  function webMercatorToLonLat(x, y) {
    const lon = (x / WEB_MERCATOR_MAX) * 180;
    const lat = (Math.atan(Math.sinh(y / WEB_MERCATOR_MAX * Math.PI)) * 180) / Math.PI;
    return { lon: lon, lat: lat };
  }

  function worldToLonLat(wx, wz, terrain) {
    const sizes = demWorldSize(terrain);
    const worldW = sizes.worldW;
    const worldH = sizes.worldH;
    const native = parseNativeBounds(terrain && terrain.bounds);
    const wgs = parseDemBounds(terrain && terrain.bounds_wgs84);

    function fromProj(px, py, proj, mercator) {
      if (mercator) return webMercatorToLonLat(px, py);
      return { lon: px, lat: py };
    }

    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const px = native.xCenter + (wx / worldW) * native.xSpan;
      const py = native.yCenter - (wz / worldH) * native.ySpan;
      return fromProj(px, py, native, true);
    }
    if (native && isGeographicCrs(terrain.crs)) {
      const lon = native.xCenter + (wx / worldW) * native.xSpan;
      const lat = native.yCenter - (wz / worldH) * native.ySpan;
      return { lon: lon, lat: lat };
    }
    if (wgs && !isGeographicCrs(terrain && terrain.crs)) {
      const sw = lonLatToWebMercator(wgs.west, wgs.south);
      const ne = lonLatToWebMercator(wgs.east, wgs.north);
      const xSpan = Math.max(Math.abs(ne.x - sw.x), 1e-9);
      const ySpan = Math.max(Math.abs(ne.y - sw.y), 1e-9);
      const xCenter = (sw.x + ne.x) / 2;
      const yCenter = (sw.y + ne.y) / 2;
      const px = xCenter + (wx / worldW) * xSpan;
      const py = yCenter - (wz / worldH) * ySpan;
      return webMercatorToLonLat(px, py);
    }
    if (!wgs) return { lon: 0, lat: 0 };
    return {
      lon: wgs.lonCenter + (wx / worldW) * wgs.lonSpan,
      lat: wgs.latCenter - (wz / worldH) * wgs.latSpan
    };
  }

  function lonLatToRasterColRow(lon, lat, terrain) {
    if (!terrain) return null;
    const width = terrain.width;
    const height = terrain.height;
    if (!(width > 1) || !(height > 1)) return null;
    const native = parseNativeBounds(terrain.bounds);
    const wgs = parseDemBounds(terrain.bounds_wgs84);
    let col;
    let row;
    if (native && isWebMercatorCrs(terrain.crs, native)) {
      const m = lonLatToWebMercator(lon, lat);
      col = ((m.x - native.left) / native.xSpan) * (width - 1);
      row = ((native.top - m.y) / native.ySpan) * (height - 1);
    } else if (wgs) {
      col = ((lon - wgs.west) / wgs.lonSpan) * (width - 1);
      row = ((wgs.north - lat) / wgs.latSpan) * (height - 1);
    } else {
      return null;
    }
    if (!isFinite(col) || !isFinite(row)) return null;
    if (col < -0.5 || row < -0.5 || col > width - 0.5 || row > height - 0.5) {
      return { col: col, row: row, width: width, height: height, inBounds: false };
    }
    return { col: col, row: row, width: width, height: height, inBounds: true };
  }

  function pointInDemMask(lon, lat, terrain) {
    if (!terrain) return false;
    const lo = Number(lon);
    const la = Number(lat);
    if (!isFinite(lo) || !isFinite(la)) return false;
    const cr = lonLatToRasterColRow(lo, la, terrain);
    if (!cr || !cr.inBounds) return false;
    const width = cr.width;
    const height = cr.height;
    const mask = decodeMask(terrain.mask_b64, width, height);
    const c0 = Math.max(0, Math.min(width - 1, Math.floor(cr.col)));
    const r0 = Math.max(0, Math.min(height - 1, Math.floor(cr.row)));
    return !!mask[r0 * width + c0];
  }

  function sampleElevation(lon, lat, terrain) {
    if (!terrain) return NaN;
    const width = terrain.width;
    const height = terrain.height;
    const elev = decodeElevations(terrain.elevations_b64, width, height);
    const mask = decodeMask(terrain.mask_b64, width, height);
    const cr = lonLatToRasterColRow(lon, lat, terrain);
    if (!cr) return Number(terrain.zmin) || 0;
    if (!cr.inBounds) return NaN;
    let col = Math.max(0, Math.min(width - 1, cr.col));
    let row = Math.max(0, Math.min(height - 1, cr.row));
    const c0 = Math.floor(col);
    const r0 = Math.floor(row);
    const c1 = Math.min(width - 1, c0 + 1);
    const r1 = Math.min(height - 1, r0 + 1);
    const idx = function (r, c) { return r * width + c; };
    if (!mask[idx(r0, c0)]) return NaN;
    const z00 = elev[idx(r0, c0)];
    const z10 = elev[idx(r0, c1)];
    const z01 = elev[idx(r1, c0)];
    const z11 = elev[idx(r1, c1)];
    const u = col - c0;
    const v = row - r0;
    return z00 * (1 - u) * (1 - v) + z10 * u * (1 - v) + z01 * (1 - u) * v + z11 * u * v;
  }

  function elevToWorldY(elev, terrain) {
    if (!terrain) return 0;
    const sizes = demWorldSize(terrain);
    const zmin = Number(terrain.zmin);
    const scale = elevationScaleForTerrain(terrain, sizes.worldW);
    return (Number(elev) - zmin) * scale;
  }

  function metersToWorldXZ(meters, terrain) {
    if (!terrain) return Number(meters) || 0;
    const m = Number(meters);
    if (!isFinite(m)) return 0;
    const sizes = demWorldSize(terrain);
    const native = parseNativeBounds(terrain.bounds);
    if (native && isWebMercatorCrs(terrain.crs, native)) {
      return m * (sizes.worldW / native.xSpan);
    }
    const wgs = parseDemBounds(terrain.bounds_wgs84);
    if (wgs) {
      const midLat = wgs.latCenter;
      const mPerDegLon = 111320.0 * Math.cos((midLat * Math.PI) / 180);
      const lonSpanM = Math.max(Math.abs(wgs.lonSpan) * Math.max(mPerDegLon, 1e-6), 1e-6);
      return m * (sizes.worldW / lonSpanM);
    }
    if (native) {
      return m * (sizes.worldW / native.xSpan);
    }
    return m * 0.01;
  }

  function metersToWorldY(meters, terrain) {
    if (!terrain) return Number(meters) || 0;
    const m = Number(meters);
    if (!isFinite(m)) return 0;
    const sizes = demWorldSize(terrain);
    return m * elevationScaleForTerrain(terrain, sizes.worldW);
  }

  function pickTerrain(clientX, clientY) {
    if (!renderer || !camera || !currentMesh || !lastTerrain) return null;
    const rect = renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObject(currentMesh, true);
    if (!hits.length) return null;
    const p = hits[0].point;
    const ll = worldToLonLat(p.x, p.z, lastTerrain);
    const elev = sampleElevation(ll.lon, ll.lat, lastTerrain);
    return {
      lon: ll.lon,
      lat: ll.lat,
      x: p.x,
      y: p.y,
      z: p.z,
      elev: elev
    };
  }

  global.Dem3D = {
    loadDem3D: loadDem3D,
    show3DMode: show3DMode,
    destroyViewer: destroyViewer,
    setExaggeration: setExaggeration,
    setContoursVisible: setContoursVisible,
    pickFileIdFor3D: pickFileIdFor3D,
    getContext: function () {
      return {
        scene: scene,
        camera: camera,
        renderer: renderer,
        controls: controls,
        mesh: currentMesh,
        terrain: lastTerrain,
        exaggeration: exaggeration
      };
    },
    lonLatToWorld: function (lon, lat) {
      if (!lastTerrain) return { x: 0, z: 0 };
      const sizes = demWorldSize(lastTerrain);
      return lonLatToWorld(lon, lat, lastTerrain, sizes.worldW, sizes.worldH);
    },
    worldToLonLat: function (wx, wz) {
      return lastTerrain ? worldToLonLat(wx, wz, lastTerrain) : { lon: 0, lat: 0 };
    },
    sampleElevation: function (lon, lat) {
      return sampleElevation(lon, lat, lastTerrain);
    },
    elevToWorldY: function (elev) {
      return elevToWorldY(elev, lastTerrain);
    },
    metersToWorldXZ: function (meters) {
      return metersToWorldXZ(meters, lastTerrain);
    },
    metersToWorldY: function (meters) {
      return metersToWorldY(meters, lastTerrain);
    },
    getLonLatBounds: function () {
      if (!lastTerrain) return null;
      return parseDemBounds(lastTerrain.bounds_wgs84) || parseDemBounds(lastTerrain.bounds);
    },
    pointInDem: function (lon, lat) {
      if (!lastTerrain) return false;
      return pointInDemMask(lon, lat, lastTerrain);
    },
    pointInDemMask: function (lon, lat) {
      if (!lastTerrain) return false;
      return pointInDemMask(lon, lat, lastTerrain);
    },
    rasterUVToWorld: function (u, v) {
      if (!lastTerrain) return { x: 0, z: 0 };
      const sizes = demWorldSize(lastTerrain);
      return {
        x: -sizes.worldW / 2 + u * sizes.worldW,
        z: -sizes.worldH / 2 + v * sizes.worldH
      };
    },
    pickTerrain: pickTerrain
  };
})(window);

/**
 * Khoi dong DEM 3D + ban do 2D (Leaflet chi dung khi doi che do).
 */
(function (global) {
  "use strict";

  const API = (window.floodUrl ? window.floodUrl("/api/flood-3d") : "/api/flood-3d");
  let map = null;
  let riverLayer = null;
  let mode3d = true;

  function $(id) {
    return document.getElementById(id);
  }

  function selectedWaterSource() {
    return window.pickWaterSource ? window.pickWaterSource() : "saint-venant";
  }

  function setHint(text) {
    const el = $("dem3dHint");
    if (el) el.textContent = text || "";
  }

  function osmTileUrl() {
    const path = "/api/basemap/osm/{z}/{x}/{y}.png?v=4";
    return window.floodUrl ? window.floodUrl(path) : path;
  }

  function refreshMapSize() {
    if (!map) return;
    map.invalidateSize({ animate: false });
    if (riverLayer && riverLayer.getBounds && riverLayer.getBounds().isValid()) {
      map.fitBounds(riverLayer.getBounds(), { padding: [40, 40] });
    }
  }

  function initMap() {
    const mapEl = $("map");
    if (!mapEl || map) return;
    map = L.map(mapEl, {
      zoomControl: true,
      preferCanvas: false
    }).setView([21.03, 105.85], 11);
    global.map = map;
    L.tileLayer(osmTileUrl(), {
      maxZoom: 19,
      maxNativeZoom: 19,
      keepBuffer: 4,
      updateWhenZooming: false,
      updateWhenIdle: false,
      attribution: "&copy; OpenStreetMap"
    }).addTo(map);
  }

  function drawRiver(meta) {
    if (!map) return;
    if (riverLayer) {
      map.removeLayer(riverLayer);
      riverLayer = null;
    }
    const river = meta && meta.river;
    if (!river || !river.lon || !river.lat || river.lon.length < 2) return;
    const latlngs = [];
    for (let i = 0; i < river.lon.length; i++) {
      latlngs.push([river.lat[i], river.lon[i]]);
    }
    riverLayer = L.polyline(latlngs, { color: "#f59e0b", weight: 3, opacity: 0.9 }).addTo(map);
    map.fitBounds(riverLayer.getBounds(), { padding: [40, 40] });
  }

  function setModeButtons() {
    const b3 = $("view3dBtn");
    const b2 = $("view2dBtn");
    if (b3) b3.classList.toggle("is-active", mode3d);
    if (b2) b2.classList.toggle("is-active", !mode3d);
  }

  async function switchTo3D() {
    mode3d = true;
    document.body.classList.add("mode-3d");
    setModeButtons();
    if (global.Dem3D) {
      Dem3D.show3DMode(true);
      try {
        await Dem3D.loadDem3D("dem", 256);
        const contourToggle = $("demContourToggle");
        Dem3D.setContoursVisible(!!(contourToggle && contourToggle.checked));
      } catch (err) {
        setHint(err.message || String(err));
        alert(err.message || "Không mở được DEM 3D");
      }
    }
  }

  function switchTo2D() {
    mode3d = false;
    document.body.classList.remove("mode-3d");
    setModeButtons();
    if (global.Dem3D) Dem3D.show3DMode(false);
    const mapEl = $("map");
    if (mapEl) mapEl.style.display = "";
    if (!map) initMap();
    requestAnimationFrame(function () {
      refreshMapSize();
      setTimeout(refreshMapSize, 80);
      setTimeout(refreshMapSize, 250);
    });
  }

  async function makeVideo() {
    setHint("Đang tạo video…");
    try {
      const res = await fetch(API + "/video", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({
          file_id: "dem",
          step: 1,
          fps: 10,
          water_source: selectedWaterSource()
        })
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "Không tạo được video");
      setHint("Đã ghi video.");
      window.open(data.url || (API + "/video"), "_blank");
    } catch (err) {
      setHint(String(err.message || err));
    }
  }

  function bind() {
    const b3 = $("view3dBtn");
    const b2 = $("view2dBtn");
    if (b3) b3.addEventListener("click", switchTo3D);
    if (b2) b2.addEventListener("click", switchTo2D);
    const exag = $("demExaggeration");
    if (exag) {
      exag.addEventListener("input", function () {
        if (global.Dem3D) Dem3D.setExaggeration(exag.value);
      });
    }
    const contourToggle = $("demContourToggle");
    if (contourToggle) {
      contourToggle.addEventListener("change", function () {
        if (global.Dem3D) Dem3D.setContoursVisible(!!contourToggle.checked);
      });
    }
    const video = $("flood3dVideoBtn");
    if (video) video.addEventListener("click", makeVideo);
  }

  async function boot() {
    initMap();
    bind();
    try {
      const res = await fetch(
        API + "/meta?water_source=" + encodeURIComponent(selectedWaterSource()),
        { headers: { "Accept": "application/json" } }
      );
      const meta = await res.json();
      if (meta.ok) {
        drawRiver(meta);
        const slider = $("flood3dTime");
        if (slider && meta.n_times) slider.max = String(Math.max(0, meta.n_times - 1));
      }
    } catch (_e) { /* 1D chua co */ }
    await switchTo3D();
  }

  global.Flood3DBoot = { switchTo3D: switchTo3D, switchTo2D: switchTo2D };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})(window);

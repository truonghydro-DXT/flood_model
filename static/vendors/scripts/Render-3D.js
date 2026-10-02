/* global L */
(function (global) {
	"use strict";

	const THREE_SCRIPT_URL = "vendors/scripts/three.min.js";
	const OVERLAY_EVENT = "population3d:overlay-data";
	const TOGGLE_BUTTON_IDS = ["render3DBtn", "toggle3D"];
	const LAYER_COLORS = [
		"#1d4ed8",
		"#0f766e",
		"#b45309",
		"#7c3aed",
		"#be123c",
		"#15803d",
		"#0369a1",
		"#c2410c"
	];
	const STATE = {
		enabled: false,
		loading: false,
		threeReady: false,
		container: null,
		renderer: null,
		scene: null,
		camera: null,
		group: null,
		renderFrame: 0,
		rebuildTimer: 0,
		overlaySourceSeen: false,
		lastOverlayByLayer: Object.create(null),
		boundMapHandlers: null,
		boundMapDetach: null
	};

	function getMap() {
		const candidate = typeof global.map !== "undefined"
			? global.map
			: typeof window !== "undefined" && window.map
				? window.map
				: null;

		if (
			candidate &&
			typeof candidate.getCenter === "function" &&
			typeof candidate.getSize === "function" &&
			typeof candidate.latLngToContainerPoint === "function"
		) {
			return candidate;
		}

		return null;
	}

	function getButton() {
		for (const id of TOGGLE_BUTTON_IDS) {
			const button = document.getElementById(id);
			if (button) return button;
		}
		return null;
	}

	function isGeoJsonPolygonGeometry(geometry) {
		return Boolean(
			geometry &&
			(geometry.type === "Polygon" || geometry.type === "MultiPolygon") &&
			Array.isArray(geometry.coordinates)
		);
	}

	function normalizeHexColor(value, fallback) {
		const raw = String(value || "").trim();
		if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw)) {
			return raw;
		}
		return fallback;
	}

	function readHeight(properties) {
		const data = properties || {};
		const selectedField = String(global.__digitalTwinHeightField || "").trim();
		if (selectedField) {
			const selectedValue = Number(data[selectedField]);
			if (Number.isFinite(selectedValue) && selectedValue > 0) {
				if (/(floors?|levels?|storeys?)/i.test(selectedField) && selectedValue <= 120) {
					return Math.max(3, selectedValue * 3.2);
				}
				return selectedValue;
			}
		}
		const candidates = [
			data.height,
			data.building_height,
			data.extrusion_height,
			data.h,
			data.floors,
			data.levels,
			data.storeys
		];

		for (const candidate of candidates) {
			const numeric = Number(candidate);
			if (Number.isFinite(numeric) && numeric > 0) {
				if ((candidate === data.floors || candidate === data.levels || candidate === data.storeys) && numeric <= 120) {
					return Math.max(3, numeric * 3.2);
				}
				return numeric;
			}
		}

		return 0;
	}

	function hashString(value) {
		const text = String(value || "");
		let hash = 0;
		for (let index = 0; index < text.length; index += 1) {
			hash = ((hash << 5) - hash) + text.charCodeAt(index);
			hash |= 0;
		}
		return Math.abs(hash);
	}

	function colorFromLayerName(layerName) {
		if (!layerName) return LAYER_COLORS[0];
		return LAYER_COLORS[hashString(layerName) % LAYER_COLORS.length];
	}

	function buildRenderableFeature(feature, layerName) {
		if (!feature || !feature.geometry || !isGeoJsonPolygonGeometry(feature.geometry)) {
			return null;
		}

		const properties = Object.assign({}, feature.properties || {});
		let height = readHeight(properties);
		if (!(height > 0)) {
			return null;
		}
		properties.height = height;
		if (!properties.color) {
			properties.color = colorFromLayerName(
				layerName || properties.layerName || properties.sourceLayer || properties.groupName
			);
		}

		return {
			type: "Feature",
			geometry: feature.geometry,
			properties
		};
	}

	function loadThree() {
		if (global.THREE) {
			STATE.threeReady = true;
			return Promise.resolve(global.THREE);
		}

		if (STATE.loading) {
			return new Promise((resolve, reject) => {
				const check = () => {
					if (global.THREE) {
						STATE.threeReady = true;
						resolve(global.THREE);
						return;
					}

					if (!STATE.loading) {
						reject(new Error("three.min.js failed to load"));
						return;
					}

					global.setTimeout(check, 50);
				};
				check();
			});
		}

		STATE.loading = true;
		return new Promise((resolve, reject) => {
			const script = document.createElement("script");
			script.src = THREE_SCRIPT_URL;
			script.async = true;
			script.onload = () => {
				STATE.loading = false;
				STATE.threeReady = Boolean(global.THREE);
				if (global.THREE) {
					resolve(global.THREE);
				} else {
					reject(new Error("three.min.js loaded but THREE is unavailable"));
				}
			};
			script.onerror = () => {
				STATE.loading = false;
				reject(new Error("Không tải được three.min.js"));
			};
			document.head.appendChild(script);
		});
	}

	function getContainer() {
		const map = getMap();
		if (map && typeof map.getContainer === "function") {
			return map.getContainer();
		}
		return document.body;
	}

	function ensureContainer() {
		if (STATE.container && STATE.container.parentNode) {
			return STATE.container;
		}

		const container = document.createElement("div");
		container.id = "three-render-3d-overlay";
		container.style.position = "absolute";
		container.style.inset = "0";
		container.style.pointerEvents = "none";
		container.style.zIndex = "620";
		container.style.overflow = "hidden";

		const mapContainer = getContainer();
		if (mapContainer && mapContainer.style && getComputedStyle(mapContainer).position === "static") {
			mapContainer.style.position = "relative";
		}
		mapContainer.appendChild(container);
		STATE.container = container;
		return container;
	}

	function disposeObject3D(object3D, THREE) {
		if (!object3D) return;

		object3D.traverse((node) => {
			if (node.geometry) {
				node.geometry.dispose();
			}

			if (node.material) {
				if (Array.isArray(node.material)) {
					node.material.forEach((material) => material && material.dispose && material.dispose());
				} else if (node.material.dispose) {
					node.material.dispose();
				}
			}
		});
		if (object3D.parent) {
			object3D.parent.remove(object3D);
		}
	}

	function clearScene() {
		if (!STATE.scene || !STATE.group || !global.THREE) {
			return;
		}
		disposeObject3D(STATE.group, global.THREE);
		STATE.group = new global.THREE.Group();
		STATE.scene.add(STATE.group);
	}

	function flattenFeaturesFromPayload(payload) {
		if (!payload) return [];

		if (payload.geojson && Array.isArray(payload.geojson.features)) {
			return payload.geojson.features;
		}

		if (payload.featureCollection && Array.isArray(payload.featureCollection.features)) {
			return payload.featureCollection.features;
		}

		if (Array.isArray(payload.features)) {
			return payload.features;
		}

		if (payload.type === "FeatureCollection" && Array.isArray(payload.features)) {
			return payload.features;
		}

		if (payload.type === "Feature") {
			return [payload];
		}

		return [];
	}

	function toWorldPoint(map, centerPoint, lat, lng) {
		const point = map.latLngToContainerPoint([lat, lng]);
		return {
			x: point.x - centerPoint.x,
			y: centerPoint.y - point.y
		};
	}

	function ringToVectorPoints(map, centerPoint, ring) {
		if (!Array.isArray(ring) || ring.length < 3) {
			return [];
		}

		const points = [];
		for (const coord of ring) {
			if (!Array.isArray(coord) || coord.length < 2) continue;
			const world = toWorldPoint(map, centerPoint, coord[1], coord[0]);
			points.push(new global.THREE.Vector2(world.x, world.y));
		}

		if (points.length > 2) {
			const first = points[0];
			const last = points[points.length - 1];
			if (Math.abs(first.x - last.x) < 1e-6 && Math.abs(first.y - last.y) < 1e-6) {
				points.pop();
			}
		}

		return points;
	}

	function buildShapeFromPolygon(map, centerPoint, coordinates) {
		if (!Array.isArray(coordinates) || !coordinates.length) {
			return null;
		}

		const outerRing = ringToVectorPoints(map, centerPoint, coordinates[0]);
		if (outerRing.length < 3) {
			return null;
		}

		const shape = new global.THREE.Shape(outerRing);
		for (let index = 1; index < coordinates.length; index += 1) {
			const holeRing = ringToVectorPoints(map, centerPoint, coordinates[index]);
			if (holeRing.length < 3) continue;
			const hole = new global.THREE.Path(holeRing);
			shape.holes.push(hole);
		}

		return shape;
	}

	function getHeightScale(map) {
		const center = map.getCenter();
		const north = global.L.latLng(center.lat + 0.0001, center.lng);
		const centerPoint = map.latLngToContainerPoint(center);
		const northPoint = map.latLngToContainerPoint(north);
		const pixelsPerMeter = Math.max(Math.abs(northPoint.y - centerPoint.y) / 11.119, 0.01);
		return pixelsPerMeter;
	}

	function buildFeatureMeshes(map, feature, THREE, pixelsPerMeter, centerPoint, materialCache) {
		const geometry = feature && feature.geometry;
		if (!isGeoJsonPolygonGeometry(geometry)) {
			return [];
		}

		const properties = feature.properties || {};
		const heightMeters = readHeight(properties);
		if (heightMeters <= 0) {
			return [];
		}

		const baseColor = normalizeHexColor(
			properties.color || properties.fill || properties.extrusion_color || properties.buildingColor,
			"#1d4ed8"
		);
		const heightPixels = Math.max(2, heightMeters * pixelsPerMeter);
		const polygons = geometry.type === "Polygon" ? [geometry.coordinates] : geometry.coordinates;
		const meshes = [];

		for (const polygon of polygons) {
			const shape = buildShapeFromPolygon(map, centerPoint, polygon);
			if (!shape) continue;

			const extrude = new THREE.ExtrudeGeometry(shape, {
				depth: heightPixels,
				bevelEnabled: false,
				steps: 1
			});

			const materialKey = `${baseColor}:${heightPixels.toFixed(2)}`;
			if (!materialCache.has(materialKey)) {
				materialCache.set(materialKey, new THREE.MeshStandardMaterial({
					color: new THREE.Color(baseColor),
					metalness: 0.08,
					roughness: 0.85,
					transparent: true,
					opacity: 0.9,
					side: THREE.DoubleSide
				}));
			}

			const mesh = new THREE.Mesh(extrude, materialCache.get(materialKey));
			mesh.userData = {
				heightMeters,
				featureId: properties.id || properties.objectid || properties.name || "building"
			};
			meshes.push(mesh);

			const outline = new THREE.LineSegments(
				new THREE.EdgesGeometry(extrude),
				new THREE.LineBasicMaterial({ color: 0x0f172a, transparent: true, opacity: 0.38 })
			);
			meshes.push(outline);
		}

		return meshes;
	}

	function collectFallbackFeatures(map) {
		const features = [];
		const importedLayers = Array.isArray(global._importedFileLayers) ? global._importedFileLayers : [];
		const seen = new Set();

		for (const entry of importedLayers) {
			if (!entry) continue;
			const sourceFeatures =
				Array.isArray(entry.features) && entry.features.length
					? entry.features
					: (entry.layer && Array.isArray(entry.layer._importSourceFeatures)
						? entry.layer._importSourceFeatures
						: []);
			for (const feature of sourceFeatures) {
				if (!feature || seen.has(feature)) continue;
				const prepared = buildRenderableFeature(feature, entry.name);
				if (!prepared) continue;
				seen.add(feature);
				features.push(prepared);
			}
		}

		if (!map || typeof map.eachLayer !== "function") {
			return features;
		}

		const visitLayer = (layer) => {
			if (!layer) return;

			if (layer.feature && layer.feature.geometry && isGeoJsonPolygonGeometry(layer.feature.geometry)) {
				if (!seen.has(layer.feature)) {
					const prepared = buildRenderableFeature(layer.feature);
					if (prepared) {
						seen.add(layer.feature);
						features.push(prepared);
					}
				}
			}

			if (layer._layers) {
				Object.keys(layer._layers).forEach((key) => visitLayer(layer._layers[key]));
			}
		};

		map.eachLayer(visitLayer);
		return features;
	}

	function getRenderableFeatures(map) {
		const collected = [];
		const overlays = Object.values(STATE.lastOverlayByLayer);

		for (const overlay of overlays) {
			const features = flattenFeaturesFromPayload(overlay);
			for (const feature of features) {
				const prepared = buildRenderableFeature(feature, overlay && overlay.layerName);
				if (!prepared) continue;
				collected.push(prepared);
			}
		}

		if (collected.length) {
			return collected;
		}

		if (STATE.overlaySourceSeen) {
			return [];
		}

		return collectFallbackFeatures(map);
	}

	function renderScene() {
		const map = getMap();
		if (!STATE.enabled || !map || !STATE.renderer || !STATE.scene || !STATE.camera || !global.THREE) {
			return;
		}

		const container = ensureContainer();
		const width = container.clientWidth || map.getSize().x || 1;
		const height = container.clientHeight || map.getSize().y || 1;
		const centerPoint = map.latLngToContainerPoint(map.getCenter());
		const pixelsPerMeter = getHeightScale(map);
		const features = getRenderableFeatures(map);
		const materialCache = new Map();

		STATE.renderer.setSize(width, height, false);
		STATE.camera.aspect = width / height;
		STATE.camera.updateProjectionMatrix();

		clearScene();

		for (const feature of features) {
			const meshes = buildFeatureMeshes(map, feature, global.THREE, pixelsPerMeter, centerPoint, materialCache);
			for (const mesh of meshes) {
				STATE.group.add(mesh);
			}
		}

		const span = Math.max(width, height);
		STATE.camera.position.set(span * 0.75, -span * 0.95, span * 0.9);
		STATE.camera.lookAt(0, 0, 0);
		STATE.renderer.render(STATE.scene, STATE.camera);
	}

	function scheduleRender() {
		if (!STATE.enabled) return;
		if (STATE.rebuildTimer) {
			clearTimeout(STATE.rebuildTimer);
		}
		STATE.rebuildTimer = global.setTimeout(() => {
			STATE.rebuildTimer = 0;
			renderScene();
		}, 50);
	}

	function attachMapHandlers() {
		const map = getMap();
		if (!map || STATE.boundMapHandlers) {
			return;
		}

		STATE.boundMapHandlers = {
			moveend: scheduleRender,
			zoomend: scheduleRender,
			resize: scheduleRender
		};

		if (typeof map.on === "function" && typeof map.off === "function") {
			map.on("moveend zoomend resize", scheduleRender);
			STATE.boundMapDetach = () => {
				map.off("moveend zoomend resize", scheduleRender);
			};
			return;
		}

		if (typeof map.addEventListener === "function" && typeof map.removeEventListener === "function") {
			map.addEventListener("moveend", scheduleRender);
			map.addEventListener("zoomend", scheduleRender);
			map.addEventListener("resize", scheduleRender);
			STATE.boundMapDetach = () => {
				map.removeEventListener("moveend", scheduleRender);
				map.removeEventListener("zoomend", scheduleRender);
				map.removeEventListener("resize", scheduleRender);
			};
			return;
		}

		STATE.boundMapDetach = null;
	}

	function detachMapHandlers() {
		if (!STATE.boundMapHandlers) {
			return;
		}

		if (typeof STATE.boundMapDetach === "function") {
			STATE.boundMapDetach();
		}

		STATE.boundMapHandlers = null;
		STATE.boundMapDetach = null;
	}

	async function enableRenderer() {
		if (STATE.enabled) {
			return;
		}

		const map = getMap();
		if (!map) {
			return;
		}

		STATE.enabled = true;
		updateButtonState();
		await loadThree();

		const THREE = global.THREE;
		const container = ensureContainer();
		const width = container.clientWidth || map.getSize().x || 1;
		const height = container.clientHeight || map.getSize().y || 1;

		STATE.scene = new THREE.Scene();
		STATE.scene.background = null;

		STATE.camera = new THREE.PerspectiveCamera(35, width / height, 1, 250000);
		STATE.renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "high-performance" });
		STATE.renderer.setPixelRatio(global.devicePixelRatio || 1);
		STATE.renderer.setSize(width, height, false);
		STATE.renderer.setClearColor(0x000000, 0);
		STATE.renderer.domElement.style.width = "100%";
		STATE.renderer.domElement.style.height = "100%";
		STATE.renderer.domElement.style.display = "block";
		STATE.renderer.domElement.style.pointerEvents = "none";
		container.appendChild(STATE.renderer.domElement);

		const ambient = new THREE.AmbientLight(0xffffff, 1.2);
		const sun = new THREE.DirectionalLight(0xffffff, 1.15);
		sun.position.set(250, -450, 700);
		STATE.scene.add(ambient, sun);
		STATE.group = new THREE.Group();
		STATE.scene.add(STATE.group);

		attachMapHandlers();
		renderScene();
	}

	function destroyRenderer() {
		STATE.enabled = false;
		updateButtonState();
		detachMapHandlers();

		if (STATE.rebuildTimer) {
			clearTimeout(STATE.rebuildTimer);
			STATE.rebuildTimer = 0;
		}

		STATE.overlaySourceSeen = false;

		if (STATE.scene && STATE.group) {
			disposeObject3D(STATE.group, global.THREE || window.THREE);
		}

		if (STATE.renderer && STATE.renderer.domElement && STATE.renderer.domElement.parentNode) {
			STATE.renderer.domElement.parentNode.removeChild(STATE.renderer.domElement);
		}

		if (STATE.renderer && STATE.renderer.dispose) {
			STATE.renderer.dispose();
		}

		STATE.renderer = null;
		STATE.scene = null;
		STATE.camera = null;
		STATE.group = null;

		if (STATE.container && STATE.container.parentNode) {
			STATE.container.parentNode.removeChild(STATE.container);
		}
		STATE.container = null;
	}

	function updateButtonState() {
		const button = getButton();
		if (!button) return;
		button.classList.toggle("active", STATE.enabled);
		button.setAttribute("aria-pressed", STATE.enabled ? "true" : "false");
		button.textContent = STATE.enabled ? "Tắt 3D-DTW" : "Digital Twins";
	}

	function toggleRenderer() {
		if (STATE.enabled) {
			destroyRenderer();
			return;
		}

		void enableRenderer().catch((error) => {
			console.error("Không thể dựng 3D:", error);
			destroyRenderer();
		});
	}

	function onOverlayData(event) {
		const payload = event && event.detail ? event.detail : null;
		if (!payload || !payload.layerName) {
			return;
		}

		STATE.overlaySourceSeen = true;

		if (payload.removed) {
			delete STATE.lastOverlayByLayer[payload.layerName];
			scheduleRender();
			return;
		}

		STATE.lastOverlayByLayer[payload.layerName] = payload;
		scheduleRender();
	}

	function attachEvents() {
		const button = getButton();
		if (button && !button.dataset.render3DAttached) {
			button.dataset.render3DAttached = "1";
			button.addEventListener("click", toggleRenderer);
		}

		if (!global.__render3DOverlayListenerAttached) {
			global.addEventListener(OVERLAY_EVENT, onOverlayData);
			global.__render3DOverlayListenerAttached = true;
		}

		updateButtonState();
	}

	function init() {
		attachEvents();
		if (getMap()) {
			ensureContainer();
		}
	}

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", init, { once: true });
	} else {
		init();
	}

	global.Render3D = {
		toggle: toggleRenderer,
		enable: enableRenderer,
		disable: destroyRenderer,
		refresh: scheduleRender,
		isEnabled: function () {
			return STATE.enabled;
		}
	};

})(window);

(function (global) {
	"use strict";

	global.USE_POPULATION_3D_SIMULATOR = true;

	const SOURCE_ID = "population-3d-source";
	const BASE_LAYER_ID = "population-3d-base";
	const EXTRUSION_LAYER_ID = "population-3d-extrusion";
	const OUTLINE_LAYER_ID = "population-3d-outline";
	const FLOOD_SOURCE_ID = "digital-twin-flood-source";
	const FLOOD_LAYER_ID = "digital-twin-flood-layer";
	const FLOOD_CAP_SOURCE_ID = "digital-twin-flood-cap-source";
	const FLOOD_CAP_LAYER_ID = "digital-twin-flood-cap-layer";
	const URBAN_OVERLAY_EVENT = "population3d:overlay-data";
	const URBAN_OVERLAY_SOURCE_PREFIX = "population-3d-urban-overlay-source-";
	const URBAN_OVERLAY_LAYER_PREFIX = "population-3d-urban-overlay-layer-";
	const FALLBACK_STYLE_URL = "https://basemap.lifetex.vn/styles/basic-preview/style.json?key=lifetex_2026";
	const COLOR_STOPS = [
		{ stop: 0, color: "#1d4ed8" },
		{ stop: 0.35, color: "#0ea5e9" },
		{ stop: 0.6, color: "#10b981" },
		{ stop: 0.82, color: "#f59e0b" },
		{ stop: 1, color: "#dc2626" }
	];
	const FLOOD_BLACK_STOPS = [
		{ stop: 0, color: "#8a8a8a" },
		{ stop: 0.25, color: "#5c5c5c" },
		{ stop: 0.5, color: "#3a3a3a" },
		{ stop: 0.75, color: "#1a1a1a" },
		{ stop: 1, color: "#000000" }
	];
	const CAMERA_DEFAULTS = {
		minHeight: 420,
		maxHeight: 4600,
		defaultPitch: 55,
		defaultBearing: 0
	};
	const OVERLAY_SURFACE_SETTINGS = {
		polygonLift: 8,
		polygonThickness: 18,
		lineLift: 6,
		lineThickness: 12,
		lineBufferBaseMeters: 3,
		lineBufferPerWidth: 2.2,
		pointLift: 4,
		markerViewportPadding: 64
	};
	const BUILDING_OVERLAY_SURFACE_SETTINGS = {
		polygonLift: 0.25,
		polygonThickness: 0.75,
		lineLift: 0.15,
		lineThickness: 0.5,
		lineBufferBaseMeters: 3,
		lineBufferPerWidth: 2.2,
		pointLift: 0.1,
		markerViewportPadding: 64
	};
	const HEIGHT_SCALE = {
		exponent: 1.55,
		highTierThreshold: 0.76,
		highTierBoost: 1.2
	};

	function getCompensatedZoom(baseZoom, pitchDegrees) {
		const zoom = Number(baseZoom);
		const pitch = Math.max(0, Math.min(75, Number(pitchDegrees) || 0));
		if (!Number.isFinite(zoom) || pitch <= 0) {
			return zoom;
		}

		const pitchRadians = (pitch * Math.PI) / 180;
		const compensation = Math.log2(1 / Math.max(Math.cos(pitchRadians), 0.001));
		return zoom - compensation;
	}

	const RENDER_MODES = {
		population: "population",
		building: "building"
	};
	const POPULATION_METRICS = {
		total: "total",
		male: "male",
		female: "female"
	};
	const RENDER_SELECTION_STORAGE_KEY = "population3d:render-selection";

	const state = {
		enabled: false,
		loading: false,
		glMap: null,
		container: null,
		geojson: { type: "FeatureCollection", features: [] },
		stats: {
			minPopulation: 0,
			maxPopulation: 0,
			maxDensity: 0
		},
		pitch: 0,
		bearing: 0,
		popup: null,
		modePopup: null,
		renderMode: RENDER_MODES.population,
		populationMetric: POPULATION_METRICS.total,
		controlsAttached: false,
		ctrlTiltHandlersAttached: false,
		ctrlTiltSession: null,
		mapEventsAttached: false,
		clickHandlerAttached: false,
		overlaySyncAttached: false,
		overlayCache: Object.create(null),
		overlayRuntimeIds: Object.create(null),
		overlayIconMeta: Object.create(null),
		pointOverlayRoot: null,
		pointOverlayEntries: Object.create(null),
		pointOverlayUpdateFrame: 0,
		chartOverlayRoot: null,
		chartOverlayEntries: Object.create(null),
		renderReloadTimer: 0,
		floodOverlay: null
	};

	function normalizeLayerKey(value) {
		const raw = String(value || "overlay").trim().toLowerCase();
		const normalized = raw.replace(/[^a-z0-9_-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
		return normalized || "overlay";
	}

	function removeLayerIfExists(layerId) {
		if (!state.glMap || !layerId) {
			return;
		}

		if (typeof state.glMap.getLayer === "function" && state.glMap.getLayer(layerId)) {
			state.glMap.removeLayer(layerId);
		}
	}

	function removeSourceIfExists(sourceId) {
		if (!state.glMap || !sourceId) {
			return;
		}

		if (typeof state.glMap.getSource === "function" && state.glMap.getSource(sourceId)) {
			state.glMap.removeSource(sourceId);
		}
	}

	function clearUrbanOverlayByKey(overlayKey) {
		clearPointOverlayByKey(overlayKey);

		const runtimeIds = state.overlayRuntimeIds[overlayKey];
		if (!runtimeIds) {
			return;
		}

		if (Array.isArray(runtimeIds.layerIds)) {
			runtimeIds.layerIds.forEach(removeLayerIfExists);
		}

		if (Array.isArray(runtimeIds.sourceIds)) {
			runtimeIds.sourceIds.forEach(removeSourceIfExists);
		}

		delete state.overlayRuntimeIds[overlayKey];
	}

	function upsertGeoJsonSource(sourceId, featureCollection) {
		const existing = state.glMap.getSource(sourceId);
		if (existing && typeof existing.setData === "function") {
			existing.setData(featureCollection);
			return;
		}

		state.glMap.addSource(sourceId, {
			type: "geojson",
			data: featureCollection
		});
	}

	function splitFeaturesByGeometry(features) {
		const points = [];
		const lines = [];
		const polygons = [];

		features.forEach(feature => {
			if (!feature || !feature.geometry || !feature.geometry.type) {
				return;
			}

			const geometryType = feature.geometry.type;
			if (geometryType === "Point" || geometryType === "MultiPoint") {
				points.push(feature);
				return;
			}

			if (geometryType === "LineString" || geometryType === "MultiLineString") {
				lines.push(feature);
				return;
			}

			if (geometryType === "Polygon" || geometryType === "MultiPolygon") {
				polygons.push(feature);
			}
		});

		return { points, lines, polygons };
	}

	function addLayerIfMissing(layerId, layerDefinition) {
		if (state.glMap.getLayer(layerId)) {
			return;
		}

		state.glMap.addLayer(layerDefinition);
	}

	function loadMapImage(url) {
		return new Promise((resolve, reject) => {
			if (!state.glMap || typeof state.glMap.loadImage !== "function") {
				reject(new Error("MapLibre chưa sẵn sàng để tải icon"));
				return;
			}

			state.glMap.loadImage(url, (error, image) => {
				if (error) {
					reject(error);
					return;
				}

				resolve(image);
			});
		});
	}

	async function ensureOverlayIconImage(overlayKey, styleHint) {
		if (!state.glMap || !styleHint || !styleHint.iconUrl) {
			return null;
		}

		const imageId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-icon`;
		if (typeof state.glMap.hasImage === "function" && state.glMap.hasImage(imageId)) {
			return imageId;
		}

		try {
			const image = await loadMapImage(styleHint.iconUrl);
			if (typeof state.glMap.hasImage !== "function" || !state.glMap.hasImage(imageId)) {
				state.glMap.addImage(imageId, image);
			}

			state.overlayIconMeta[imageId] = {
				width: Number(image && image.width) || 24,
				height: Number(image && image.height) || 24
			};

			return imageId;
		} catch (error) {
			console.warn("Population 3D simulator: không thể tải icon overlay", styleHint.iconUrl, error);
			return null;
		}
	}

	function getOverlayIconScale(imageId, styleHint) {
		const requestedSize = Array.isArray(styleHint && styleHint.iconSize)
			? styleHint.iconSize
			: null;
		const imageMeta = state.overlayIconMeta[imageId];

		if (!requestedSize || !imageMeta) {
			return 1;
		}

		const requestedMax = Math.max(Number(requestedSize[0]) || 0, Number(requestedSize[1]) || 0, 1);
		const actualMax = Math.max(Number(imageMeta.width) || 0, Number(imageMeta.height) || 0, 1);
		return requestedMax / actualMax;
	}

	async function applyUrbanOverlayLayer(payload) {
		if (!state.glMap || !state.glMap.isStyleLoaded()) {
			return;
		}

		const overlaySettings = getOverlaySurfaceSettings();

		const layerName = payload && (payload.layerName || payload.layerTitle);
		const overlayKey = normalizeLayerKey(layerName);
		const collection = payload && payload.geojson && Array.isArray(payload.geojson.features)
			? payload.geojson.features.filter(feature => feature && feature.geometry)
			: [];

		if (!collection.length || payload.removed) {
			clearUrbanOverlayByKey(overlayKey);
			return;
		}

		const styleHint = payload.styleHint || {};
		const color = String(styleHint.color || "#f97316");
		const lineWidth = Number(styleHint.lineWidth || 2);
		const pointColor = String(styleHint.pointColor || color);
		const pointRadius = Number(styleHint.pointRadius || 5);

		const geometryGroups = splitFeaturesByGeometry(collection);
		const runtimeIds = {
			layerIds: [],
			sourceIds: []
		};

		if (geometryGroups.polygons.length) {
			const polygonFeatures = geometryGroups.polygons.map(decoratePolygonSurfaceFeature);
			const sourceId = `${URBAN_OVERLAY_SOURCE_PREFIX}${overlayKey}-polygon`;
			const fillLayerId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-polygon-surface`;

			upsertGeoJsonSource(sourceId, {
				type: "FeatureCollection",
				features: polygonFeatures
			});

			addLayerIfMissing(fillLayerId, {
				id: fillLayerId,
				type: "fill-extrusion",
				source: sourceId,
				paint: {
					"fill-extrusion-color": color,
					"fill-extrusion-base": ["coalesce", ["get", "surface_base_height"], overlaySettings.polygonLift],
					"fill-extrusion-height": [
						"coalesce",
						["get", "surface_top_height"],
						overlaySettings.polygonLift + overlaySettings.polygonThickness
					],
					"fill-extrusion-opacity": 0.72,
					"fill-extrusion-vertical-gradient": false
				}
			});

			runtimeIds.layerIds.push(fillLayerId);
			runtimeIds.sourceIds.push(sourceId);
		}

		if (geometryGroups.lines.length) {
			const elevatedLineFeatures = decorateLineSurfaceFeatures(geometryGroups.lines, lineWidth);

			if (elevatedLineFeatures.length) {
				const sourceId = `${URBAN_OVERLAY_SOURCE_PREFIX}${overlayKey}-line-surface`;
				const layerId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-line-surface`;

				upsertGeoJsonSource(sourceId, {
					type: "FeatureCollection",
					features: elevatedLineFeatures
				});

				addLayerIfMissing(layerId, {
					id: layerId,
					type: "fill-extrusion",
					source: sourceId,
					paint: {
						"fill-extrusion-color": color,
						"fill-extrusion-base": ["coalesce", ["get", "surface_base_height"], overlaySettings.lineLift],
						"fill-extrusion-height": [
							"coalesce",
							["get", "surface_top_height"],
							overlaySettings.lineLift + overlaySettings.lineThickness
						],
						"fill-extrusion-opacity": 0.92,
						"fill-extrusion-vertical-gradient": false
					}
				});

				runtimeIds.layerIds.push(layerId);
				runtimeIds.sourceIds.push(sourceId);
			} else {
				const sourceId = `${URBAN_OVERLAY_SOURCE_PREFIX}${overlayKey}-line`;
				const layerId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-line`;

				upsertGeoJsonSource(sourceId, {
					type: "FeatureCollection",
					features: geometryGroups.lines
				});

				addLayerIfMissing(layerId, {
					id: layerId,
					type: "line",
					source: sourceId,
					paint: {
						"line-color": color,
						"line-width": lineWidth,
						"line-opacity": 0.98
					}
				});

				runtimeIds.layerIds.push(layerId);
				runtimeIds.sourceIds.push(sourceId);
			}
		}

		if (geometryGroups.points.length) {
			if (canRenderElevatedPointMarkers()) {
				clearPointOverlayByKey(overlayKey);
				renderPointOverlayMarkers(overlayKey, geometryGroups.points, styleHint);
			} else {
				clearPointOverlayByKey(overlayKey);

				const sourceId = `${URBAN_OVERLAY_SOURCE_PREFIX}${overlayKey}-point`;
				const symbolLayerId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-point-symbol`;
				const circleLayerId = `${URBAN_OVERLAY_LAYER_PREFIX}${overlayKey}-point-circle`;

				upsertGeoJsonSource(sourceId, {
					type: "FeatureCollection",
					features: geometryGroups.points
				});

				const iconImageId = await ensureOverlayIconImage(overlayKey, styleHint);
				if (iconImageId) {
					removeLayerIfExists(circleLayerId);

					addLayerIfMissing(symbolLayerId, {
						id: symbolLayerId,
						type: "symbol",
						source: sourceId,
						layout: {
							"icon-image": iconImageId,
							"icon-size": getOverlayIconScale(iconImageId, styleHint),
							"icon-allow-overlap": true,
							"icon-ignore-placement": true,
							"icon-anchor": "center",
							"icon-pitch-alignment": "map",
							"icon-rotation-alignment": "map",
							"icon-keep-upright": false
						},
						paint: {
							"icon-opacity": 1
						}
					});

					runtimeIds.layerIds.push(symbolLayerId);
				} else {
					removeLayerIfExists(symbolLayerId);

					addLayerIfMissing(circleLayerId, {
						id: circleLayerId,
						type: "circle",
						source: sourceId,
						paint: {
							"circle-color": pointColor,
							"circle-radius": pointRadius,
							"circle-stroke-color": "#0f172a",
							"circle-stroke-width": 0.8,
							"circle-opacity": 0.95,
							"circle-pitch-alignment": "map",
							"circle-pitch-scale": "map"
						}
					});

					runtimeIds.layerIds.push(circleLayerId);
				}

				runtimeIds.sourceIds.push(sourceId);
			}
		} else {
			clearPointOverlayByKey(overlayKey);
		}

		const previousRuntimeIds = state.overlayRuntimeIds[overlayKey];
		if (previousRuntimeIds) {
			const nextLayerSet = new Set(runtimeIds.layerIds);
			const nextSourceSet = new Set(runtimeIds.sourceIds);

			(previousRuntimeIds.layerIds || []).forEach(layerId => {
				if (!nextLayerSet.has(layerId)) {
					removeLayerIfExists(layerId);
				}
			});

			(previousRuntimeIds.sourceIds || []).forEach(sourceId => {
				if (!nextSourceSet.has(sourceId)) {
					removeSourceIfExists(sourceId);
				}
			});
		}

		state.overlayRuntimeIds[overlayKey] = runtimeIds;
	}

	function applyCachedUrbanOverlays() {
		if (!state.glMap || !state.glMap.isStyleLoaded()) {
			return;
		}

		Object.keys(state.overlayCache).forEach(overlayKey => {
			void applyUrbanOverlayLayer(state.overlayCache[overlayKey]);
		});
	}

	function onUrbanOverlaySync(event) {
		const detail = event && event.detail ? event.detail : null;
		if (!detail) {
			return;
		}

		const overlayKey = normalizeLayerKey(detail.layerName || detail.layerTitle);

		if (detail.removed) {
			delete state.overlayCache[overlayKey];
			clearPointOverlayByKey(overlayKey);
			clearChartOverlayByKey(overlayKey);

			if (state.glMap && state.glMap.isStyleLoaded()) {
				// Remove MapLibre layers and sources
				const runtimeIds = state.overlayRuntimeIds[overlayKey];
				if (runtimeIds) {
					(runtimeIds.layerIds || []).forEach(removeLayerIfExists);
					(runtimeIds.sourceIds || []).forEach(removeSourceIfExists);
				}
			}
			delete state.overlayRuntimeIds[overlayKey];
			return;
		}

		state.overlayCache[overlayKey] = {
			...detail,
			layerName: detail.layerName || overlayKey,
			layerTitle: detail.layerTitle || detail.layerName || overlayKey,
			geojson: detail.geojson && detail.geojson.type === "FeatureCollection"
				? detail.geojson
				: { type: "FeatureCollection", features: [] }
		};

		if (state.enabled && state.glMap) {
			if (state.glMap.isStyleLoaded()) {
				void applyUrbanOverlayLayer(state.overlayCache[overlayKey]);
			} else {
				// Schedule retry when style finishes loading
				const capturedKey = overlayKey;
				state.glMap.once("styledata", () => {
					if (state.enabled && state.overlayCache[capturedKey] && state.glMap && state.glMap.isStyleLoaded()) {
						void applyUrbanOverlayLayer(state.overlayCache[capturedKey]);
					}
				});
			}
		}
	}

	function attachUrbanOverlaySync() {
		if (state.overlaySyncAttached || typeof window === "undefined") {
			return;
		}

		window.addEventListener(URBAN_OVERLAY_EVENT, onUrbanOverlaySync);
		state.overlaySyncAttached = true;
	}

	function emitSimulatorStateChange() {
		if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") {
			return;
		}

		window.dispatchEvent(new CustomEvent("population3d:state-change", {
			detail: {
				enabled: state.enabled
			}
		}));
	}

	function getLeafletMap() {
		return typeof map !== "undefined" ? map : null;
	}

	function getToggleButton() {
		return document.getElementById("render3DBtn") || document.getElementById("toggle3D");
	}

	function getTiltSlider() {
		return document.getElementById("tiltSlider");
	}

	function getRotationSlider() {
		return document.getElementById("rotationSlider");
	}

	function clamp(value, min, max) {
		return Math.max(min, Math.min(max, value));
	}

	function getOverlaySurfaceSettings() {
		return state.renderMode === RENDER_MODES.building
			? BUILDING_OVERLAY_SURFACE_SETTINGS
			: OVERLAY_SURFACE_SETTINGS;
	}

	function getBuildingOverlayReferenceHeight() {
		const minimumBuildingHeight = Number(state.stats && state.stats.minPopulation) || 0;
		return Math.max(0, minimumBuildingHeight * 0.1);
	}

	function getRoadOverlayReferenceHeight(lnglat) {
		if (state.renderMode === RENDER_MODES.building) {
			return 0.05;
		}

		return getSurfaceHeightAtLngLat(lnglat);
	}

	function easeHeight(value) {
		const normalized = clamp(value, 0, 1);
		const scaled = Math.pow(normalized, HEIGHT_SCALE.exponent);

		if (normalized < HEIGHT_SCALE.highTierThreshold) {
			return scaled;
		}

		return clamp(scaled * HEIGHT_SCALE.highTierBoost, 0, 1);
	}

	function hexToRgb(hex) {
		const normalized = String(hex || "").replace("#", "");
		const fullHex = normalized.length === 3
			? normalized.split("").map(char => char + char).join("")
			: normalized;

		return {
			r: parseInt(fullHex.slice(0, 2), 16),
			g: parseInt(fullHex.slice(2, 4), 16),
			b: parseInt(fullHex.slice(4, 6), 16)
		};
	}

	function mixColor(startHex, endHex, ratio) {
		const start = hexToRgb(startHex);
		const end = hexToRgb(endHex);
		const t = clamp(ratio, 0, 1);

		const red = Math.round(start.r + (end.r - start.r) * t);
		const green = Math.round(start.g + (end.g - start.g) * t);
		const blue = Math.round(start.b + (end.b - start.b) * t);

		return `rgb(${red}, ${green}, ${blue})`;
	}

	function colorFromStops(stops, normalizedValue) {
		const value = clamp(normalizedValue, 0, 1);
		const list = stops && stops.length ? stops : COLOR_STOPS;

		for (let index = 0; index < list.length - 1; index += 1) {
			const current = list[index];
			const next = list[index + 1];

			if (value >= current.stop && value <= next.stop) {
				const localRatio = (value - current.stop) / (next.stop - current.stop || 1);
				return mixColor(current.color, next.color, localRatio);
			}
		}

		return list[list.length - 1].color;
	}

	function getColorByPopulation(normalizedValue) {
		return colorFromStops(COLOR_STOPS, normalizedValue);
	}

	function getFloodBlackColor(r, g, b) {
		const luma = (0.299 * Number(r) + 0.587 * Number(g) + 0.114 * Number(b)) / 255;
		const depthT = clamp((0.86 - luma) / 0.78, 0, 1);
		return colorFromStops(FLOOD_BLACK_STOPS, depthT);
	}

	function formatInteger(value) {
		return Math.round(Number(value) || 0).toLocaleString("vi-VN");
	}

	function formatDensity(value) {
		return Number(value || 0).toLocaleString("vi-VN", {
			minimumFractionDigits: 0,
			maximumFractionDigits: 1
		});
	}

	function readGeoJson(name) {
		try {
			if (name === "geojsonBuilding" && typeof geojsonBuilding !== "undefined" && geojsonBuilding) {
				return geojsonBuilding;
			}
			if (name === "buildingGeoJSON" && typeof buildingGeoJSON !== "undefined" && buildingGeoJSON) {
				return buildingGeoJSON;
			}
			if (name === "currentGeoJSON" && typeof currentGeoJSON !== "undefined" && currentGeoJSON) {
				return currentGeoJSON;
			}
		} catch (error) {
			return null;
		}

		return null;
	}

	function normalizeRenderMode(value) {
		return String(value || RENDER_MODES.population).toLowerCase() === RENDER_MODES.building
			? RENDER_MODES.building
			: RENDER_MODES.population;
	}

	function normalizePopulationMetric(value) {
		const metric = String(value || POPULATION_METRICS.total).toLowerCase();
		return metric === POPULATION_METRICS.male || metric === POPULATION_METRICS.female
			? metric
			: POPULATION_METRICS.total;
	}

	function persistRenderSelection() {
		if (typeof localStorage === "undefined") {
			return;
		}

		try {
			localStorage.setItem(RENDER_SELECTION_STORAGE_KEY, JSON.stringify({
				mode: state.renderMode,
				metric: state.populationMetric
			}));
		} catch (error) {
			console.warn("Không thể lưu lựa chọn 3D gần nhất:", error);
		}
	}

	function restoreRenderSelection() {
		if (typeof localStorage === "undefined") {
			return;
		}

		try {
			const stored = localStorage.getItem(RENDER_SELECTION_STORAGE_KEY);
			if (!stored) {
				return;
			}

			const parsed = JSON.parse(stored);
			state.renderMode = normalizeRenderMode(parsed && parsed.mode);
			state.populationMetric = state.renderMode === RENDER_MODES.population
				? normalizePopulationMetric(parsed && parsed.metric)
				: POPULATION_METRICS.total;
		} catch (error) {
			console.warn("Không thể khôi phục lựa chọn 3D gần nhất:", error);
		}
	}

	function getPopulationMetrics(feature) {
		const properties = feature && feature.properties ? feature.properties : {};
		const total = getPopulationTotal(feature);
		const computed = typeof calcGenderPopulation === "function"
			? calcGenderPopulation(feature)
			: null;

		if (computed && Number.isFinite(Number(computed.male)) && Number.isFinite(Number(computed.female))) {
			const male = Number(computed.male) || 0;
			const female = Number(computed.female) || 0;
			return {
				total: Number.isFinite(Number(computed.total)) ? Number(computed.total) || total : total || male + female,
				male,
				female
			};
		}

		let male = 0;
		let female = 0;
		const maxAge = typeof MAX_AGE === "number" ? MAX_AGE : 84;
		for (let age = 0; age <= maxAge; age += 1) {
			male += Number(properties[`y_m_${age}`] || 0);
			female += Number(properties[`y_f_${age}`] || 0);
		}

		return {
			total: total || male + female,
			male,
			female
		};
	}

	function getPopulationMetricValue(feature, metric) {
		const population = getPopulationMetrics(feature);
		switch (normalizePopulationMetric(metric)) {
			case POPULATION_METRICS.male:
				return Number(population.male) || 0;
			case POPULATION_METRICS.female:
				return Number(population.female) || 0;
			default:
				return Number(population.total) || 0;
		}
	}

	function getRenderModeLabel() {
		if (state.renderMode === RENDER_MODES.building) {
			return "Tòa nhà";
		}

		if (state.populationMetric === POPULATION_METRICS.male) {
			return "Dân số nam";
		}

		if (state.populationMetric === POPULATION_METRICS.female) {
			return "Dân số nữ";
		}

		return "Tổng dân số";
	}

	restoreRenderSelection();

	function getPopulationTotal(feature) {
		if (typeof calcGenderPopulation === "function") {
			const result = calcGenderPopulation(feature);
			const total = Number(result && result.total);
			if (Number.isFinite(total)) {
				return total;
			}
		}

		const properties = feature && feature.properties ? feature.properties : {};
		const maxAge = typeof MAX_AGE === "number" ? MAX_AGE : 84;
		const areaKm2 = typeof calcPolygonAreaKm2 === "function"
			? calcPolygonAreaKm2(feature)
			: Number(properties.area_km2 || 0);

		let totalDensity = 0;
		for (let age = 0; age <= maxAge; age += 1) {
			totalDensity += Number(properties[`y_m_${age}`] || 0);
			totalDensity += Number(properties[`y_f_${age}`] || 0);
		}

		return totalDensity * areaKm2;
	}

	function preparePopulationGeoJSON(rawGeojson, metric) {
		const rawFeatures = Array.isArray(rawGeojson && rawGeojson.features)
			? rawGeojson.features.filter(feature => feature && feature.geometry)
			: [];

		if (!rawFeatures.length) {
			return {
				geojson: { type: "FeatureCollection", features: [] },
				stats: {
					minPopulation: 0,
					maxPopulation: 0,
					maxDensity: 0
				}
			};
		}

		const selectedMetric = normalizePopulationMetric(metric);
		const preparedValues = rawFeatures.map(feature => {
			const populationMetrics = getPopulationMetrics(feature);
			const populationValue = getPopulationMetricValue(feature, selectedMetric);
			const areaKm2 = typeof calcPolygonAreaKm2 === "function"
				? calcPolygonAreaKm2(feature)
				: 0;
			const density = areaKm2 > 0 ? populationValue / areaKm2 : 0;

			return {
				populationMetrics,
				populationValue,
				areaKm2,
				density
			};
		});

		const populationTotals = preparedValues.map(item => item.populationValue);
		const populationMin = Math.min(...populationTotals);
		const populationMax = Math.max(...populationTotals, 1);
		const populationSpread = populationMax - populationMin;
		const maxDensity = Math.max(...preparedValues.map(item => item.density), 0);

		const features = rawFeatures.map((feature, index) => {
			const metrics = preparedValues[index];
			const normalizedPopulation = populationSpread === 0
				? 0.65
				: (metrics.populationValue - populationMin) / populationSpread;
			const height = Math.round(
				CAMERA_DEFAULTS.minHeight +
				easeHeight(normalizedPopulation) * (CAMERA_DEFAULTS.maxHeight - CAMERA_DEFAULTS.minHeight)
			);
			const populationLabel = selectedMetric === POPULATION_METRICS.male
				? "Dân số nam"
				: selectedMetric === POPULATION_METRICS.female
					? "Dân số nữ"
					: "Tổng dân số";

			return {
				type: "Feature",
				geometry: feature.geometry,
				properties: {
					...(feature.properties || {}),
					population_total: metrics.populationMetrics.total,
					population_male: metrics.populationMetrics.male,
					population_female: metrics.populationMetrics.female,
					render_mode: RENDER_MODES.population,
					render_metric: selectedMetric,
					render_value: metrics.populationValue,
					render_label: populationLabel,
					population_density: metrics.density,
					extrusion_height: height,
					extrusion_color: getColorByPopulation(normalizedPopulation),
					extrusion_rank: normalizedPopulation,
					extrusion_label: feature.properties && feature.properties.tenxa
						? feature.properties.tenxa
						: "Không rõ xã"
				}
			};
		});

		return {
			geojson: {
				type: "FeatureCollection",
				features
			},
			stats: {
				minPopulation: populationMin,
				maxPopulation: populationMax,
				maxDensity
			}
		};
	}

	function prepareBuildingGeoJSON(rawGeojson) {
		const rawFeatures = Array.isArray(rawGeojson && rawGeojson.features)
			? rawGeojson.features.filter(feature => feature && feature.geometry)
			: [];

		if (!rawFeatures.length) {
			return {
				geojson: { type: "FeatureCollection", features: [] },
				stats: {
					minPopulation: 0,
					maxPopulation: 0,
					maxDensity: 0
				}
			};
		}

		const preparedValues = rawFeatures.map(feature => {
			const properties = feature.properties || {};
			const heightValue = Number(
				properties.height ||
				properties.building_height ||
				properties.h ||
				properties.levels ||
				12
			);

			return {
				heightValue: Number.isFinite(heightValue) && heightValue > 0 ? heightValue : 12
			};
		});

		const buildingHeights = preparedValues.map(item => item.heightValue);
		const heightMin = Math.min(...buildingHeights);
		const heightMax = Math.max(...buildingHeights, 1);
		const heightSpread = heightMax - heightMin;

		const features = rawFeatures.map((feature, index) => {
			const metrics = preparedValues[index];
			const normalizedHeight = heightSpread === 0
				? 0.5
				: (metrics.heightValue - heightMin) / heightSpread;

			return {
				type: "Feature",
				geometry: feature.geometry,
				properties: {
					...(feature.properties || {}),
					render_mode: RENDER_MODES.building,
					render_metric: "height",
					render_value: metrics.heightValue,
					render_label: "Tòa nhà",
					population_total: metrics.heightValue,
					population_density: 0,
					extrusion_height: Math.max(12, Math.round(metrics.heightValue)),
					extrusion_color: feature.properties && feature.properties.color
						? feature.properties.color
						: getColorByPopulation(normalizedHeight),
					extrusion_rank: normalizedHeight,
					extrusion_label: feature.properties && (feature.properties.name || feature.properties.tenxa || feature.properties.id)
						? String(feature.properties.name || feature.properties.tenxa || feature.properties.id)
						: "Tòa nhà"
				}
			};
		});

		return {
			geojson: {
				type: "FeatureCollection",
				features
			},
			stats: {
				minPopulation: heightMin,
				maxPopulation: heightMax,
				maxDensity: 0
			}
		};
	}

	function getSimulatorBaseStyle() {
		const leafletMap = getLeafletMap();
		if (
			leafletMap &&
			typeof googleSat !== "undefined" &&
			googleSat &&
			typeof leafletMap.hasLayer === "function" &&
			leafletMap.hasLayer(googleSat)
		) {
			return {
				version: 8,
				name: "population-3d-google-satellite",
				sources: {
					"simulator-google-satellite": {
						type: "raster",
						tiles: ["https://mt1.google.com/vt/lyrs=s,h&hl=vi&x={x}&y={y}&z={z}"],
						tileSize: 256,
						attribution: "© Google"
					}
				},
				layers: [
					{
						id: "simulator-google-satellite",
						type: "raster",
						source: "simulator-google-satellite"
					}
				]
			};
		}

		if (typeof LifetexBase !== "undefined" && LifetexBase && LifetexBase.options && LifetexBase.options.style) {
			return LifetexBase.options.style;
		}

		return FALLBACK_STYLE_URL;
	}

	function getEmergencyBaseStyle() {
		return {
			version: 8,
			name: "population-3d-emergency-style",
			sources: {
				"simulator-osm-raster": {
					type: "raster",
					tiles: [
						"https://tile.openstreetmap.org/{z}/{x}/{y}.png"
					],
					tileSize: 256,
					attribution: "&copy; OpenStreetMap contributors"
				}
			},
			layers: [
				{
					id: "simulator-osm-raster",
					type: "raster",
					source: "simulator-osm-raster"
				}
			]
		};
	}

	function applySceneLighting() {
		if (!state.glMap || typeof state.glMap.setLight !== "function") {
			return;
		}

		state.glMap.setLight({
			anchor: "viewport",
			color: "#fff7ed",
			intensity: 0.8,
			position: [1.2, 210, 35]
		});
	}

	function ensureContainer() {
		const leafletMap = getLeafletMap();
		if (!leafletMap) {
			return null;
		}

		if (state.container) {
			return state.container;
		}

		const container = document.createElement("div");
		container.id = "population-3d-simulator-layer";
		container.style.position = "absolute";
		container.style.inset = "0";
		container.style.zIndex = "550";
		container.style.pointerEvents = "none";
		container.style.display = "none";
		container.style.background = "transparent";

		leafletMap.getContainer().appendChild(container);
		state.container = container;

		return container;
	}

	function ensurePointOverlayRoot() {
		const container = ensureContainer();
		if (!container) {
			return null;
		}

		if (state.pointOverlayRoot && state.pointOverlayRoot.parentNode === container) {
			return state.pointOverlayRoot;
		}

		const overlayRoot = document.createElement("div");
		overlayRoot.id = "population-3d-point-overlay-root";
		overlayRoot.style.position = "absolute";
		overlayRoot.style.inset = "0";
		overlayRoot.style.zIndex = "5";
		overlayRoot.style.pointerEvents = "none";
		overlayRoot.style.overflow = "hidden";
		overlayRoot.style.display = "none";

		container.appendChild(overlayRoot);
		state.pointOverlayRoot = overlayRoot;
		return overlayRoot;
	}

	function setInteractiveHandlersEnabled(enabled) {
		if (!state.glMap) {
			return;
		}

		const handlers = [
			state.glMap.boxZoom,
			state.glMap.dragPan,
			state.glMap.dragRotate,
			state.glMap.doubleClickZoom,
			state.glMap.keyboard,
			state.glMap.scrollZoom,
			state.glMap.touchZoomRotate
		];

		handlers.forEach(handler => {
			if (!handler) {
				return;
			}

			if (enabled) {
				if (typeof handler.enable === "function") {
					handler.enable();
				}
				return;
			}

			if (typeof handler.disable === "function") {
				handler.disable();
			}
		});
	}

	function ensureMaplibreMap() {
		const leafletMap = getLeafletMap();
		const container = ensureContainer();

		if (!leafletMap || !container || typeof maplibregl === "undefined") {
			return Promise.reject(new Error("MapLibre hoặc Leaflet chưa sẵn sàng"));
		}

		container.style.display = "block";

		if (state.glMap) {
			state.glMap.resize();
			return Promise.resolve(state.glMap);
		}

		return new Promise((resolve, reject) => {
			const center = leafletMap.getCenter();
			let mapLoaded = false;
			let fallbackApplied = false;
			let fallbackTimer = null;

			const applyEmergencyFallbackStyle = () => {
				if (!state.glMap || fallbackApplied) {
					return;
				}

				fallbackApplied = true;
				console.warn("Population 3D simulator: dùng style dự phòng để đảm bảo hiển thị 3D");

				try {
					state.glMap.setStyle(getEmergencyBaseStyle());
				} catch (error) {
					console.error("Population 3D simulator: không thể áp dụng style dự phòng", error);
				}
			};

			state.glMap = new maplibregl.Map({
				container,
				style: getSimulatorBaseStyle(),
				center: [center.lng, center.lat],
				zoom: getCompensatedZoom(leafletMap.getZoom(), state.pitch),
				pitch: state.pitch,
				bearing: state.bearing,
				attributionControl: false,
				antialias: true,
				preserveDrawingBuffer: false,
				interactive: false,
				renderWorldCopies: false
			});

			setInteractiveHandlersEnabled(false);

			fallbackTimer = setTimeout(() => {
				if (!mapLoaded) {
					applyEmergencyFallbackStyle();
				}
			}, 3000);

			state.glMap.on("error", event => {
				console.error("Population 3D simulator error:", event && event.error ? event.error : event);

				if (!mapLoaded) {
					applyEmergencyFallbackStyle();
				}
			});

			state.glMap.once("load", () => {
				mapLoaded = true;
				if (fallbackTimer) {
					clearTimeout(fallbackTimer);
					fallbackTimer = null;
				}

				applySceneLighting();
				applyCachedUrbanOverlays();
				applyCachedFloodOverlay();
				const canvas = state.glMap.getCanvas();
				canvas.style.pointerEvents = "none";
				canvas.style.outline = "none";
				container.style.background = "#dbeafe";
				resolve(state.glMap);
			});

			state.glMap.once("styleimagemissing", () => {});

			if (!state.glMap) {
				reject(new Error("Không thể khởi tạo bản đồ MapLibre"));
			}
		});
	}

	function applyRenderSource() {
		if (!state.glMap || !state.glMap.isStyleLoaded()) {
			return;
		}

		const source = state.glMap.getSource(SOURCE_ID);
		if (!source) {
			state.glMap.addSource(SOURCE_ID, {
				type: "geojson",
				data: state.geojson
			});

			state.glMap.addLayer({
				id: BASE_LAYER_ID,
				type: "fill",
				source: SOURCE_ID,
				paint: {
					"fill-color": ["get", "extrusion_color"],
					"fill-opacity": 0.12
				}
			});

			state.glMap.addLayer({
				id: EXTRUSION_LAYER_ID,
				type: "fill-extrusion",
				source: SOURCE_ID,
				paint: {
					"fill-extrusion-color": ["get", "extrusion_color"],
					"fill-extrusion-height": ["get", "extrusion_height"],
					"fill-extrusion-base": 0,
					"fill-extrusion-opacity": 0.88,
					"fill-extrusion-vertical-gradient": true
				}
			});

			state.glMap.addLayer({
				id: OUTLINE_LAYER_ID,
				type: "line",
				source: SOURCE_ID,
				paint: {
					"line-color": "#0f172a",
					"line-width": 1.2,
					"line-opacity": 0.7
				}
			});

			applyCachedFloodOverlay();
			return;
		}

		source.setData(state.geojson);
		applyCachedFloodOverlay();
	}

	function applyCachedFloodOverlay() {
		if (!state.floodOverlay) {
			return;
		}
		setFloodOverlay(state.floodOverlay);
	}

	function readFloodImagePixels(img) {
		const canvas = document.createElement("canvas");
		const maxEdge = 96;
		const scale = Math.min(1, maxEdge / Math.max(img.width || 1, img.height || 1));
		const width = Math.max(2, Math.round((img.width || 1) * scale));
		const height = Math.max(2, Math.round((img.height || 1) * scale));
		canvas.width = width;
		canvas.height = height;
		const ctx = canvas.getContext("2d");
		if (!ctx) {
			return null;
		}
		ctx.imageSmoothingEnabled = false;
		ctx.drawImage(img, 0, 0, width, height);
		const sampled = {
			width: width,
			height: height,
			data: ctx.getImageData(0, 0, width, height).data
		};
		fillFloodRasterHoles(sampled, 6);
		return sampled;
	}

	function fillFloodRasterHoles(sampled, passes) {
		const width = sampled.width;
		const height = sampled.height;
		const data = sampled.data;
		const src = new Uint8ClampedArray(data);
		const dst = data;
		const rounds = Math.max(1, passes || 4);

		for (let round = 0; round < rounds; round += 1) {
			if (round > 0) {
				src.set(dst);
			}
			for (let row = 0; row < height; row += 1) {
				for (let col = 0; col < width; col += 1) {
					const idx = (row * width + col) * 4;
					if (src[idx + 3] >= 40) {
						continue;
					}
					let r = 0;
					let g = 0;
					let b = 0;
					let a = 0;
					let n = 0;
					for (let dy = -1; dy <= 1; dy += 1) {
						for (let dx = -1; dx <= 1; dx += 1) {
							if (!dx && !dy) {
								continue;
							}
							const rr = row + dy;
							const cc = col + dx;
							if (rr < 0 || cc < 0 || rr >= height || cc >= width) {
								continue;
							}
							const nidx = (rr * width + cc) * 4;
							if (src[nidx + 3] < 40) {
								continue;
							}
							r += src[nidx];
							g += src[nidx + 1];
							b += src[nidx + 2];
							a += src[nidx + 3];
							n += 1;
						}
					}
					if (n < 3) {
						continue;
					}
					dst[idx] = Math.round(r / n);
					dst[idx + 1] = Math.round(g / n);
					dst[idx + 2] = Math.round(b / n);
					dst[idx + 3] = Math.max(180, Math.round(a / n));
				}
			}
		}
	}

	function interpolateFloodHeights(heights, wet, width, height, passes) {
		const rounds = Math.max(1, passes || 8);
		for (let round = 0; round < rounds; round += 1) {
			const next = heights.slice();
			let changed = false;
			for (let i = 0; i < wet.length; i += 1) {
				if (!wet[i] || heights[i] > 0) {
					continue;
				}
				const row = Math.floor(i / width);
				const col = i - row * width;
				let sum = 0;
				let n = 0;
				for (let dy = -1; dy <= 1; dy += 1) {
					for (let dx = -1; dx <= 1; dx += 1) {
						if (!dx && !dy) {
							continue;
						}
						const rr = row + dy;
						const cc = col + dx;
						if (rr < 0 || cc < 0 || rr >= height || cc >= width) {
							continue;
						}
						const ni = rr * width + cc;
						if (!wet[ni] || !(heights[ni] > 0)) {
							continue;
						}
						sum += heights[ni];
						n += 1;
					}
				}
				if (n < 2) {
					continue;
				}
				next[i] = sum / n;
				changed = true;
			}
			for (let i = 0; i < next.length; i += 1) {
				heights[i] = next[i];
			}
			if (!changed) {
				break;
			}
		}
	}

	function buildFloodCapFeatures(payload, img) {
		const sampled = readFloodImagePixels(img);
		if (!sampled) {
			return [];
		}
		const bounds = payload.leaflet_bounds;
		const south = Number(bounds[0][0]);
		const west = Number(bounds[0][1]);
		const north = Number(bounds[1][0]);
		const east = Number(bounds[1][1]);
		const overlaySettings = getOverlaySurfaceSettings();
		const thickness = Math.max(6, Number(overlaySettings.polygonThickness) || 6);
		const lift = Math.max(0.35, Number(overlaySettings.polygonLift) || 0.35);
		const features = [];
		const cellW = (east - west) / sampled.width;
		const cellH = (north - south) / sampled.height;
		const overlap = 0.12;
		const wet = new Array(sampled.width * sampled.height).fill(false);
		const heights = new Array(sampled.width * sampled.height).fill(0);

		for (let row = 0; row < sampled.height; row += 1) {
			for (let col = 0; col < sampled.width; col += 1) {
				const idx = (row * sampled.width + col) * 4;
				if (sampled.data[idx + 3] < 40) {
					continue;
				}
				const cellIndex = row * sampled.width + col;
				wet[cellIndex] = true;
				const lng = west + (col + 0.5) * cellW;
				const lat = north - (row + 0.5) * cellH;
				const feature = findFeatureAtLngLat({ lng: lng, lat: lat });
				heights[cellIndex] = Number(feature && feature.properties && feature.properties.extrusion_height) || 0;
			}
		}
		interpolateFloodHeights(heights, wet, sampled.width, sampled.height, 10);

		for (let row = 0; row < sampled.height; row += 1) {
			for (let col = 0; col < sampled.width; col += 1) {
				const cellIndex = row * sampled.width + col;
				if (!wet[cellIndex]) {
					continue;
				}
				const idx = cellIndex * 4;
				const padX = cellW * overlap;
				const padY = cellH * overlap;
				const cellWest = west + col * cellW - padX;
				const cellEast = cellWest + cellW + padX * 2;
				const cellNorth = north - row * cellH + padY;
				const cellSouth = cellNorth - cellH - padY * 2;
				const base = heights[cellIndex] + lift;
				features.push({
					type: "Feature",
					geometry: {
						type: "Polygon",
						coordinates: [[
							[cellWest, cellSouth],
							[cellEast, cellSouth],
							[cellEast, cellNorth],
							[cellWest, cellNorth],
							[cellWest, cellSouth]
						]]
					},
					properties: {
						flood_color: getFloodBlackColor(
							sampled.data[idx],
							sampled.data[idx + 1],
							sampled.data[idx + 2]
						),
						surface_base_height: base,
						surface_top_height: base + thickness
					}
				});
			}
		}
		return features;
	}

	function upsertFloodCapLayer(features) {
		if (!state.glMap) {
			return;
		}
		const collection = {
			type: "FeatureCollection",
			features: features || []
		};
		const source = typeof state.glMap.getSource === "function"
			? state.glMap.getSource(FLOOD_CAP_SOURCE_ID)
			: null;
		if (source && typeof source.setData === "function") {
			source.setData(collection);
			if (state.glMap.getLayer && !state.glMap.getLayer(FLOOD_CAP_LAYER_ID)) {
				state.glMap.addLayer({
					id: FLOOD_CAP_LAYER_ID,
					type: "fill-extrusion",
					source: FLOOD_CAP_SOURCE_ID,
					paint: {
						"fill-extrusion-color": ["get", "flood_color"],
						"fill-extrusion-base": ["get", "surface_base_height"],
						"fill-extrusion-height": ["get", "surface_top_height"],
						"fill-extrusion-opacity": 0.92,
						"fill-extrusion-vertical-gradient": false
					}
				});
			} else if (state.glMap.getLayer && state.glMap.getLayer(FLOOD_CAP_LAYER_ID)) {
				if (typeof state.glMap.setPaintProperty === "function") {
					state.glMap.setPaintProperty(FLOOD_CAP_LAYER_ID, "fill-extrusion-opacity", 0.92);
				}
				if (typeof state.glMap.moveLayer === "function") {
					state.glMap.moveLayer(FLOOD_CAP_LAYER_ID);
				}
			}
			return;
		}
		if (state.glMap.getLayer && state.glMap.getLayer(FLOOD_CAP_LAYER_ID)) {
			state.glMap.removeLayer(FLOOD_CAP_LAYER_ID);
		}
		if (source) {
			state.glMap.removeSource(FLOOD_CAP_SOURCE_ID);
		}
		state.glMap.addSource(FLOOD_CAP_SOURCE_ID, {
			type: "geojson",
			data: collection
		});
		state.glMap.addLayer({
			id: FLOOD_CAP_LAYER_ID,
			type: "fill-extrusion",
			source: FLOOD_CAP_SOURCE_ID,
			paint: {
				"fill-extrusion-color": ["get", "flood_color"],
				"fill-extrusion-base": ["get", "surface_base_height"],
				"fill-extrusion-height": ["get", "surface_top_height"],
				"fill-extrusion-opacity": 0.92,
				"fill-extrusion-vertical-gradient": false
			}
		});
	}

	function removeFloodRasterLayer() {
		if (!state.glMap) {
			return;
		}
		if (state.glMap.getLayer && state.glMap.getLayer(FLOOD_LAYER_ID)) {
			state.glMap.removeLayer(FLOOD_LAYER_ID);
		}
		if (state.glMap.getSource && state.glMap.getSource(FLOOD_SOURCE_ID)) {
			state.glMap.removeSource(FLOOD_SOURCE_ID);
		}
	}

	function setFloodOverlay(payload) {
		if (!payload || !payload.png_b64 || !payload.leaflet_bounds) {
			return;
		}
		state.floodOverlay = payload;
		if (!state.enabled || !state.glMap) {
			return;
		}

		const url = "data:image/png;base64," + payload.png_b64;
		const seq = (state.floodOverlaySeq = (state.floodOverlaySeq || 0) + 1);
		const img = new Image();
		img.onload = function () {
			if (seq !== state.floodOverlaySeq) {
				return;
			}
			const apply = function () {
				if (!state.glMap || seq !== state.floodOverlaySeq) {
					return;
				}
				removeFloodRasterLayer();
				upsertFloodCapLayer(buildFloodCapFeatures(payload, img));
			};
			if (typeof state.glMap.isStyleLoaded === "function" && !state.glMap.isStyleLoaded()) {
				state.glMap.once("idle", apply);
				return;
			}
			apply();
		};
		img.onerror = function () {
			console.warn("Không tải được ảnh lớp ngập cho Digital Twin.");
		};
		img.src = url;
	}

	function clearFloodOverlay() {
		state.floodOverlay = null;
		state.floodOverlaySeq = (state.floodOverlaySeq || 0) + 1;
		if (!state.glMap) {
			return;
		}
		try {
			removeFloodRasterLayer();
			if (state.glMap.getLayer && state.glMap.getLayer(FLOOD_CAP_LAYER_ID)) {
				state.glMap.removeLayer(FLOOD_CAP_LAYER_ID);
			}
			if (state.glMap.getSource && state.glMap.getSource(FLOOD_CAP_SOURCE_ID)) {
				state.glMap.removeSource(FLOOD_CAP_SOURCE_ID);
			}
		} catch (_error) {
			/* ignore */
		}
	}

	function syncMapCamera(forceResize) {
		const leafletMap = getLeafletMap();
		if (!state.enabled || !state.glMap || !leafletMap) {
			return;
		}

		const center = leafletMap.getCenter();
		state.glMap.jumpTo({
			center: [center.lng, center.lat],
			zoom: getCompensatedZoom(leafletMap.getZoom(), state.pitch),
			pitch: state.pitch,
			bearing: state.bearing,
			animate: false
		});

		if (forceResize) {
			state.glMap.resize();
		}

		schedulePointOverlayUpdate();
	}

	function attachLeafletMapEvents() {
		const leafletMap = getLeafletMap();
		if (!leafletMap || state.mapEventsAttached) {
			return;
		}

		leafletMap.on("move", syncMapCamera);
		leafletMap.on("zoom", syncMapCamera);
		leafletMap.on("resize", () => syncMapCamera(true));
		leafletMap.on("moveend", scheduleRenderDataReload);
		leafletMap.on("zoomend", scheduleRenderDataReload);
		state.mapEventsAttached = true;
	}

	function scheduleRenderDataReload() {
		if (!state.enabled || state.renderMode !== RENDER_MODES.building) {
			return;
		}

		if (state.renderReloadTimer) {
			clearTimeout(state.renderReloadTimer);
		}

		state.renderReloadTimer = setTimeout(() => {
			state.renderReloadTimer = 0;
			if (!state.enabled || state.renderMode !== RENDER_MODES.building) {
				return;
			}

			void refreshRenderData().then(() => {
				applyRenderSource();
			});
		}, 180);
	}

	function closeSimulatorPopup() {
		const leafletMap = getLeafletMap();
		if (!leafletMap || !state.popup) {
			return;
		}

		try {
			leafletMap.closePopup(state.popup);
		} catch (error) {
			console.warn("Không thể đóng popup 3D:", error);
		}

		state.popup = null;
	}

	function closeRenderModePopup() {
		const leafletMap = getLeafletMap();
		if (!leafletMap || !state.modePopup) {
			return;
		}

		try {
			leafletMap.closePopup(state.modePopup);
		} catch (error) {
			console.warn("Không thể đóng popup chọn dữ liệu 3D:", error);
		}

		state.modePopup = null;
	}

	function buildRenderModePopupContent() {
		const activeMode = state.renderMode === RENDER_MODES.building ? RENDER_MODES.building : RENDER_MODES.population;
		const activeMetric = normalizePopulationMetric(state.populationMetric);
		const currentLabel = getRenderModeLabel();

		function buttonStyle(isActive) {
			return [
				"display:block",
				"width:100%",
				"margin:0",
				"padding:10px 12px",
				"border-radius:10px",
				"border:1px solid " + (isActive ? "#2563eb" : "#d1d5db"),
				"background:" + (isActive ? "linear-gradient(180deg,#2563eb,#1d4ed8)" : "#fff"),
				"color:" + (isActive ? "#fff" : "#0f172a"),
				"font-weight:700",
				"cursor:pointer",
				"text-align:left"
			].join(";");
		}

		return [
			'<div style="min-width:280px;max-width:360px;font-size:13px;line-height:1.45;">',
			'  <div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:8px;">Chọn dữ liệu vẽ 3D</div>',
			'  <div style="padding:8px 10px;margin-bottom:10px;border-radius:10px;background:#f8fafc;border:1px solid #e2e8f0;color:#0f172a;">',
			'    Đang chọn: <b>' + currentLabel + '</b>',
			'  </div>',
			'  <div style="font-weight:700;color:#1e3a8a;margin:6px 0;">Dân số</div>',
			'  <div style="display:grid;grid-template-columns:1fr;gap:6px;">',
			'    <button type="button" data-render-mode="population" data-render-metric="total" style="' + buttonStyle(activeMode === RENDER_MODES.population && activeMetric === POPULATION_METRICS.total) + '">Tổng dân số</button>',
			'    <button type="button" data-render-mode="population" data-render-metric="male" style="' + buttonStyle(activeMode === RENDER_MODES.population && activeMetric === POPULATION_METRICS.male) + '">Dân số nam</button>',
			'    <button type="button" data-render-mode="population" data-render-metric="female" style="' + buttonStyle(activeMode === RENDER_MODES.population && activeMetric === POPULATION_METRICS.female) + '">Dân số nữ</button>',
			'  </div>',
			'  <div style="font-weight:700;color:#1e3a8a;margin:10px 0 6px;">Tòa nhà</div>',
			'  <button type="button" data-render-mode="building" data-render-metric="height" style="' + buttonStyle(activeMode === RENDER_MODES.building) + '">Tòa nhà</button>',
			'  <div style="display:flex;gap:8px;margin-top:12px;">',
			'    <button type="button" data-render-action="cancel" style="flex:1;padding:9px 12px;border-radius:10px;border:1px solid #cbd5e1;background:#fff;color:#0f172a;font-weight:700;cursor:pointer;">Hủy</button>',
			'    <button type="button" data-render-action="apply" style="flex:1;padding:9px 12px;border-radius:10px;border:1px solid #1d4ed8;background:linear-gradient(180deg,#2563eb,#1d4ed8);color:#fff;font-weight:700;cursor:pointer;">Vẽ 3D</button>',
			'  </div>',
			'</div>'
		].join("");
	}

	function applyRenderSelection(selection, options = {}) {
		const nextMode = normalizeRenderMode(selection && selection.mode);
		const nextMetric = normalizePopulationMetric(selection && selection.metric);

		state.renderMode = nextMode;
		state.populationMetric = nextMode === RENDER_MODES.population ? nextMetric : POPULATION_METRICS.total;
		persistRenderSelection();

		if (options && options.autoEnable === false) {
			return Promise.resolve();
		}

		return enableSimulator();
	}

	function showRenderModePopup() {
		const leafletMap = getLeafletMap();
		if (!leafletMap) {
			return;
		}

		closeRenderModePopup();

		state.modePopup = L.popup({
			maxWidth: 380,
			autoClose: true,
			closeOnClick: false,
			className: "render-mode-popup"
		})
			.setLatLng(leafletMap.getCenter())
			.setContent(buildRenderModePopupContent())
			.openOn(leafletMap);

		const bindPopupActions = popupElement => {
			if (!popupElement || popupElement.dataset.renderModeBound === "1") {
				return;
			}

			popupElement.dataset.renderModeBound = "1";

			popupElement.addEventListener("click", event => {
				const target = event.target && typeof event.target.closest === "function"
					? event.target.closest("button[data-render-mode], button[data-render-action]")
					: null;
				if (!target) {
					return;
				}

				if (target.dataset.renderAction === "cancel") {
					closeRenderModePopup();
					return;
				}

				if (target.dataset.renderAction === "apply") {
					event.preventDefault();
					closeRenderModePopup();
					void enableSimulator();
					return;
				}

				if (target.dataset.renderMode) {
					event.preventDefault();
					void applyRenderSelection({
						mode: target.dataset.renderMode,
						metric: target.dataset.renderMetric
					}, { autoEnable: false }).then(() => {
						if (state.modePopup && typeof state.modePopup.setContent === "function") {
							state.modePopup.setContent(buildRenderModePopupContent());
							const refreshedElement = state.modePopup.getElement && state.modePopup.getElement();
							if (refreshedElement) {
								bindPopupActions(refreshedElement);
							}
						}
					});
				}
			});
		};

		bindPopupActions(state.modePopup && typeof state.modePopup.getElement === "function" ? state.modePopup.getElement() : null);

		leafletMap.once("popupopen", event => {
			const popup = event && event.popup ? event.popup : null;
			bindPopupActions(popup && typeof popup.getElement === "function" ? popup.getElement() : null);
		});
	}

	function showFeaturePopup(feature, latlng) {
		const leafletMap = getLeafletMap();
		if (!leafletMap || !feature) {
			return;
		}

		const properties = feature.properties || {};
		const renderLabel = properties.render_label || getRenderModeLabel();
		const xaCode = properties.maxa ? `<div>Mã xã: <b>${properties.maxa}</b></div>` : "";
		const renderValue = Number(properties.render_value || properties.population_total || 0);
		const renderValueLabel = state.renderMode === RENDER_MODES.building
			? "Chiều cao"
			: renderLabel;
		const populationDetails = state.renderMode === RENDER_MODES.population
			? [
				`<div>Tổng dân số: <b>${formatInteger(properties.population_total)}</b> người</div>`,
				`<div>Dân số nam: <b>${formatInteger(properties.population_male)}</b> người</div>`,
				`<div>Dân số nữ: <b>${formatInteger(properties.population_female)}</b> người</div>`
			].join("")
			: `<div>Chiều cao tòa nhà: <b>${formatInteger(renderValue)}</b> m</div>`;

		closeSimulatorPopup();

		state.popup = L.popup({ maxWidth: 280 })
			.setLatLng(latlng)
			.setContent(`
				<div style="font-size:13px;line-height:1.5;min-width:220px;">
					<div style="font-size:14px;font-weight:700;color:#0f172a;margin-bottom:6px;">
						${properties.extrusion_label || properties.name || "Không rõ dữ liệu"}
					</div>
					<div style="margin-bottom:4px;color:#1e3a8a;font-weight:600;">${renderValueLabel}</div>
					${xaCode}
					${populationDetails}
					<div>${state.renderMode === RENDER_MODES.population ? `Mật độ: <b>${formatDensity(properties.population_density)}</b> người/km²` : `Loại dữ liệu: <b>Tòa nhà</b>`}</div>
					<div>Chiều cao giả lập: <b>${formatInteger(properties.extrusion_height)}</b> m</div>
				</div>
			`)
			.openOn(leafletMap);
	}

	function findFeatureAtLngLat(lnglat) {
		if (!global.turf || !state.geojson || !Array.isArray(state.geojson.features)) {
			return null;
		}

		const lng = Number(lnglat && lnglat.lng);
		const lat = Number(lnglat && lnglat.lat);
		if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
			return null;
		}

		const point = turf.point([lng, lat]);

		for (const feature of state.geojson.features) {
			try {
				if (feature && feature.geometry && turf.booleanPointInPolygon(point, feature)) {
					return feature;
				}
			} catch (error) {
				console.warn("Không thể xác định polygon xã:", error);
			}
		}

		return null;
	}

	function findFeatureAtLatLng(latlng) {
		return findFeatureAtLngLat(latlng);
	}

	function getFirstCoordinate(coordinates) {
		if (!Array.isArray(coordinates)) {
			return null;
		}

		if (coordinates.length >= 2 && typeof coordinates[0] === "number" && typeof coordinates[1] === "number") {
			return [Number(coordinates[0]), Number(coordinates[1])];
		}

		for (const candidate of coordinates) {
			const result = getFirstCoordinate(candidate);
			if (result) {
				return result;
			}
		}

		return null;
	}

	function getFeatureRepresentativeLngLat(feature) {
		if (!feature || !feature.geometry) {
			return null;
		}

		const geometryType = feature.geometry.type;
		const coordinates = feature.geometry.coordinates;
		if (geometryType === "Point" && Array.isArray(coordinates) && coordinates.length >= 2) {
			return {
				lng: Number(coordinates[0]),
				lat: Number(coordinates[1])
			};
		}

		if (geometryType === "MultiPoint" && Array.isArray(coordinates) && Array.isArray(coordinates[0])) {
			return {
				lng: Number(coordinates[0][0]),
				lat: Number(coordinates[0][1])
			};
		}

		if (global.turf) {
			try {
				const anchorFeature = typeof turf.pointOnFeature === "function"
					? turf.pointOnFeature(feature)
					: (typeof turf.centroid === "function" ? turf.centroid(feature) : null);

				if (
					anchorFeature &&
					anchorFeature.geometry &&
					Array.isArray(anchorFeature.geometry.coordinates) &&
					anchorFeature.geometry.coordinates.length >= 2
				) {
					return {
						lng: Number(anchorFeature.geometry.coordinates[0]),
						lat: Number(anchorFeature.geometry.coordinates[1])
					};
				}
			} catch (error) {
				console.warn("Không thể tính điểm đại diện cho overlay 3D:", error);
			}
		}

		const fallbackCoordinate = getFirstCoordinate(coordinates);
		if (!fallbackCoordinate) {
			return null;
		}

		return {
			lng: fallbackCoordinate[0],
			lat: fallbackCoordinate[1]
		};
	}

	function getSurfaceHeightAtLngLat(lnglat) {
		if (state.renderMode === RENDER_MODES.building) {
			return getBuildingOverlayReferenceHeight();
		}

		const feature = findFeatureAtLngLat(lnglat);
		return Number(feature && feature.properties && feature.properties.extrusion_height) || 0;
	}

	function decoratePolygonSurfaceFeature(feature) {
		const overlaySettings = getOverlaySurfaceSettings();
		const anchor = getFeatureRepresentativeLngLat(feature);
		const surfaceHeight = getSurfaceHeightAtLngLat(anchor);
		const surfaceBaseHeight = surfaceHeight + overlaySettings.polygonLift;
		const surfaceTopHeight = surfaceBaseHeight + overlaySettings.polygonThickness;

		return {
			...feature,
			properties: {
				...(feature.properties || {}),
				surface_height: surfaceHeight,
				surface_base_height: surfaceBaseHeight,
				surface_top_height: surfaceTopHeight
			}
		};
	}

	function getLineSurfaceBufferMeters(lineWidth) {
		const width = Number(lineWidth) || 1;
		const adaptiveBuffer = OVERLAY_SURFACE_SETTINGS.lineBufferBaseMeters + width * OVERLAY_SURFACE_SETTINGS.lineBufferPerWidth;
		return Math.max(2.5, Math.min(22, adaptiveBuffer));
	}

	function bufferLineFeatureToPolygons(lineFeature, bufferMeters) {
		if (!global.turf || typeof turf.buffer !== "function") {
			return [];
		}

		try {
			const buffered = turf.buffer(lineFeature, bufferMeters, {
				units: "meters",
				steps: 8
			});

			if (!buffered) {
				return [];
			}

			const asFeatures = buffered.type === "FeatureCollection"
				? buffered.features
				: [buffered];

			return asFeatures.filter(feature => {
				const geometryType = feature && feature.geometry ? feature.geometry.type : "";
				return geometryType === "Polygon" || geometryType === "MultiPolygon";
			});
		} catch (error) {
			console.warn("Không thể dựng line surface cho overlay 3D:", error);
			return [];
		}
	}

	function decorateLineSurfaceFeatures(lineFeatures, lineWidth) {
		const overlaySettings = getOverlaySurfaceSettings();
		const lineThickness = Math.max(overlaySettings.lineThickness, (Number(lineWidth) || 1) * 3.2);
		const bufferMeters = getLineSurfaceBufferMeters(lineWidth);
		const output = [];

		lineFeatures.forEach(lineFeature => {
			const anchor = getFeatureRepresentativeLngLat(lineFeature);
			const surfaceHeight = getRoadOverlayReferenceHeight(anchor);
			const surfaceBaseHeight = surfaceHeight + (state.renderMode === RENDER_MODES.building ? 0.05 : overlaySettings.lineLift);
			const surfaceTopHeight = surfaceBaseHeight + (state.renderMode === RENDER_MODES.building ? 0.15 : lineThickness);

			const bufferedPolygons = bufferLineFeatureToPolygons(lineFeature, bufferMeters);
			bufferedPolygons.forEach(polygonFeature => {
				output.push({
					...polygonFeature,
					properties: {
						...(lineFeature.properties || {}),
						...(polygonFeature.properties || {}),
						surface_height: surfaceHeight,
						surface_base_height: surfaceBaseHeight,
						surface_top_height: surfaceTopHeight
					}
				});
			});
		});

		return output;
	}

	function extractPointCoordinates(feature) {
		if (!feature || !feature.geometry) {
			return [];
		}

		if (feature.geometry.type === "Point" && Array.isArray(feature.geometry.coordinates)) {
			return [feature.geometry.coordinates];
		}

		if (feature.geometry.type === "MultiPoint" && Array.isArray(feature.geometry.coordinates)) {
			return feature.geometry.coordinates;
		}

		return [];
	}

	function clearPointOverlayByKey(overlayKey) {
		const overlayEntry = state.pointOverlayEntries[overlayKey];
		if (!overlayEntry) {
			return;
		}

		(overlayEntry.markers || []).forEach(marker => {
			if (marker && marker.element && marker.element.parentNode) {
				marker.element.parentNode.removeChild(marker.element);
			}
		});

		delete state.pointOverlayEntries[overlayKey];
	}

	// ─── Chart Overlay (pie/bar canvas charts positioned above 3D blocks) ────────

	function ensureChartOverlayRoot() {
		const container = ensureContainer();
		if (!container) {
			return null;
		}

		if (state.chartOverlayRoot && state.chartOverlayRoot.parentNode === container) {
			return state.chartOverlayRoot;
		}

		const overlayRoot = document.createElement("div");
		overlayRoot.id = "population-3d-chart-overlay-root";
		overlayRoot.style.position = "absolute";
		overlayRoot.style.inset = "0";
		overlayRoot.style.zIndex = "6";
		overlayRoot.style.pointerEvents = "none";
		overlayRoot.style.overflow = "visible";
		overlayRoot.style.display = "none";

		container.appendChild(overlayRoot);
		state.chartOverlayRoot = overlayRoot;
		return overlayRoot;
	}

	function clearChartOverlayByKey(overlayKey) {
		const entry = state.chartOverlayEntries[overlayKey];
		if (!entry) {
			return;
		}

		if (entry.element && entry.element.parentNode) {
			entry.element.parentNode.removeChild(entry.element);
		}

		delete state.chartOverlayEntries[overlayKey];
	}

	function clearAllChartOverlayEntries() {
		Object.keys(state.chartOverlayEntries).forEach(clearChartOverlayByKey);
	}

	function addChartOverlayEntry(overlayKey, opts) {
		const root = ensureChartOverlayRoot();
		if (!root) {
			return;
		}

		clearChartOverlayByKey(overlayKey);

		const lng = Number(Array.isArray(opts.lnglat) ? opts.lnglat[0] : (opts.lnglat && opts.lnglat.lng));
		const lat = Number(Array.isArray(opts.lnglat) ? opts.lnglat[1] : (opts.lnglat && opts.lnglat.lat));
		if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
			return;
		}

		const surfaceHeight = getSurfaceHeightAtLngLat({ lng, lat });
		const overlaySettings = getOverlaySurfaceSettings();
		const altitude = surfaceHeight + overlaySettings.polygonLift + overlaySettings.polygonThickness;

		const wrapper = document.createElement("div");
		wrapper.style.position = "absolute";
		wrapper.style.left = "0";
		wrapper.style.top = "0";
		wrapper.style.pointerEvents = "none";
		wrapper.style.willChange = "transform, opacity";

		const rawNode = opts.node;
		if (rawNode instanceof Element) {
			wrapper.appendChild(rawNode);
		} else if (typeof rawNode === "string") {
			wrapper.innerHTML = rawNode;
		}

		root.appendChild(wrapper);

		state.chartOverlayEntries[overlayKey] = {
			element: wrapper,
			lng,
			lat,
			altitude,
			anchorX: Number(opts.anchorX) || 0,
			anchorY: Number(opts.anchorY) || 0
		};

		schedulePointOverlayUpdate();
	}

	function updateChartOverlayPositions() {
		const root = state.chartOverlayRoot;
		if (!root) {
			return;
		}

		if (!state.enabled || !state.glMap) {
			root.style.display = "none";
			return;
		}

		root.style.display = "block";

		const viewportWidth = Number(state.container && state.container.clientWidth) || 0;
		const viewportHeight = Number(state.container && state.container.clientHeight) || 0;
		const padding = OVERLAY_SURFACE_SETTINGS.markerViewportPadding * 2;

		Object.keys(state.chartOverlayEntries).forEach(key => {
			const entry = state.chartOverlayEntries[key];
			if (!entry) {
				return;
			}

			const projectedPoint = projectLngLatAtAltitude(entry.lng, entry.lat, entry.altitude);
			if (!projectedPoint || !Number.isFinite(projectedPoint.x) || !Number.isFinite(projectedPoint.y)) {
				entry.element.style.display = "none";
				return;
			}

			const outsideViewport =
				projectedPoint.x < -padding ||
				projectedPoint.x > viewportWidth + padding ||
				projectedPoint.y < -padding ||
				projectedPoint.y > viewportHeight + padding;

			if (outsideViewport) {
				entry.element.style.display = "none";
				return;
			}

			entry.element.style.display = "block";
			entry.element.style.transform =
				`translate(${projectedPoint.x}px, ${projectedPoint.y}px) translate(-${entry.anchorX}px, -${entry.anchorY}px)`;
			entry.element.style.zIndex = String(1000 + Math.round(projectedPoint.y));
		});
	}

	// ─────────────────────────────────────────────────────────────────────────────

	function canRenderElevatedPointMarkers() {
		return Boolean(
			state.glMap &&
			state.glMap.transform &&
			(
				typeof state.glMap.transform.locationToScreenPoint === "function" ||
				typeof state.glMap.transform.coordinatePoint === "function"
			)
		);
	}

	function projectLngLatAtAltitude(lng, lat, altitude) {
		if (!state.glMap) {
			return null;
		}

		const fallbackPoint = typeof state.glMap.project === "function"
			? state.glMap.project([lng, lat])
			: null;

		if (!state.glMap.transform) {
			return fallbackPoint;
		}

		const lngLat =
			typeof maplibregl !== "undefined" &&
			maplibregl.LngLat &&
			typeof maplibregl.LngLat.convert === "function"
				? maplibregl.LngLat.convert([lng, lat])
				: { lng, lat };

		try {
			if (typeof state.glMap.transform.locationToScreenPoint === "function") {
				return state.glMap.transform.locationToScreenPoint(lngLat, {
					getElevationForLngLat: () => altitude
				});
			}

			if (
				typeof state.glMap.transform.coordinatePoint === "function" &&
				typeof maplibregl !== "undefined" &&
				maplibregl.MercatorCoordinate &&
				typeof maplibregl.MercatorCoordinate.fromLngLat === "function"
			) {
				const mercatorCoordinate = maplibregl.MercatorCoordinate.fromLngLat(lngLat, altitude);
				return state.glMap.transform.coordinatePoint(
					mercatorCoordinate,
					altitude,
					state.glMap.transform._pixelMatrix3D
				);
			}
		} catch (error) {
			console.warn("Không thể project point overlay lên mặt 3D:", error);
		}

		return fallbackPoint;
	}

	function createPointMarkerElement(styleHint) {
		const color = String((styleHint && styleHint.pointColor) || (styleHint && styleHint.color) || "#f97316");
		const pointRadius = Number((styleHint && styleHint.pointRadius) || 5);
		const size = Array.isArray(styleHint && styleHint.iconSize)
			? styleHint.iconSize
			: [Math.max(pointRadius * 2 + 6, 14), Math.max(pointRadius * 2 + 6, 14)];

		const markerElement = document.createElement("div");
		markerElement.style.position = "absolute";
		markerElement.style.left = "0";
		markerElement.style.top = "0";
		markerElement.style.pointerEvents = "none";
		markerElement.style.willChange = "transform, opacity";

		if (styleHint && styleHint.iconUrl) {
			markerElement.dataset.anchorMode = "bottom";
			markerElement.style.width = `${Math.max(Number(size[0]) || 0, 12)}px`;
			markerElement.style.height = `${Math.max(Number(size[1]) || 0, 12)}px`;
			markerElement.style.filter = "drop-shadow(0 4px 10px rgba(15, 23, 42, 0.38))";

			const image = document.createElement("img");
			image.src = styleHint.iconUrl;
			image.alt = "";
			image.draggable = false;
			image.style.display = "block";
			image.style.width = "100%";
			image.style.height = "100%";
			image.style.objectFit = "contain";
			image.style.pointerEvents = "none";

			markerElement.appendChild(image);
			return markerElement;
		}

		const diameter = Math.max(pointRadius * 2.4, 10);
		markerElement.dataset.anchorMode = "center";
		markerElement.style.width = `${diameter}px`;
		markerElement.style.height = `${diameter}px`;
		markerElement.style.borderRadius = "999px";
		markerElement.style.background = color;
		markerElement.style.border = "1px solid rgba(15, 23, 42, 0.9)";
		markerElement.style.boxShadow = "0 3px 10px rgba(15, 23, 42, 0.24)";

		return markerElement;
	}

	function updatePointOverlayPositions() {
		state.pointOverlayUpdateFrame = 0;

		const overlayRoot = ensurePointOverlayRoot();
		if (!overlayRoot) {
			return;
		}

		if (!state.enabled || !state.glMap) {
			overlayRoot.style.display = "none";
			return;
		}

		overlayRoot.style.display = "block";

		const viewportWidth = Number(state.container && state.container.clientWidth) || 0;
		const viewportHeight = Number(state.container && state.container.clientHeight) || 0;
		const padding = OVERLAY_SURFACE_SETTINGS.markerViewportPadding;

		Object.keys(state.pointOverlayEntries).forEach(overlayKey => {
			const overlayEntry = state.pointOverlayEntries[overlayKey];
			(overlayEntry && overlayEntry.markers ? overlayEntry.markers : []).forEach(marker => {
				const projectedPoint = projectLngLatAtAltitude(marker.lng, marker.lat, marker.altitude);
				if (!projectedPoint || !Number.isFinite(projectedPoint.x) || !Number.isFinite(projectedPoint.y)) {
					marker.element.style.display = "none";
					return;
				}

				const outsideViewport =
					projectedPoint.x < -padding ||
					projectedPoint.x > viewportWidth + padding ||
					projectedPoint.y < -padding ||
					projectedPoint.y > viewportHeight + padding;

				if (outsideViewport) {
					marker.element.style.display = "none";
					return;
				}

				marker.element.style.display = "block";
				marker.element.style.transform = marker.anchorMode === "bottom"
					? `translate(${projectedPoint.x}px, ${projectedPoint.y}px) translate(-50%, -100%)`
					: `translate(${projectedPoint.x}px, ${projectedPoint.y}px) translate(-50%, -50%)`;
				marker.element.style.zIndex = String(1000 + Math.round(projectedPoint.y));
			});
		});

		updateChartOverlayPositions();
	}

	function schedulePointOverlayUpdate() {
		if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
			updatePointOverlayPositions();
			return;
		}

		if (state.pointOverlayUpdateFrame) {
			return;
		}

		state.pointOverlayUpdateFrame = window.requestAnimationFrame(updatePointOverlayPositions);
	}

	function renderPointOverlayMarkers(overlayKey, features, styleHint) {
		const overlayRoot = ensurePointOverlayRoot();
		if (!overlayRoot) {
			return;
		}

		const markers = [];
		const overlaySettings = getOverlaySurfaceSettings();
		features.forEach(feature => {
			extractPointCoordinates(feature).forEach(coordinates => {
				if (!Array.isArray(coordinates) || coordinates.length < 2) {
					return;
				}

				const lng = Number(coordinates[0]);
				const lat = Number(coordinates[1]);
				if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
					return;
				}

				const markerElement = createPointMarkerElement(styleHint);
				overlayRoot.appendChild(markerElement);

				markers.push({
					element: markerElement,
					lng,
					lat,
					altitude: getSurfaceHeightAtLngLat({ lng, lat }) + overlaySettings.pointLift,
					anchorMode: markerElement.dataset.anchorMode === "center" ? "center" : "bottom"
				});
			});
		});

		state.pointOverlayEntries[overlayKey] = { markers };
		schedulePointOverlayUpdate();
	}

	function handleLeafletMapClick(event) {
		if (!state.enabled) {
			return;
		}

		const feature = findFeatureAtLatLng(event.latlng);
		if (!feature) {
			return;
		}

		showFeaturePopup(feature, event.latlng);
	}

	function attachClickHandler() {
		const leafletMap = getLeafletMap();
		if (!leafletMap || state.clickHandlerAttached) {
			return;
		}

		leafletMap.on("click", handleLeafletMapClick);
		state.clickHandlerAttached = true;
	}

	function detachClickHandler() {
		const leafletMap = getLeafletMap();
		if (!leafletMap || !state.clickHandlerAttached) {
			return;
		}

		leafletMap.off("click", handleLeafletMapClick);
		state.clickHandlerAttached = false;
	}

	function updateButtonState() {
		const toggleButton = getToggleButton();
		if (!toggleButton) {
			return;
		}

		toggleButton.classList.toggle("active", state.enabled);
		toggleButton.disabled = state.loading;
		toggleButton.setAttribute("aria-pressed", state.enabled ? "true" : "false");
	}

	function updateControlInputsFromState() {
		const tiltSlider = getTiltSlider();
		const rotationSlider = getRotationSlider();
		if (tiltSlider) {
			tiltSlider.value = String(Math.round(Number(state.pitch) || 0));
		}
		if (rotationSlider) {
			rotationSlider.value = String(Math.round(Number(state.bearing) || 0));
		}
	}

	function wrapBearing(value) {
		let next = Number(value) || 0;
		while (next > 360) next -= 360;
		while (next < -360) next += 360;
		return next;
	}

	function updateCameraFromControls(applyImmediately) {
		const tiltSlider = getTiltSlider();
		const rotationSlider = getRotationSlider();

		state.pitch = clamp(
			tiltSlider ? (Number(tiltSlider.value) || 0) : (Number(state.pitch) || CAMERA_DEFAULTS.defaultPitch),
			0,
			75
		);
		state.bearing = clamp(
			rotationSlider ? (Number(rotationSlider.value) || 0) : (Number(state.bearing) || CAMERA_DEFAULTS.defaultBearing),
			-360,
			360
		);

		if (applyImmediately) {
			syncMapCamera();
		}
	}

	function attachCtrlTiltHandlers() {
		if (state.ctrlTiltHandlersAttached) {
			return;
		}
		const leafletMap = getLeafletMap();
		if (!leafletMap || typeof leafletMap.getContainer !== "function") {
			return;
		}
		const container = leafletMap.getContainer();
		if (!container) {
			return;
		}

		function finishCtrlTiltDrag() {
			if (!state.ctrlTiltSession) {
				return;
			}
			state.ctrlTiltSession = null;
			container.style.cursor = "";
			try {
				if (leafletMap.dragging && typeof leafletMap.dragging.enable === "function") {
					leafletMap.dragging.enable();
				}
			} catch (_eDragEnable) {
				/* ignore */
			}
			window.removeEventListener("mousemove", onMouseMove, true);
			window.removeEventListener("mouseup", onMouseUp, true);
		}

		function onMouseMove(event) {
			if (!state.ctrlTiltSession) {
				return;
			}
			if (state.ctrlTiltSession.mode === "bearing") {
				const deltaX = Number(event.clientX) - Number(state.ctrlTiltSession.startX);
				state.bearing = wrapBearing(state.ctrlTiltSession.startBearing + deltaX * 0.45);
			} else {
				const deltaY = Number(event.clientY) - Number(state.ctrlTiltSession.startY);
				state.pitch = clamp(state.ctrlTiltSession.startPitch - deltaY * 0.22, 0, 75);
			}
			updateControlInputsFromState();
			syncMapCamera();
			event.preventDefault();
			event.stopPropagation();
		}

		function onMouseUp(event) {
			if (state.ctrlTiltSession) {
				event.preventDefault();
				event.stopPropagation();
			}
			finishCtrlTiltDrag();
		}

		function onMouseDown(event) {
			if (!state.enabled) {
				return;
			}
			if (event.button !== 0 || (!event.ctrlKey && !event.shiftKey)) {
				return;
			}
			const mode = event.shiftKey ? "bearing" : "pitch";
			state.ctrlTiltSession = {
				mode: mode,
				startX: Number(event.clientX) || 0,
				startY: Number(event.clientY) || 0,
				startPitch: Number(state.pitch) || 0,
				startBearing: Number(state.bearing) || 0
			};
			container.style.cursor = mode === "bearing" ? "ew-resize" : "ns-resize";
			try {
				if (leafletMap.dragging && typeof leafletMap.dragging.disable === "function") {
					leafletMap.dragging.disable();
				}
			} catch (_eDragDisable) {
				/* ignore */
			}
			window.addEventListener("mousemove", onMouseMove, true);
			window.addEventListener("mouseup", onMouseUp, true);
			event.preventDefault();
			event.stopPropagation();
		}

		container.addEventListener("mousedown", onMouseDown, true);
		state.ctrlTiltHandlersAttached = true;
	}

	async function refreshRenderData() {
		if (typeof global.getDigitalTwinOverlayFeatureCollection === "function") {
			try {
				const dtwGeojson = global.getDigitalTwinOverlayFeatureCollection();
				if (dtwGeojson && Array.isArray(dtwGeojson.features) && dtwGeojson.features.length) {
					const rawValues = dtwGeojson.features
						.map(feature => Number(feature && feature.properties && (feature.properties.dtw_value_raw || feature.properties.extrusion_height)))
						.filter(value => Number.isFinite(value) && value > 0);
					const minValue = rawValues.length ? Math.min(...rawValues) : 0;
					const maxValue = rawValues.length ? Math.max(...rawValues) : 0;
					state.geojson = dtwGeojson;
					state.stats = {
						minPopulation: minValue,
						maxPopulation: maxValue,
						maxDensity: 0
					};
					return;
				}
			} catch (_eDtwOverlay) {
				/* ignore */
			}
		}

		if (state.renderMode === RENDER_MODES.building) {
			if (typeof loadBuildingsForBbox === "function") {
				try {
					await loadBuildingsForBbox(typeof provduild !== "undefined" ? provduild : undefined);
				} catch (error) {
					console.warn("Không thể tải dữ liệu tòa nhà theo bbox để dựng 3D:", error);
				}
			}

			const rawGeojson = readGeoJson("geojsonBuilding") || readGeoJson("buildingGeoJSON") || readGeoJson("currentGeoJSON");

			const preparedBuilding = prepareBuildingGeoJSON(rawGeojson);
			state.geojson = preparedBuilding.geojson;
			state.stats = preparedBuilding.stats;
			return;
		}

		if (typeof loadPopulationPolygons !== "function") {
			state.geojson = { type: "FeatureCollection", features: [] };
			state.stats = {
				minPopulation: 0,
				maxPopulation: 0,
				maxDensity: 0
			};
			return;
		}

		const rawGeojson = await loadPopulationPolygons();
		const prepared = preparePopulationGeoJSON(rawGeojson, state.populationMetric);
		state.geojson = prepared.geojson;
		state.stats = prepared.stats;
	}

	async function enableSimulator() {
		if (state.loading) {
			return;
		}

		state.loading = true;
		updateButtonState();

		try {
			const tiltSlider = getTiltSlider();
			const rotationSlider = getRotationSlider();

			if (tiltSlider && Number(tiltSlider.value) === 0) {
				tiltSlider.value = String(CAMERA_DEFAULTS.defaultPitch);
			}
			if (!tiltSlider && !(Number(state.pitch) > 0)) {
				state.pitch = CAMERA_DEFAULTS.defaultPitch;
			}
			if (rotationSlider && !rotationSlider.value) {
				rotationSlider.value = String(CAMERA_DEFAULTS.defaultBearing);
			}
			if (!rotationSlider && !Number.isFinite(Number(state.bearing))) {
				state.bearing = CAMERA_DEFAULTS.defaultBearing;
			}

			updateCameraFromControls(false);
			await refreshRenderData();
			await ensureMaplibreMap();

			applyRenderSource();
			applyCachedUrbanOverlays();
			applyCachedFloodOverlay();
			attachLeafletMapEvents();
			attachCtrlTiltHandlers();
			attachClickHandler();

			if (state.container) {
				state.container.style.display = "block";
			}

			state.enabled = true;
			emitSimulatorStateChange();
			syncMapCamera(true);
		} catch (error) {
			console.error("Không thể bật mô phỏng 3D:", error);
			alert("Không thể tải dữ liệu 3D đã chọn.");
		} finally {
			state.loading = false;
			updateButtonState();
		}
	}

	function disableSimulator() {
		state.enabled = false;
		emitSimulatorStateChange();

		clearAllChartOverlayEntries();
		closeRenderModePopup();

		if (state.container) {
			state.container.style.display = "none";
		}

		detachClickHandler();
		closeSimulatorPopup();
		updateButtonState();
	}

	async function toggleSimulator() {
		if (state.enabled) {
			disableSimulator();
			return;
		}

		showRenderModePopup();
	}

	function attachControls() {
		if (state.controlsAttached) {
			return;
		}

		const toggleButton = getToggleButton();
		const tiltSlider = getTiltSlider();
		const rotationSlider = getRotationSlider();

		if (!toggleButton || !tiltSlider || !rotationSlider) {
			return;
		}

		toggleButton.addEventListener("click", () => {
			void toggleSimulator();
		});

		tiltSlider.addEventListener("input", event => {
			state.pitch = clamp(Number(event.target.value) || 0, 0, 75);
			updateControlInputsFromState();
			syncMapCamera();
		});

		rotationSlider.addEventListener("input", event => {
			state.bearing = clamp(Number(event.target.value) || 0, -360, 360);
			updateControlInputsFromState();
			syncMapCamera();
		});

		updateControlInputsFromState();
		updateCameraFromControls(false);
		updateButtonState();
		state.controlsAttached = true;
	}

	function init() {
		if (typeof maplibregl === "undefined") {
			console.error("maplibre-gl chưa được nạp");
			return;
		}

		attachUrbanOverlaySync();
		attachCtrlTiltHandlers();
		attachControls();
	}

	global.population3DSimulator = {
		enable: enableSimulator,
		disable: disableSimulator,
		toggle: toggleSimulator,
		refresh: refreshRenderData,
		openModePopup: showRenderModePopup,
		setSelection: applyRenderSelection,
		getSelectionLabel: getRenderModeLabel,
		isEnabled: () => state.enabled,
		sync: syncMapCamera,
		addChartMarker: addChartOverlayEntry,
		clearChartMarker: clearChartOverlayByKey,
		clearAllChartMarkers: clearAllChartOverlayEntries,
		setFloodOverlay: setFloodOverlay,
		clearFloodOverlay: clearFloodOverlay
	};

	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", init, { once: true });
	} else {
		init();
	}
}(window));

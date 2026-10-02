(function () {
  "use strict";
  const shell = document.querySelector(".app-shell");
  const apiUrl = shell.dataset.apiUrl;
  const paramsUrl = apiUrl.replace(/\/api\/simulate$/, "/api/params");
  const inflowBody = document.getElementById("inflowBody");
  const inflowPanel = document.getElementById("inflowPanel");
  const operationTable = document.getElementById("operationCard") || document.querySelector(".table-card");
  const errorBox = document.getElementById("errorBox");
  const sample = [
    { hour: 0, inflow_m3s: 80 }, { hour: 1, inflow_m3s: 120 },
    { hour: 2, inflow_m3s: 220 }, { hour: 3, inflow_m3s: 480 },
    { hour: 4, inflow_m3s: 760 }, { hour: 5, inflow_m3s: 520 },
    { hour: 6, inflow_m3s: 260 }, { hour: 7, inflow_m3s: 140 },
    { hour: 8, inflow_m3s: 90 }
  ];
  const configInputs = Array.from(document.querySelectorAll("[data-config]"));
  const initialLevelInput = document.querySelector('[data-config="initial_level_m"]');
  const areaInput = document.querySelector('[data-config="storage_area_m2"]');
  const spillwayGateCount = document.getElementById("spillwayGateCount");
  const spillwayGateBody = document.getElementById("spillwayGateBody");
  const spillwayWidthTotal = document.getElementById("spillwayWidthTotal");
  const spillwayWidthLabel = document.getElementById("spillwayWidthLabel");
  let lastResult = null;
  const seriesOn = {
    level: true, hs: true, hbt: true, hdl: true, hp: true,
    inflow: true, total: true, dam: true, turbine: true, spill: true, qmt: true
  };
  function seriesVisible(key) { return seriesOn[key] !== false; }
  function syncLegendState() {
    document.querySelectorAll("#chartLegend [data-series]").forEach((item) => {
      const on = seriesVisible(item.dataset.series);
      item.classList.toggle("off", !on);
      item.setAttribute("aria-pressed", on ? "true" : "false");
      item.title = on ? "Ẩn đường này" : "Hiện đường này";
    });
  }
  let hasRows = [];
  let spillwayGates = [
    { width_m: 20, control: "free", opening_m: 0 },
    { width_m: 20, control: "free", opening_m: 0 }
  ];
  let turbineUnits = [];

  function syncTableHeight() {
    window.requestAnimationFrame(() => {
      if (window.matchMedia("(min-width: 981px)").matches) {
        operationTable.style.height = "";
        return;
      }
      operationTable.style.height = Math.ceil(inflowPanel.getBoundingClientRect().height) + "px";
    });
  }
  function showError(message) { errorBox.textContent = message || "Không chạy được mô phỏng"; errorBox.hidden = false; syncTableHeight(); }
  function clearError() { errorBox.hidden = true; syncTableHeight(); }
  function inflowRow(point) {
    const tr = document.createElement("tr");
    const hour = point && point.hour !== undefined ? point.hour : "";
    const flow = point && point.inflow_m3s !== undefined ? point.inflow_m3s : "";
    tr.innerHTML = '<td class="row-number"></td>' +
      '<td><input data-col="hour" type="number" step="any" value="' + hour + '" aria-label="Giờ"></td>' +
      '<td><input data-col="inflow_m3s" type="number" min="0" step="any" value="' + flow + '" aria-label="Lưu lượng vào"></td>' +
      '<td><button class="delete-inflow-row" type="button" title="Xóa hàng" aria-label="Xóa hàng">×</button></td>';
    return tr;
  }
  function renumberInflowRows() {
    Array.from(inflowBody.rows).forEach((row, index) => {
      row.querySelector(".row-number").textContent = index + 1;
    });
  }
  function addInflowRow(point, focusColumn) {
    const row = inflowRow(point || {});
    inflowBody.appendChild(row);
    renumberInflowRows();
    syncTableHeight();
    if (focusColumn) row.querySelector('[data-col="' + focusColumn + '"]').focus();
    return row;
  }
  function renderInflowTable(points) {
    inflowBody.innerHTML = "";
    (points || []).forEach((point) => addInflowRow(point));
    if (!inflowBody.rows.length) addInflowRow({});
  }
  function readInflowTable() {
    const points = [];
    Array.from(inflowBody.rows).forEach((row, index) => {
      const hourText = row.querySelector('[data-col="hour"]').value.trim();
      const flowText = row.querySelector('[data-col="inflow_m3s"]').value.trim();
      if (!hourText && !flowText) return;
      const hour = Number(hourText);
      const inflow = Number(flowText);
      if (!Number.isFinite(hour) || !Number.isFinite(inflow) || inflow < 0) {
        throw new Error("Hàng " + (index + 1) + " có giờ hoặc lưu lượng không hợp lệ");
      }
      points.push({ hour: hour, inflow_m3s: inflow });
    });
    if (!points.length) throw new Error("Hydrograph cần ít nhất một hàng dữ liệu");
    return points;
  }
  function pasteInflowCells(event) {
    const target = event.target.closest("input[data-col]");
    if (!target) return;
    const text = event.clipboardData && event.clipboardData.getData("text");
    if (!text || !/[\t\r\n,]/.test(text)) return;
    let rows = text.trim().split(/\r?\n/).filter(Boolean).map((line) => {
      return line.split(line.indexOf("\t") >= 0 ? "\t" : ",").map((cell) => cell.trim());
    });
    if (rows.length && rows[0].some((cell) => /hour|inflow|q vào|lưu lượng/i.test(cell))) rows = rows.slice(1);
    if (!rows.length) return;
    event.preventDefault();
    const startRow = Array.from(inflowBody.rows).indexOf(target.closest("tr"));
    const startCol = target.dataset.col === "hour" ? 0 : 1;
    rows.forEach((cells, rowOffset) => {
      const rowIndex = startRow + rowOffset;
      while (inflowBody.rows.length <= rowIndex) addInflowRow({});
      const row = inflowBody.rows[rowIndex];
      if (cells.length >= 2) {
        row.querySelector('[data-col="hour"]').value = cells[0];
        row.querySelector('[data-col="inflow_m3s"]').value = cells[1];
      } else {
        row.querySelector(startCol === 0 ? '[data-col="hour"]' : '[data-col="inflow_m3s"]').value = cells[0];
      }
    });
    clearError();
  }
  function gateControl(value) {
    const mode = String(value || "free").toLowerCase();
    return mode === "controlled" || mode === "closed" ? mode : "free";
  }
  function blankGate(source) {
    const previous = source || spillwayGates[spillwayGates.length - 1] || {};
    return {
      width_m: Number(previous.width_m) > 0 ? Number(previous.width_m) : 20,
      control: gateControl(previous.control),
      opening_m: Math.max(Number(previous.opening_m) || 0, 0)
    };
  }
  function syncSpillwayWidth() {
    const total = spillwayGates.reduce((sum, gate) => sum + (Number(gate.width_m) || 0), 0);
    if (spillwayWidthTotal) spillwayWidthTotal.value = String(total);
    if (spillwayWidthLabel) spillwayWidthLabel.textContent = format(total, 1);
  }
  function renderSpillwayGates() {
    if (!spillwayGateCount || !spillwayGateBody) return;
    const raw = spillwayGateCount.value;
    const count = raw === "" ? 0 : Math.max(0, Math.floor(Number(raw) || 0));
    spillwayGateCount.value = String(count);
    while (spillwayGates.length < count) spillwayGates.push(blankGate());
    spillwayGates = spillwayGates.slice(0, count);
    if (!spillwayGates.length) {
      spillwayGateBody.innerHTML = "<tr><td colspan=\"5\" class=\"empty\">Chưa có cửa xả · rộng tràn = 0 m</td></tr>";
    } else {
      spillwayGateBody.innerHTML = spillwayGates.map((gate, index) => {
        const mode = gateControl(gate.control);
        const opening = mode === "controlled" ? "" : " disabled";
        const option = (value, label) => "<option value=\"" + value + "\"" + (mode === value ? " selected" : "") + ">" + label + "</option>";
        return "<tr><td class=\"row-number\">" + (index + 1) + "</td>" +
          "<td><input data-gate-width type=\"number\" min=\"0\" step=\"0.1\" value=\"" + (Number(gate.width_m) || 0) + "\" aria-label=\"Bề rộng cửa " + (index + 1) + "\"></td>" +
          "<td><select data-gate-control aria-label=\"Chế độ tràn cửa " + (index + 1) + "\">" +
          option("free", "Tự do") + option("controlled", "Có điều khiển") + option("closed", "Đóng") +
          "</select></td>" +
          "<td><input data-gate-opening type=\"number\" min=\"0\" step=\"0.01\" value=\"" + (Number(gate.opening_m) || 0) + "\"" + opening + " aria-label=\"Độ mở cửa " + (index + 1) + "\"></td>" +
          "<td><button class=\"delete-inflow-row\" type=\"button\" title=\"Xóa cửa\" aria-label=\"Xóa cửa\">×</button></td></tr>";
      }).join("");
    }
    syncSpillwayWidth();
    syncTableHeight();
  }
  function addSpillwayGate() {
    if (!spillwayGateCount) return;
    spillwayGateCount.value = String((spillwayGates.length || 0) + 1);
    renderSpillwayGates();
  }
  function removeSpillwayGate(index) {
    if (!spillwayGates.length) return;
    spillwayGates.splice(index, 1);
    if (spillwayGateCount) spillwayGateCount.value = String(spillwayGates.length);
    renderSpillwayGates();
  }
  function readConfig() {
    syncSpillwayWidth();
    syncTurbineCapacity();
    const data = {};
    configInputs.forEach((el) => {
      if (el.dataset.config === "storage_area_m2") {
        data.storage_area_m2 = Number(el.value) * 1e6;
        return;
      }
      if (el.dataset.config === "spillway_width_m") return;
      data[el.dataset.config] = el.type === "number" ? Number(el.value) : el.value;
    });
    data.spillway_width_m = spillwayGates.reduce((sum, gate) => sum + (Number(gate.width_m) || 0), 0);
    data.spillway_gates = spillwayGates.map((gate) => ({
      width_m: Number(gate.width_m) || 0,
      control: gateControl(gate.control),
      opening_m: Math.max(Number(gate.opening_m) || 0, 0)
    }));
    const first = data.spillway_gates[0];
    data.spillway_control = first ? first.control : "closed";
    const controlledGate = data.spillway_gates.find((gate) => gate.control === "controlled");
    data.spillway_opening_m = controlledGate ? controlledGate.opening_m : 0;
    data.installed_mw = turbineUnits.reduce((sum, unit) => sum + (Number(unit.rated_mw) || 0), 0);
    return data;
  }
  function applyConfig(payload) {
    const cfg = (payload && payload.config) || {};
    configInputs.forEach((el) => {
      const key = el.dataset.config;
      if (cfg[key] === undefined || cfg[key] === null) return;
      if (key === "storage_area_m2") {
        el.value = (Number(cfg[key]) / 1e6).toFixed(3);
        return;
      }
      if (key === "spillway_width_m") return;
      el.value = cfg[key];
    });
    const fallbackControl = gateControl(cfg.spillway_control);
    const fallbackOpening = Math.max(Number(cfg.spillway_opening_m) || 0, 0);
    if (Array.isArray(payload.spillway_gates) && payload.spillway_gates.length) {
      spillwayGates = payload.spillway_gates.map((gate) => ({
        width_m: Number(gate && gate.width_m) || 0,
        control: gateControl(gate && gate.control),
        opening_m: Math.max(Number(gate && gate.opening_m) || 0, 0)
      }));
    } else if (Array.isArray(payload.spillway_gates_m)) {
      spillwayGates = payload.spillway_gates_m.map((width) => ({
        width_m: Number(width) || 0,
        control: fallbackControl,
        opening_m: fallbackOpening
      }));
    } else if (Number.isFinite(Number(cfg.spillway_width_m))) {
      const width = Number(cfg.spillway_width_m);
      spillwayGates = width > 0 ? [{ width_m: width, control: fallbackControl, opening_m: fallbackOpening }] : [];
    }
    if (spillwayGateCount) spillwayGateCount.value = String(spillwayGates.length);
    if (Array.isArray(payload.turbine_units)) {
      turbineUnits = payload.turbine_units.map(function (item) {
        return {
          rated_mw: Number(item && item.rated_mw) || 0,
          qmax_m3s: Number(item && item.qmax_m3s) || 0
        };
      });
    } else {
      const count = Math.max(0, Math.floor(Number(cfg.unit_count) || 0));
      const power = Number(cfg.unit_rated_mw) || 0;
      const flow = Number(cfg.unit_qmax_m3s) || 0;
      turbineUnits = Array.from({ length: count }, function () {
        return { rated_mw: power, qmax_m3s: flow };
      });
    }
    const unitCountEl = document.getElementById("turbineUnitCount");
    if (unitCountEl) unitCountEl.value = String(turbineUnits.length);
    renderTurbineUnits();
    if (payload.start_date) {
      const startEl = document.getElementById("operationStartDate");
      if (startEl) startEl.value = String(payload.start_date);
    }
    renderSpillwayGates();
    syncOperationUi();
    syncScheduleButton();
    if (!lastResult) showCurrentFromInitial();
  }
  async function loadSavedParams() {
    try {
      const response = await fetch(paramsUrl, { cache: "no-store", headers: { Accept: "application/json" } });
      const payload = await response.json();
      if (response.ok && payload.ok) applyConfig(payload);
    } catch (error) { /* giu gia tri mac dinh tren form */ }
  }
  async function saveParams() {
    clearError();
    const button = document.getElementById("saveParamsBtn");
    button.disabled = true;
    const prev = button.textContent;
    button.textContent = "Đang lưu…";
    try {
      const response = await fetch(paramsUrl, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          config: readConfig(),
          spillway_gates: spillwayGates.map((gate) => ({
            width_m: Number(gate.width_m) || 0,
            control: gateControl(gate.control),
            opening_m: Math.max(Number(gate.opening_m) || 0, 0)
          })),
          spillway_gates_m: spillwayGates.map((gate) => Number(gate.width_m) || 0),
          turbine_units: turbineUnits.map(function (unit) {
            return { rated_mw: Number(unit.rated_mw) || 0, qmax_m3s: Number(unit.qmax_m3s) || 0 };
          }),
          start_date: startDateValue()
        })
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Không lưu được thông số");
      applyConfig(payload);
      document.getElementById("runStatus").textContent = (payload.synced_construction
        ? "Đã lưu thông số hồ và đồng bộ đập/hồ chứa · "
        : "Đã lưu thông số hồ · ") + new Date().toLocaleTimeString("vi-VN");
    } catch (error) { showError(error.message || String(error)); }
    finally { button.disabled = false; button.textContent = prev || "Lưu thông số"; }
  }
  function interpHAS(level, key) {
    if (hasRows.length < 2 || !Number.isFinite(level)) return null;
    if (level <= hasRows[0].level) return hasRows[0][key];
    const last = hasRows[hasRows.length - 1];
    if (level >= last.level) return last[key];
    for (let i = 1; i < hasRows.length; i++) {
      const lo = hasRows[i - 1];
      const hi = hasRows[i];
      if (level <= hi.level) {
        const span = hi.level - lo.level;
        const t = Math.abs(span) < 1e-12 ? 0 : (level - lo.level) / span;
        return lo[key] + t * (hi[key] - lo[key]);
      }
    }
    return last[key];
  }
  function syncAreaFromInitialLevel() {
    const area = interpHAS(Number(initialLevelInput && initialLevelInput.value), "area");
    if (areaInput && area != null && area > 0) areaInput.value = (area / 1e6).toFixed(3);
    if (!lastResult) showCurrentFromInitial();
  }
  function showCurrentState(level, storageM3, note) {
    const levelEl = document.getElementById("currentLevel");
    const noteEl = document.getElementById("currentLevelNote");
    const storageEl = document.getElementById("currentStorage");
    if (levelEl) levelEl.textContent = Number.isFinite(level) ? format(level, 2) + " m" : "—";
    if (noteEl) noteEl.textContent = note || "—";
    if (storageEl) storageEl.textContent = Number.isFinite(storageM3) ? format(storageM3 / 1e6, 3) : "—";
  }
  function showCurrentFromInitial() {
    const level = Number(initialLevelInput && initialLevelInput.value);
    const storage = interpHAS(level, "storage");
    showCurrentState(level, storage, "H ban đầu");
  }
  async function loadNamInflow() {
    clearError();
    const button = document.getElementById("namBtn");
    button.disabled = true;
    button.textContent = "Đang đọc NAM…";
    try {
      const response = await fetch(apiUrl.replace(/\/api\/simulate$/, "/api/nam-inflow"), { headers: { Accept: "application/json" } });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Không đọc được kết quả MIKE NAM");
      renderInflowTable(payload.inflow);
      document.getElementById("runStatus").textContent = "Đã nạp " + payload.count + " giờ từ MIKE NAM";
    } catch (error) { showError(error.message || String(error)); }
    finally { button.disabled = false; button.textContent = "Nạp Q từ NAM"; }
  }
  function pad2(value) { return String(value).padStart(2, "0"); }
  function startDateValue() {
    const el = document.getElementById("operationStartDate");
    return (el && el.value) || "2024-07-01";
  }
  function operationDateTime(hour) {
    const parts = startDateValue().split("-").map(Number);
    if (parts.length < 3 || !parts[0] || !parts[1] || !parts[2]) return null;
    const dt = new Date(parts[0], parts[1] - 1, parts[2], 0, 0, 0);
    if (Number.isNaN(dt.getTime())) return null;
    dt.setMinutes(dt.getMinutes() + Math.round(Number(hour) * 60));
    return dt;
  }
  function operationDate(hour) {
    const dt = operationDateTime(hour);
    if (!dt) return "";
    return pad2(dt.getDate()) + "/" + pad2(dt.getMonth() + 1) + "/" + dt.getFullYear();
  }
  function operationClockDate(hour) {
    const dt = operationDateTime(hour);
    if (!dt) return "";
    return pad2(dt.getHours()) + ":" + pad2(dt.getMinutes()) + " · " + operationDate(hour);
  }
  function format(value, digits) { return Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits === undefined ? 1 : digits }); }
  function zoneLabel(zone) {
    return ({
      conservation: "Bảo đảm", buffer: "Đệm", flood_control: "Đón lũ", surcharge: "Vượt lũ",
      dead: "H ≤ Hₛ", pre_flood: "Trước lũ", flood_rise: "Lũ lên",
      flood_peak: "Đỉnh lũ", flood_recede: "Lũ xuống", after_flood: "Sau lũ",
      dam_safety: "An toàn đập",
      hydro_stop: "Dừng máy", hydro_store: "Tích nước", hydro_peak: "Cao điểm",
      hydro_flood: "Mùa lũ", hydro_surplus: "Nước thừa",
      supply_dead: "Hết nước", supply_drought: "Hạn", supply_normal: "Cấp đủ",
      supply_surplus: "Nước thừa",
      env_dead: "Hết nước", env_min: "Đảm bảo Qmt", env_surplus: "Lũ / thừa",
      fp_stop: "Dừng máy", fp_store: "Tích / phát", fp_gen: "Cao điểm",
      fp_flood: "Xả lũ + phát", fp_recede: "Lũ xuống", fp_crest: "Đỉnh lũ + phát"
    })[zone] || zone;
  }
  const floodCutLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Hx dâng bình thường",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Qp đảm bảo hạ du",
    normal_release_m3s: "Xả thường",
    minimum_release_m3s: "Xả tối thiểu",
    outlet_capacity_m3s: "Khả năng xả lớn nhất"
  };
  const hydropowerLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Hx dâng bình thường",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Q xả tại H đón lũ",
    normal_release_m3s: "Q định mức",
    minimum_release_m3s: "Qmin phát",
    outlet_capacity_m3s: "Qmax turbine"
  };
  const waterSupplyLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Hbt dâng BT",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Q xả tại H đón lũ",
    normal_release_m3s: "Qc cấp nước",
    minimum_release_m3s: "Qmt môi trường",
    outlet_capacity_m3s: "Qmax xả"
  };
  const environmentalLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Hbt dâng BT",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Qp đảm bảo hạ du",
    normal_release_m3s: "Q dư phát điện",
    minimum_release_m3s: "Qmt môi trường",
    outlet_capacity_m3s: "Qmax xả"
  };
  const floodPowerLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Hx dâng bình thường",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Qp đảm bảo hạ du",
    normal_release_m3s: "Q định mức",
    minimum_release_m3s: "Qmin phát",
    outlet_capacity_m3s: "Qmax xả"
  };
  const defaultLabels = {
    min_level_m: "Hs chết",
    flood_reception_level_m: "H đón lũ",
    conservation_level_m: "Bảo đảm",
    flood_control_level_m: "Hp phòng lũ",
    max_level_m: "Hmax thiết kế",
    flood_release_m3s: "Q xả tại H đón lũ",
    normal_release_m3s: "Xả thường",
    minimum_release_m3s: "Xả tối thiểu",
    outlet_capacity_m3s: "Khả năng xả lớn nhất"
  };
  function syncOperationUi() {
    const mode = (document.querySelector('[data-config="operation_set"]') || {}).value;
    const labels = mode === "flood_control" ? floodCutLabels : mode === "hydropower" ? hydropowerLabels : mode === "water_supply" ? waterSupplyLabels : mode === "environmental" ? environmentalLabels : mode === "flood_power" ? floodPowerLabels : defaultLabels;
    Object.keys(labels).forEach((key) => {
      const label = document.querySelector('[data-label-for="' + key + '"]');
      if (label) label.childNodes[0].textContent = labels[key];
    });
    const qmtItem = document.querySelector('#chartLegend [data-series="qmt"]');
    if (qmtItem) qmtItem.hidden = mode !== "environmental";
    if (lastResult) drawChart(lastResult);
    syncTableHeight();
    syncTurbineCapacity();
  }
  function plainNum(value) {
    const number = Number(value) || 0;
    return Number.isInteger(number) ? String(number) : String(Math.round(number * 1000) / 1000);
  }
  function renderTurbineUnits() {
    const countEl = document.getElementById("turbineUnitCount");
    const body = document.getElementById("turbineUnitBody");
    if (!countEl || !body) return;
    const raw = countEl.value;
    const count = raw === "" ? 0 : Math.max(0, Math.floor(Number(raw) || 0));
    countEl.value = String(count);
    while (turbineUnits.length < count) turbineUnits.push({ rated_mw: 0, qmax_m3s: 0 });
    turbineUnits = turbineUnits.slice(0, count);
    if (!turbineUnits.length) {
      body.innerHTML = "<tr><td colspan=\"4\" class=\"empty\">Nhập số tổ máy để khai từng tổ</td></tr>";
    } else {
      body.innerHTML = turbineUnits.map(function (unit, index) {
        return "<tr><td class=\"row-number\">" + (index + 1) + "</td>" +
          "<td><input data-unit-power type=\"number\" min=\"0\" step=\"0.1\" value=\"" + plainNum(unit.rated_mw) + "\" aria-label=\"Công suất tổ " + (index + 1) + "\"></td>" +
          "<td><input data-unit-qmax type=\"number\" min=\"0\" step=\"0.1\" value=\"" + plainNum(unit.qmax_m3s) + "\" aria-label=\"Q max tổ " + (index + 1) + "\"></td>" +
          "<td><button class=\"delete-inflow-row\" type=\"button\" title=\"Xóa tổ\" aria-label=\"Xóa tổ\">×</button></td></tr>";
      }).join("");
    }
    syncTurbineCapacity();
  }
  let savedOutletCapacity = null;
  function syncTurbineCapacity() {
    const power = turbineUnits.reduce(function (sum, unit) { return sum + (Number(unit.rated_mw) || 0); }, 0);
    const flow = turbineUnits.reduce(function (sum, unit) { return sum + (Number(unit.qmax_m3s) || 0); }, 0);
    const powerLabel = document.getElementById("turbinePowerLabel");
    if (powerLabel) powerLabel.textContent = plainNum(power);
    const outlet = document.querySelector('[data-config="outlet_capacity_m3s"]');
    if (!outlet) return;
    const mode = (document.querySelector('[data-config="operation_set"]') || {}).value;
    if (mode === "hydropower") {
      if (!outlet.readOnly) savedOutletCapacity = outlet.value;
      outlet.value = plainNum(flow);
      outlet.readOnly = true;
      outlet.title = "Tổng Q max của các tổ máy";
      return;
    }
    if (outlet.readOnly) {
      outlet.readOnly = false;
      outlet.title = "";
      if (savedOutletCapacity !== null) {
        outlet.value = savedOutletCapacity;
        savedOutletCapacity = null;
      }
    }
  }
  function drawChart(data) {
    const canvas = document.getElementById("resultChart");
    const box = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    const width = box.clientWidth; const height = box.clientHeight;
    canvas.width = width * ratio; canvas.height = height * ratio;
    const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
    const hours = data.hour; const levels = data.level_m; const flows = data.inflow_m3s;
    const turbines = data.turbine_m3s || [];
    const dams = data.dam_release_m3s || [];
    const spills = data.spill_m3s || [];
    const totals = hours.map((_, i) => Number(dams[i] || 0) + Number(turbines[i] || 0) + Number(spills[i] || 0));
    const mode = (document.querySelector('[data-config="operation_set"]') || {}).value;
    const qmtInput = Number((document.querySelector('[data-config="minimum_release_m3s"]') || {}).value);
    const qmt = mode === "environmental" && Number.isFinite(qmtInput) ? qmtInput : NaN;
    const releases = dams.concat(turbines, spills);
    if (Number.isFinite(qmt)) releases.push(qmt);
    const maxInflow = Math.max.apply(null, flows.concat(totals, [0])) || 1;
    const relMin = releases.length ? Math.min.apply(null, releases) : 0;
    const relMax = releases.length ? Math.max.apply(null, releases) : 0;
    const zoomRelease = mode === "water_supply" || mode === "environmental" || (maxInflow > relMax * 1.4 && relMax > 0);
    const pad = { left: 56, right: zoomRelease ? 100 : 56, top: 12, bottom: 44 };
    const plotW = width - pad.left - pad.right; const plotH = height - pad.top - pad.bottom;
    const x = (i) => pad.left + (hours.length === 1 ? plotW / 2 : i * plotW / (hours.length - 1));
    const cfgLevel = (key) => Number((document.querySelector('[data-config="' + key + '"]') || {}).value);
    const hs = cfgLevel("min_level_m");
    const hdl = cfgLevel("flood_reception_level_m");
    const hbt = cfgLevel("conservation_level_m");
    const hp = cfgLevel("flood_control_level_m");
    const marks = [hs, hdl, hbt, hp].filter((value) => Number.isFinite(value));
    const minLevel = Math.min.apply(null, levels.concat(marks)) - 0.4;
    const maxLevel = Math.max.apply(null, levels.concat(marks)) + 0.4;
    const relSpan = Math.max(relMax - relMin, relMax * 0.08, 8);
    const minRel = zoomRelease ? Math.max(0, relMin - relSpan * 0.25) : 0;
    const maxRel = zoomRelease ? relMax + relSpan * 0.25 : (Math.max(maxInflow, relMax, 1));
    const yLevel = (v) => pad.top + (maxLevel - v) * plotH / Math.max(maxLevel - minLevel, 1e-6);
    const yInflow = (v) => pad.top + plotH - v * plotH / maxInflow;
    const yRelease = (v) => pad.top + (maxRel - v) * plotH / Math.max(maxRel - minRel, 1e-6);
    const midY = pad.top + plotH / 2;
    const axisTitle = (text, ax, color) => {
      ctx.save();
      ctx.translate(ax, midY);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = color;
      ctx.fillText(text, 0, 0);
      ctx.restore();
    };
    ctx.font = "10px DM Mono, monospace"; ctx.fillStyle = "#8b9a99"; ctx.strokeStyle = "#e4ebe6"; ctx.lineWidth = 1;
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + i * plotH / 4;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillStyle = "#8b9a99";
      ctx.fillText(format(maxLevel - i * (maxLevel - minLevel) / 4, 1), pad.left - 7, y);
      if (zoomRelease) {
        ctx.textAlign = "left"; ctx.fillStyle = "#0f172a";
        ctx.fillText(format(maxRel - i * (maxRel - minRel) / 4, 0), width - pad.right + 6, y);
        ctx.textAlign = "right"; ctx.fillStyle = "#f97316";
        ctx.fillText(format(maxInflow - i * maxInflow / 4, 0), width - 16, y);
      } else {
        ctx.textAlign = "left"; ctx.fillStyle = "#f97316";
        ctx.fillText(format(maxRel - i * (maxRel - minRel) / 4, 0), width - pad.right + 6, y);
      }
    }
    ctx.textBaseline = "alphabetic";
    axisTitle("Mực nước (m)", 10, "#087f73");
    if (zoomRelease) {
      axisTitle("Q xả (m³/s)", width - pad.right + 40, "#0f172a");
      axisTitle("Q vào và Tổng xả (m³/s)", width - 7, "#f97316");
    } else {
      axisTitle("Lưu lượng (m³/s)", width - 10, "#f97316");
    }
    const drawLevelMark = (level, color, label) => {
      if (!Number.isFinite(level)) return;
      const y = yLevel(level);
      if (y < pad.top - 2 || y > pad.top + plotH + 2) return;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(label, pad.left + 4, y - 1);
    };
    const drawSeries = (values, y, color, dashed) => { ctx.beginPath(); values.forEach((value, i) => i ? ctx.lineTo(x(i), y(value)) : ctx.moveTo(x(i), y(value))); ctx.strokeStyle = color; ctx.lineWidth = 2.5; ctx.setLineDash(dashed ? [5, 4] : []); ctx.stroke(); ctx.setLineDash([]); };
    if (seriesVisible("hs")) drawLevelMark(hs, "#d97706", "Hs");
    if (seriesVisible("hdl")) drawLevelMark(hdl, "#0284c7", "Hđl");
    if (seriesVisible("hbt")) drawLevelMark(hbt, "#65a30d", "Hbt");
    if (seriesVisible("hp")) drawLevelMark(hp, "#c026d3", "Hp");
    if (mode === "environmental" && seriesVisible("qmt") && Number.isFinite(qmt)) {
      const y = yRelease(qmt);
      if (y >= pad.top - 2 && y <= pad.top + plotH + 2) {
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.strokeStyle = "#059669";
        ctx.lineWidth = 1.6;
        ctx.setLineDash([2, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#059669";
        ctx.textAlign = "right";
        ctx.textBaseline = "bottom";
        ctx.fillText("Qmt " + format(qmt, 0), width - pad.right - 4, y - 1);
      }
    }
    if (seriesVisible("level")) drawSeries(levels, yLevel, "#087f73", false);
    const yQin = zoomRelease ? yInflow : yRelease;
    if (seriesVisible("inflow")) drawSeries(flows, yQin, "#f97316", true);
    if (seriesVisible("total") && totals.length) drawSeries(totals, yQin, "#7c3aed", true);
    if (seriesVisible("dam") && dams.length) drawSeries(dams, yRelease, "#0f172a", false);
    if (seriesVisible("turbine") && turbines.length) drawSeries(turbines, yRelease, "#2563eb", false);
    if (seriesVisible("spill") && spills.length) drawSeries(spills, yRelease, "#e11d48", false);
    ctx.fillStyle = "#8b9a99"; ctx.textBaseline = "alphabetic";
    const step = Math.max(1, Math.ceil(hours.length / 6));
    const ticks = [];
    for (let i = 0; i < hours.length; i += step) ticks.push(i);
    if (hours.length && ticks[ticks.length - 1] !== hours.length - 1) {
      if (hours.length - 1 - ticks[ticks.length - 1] < Math.ceil(step / 2)) ticks[ticks.length - 1] = hours.length - 1;
      else ticks.push(hours.length - 1);
    }
    ticks.forEach((i) => {
      const xPos = x(i);
      ctx.textAlign = xPos < pad.left + 42 ? "left" : xPos > width - pad.right - 42 ? "right" : "center";
      const dt = operationDateTime(hours[i]);
      if (!dt) {
        ctx.fillText("h" + format(hours[i], 0), xPos, height - 8);
        return;
      }
      ctx.fillText(pad2(dt.getHours()) + ":" + pad2(dt.getMinutes()), xPos, height - 22);
      ctx.fillText(pad2(dt.getDate()) + "/" + pad2(dt.getMonth() + 1) + "/" + dt.getFullYear(), xPos, height - 8);
    });
    ctx.textAlign = "left";
  }
  function render(data, summary) {
    lastResult = data; document.getElementById("peakLevel").textContent = format(summary.peak_level_m, 2) + " m";
    document.getElementById("peakHour").textContent = operationClockDate(summary.peak_level_hour) || "—";
    document.getElementById("peakRelease").textContent = format(summary.peak_release_m3s, 1);
    const mode = (document.querySelector('[data-config="operation_set"]') || {}).value;
    const hydro = mode === "hydropower" || mode === "flood_power";
    document.getElementById("powerMetricLabel").textContent = hydro ? "Công suất lớn nhất" : "Xả tràn lớn nhất";
    document.getElementById("peakSpill").textContent = hydro ? format(summary.peak_power_mw || 0, 2) : format(summary.peak_spill_m3s, 1);
    document.getElementById("powerMetricUnit").textContent = hydro ? format(summary.energy_mwh || 0, 1) + " MWh" : "m³/s";
    document.getElementById("finalZone").textContent = zoneLabel(summary.final_zone); document.getElementById("stepCount").textContent = data.hour.length + " bước tính"; document.getElementById("runStatus").textContent = "Đã tính · " + new Date().toLocaleTimeString("vi-VN"); document.getElementById("tableMeta").textContent = data.hour[0] + " — " + data.hour[data.hour.length - 1] + " giờ";
    const last = data.hour.length - 1;
    const lastHour = Number(data.hour[last]);
    const when = operationClockDate(lastHour);
    showCurrentState(Number(data.level_m[last]), Number(data.storage_m3[last]), when || "—");
    const exportBtn = document.getElementById("exportExcelBtn");
    if (exportBtn) exportBtn.disabled = false;
    drawChart(data); fillOperationRows(data);
  }
  function fillOperationRows(data) {
    const body = document.getElementById("resultBody");
    body.innerHTML = data.hour.map((hour, i) => {
      const turbine = Number((data.turbine_m3s || [])[i] || 0);
      const dam = Number((data.dam_release_m3s || [])[i] || 0);
      const spill = Number((data.spill_m3s || [])[i] || 0);
      return "<tr><td>" + format(hour, 0) + "</td><td>" + operationDate(hour) + "</td><td>" + format(data.inflow_m3s[i], 1) + "</td><td>" + format(data.level_m[i], 2) + "</td><td>" + format(data.storage_m3[i] / 1e6, 3) + "</td><td>" + format(dam, 1) + "</td><td>" + format(turbine, 1) + "</td><td>" + format(spill, 1) + "</td><td>" + format(dam + turbine + spill, 1) + "</td><td>" + format((data.power_mw || [])[i] || 0, 2) + "</td><td>" + zoneLabel(data.zone[i]) + "</td></tr>";
    }).join("");
  }
  function csvCell(value) {
    const text = value == null ? "" : String(value);
    if (/[",\n\r]/.test(text)) return '"' + text.replace(/"/g, '""') + '"';
    return text;
  }
  function exportOperationExcel() {
    if (!lastResult || !lastResult.hour || !lastResult.hour.length) {
      showError("Chạy mô phỏng trước khi xuất Excel");
      return;
    }
    const headers = ["Giờ", "Ngày tháng năm", "Inflow", "Mực nước", "S (10⁶ m³)", "Xả qua đập", "Xả qua tuabin", "Tràn", "Tổng xả", "P (MW)", "Vùng"];
    const lines = [headers.join(",")];
    lastResult.hour.forEach((hour, i) => {
      const turbine = Number((lastResult.turbine_m3s || [])[i] || 0);
      const dam = Number((lastResult.dam_release_m3s || [])[i] || 0);
      const spill = Number((lastResult.spill_m3s || [])[i] || 0);
      lines.push([
        Number(hour).toFixed(0),
        operationDate(hour),
        Number(lastResult.inflow_m3s[i]).toFixed(3),
        Number(lastResult.level_m[i]).toFixed(3),
        (Number(lastResult.storage_m3[i]) / 1e6).toFixed(4),
        dam.toFixed(3),
        turbine.toFixed(3),
        spill.toFixed(3),
        (dam + turbine + spill).toFixed(3),
        Number((lastResult.power_mw || [])[i] || 0).toFixed(3),
        zoneLabel(lastResult.zone[i])
      ].map(csvCell).join(","));
    });
    const blob = new Blob(["\uFEFF" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const name = ((document.querySelector('[data-config="name"]') || {}).value || "RS1").replace(/[^\w\-]+/g, "_");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "bang_dieu_hanh_" + name + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }
  async function run() {
    clearError(); const button = document.getElementById("runBtn"); button.disabled = true; button.querySelector("span").textContent = "Đang tính…";
    try { const response = await fetch(apiUrl, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify({ inflow: readInflowTable(), config: readConfig() }) }); const payload = await response.json(); if (!response.ok || !payload.ok) throw new Error(payload.error || "API mô phỏng trả về lỗi"); render(payload.result, payload.summary); } catch (error) { showError(error.message || String(error)); } finally { button.disabled = false; button.querySelector("span").textContent = "Mô phỏng"; }
  }
  let hasHoverIndex = -1;
  let hasChartLayout = null;
  function hideHASTip() {
    const tip = document.getElementById("hasTip");
    if (tip) tip.hidden = true;
  }
  function showHASTip(index, mx, my) {
    const tip = document.getElementById("hasTip");
    const row = hasRows[index];
    const box = document.querySelector(".has-chart-wrap");
    if (!tip || !row || !box) return;
    tip.innerHTML = "<div><b>H</b> " + format(row.level, 2) + " m</div>" +
      "<div class=\"has-tip-a\"><b>A</b> " + format(row.area / 1e6, 3) + " km²</div>" +
      "<div class=\"has-tip-s\"><b>S</b> " + format(row.storage / 1e6, 3) + " 10⁶ m³</div>";
    tip.hidden = false;
    const tw = tip.offsetWidth; const th = tip.offsetHeight;
    const left = Math.min(Math.max(mx + 14, 8), box.clientWidth - tw - 8);
    const top = Math.min(Math.max(my - th - 10, 8), box.clientHeight - th - 8);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function nearestHASIndex(mx, my) {
    if (!hasChartLayout || !hasRows.length) return -1;
    const { pad, width, height, xOf, yA, yS } = hasChartLayout;
    if (mx < pad.left || mx > width - pad.right || my < pad.top || my > height - pad.bottom) return -1;
    let best = -1;
    let bestD = 22 * 22;
    hasRows.forEach((row, i) => {
      const x = xOf(row.level);
      const pts = [yA(row.area / 1e6), yS(row.storage / 1e6)];
      pts.forEach((y) => {
        const d = (mx - x) * (mx - x) + (my - y) * (my - y);
        if (d < bestD) { bestD = d; best = i; }
      });
    });
    return best;
  }
  function drawHASChart(rows, hoverIndex) {
    const canvas = document.getElementById("hasChart");
    if (!canvas) return;
    const box = canvas.parentElement;
    const ratio = window.devicePixelRatio || 1;
    const width = box.clientWidth; const height = box.clientHeight;
    const nextW = Math.max(width, 1) * ratio; const nextH = Math.max(height, 1) * ratio;
    if (canvas.width !== nextW || canvas.height !== nextH) {
      canvas.width = nextW; canvas.height = nextH;
    }
    const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "10px DM Mono, monospace";
    hasChartLayout = null;
    if (!rows || rows.length < 2) {
      ctx.fillStyle = "#8b9a99"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText("Chưa có đường H–A–S", width / 2, height / 2);
      hideHASTip();
      return;
    }
    const levels = rows.map((row) => row.level);
    const areas = rows.map((row) => row.area / 1e6);
    const storages = rows.map((row) => row.storage / 1e6);
    const minH = Math.min.apply(null, levels);
    const maxH = Math.max.apply(null, levels);
    const maxA = Math.max.apply(null, areas.concat([0.01]));
    const maxS = Math.max.apply(null, storages.concat([0.01]));
    const pad = { left: 48, right: 48, top: 10, bottom: 36 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const xOf = (h) => pad.left + (h - minH) * plotW / Math.max(maxH - minH, 1e-6);
    const yA = (a) => pad.top + (maxA - a) * plotH / maxA;
    const yS = (s) => pad.top + (maxS - s) * plotH / maxS;
    hasChartLayout = { pad: pad, width: width, height: height, xOf: xOf, yA: yA, yS: yS };
    ctx.strokeStyle = "#e4ebe6"; ctx.lineWidth = 1; ctx.fillStyle = "#8b9a99"; ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + i * plotH / 4;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillStyle = "#087f73";
      ctx.fillText(format(maxA - i * maxA / 4, 2), pad.left - 6, y);
      ctx.textAlign = "left"; ctx.fillStyle = "#7c3aed";
      ctx.fillText(format(maxS - i * maxS / 4, 1), width - pad.right + 6, y);
    }
    ctx.textAlign = "center"; ctx.fillStyle = "#8b9a99"; ctx.textBaseline = "alphabetic";
    const step = Math.max(1, Math.ceil(levels.length / 6));
    levels.forEach((level, i) => {
      if (i % step === 0 || i === levels.length - 1) ctx.fillText(format(level, 1), xOf(level), height - 18);
    });
    ctx.fillStyle = "#5a6a69";
    ctx.fillText("H (m)", pad.left + plotW / 2, height - 4);
    ctx.save();
    ctx.translate(11, pad.top + plotH / 2); ctx.rotate(-Math.PI / 2);
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = "#087f73";
    ctx.fillText("A (km²)", 0, 0);
    ctx.restore();
    ctx.save();
    ctx.translate(width - 11, pad.top + plotH / 2); ctx.rotate(Math.PI / 2);
    ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = "#7c3aed";
    ctx.fillText("S (10⁶ m³)", 0, 0);
    ctx.restore();
    if (hoverIndex >= 0 && hoverIndex < rows.length) {
      const gx = xOf(levels[hoverIndex]);
      ctx.beginPath(); ctx.moveTo(gx, pad.top); ctx.lineTo(gx, pad.top + plotH);
      ctx.strokeStyle = "#c5d0cb"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]); ctx.stroke(); ctx.setLineDash([]);
    }
    const drawXY = (xs, ys, yMap, color) => {
      ctx.beginPath();
      xs.forEach((x, i) => i ? ctx.lineTo(xOf(x), yMap(ys[i])) : ctx.moveTo(xOf(x), yMap(ys[i])));
      ctx.strokeStyle = color; ctx.lineWidth = 2.4; ctx.setLineDash([]); ctx.stroke();
    };
    drawXY(levels, areas, yA, "#087f73");
    drawXY(levels, storages, yS, "#7c3aed");
    const drawDots = (ys, yMap, color) => {
      levels.forEach((level, i) => {
        const active = i === hoverIndex;
        ctx.beginPath();
        ctx.arc(xOf(level), yMap(ys[i]), active ? 5 : 2.6, 0, Math.PI * 2);
        ctx.fillStyle = "#fff"; ctx.fill();
        ctx.strokeStyle = color; ctx.lineWidth = active ? 2.2 : 1.4; ctx.stroke();
      });
    };
    drawDots(areas, yA, "#087f73");
    drawDots(storages, yS, "#7c3aed");
  }
  function applyHasBed() {
    if (!hasRows.length) return;
    const bed = hasRows.reduce((lowest, row) => Math.min(lowest, row.level), Infinity);
    const input = document.querySelector('[data-config="bed_m"]');
    if (input && isFinite(bed)) input.value = bed.toFixed(2);
  }
  function renderHAS(table) {
    const body = document.getElementById("hasBody");
    const meta = document.getElementById("hasMeta");
    if (!table || !table.level_m || table.level_m.length < 2) {
      body.innerHTML = "<tr><td colspan=\"3\" class=\"empty\">Lập H–A–S từ DEM Sông Hồng để nội suy dung tích</td></tr>";
      meta.textContent = "Chưa lập bảng";
      hasRows = [];
      drawHASChart([]);
      return;
    }
    const rows = table.level_m.map((level, i) => ({
      level: Number(level),
      area: Number(table.area_m2[i] || 0),
      storage: Number(table.storage_m3[i] || 0)
    })).filter((row) => row.area > 0);
    if (rows.length < 2) {
      body.innerHTML = "<tr><td colspan=\"3\" class=\"empty\">Không còn mốc nào có A &gt; 0</td></tr>";
      meta.textContent = "A = 0";
      hasRows = [];
      drawHASChart([]);
      return;
    }
    body.innerHTML = rows.map((row) => {
      return "<tr><td>" + format(row.level, 2) + "</td><td>" + format(row.area / 1e6, 3) + "</td><td>" + format(row.storage / 1e6, 3) + "</td></tr>";
    }).join("");
    hasRows = rows;
    hasHoverIndex = -1;
    hideHASTip();
    drawHASChart(rows, hasHoverIndex);
    const maxA = rows[rows.length - 1].area;
    meta.textContent = rows.length + " mốc · đáy " + format(rows[0].level, 2) + " m · Amax " + format(maxA / 1e6, 2) + " km²";
    applyHasBed();
    syncAreaFromInitialLevel();
  }
  async function loadHAS() {
    try {
      const response = await fetch(apiUrl.replace(/\/api\/simulate$/, "/api/has"), { headers: { Accept: "application/json" } });
      const payload = await response.json();
      if (response.ok && payload.ok) renderHAS(payload.has);
    } catch (error) { /* bang se duoc lap khi bam nut */ }
  }
  async function buildHASFromDem() {
    clearError();
    const button = document.getElementById("hasDemBtn");
    button.disabled = true;
    button.textContent = "Đang lập H–A–S…";
    try {
      const response = await fetch(apiUrl.replace(/\/api\/simulate$/, "/api/has-from-dem"), {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ config: readConfig(), step_m: 0.25 })
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Không lập được H–A–S từ DEM");
      renderHAS(payload.has);
      document.getElementById("runStatus").textContent = "Đã lập H–A–S từ DEM · " + payload.has.level_m.length + " mốc";
    } catch (error) { showError(error.message || String(error)); }
    finally { button.disabled = false; button.textContent = "H–A–S từ DEM"; }
  }
  if (spillwayGateCount) spillwayGateCount.addEventListener("input", renderSpillwayGates);
  const addSpillwayGateBtn = document.getElementById("addSpillwayGateBtn");
  if (addSpillwayGateBtn) addSpillwayGateBtn.addEventListener("click", addSpillwayGate);
  if (spillwayGateBody) spillwayGateBody.addEventListener("click", (event) => {
    const button = event.target.closest(".delete-inflow-row");
    if (!button) return;
    const index = Array.from(spillwayGateBody.rows).indexOf(button.closest("tr"));
    if (index >= 0) removeSpillwayGate(index);
  });
  if (spillwayGateBody) spillwayGateBody.addEventListener("input", (event) => {
    const row = event.target.closest("tr");
    if (!row) return;
    const index = Array.from(spillwayGateBody.rows).indexOf(row);
    if (index < 0 || !spillwayGates[index]) return;
    if (event.target.matches("[data-gate-width]")) {
      spillwayGates[index].width_m = Number(event.target.value) || 0;
      syncSpillwayWidth();
    }
    if (event.target.matches("[data-gate-opening]")) {
      spillwayGates[index].opening_m = Math.max(Number(event.target.value) || 0, 0);
    }
  });
  if (spillwayGateBody) spillwayGateBody.addEventListener("change", (event) => {
    const select = event.target.closest("[data-gate-control]");
    if (!select) return;
    const row = select.closest("tr");
    const index = Array.from(spillwayGateBody.rows).indexOf(row);
    if (index < 0 || !spillwayGates[index]) return;
    spillwayGates[index].control = gateControl(select.value);
    const opening = row.querySelector("[data-gate-opening]");
    if (opening) opening.disabled = spillwayGates[index].control !== "controlled";
  });
  renderSpillwayGates();
  if (initialLevelInput) initialLevelInput.addEventListener("input", syncAreaFromInitialLevel);
  const operationSelect = document.querySelector('[data-config="operation_set"]');
  if (operationSelect) operationSelect.addEventListener("change", syncOperationUi);
  const peakGenerationHours = [9, 10, 11, 12, 17, 18, 19, 20, 21];
  let scheduleDraftMode = "peak";
  let scheduleDraftHours = peakGenerationHours.slice();
  function scheduleModeValue() {
    const el = document.getElementById("generationSchedule");
    return el && el.value === "custom" ? "custom" : "peak";
  }
  function scheduleHourList(raw) {
    const hours = String(raw || "").split(/[,;]/).map((part) => part.trim()).filter(Boolean).map(Number).filter((hour) => Number.isInteger(hour) && hour >= 0 && hour <= 23);
    return [...new Set(hours)].sort((a, b) => a - b);
  }
  function syncScheduleButton() {
    const button = document.getElementById("generationScheduleBtn");
    if (!button) return;
    if (scheduleModeValue() === "custom") {
      const count = scheduleHourList((document.getElementById("generationHours") || {}).value).length;
      button.textContent = "Tùy chọn · " + count + " giờ";
      return;
    }
    button.textContent = "Lịch phát điện";
  }
  function renderScheduleHours() {
    const box = document.getElementById("scheduleHours");
    const note = document.getElementById("scheduleNote");
    if (!box) return;
    const locked = scheduleDraftMode !== "custom";
    const selected = new Set(locked ? peakGenerationHours : scheduleDraftHours);
    if (note) {
      note.textContent = locked
        ? "09–13h và 17–22h. Các giờ này đang được dùng cho phát điện."
        : "Chọn các giờ trong ngày được tính là giờ phát điện.";
    }
    box.innerHTML = Array.from({ length: 24 }, (_, hour) => {
      const on = selected.has(hour);
      return "<button type=\"button\" data-hour=\"" + hour + "\" class=\"" + (on ? "is-on" : "") + "\"" + (locked ? " disabled" : "") + " aria-pressed=\"" + on + "\">" + String(hour).padStart(2, "0") + "</button>";
    }).join("");
  }
  function openScheduleDialog() {
    const dialog = document.getElementById("scheduleDialog");
    if (!dialog) return;
    scheduleDraftMode = scheduleModeValue();
    const saved = scheduleHourList((document.getElementById("generationHours") || {}).value);
    scheduleDraftHours = saved.length ? saved : peakGenerationHours.slice();
    dialog.querySelectorAll("[name=\"scheduleMode\"]").forEach((input) => {
      input.checked = input.value === scheduleDraftMode;
    });
    renderScheduleHours();
    dialog.hidden = false;
  }
  function closeScheduleDialog() {
    const dialog = document.getElementById("scheduleDialog");
    if (dialog) dialog.hidden = true;
  }
  function applyScheduleDialog() {
    const modeEl = document.getElementById("generationSchedule");
    const hoursEl = document.getElementById("generationHours");
    if (modeEl) modeEl.value = scheduleDraftMode === "custom" ? "custom" : "peak";
    if (hoursEl) hoursEl.value = scheduleDraftMode === "custom" ? scheduleDraftHours.join(",") : "";
    syncScheduleButton();
    closeScheduleDialog();
  }
  const generationScheduleBtn = document.getElementById("generationScheduleBtn");
  if (generationScheduleBtn) generationScheduleBtn.addEventListener("click", openScheduleDialog);
  const scheduleDialog = document.getElementById("scheduleDialog");
  if (scheduleDialog) {
    scheduleDialog.addEventListener("click", (event) => {
      if (event.target === scheduleDialog) closeScheduleDialog();
    });
    scheduleDialog.addEventListener("change", (event) => {
      if (!event.target.matches("[name=\"scheduleMode\"]")) return;
      scheduleDraftMode = event.target.value === "custom" ? "custom" : "peak";
      renderScheduleHours();
    });
    const hoursBox = document.getElementById("scheduleHours");
    if (hoursBox) hoursBox.addEventListener("click", (event) => {
      const button = event.target.closest("[data-hour]");
      if (!button || scheduleDraftMode !== "custom") return;
      const hour = Number(button.dataset.hour);
      if (scheduleDraftHours.includes(hour)) scheduleDraftHours = scheduleDraftHours.filter((item) => item !== hour);
      else scheduleDraftHours = scheduleDraftHours.concat(hour).sort((a, b) => a - b);
      renderScheduleHours();
    });
  }
  const scheduleCancel = document.getElementById("scheduleCancel");
  if (scheduleCancel) scheduleCancel.addEventListener("click", closeScheduleDialog);
  const scheduleApply = document.getElementById("scheduleApply");
  if (scheduleApply) scheduleApply.addEventListener("click", applyScheduleDialog);
  syncScheduleButton();
  const turbineUnitCount = document.getElementById("turbineUnitCount");
  if (turbineUnitCount) turbineUnitCount.addEventListener("input", renderTurbineUnits);
  const addTurbineUnitBtn = document.getElementById("addTurbineUnitBtn");
  if (addTurbineUnitBtn) addTurbineUnitBtn.addEventListener("click", function () {
    const el = document.getElementById("turbineUnitCount");
    if (!el) return;
    el.value = String(turbineUnits.length + 1);
    renderTurbineUnits();
  });
  const turbineUnitBody = document.getElementById("turbineUnitBody");
  if (turbineUnitBody) {
    turbineUnitBody.addEventListener("input", function (event) {
      const row = event.target.closest("tr");
      if (!row) return;
      const index = Array.from(turbineUnitBody.rows).indexOf(row);
      if (index < 0 || !turbineUnits[index]) return;
      if (event.target.matches("[data-unit-power]")) turbineUnits[index].rated_mw = Number(event.target.value) || 0;
      if (event.target.matches("[data-unit-qmax]")) turbineUnits[index].qmax_m3s = Number(event.target.value) || 0;
      syncTurbineCapacity();
    });
    turbineUnitBody.addEventListener("click", function (event) {
      const button = event.target.closest(".delete-inflow-row");
      if (!button) return;
      const index = Array.from(turbineUnitBody.rows).indexOf(button.closest("tr"));
      if (index < 0) return;
      turbineUnits.splice(index, 1);
      const el = document.getElementById("turbineUnitCount");
      if (el) el.value = String(turbineUnits.length);
      renderTurbineUnits();
    });
  }
  renderTurbineUnits();
  syncOperationUi();
  const exportExcelBtn = document.getElementById("exportExcelBtn");
  if (exportExcelBtn) exportExcelBtn.addEventListener("click", exportOperationExcel);
  const startDateInput = document.getElementById("operationStartDate");
  if (startDateInput) startDateInput.addEventListener("change", () => {
    if (lastResult) { fillOperationRows(lastResult); drawChart(lastResult); }
  });
  document.getElementById("runBtn").addEventListener("click", run);
  document.getElementById("saveParamsBtn").addEventListener("click", saveParams);
  document.getElementById("hasDemBtn").addEventListener("click", buildHASFromDem);
  document.getElementById("namBtn").addEventListener("click", loadNamInflow);
  document.getElementById("addInflowRowBtn").addEventListener("click", () => addInflowRow({}, "hour"));
  inflowBody.addEventListener("paste", pasteInflowCells);
  inflowBody.addEventListener("click", (event) => {
    const button = event.target.closest(".delete-inflow-row");
    if (!button) return;
    button.closest("tr").remove();
    if (!inflowBody.rows.length) addInflowRow({});
    renumberInflowRows();
    syncTableHeight();
  });
  inflowBody.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches("input[data-col]")) return;
    event.preventDefault();
    const row = event.target.closest("tr");
    const next = row.nextElementSibling || addInflowRow({});
    next.querySelector('[data-col="' + event.target.dataset.col + '"]').focus();
  });
  const chartLegend = document.getElementById("chartLegend");
  if (chartLegend) {
    chartLegend.addEventListener("click", (event) => {
      const item = event.target.closest("[data-series]");
      if (!item) return;
      const key = item.dataset.series;
      seriesOn[key] = !seriesVisible(key);
      syncLegendState();
      if (lastResult) drawChart(lastResult);
    });
    syncLegendState();
  }
  renderInflowTable(sample);
  drawHASChart([]);
  const hasCanvas = document.getElementById("hasChart");
  if (hasCanvas) {
    hasCanvas.addEventListener("mousemove", (event) => {
      const rect = hasCanvas.getBoundingClientRect();
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      const next = nearestHASIndex(mx, my);
      if (next !== hasHoverIndex) {
        hasHoverIndex = next;
        drawHASChart(hasRows, hasHoverIndex);
      }
      if (hasHoverIndex >= 0) showHASTip(hasHoverIndex, mx, my);
      else hideHASTip();
    });
    hasCanvas.addEventListener("mouseleave", () => {
      hasHoverIndex = -1;
      hideHASTip();
      if (hasRows.length) drawHASChart(hasRows, -1);
    });
  }
  loadHAS().then(() => loadSavedParams()).then(() => { applyHasBed(); syncTableHeight(); if (hasRows.length) drawHASChart(hasRows, hasHoverIndex); });
  syncTableHeight();
  window.addEventListener("resize", () => { syncTableHeight(); if (lastResult) drawChart(lastResult); if (hasRows.length) drawHASChart(hasRows, hasHoverIndex); else drawHASChart([]); });
}());
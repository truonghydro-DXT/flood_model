"""Mo phong ho chua theo cach tiep can cua HEC-ResSim.

Mo hinh nay la mo hinh van hanh ho chua theo buoc thoi gian: can bang nuoc,
duong quan he muc nuoc - dung tich, vung van hanh, xa qua cong va tran xa.
CSV dau vao toi thieu can co cac cot ``hour`` va ``inflow_m3s`` (hoac
``q_in_m3s``/``inflow``). Ket qua duoc ghi ra CSV de dung tiep cho mo hinh
thuy luc 1D.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


SECONDS_PER_HOUR = 3600.0
ROOT = Path(__file__).resolve().parent
if str(ROOT.parent) not in sys.path:
	sys.path.insert(0, str(ROOT.parent))
DEFAULT_DEM = ROOT / "projects" / "data" / "dem-song-hong.tif"
DEFAULT_HAS_CSV = ROOT / "construction_input" / "reservoir_has.csv"
GEOM_CSV = ROOT / "saint_venant_output" / "demo_river_geometry.csv"


@dataclass(frozen=True)
class ReservoirConfig:
	"""Thong so ho va cac quy tac dieu hanh chinh."""

	name: str = "RS1"
	operation_set: str = "normal"
	bed_m: float = 8.0
	initial_level_m: float = 12.0
	storage_area_m2: float = 2_500_000.0
	min_level_m: float = 9.0
	flood_reception_level_m: float = 10.5
	conservation_level_m: float = 12.0
	flood_control_level_m: float = 14.0
	max_level_m: float = 18.0
	normal_release_m3s: float = 120.0
	minimum_release_m3s: float = 60.0
	flood_release_m3s: float = 600.0
	outlet_capacity_m3s: float = 1_200.0
	spillway_crest_m: float = 17.0
	spillway_width_m: float = 40.0
	spillway_coefficient: float = 1.838
	dam_crest_m: float = 0.0
	dam_width_m: float = 0.0
	outlet_sill_m: float = 0.0
	gate_left_offset_m: float = 0.0
	gate_spacing_m: float = 0.0
	unit_count: float = 0.0
	unit_rated_mw: float = 0.0
	unit_qmax_m3s: float = 0.0
	installed_mw: float = 0.0
	evaporation_mm_day: float = 0.0
	tailwater_m: float = 0.0
	turbine_efficiency: float = 0.92
	has_file: str = ""
	spillway_control: str = "free"
	spillway_opening_m: float = 0.0
	spillway_orifice_coefficient: float = 0.6
	generation_schedule: str = "peak"
	generation_hours: str = ""
	spillway_gates: tuple[tuple[float, str, float], ...] | None = None

	def validate(self) -> None:
		if self.operation_set not in (
			"normal", "flood_control", "hydropower", "water_supply",
			"environmental", "flood_power",
		):
			raise ValueError("Operation set khong hop le")
		levels = (
			self.bed_m,
			self.min_level_m,
			self.flood_reception_level_m,
			self.conservation_level_m,
			self.flood_control_level_m,
			self.max_level_m,
			self.spillway_crest_m,
		)
		if any(not math.isfinite(value) for value in levels):
			raise ValueError("Cao trinh ho phai la so huu han")
		if not (self.bed_m < self.min_level_m <= self.flood_reception_level_m
				<= self.conservation_level_m <= self.flood_control_level_m
				<= self.max_level_m):
			raise ValueError("Thu tu cao trinh ho khong hop le")
		if self.storage_area_m2 <= 0 or min(self.spillway_width_m, self.dam_width_m) < 0:
			raise ValueError("Dien tich mat ho va be rong tran phai khong am")
		if min(self.normal_release_m3s, self.flood_release_m3s,
			   self.outlet_capacity_m3s, self.spillway_coefficient,
			   self.evaporation_mm_day) < 0:
			raise ValueError("Thong so xa va boc hoi phai khong am")
		if not 0.0 < self.turbine_efficiency <= 1.0:
			raise ValueError("Hieu suat turbine phai trong (0, 1]")
		if min(self.unit_count, self.unit_rated_mw, self.unit_qmax_m3s, self.installed_mw) < 0:
			raise ValueError("So to may, cong suat dinh muc va Q max phai khong am")
		if self.spillway_control not in ("free", "controlled", "closed"):
			raise ValueError("Che do tran khong hop le")
		if self.spillway_opening_m < 0:
			raise ValueError("Do mo tran phai khong am")
		if not 0.0 < self.spillway_orifice_coefficient <= 1.0:
			raise ValueError("He so cua van phai trong (0, 1]")


@dataclass(frozen=True)
class ReservoirPoint:
	hour: float
	inflow_m3s: float


@dataclass
class ReservoirResult:
	hour: list[float]
	inflow_m3s: list[float]
	level_m: list[float]
	storage_m3: list[float]
	release_m3s: list[float]
	turbine_m3s: list[float]
	dam_release_m3s: list[float]
	spill_m3s: list[float]
	evaporation_m3s: list[float]
	zone: list[str]
	power_mw: list[float]


@dataclass
class ReservoirCurve:
	"""Duong quan he cao trinh - dien tich - dung tich - luu luong xa."""

	level_m: list[float]
	area_m2: list[float]
	storage_m3: list[float]
	outlet_m3s: list[float]
	spillway_m3s: list[float]
	total_discharge_m3s: list[float]


@dataclass
class ReservoirHAS:
	"""Bang H–A–S: cao trinh, dien tich mat, dung tich."""

	level_m: list[float] = field(default_factory=list)
	area_m2: list[float] = field(default_factory=list)
	storage_m3: list[float] = field(default_factory=list)
	source: str = ""
	lon: float | None = None
	lat: float | None = None
	cell_m2: float = 0.0
	n_cells: int = 0
	bed_m: float | None = None
	note: str = ""

	def empty(self) -> bool:
		return len(self.level_m) < 2

	def keep_positive_area(self) -> "ReservoirHAS":
		"""Bo cac moc A = 0; chi giu doan da co mat nuoc."""
		kept = [
			(h, a, s)
			for h, a, s in zip(self.level_m, self.area_m2, self.storage_m3)
			if a > 0
		]
		if len(kept) < 2:
			raise ValueError("Bang H-A-S khong con moc nao co A > 0")
		self.level_m, self.area_m2, self.storage_m3 = map(list, zip(*kept))
		self.bed_m = float(self.level_m[0])
		return self


def _interp_series(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
	if not xs or not ys:
		return float("nan")
	if x <= xs[0]:
		return float(ys[0])
	if x >= xs[-1]:
		return float(ys[-1])
	lo, hi = 0, len(xs) - 1
	while hi - lo > 1:
		mid = (lo + hi) // 2
		if xs[mid] <= x:
			lo = mid
		else:
			hi = mid
	span = xs[hi] - xs[lo]
	if abs(span) < 1e-12:
		return float(ys[lo])
	t = (x - xs[lo]) / span
	return float(ys[lo] + t * (ys[hi] - ys[lo]))


def has_bed_m(config: ReservoirConfig | None = None) -> float | None:
	"""H thap nhat trong bang H-A-S (moc co dien tich mat > 0)."""
	table = resolve_has(config or ReservoirConfig())
	if table is None or table.empty():
		return None
	levels = [
		float(level)
		for level, area in zip(table.level_m, table.area_m2)
		if math.isfinite(float(level)) and math.isfinite(float(area)) and float(area) > 0
	]
	if not levels:
		levels = [float(level) for level in table.level_m if math.isfinite(float(level))]
	if not levels:
		return None
	return round(min(levels), 2)


def resolve_has(config: ReservoirConfig, has: ReservoirHAS | None = None) -> ReservoirHAS | None:
	if has is not None and not has.empty():
		return has
	path = Path(config.has_file) if str(config.has_file or "").strip() else DEFAULT_HAS_CSV
	if not path.is_absolute():
		path = ROOT / path
	if path.is_file():
		try:
			return read_has_csv(path)
		except (OSError, ValueError):
			return None
	return None


def area_from_level(level_m: float, config: ReservoirConfig, has: ReservoirHAS | None = None) -> float:
	table = resolve_has(config, has)
	if table is not None and not table.empty():
		return max(_interp_series(float(level_m), table.level_m, table.area_m2), 0.0)
	return max(config.storage_area_m2, 0.0)


def storage_from_level(level_m: float, config: ReservoirConfig, has: ReservoirHAS | None = None) -> float:
	table = resolve_has(config, has)
	if table is not None and not table.empty():
		return max(_interp_series(float(level_m), table.level_m, table.storage_m3), 0.0)
	return max(float(level_m) - config.bed_m, 0.0) * config.storage_area_m2


def level_from_storage(storage_m3: float, config: ReservoirConfig, has: ReservoirHAS | None = None) -> float:
	table = resolve_has(config, has)
	if table is not None and not table.empty():
		return float(_interp_series(max(float(storage_m3), 0.0), table.storage_m3, table.level_m))
	return config.bed_m + max(float(storage_m3), 0.0) / config.storage_area_m2


def operation_zone(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float | None = None,
	prev_inflow_m3s: float | None = None,
	hour: float | None = None,
) -> str:
	"""Phan vung van hanh; phong lu va phat dien dung giai doan rieng."""
	if config.operation_set == "flood_control":
		return _flood_cut_stage(
			level_m, config, inflow_m3s or 0.0, prev_inflow_m3s
		)
	if config.operation_set == "hydropower":
		return _hydropower_stage(level_m, config, inflow_m3s or 0.0, hour)
	if config.operation_set == "water_supply":
		return _water_supply_stage(level_m, config)
	if config.operation_set == "environmental":
		return _environmental_stage(level_m, config, inflow_m3s or 0.0)
	if config.operation_set == "flood_power":
		return _flood_power_stage(
			level_m, config, inflow_m3s or 0.0, prev_inflow_m3s, hour,
		)
	if level_m < config.conservation_level_m:
		return "conservation"
	if level_m < config.flood_control_level_m:
		return "buffer"
	if level_m < config.max_level_m:
		return "flood_control"
	return "surcharge"


def _flood_cut_stage(
	level_m: float,
	config: ReservoirConfig,
	inflow_m3s: float,
	prev_inflow_m3s: float | None,
) -> str:
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	qp = max(config.flood_release_m3s, 0.0)
	qin = max(float(inflow_m3s), 0.0)
	qin_prev = qin if prev_inflow_m3s is None else max(float(prev_inflow_m3s), 0.0)
	if level_m <= hs:
		return "dead"
	if level_m > hp:
		return "dam_safety"
	if level_m > hx:
		return "flood_peak" if qin + 1e-9 >= qin_prev else "flood_recede"
	if qin > qp:
		return "flood_rise"
	return "pre_flood" if qin + 1e-9 >= qin_prev else "after_flood"


def _flood_cut_qh(level_m: float, config: ReservoirConfig) -> float:
	"""Quy tac Q–H cat lu: 0 khi H≤Hs, noi suy den Qp khi Hs<H≤Hx, Qp khi H>Hx."""
	hs = config.min_level_m
	hx = config.conservation_level_m
	qp = max(config.flood_release_m3s, 0.0)
	level = float(level_m)
	if level <= hs:
		return 0.0
	if level >= hx:
		return qp
	return qp * (level - hs) / max(hx - hs, 1e-9)


def _release_toward_level(
	level_m: float,
	target_m: float,
	inflow_m3s: float,
	config: ReservoirConfig,
	*,
	dt_s: float | None = None,
	has: ReservoirHAS | None = None,
	q_min: float = 0.0,
	q_max: float = 0.0,
) -> float:
	"""Q sao cho H tien ve target trong mot buoc, kep trong [q_min, q_max]."""
	qin = max(float(inflow_m3s), 0.0)
	lo = max(float(q_min), 0.0)
	hi = max(float(q_max), lo)
	if dt_s is None or not math.isfinite(float(dt_s)) or float(dt_s) <= 0:
		if level_m > target_m + 0.02:
			return hi
		if level_m < target_m - 0.02:
			return lo
		return min(hi, max(lo, qin))
	s_now = storage_from_level(level_m, config, has)
	s_tgt = storage_from_level(target_m, config, has)
	needed = qin - (s_tgt - s_now) / float(dt_s)
	return min(hi, max(lo, needed))


def _flood_cut_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float = 0.0,
	prev_inflow_m3s: float | None = None,
	prev_release_m3s: float | None = None,
	dt_s: float | None = None,
	has: ReservoirHAS | None = None,
) -> float:
	"""Cat lu tai Qp khi Qin>Qp; sau lu dua H ve Hx (Hbt), an toan dap."""
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	hmax = config.max_level_m
	qp = max(config.flood_release_m3s, 0.0)
	qmin = max(config.minimum_release_m3s, 0.0)
	qcap = max(config.outlet_capacity_m3s, qp)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)
	qin_prev = qin if prev_inflow_m3s is None else max(float(prev_inflow_m3s), 0.0)
	qprev = 0.0 if prev_release_m3s is None else max(float(prev_release_m3s), 0.0)

	if level <= hs:
		return 0.0
	if level >= hmax:
		return qcap
	if level > hp:
		frac = (level - hp) / max(hmax - hp, 1e-9)
		return min(qcap, qp + frac * (qcap - qp))

	if qin > qp:
		if not _spillway_gates_open(config) and level <= hx:
			return min(qin, qmin)
		target = qp
		if qin + 1e-9 < qin_prev and qprev + 1e-9 < target:
			target = min(target, qprev + max(qp * 0.15, 10.0))
		return min(qcap, max(target, 0.0))

	return _release_toward_level(
		level, hx, qin, config,
		dt_s=dt_s, has=has,
		q_min=0.0 if level < hx else qmin,
		q_max=min(qcap, qp if qp > 0 else qcap),
	)


_PEAK_GENERATION_HOURS = frozenset((*range(9, 13), *range(17, 22)))


def _custom_generation_hours(config: ReservoirConfig) -> frozenset[int]:
	hours: set[int] = set()
	for part in str(config.generation_hours or "").replace(";", ",").split(","):
		part = part.strip()
		if not part:
			continue
		try:
			value = int(float(part))
		except ValueError:
			continue
		if 0 <= value <= 23:
			hours.add(value)
	return frozenset(hours)


def _peak_generation_hour(hour: float | None, config: ReservoirConfig | None = None) -> bool:
	"""Gio phat dien trong ngay. Mac dinh 9–13h va 17–22h; tuy chon theo lich 24h."""
	if hour is None or not math.isfinite(float(hour)):
		return False
	slot = int(float(hour) % 24.0)
	if config is not None and str(config.generation_schedule or "peak").strip().lower() == "custom":
		return slot in _custom_generation_hours(config)
	return slot in _PEAK_GENERATION_HOURS


def _hydropower_stage(
	level_m: float,
	config: ReservoirConfig,
	inflow_m3s: float,
	hour: float | None = None,
) -> str:
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	qrated = max(config.normal_release_m3s, 0.0)
	if level_m <= hs:
		return "hydro_stop"
	if level_m > hp:
		return "dam_safety"
	if level_m > hx:
		return "hydro_surplus"
	if inflow_m3s > qrated:
		return "hydro_flood"
	return "hydro_peak" if _peak_generation_hour(hour, config) else "hydro_store"


def _hydropower_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float = 0.0,
	hour: float | None = None,
) -> float:
	"""Van hanh phat dien: giu cot nuoc, phat gio cao diem, dung nuoc thua, an toan dap."""
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	hmax = config.max_level_m
	qmin = max(config.minimum_release_m3s, 0.0)
	qrated = max(config.normal_release_m3s, qmin)
	qmax = max(config.outlet_capacity_m3s, 0.0)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)

	if level <= hs:
		return 0.0
	if level >= hmax:
		return qmax
	if level > hp:
		frac = (level - hp) / max(hmax - hp, 1e-9)
		return min(qmax, qrated + frac * (qmax - qrated))
	if level > hx:
		if not _spillway_gates_open(config):
			return min(qmax, qrated)
		return min(qmax, max(qrated, qin))
	if qin > qrated:
		if not _spillway_gates_open(config):
			return min(qmax, qrated)
		return min(qmax, max(qrated, min(qin, qmax)))
	return _generation_flow(level, qin, config, hour)


def _supply_restrict_level(config: ReservoirConfig) -> float:
	"""Hc: duoi muc nay tiet giam cap nuoc, nam giua Hs va Hbt."""
	hs = config.min_level_m
	hbt = config.conservation_level_m
	return hs + 0.4 * max(hbt - hs, 0.0)


def _supply_qc_at_hour(config: ReservoirConfig, hour: float | None) -> float:
	"""Nhu cau cap nuoc Qc(t): cao diem tuoi, giam ve dem (bang ke hoach thang/ngay)."""
	qc = max(config.normal_release_m3s, 0.0)
	if hour is None or not math.isfinite(float(hour)):
		return qc
	clock = float(hour) % 24.0
	if 6.0 <= clock < 10.0 or 14.0 <= clock < 18.0:
		return qc
	if clock < 5.0 or clock >= 22.0:
		return qc * 0.45
	return qc * 0.7


def _water_supply_stage(level_m: float, config: ReservoirConfig) -> str:
	hs = config.min_level_m
	hc = _supply_restrict_level(config)
	hbt = config.conservation_level_m
	hp = config.flood_control_level_m
	if level_m <= hs:
		return "supply_dead"
	if level_m > hp:
		return "dam_safety"
	if level_m > hbt:
		return "supply_surplus"
	if level_m <= hc:
		return "supply_drought"
	return "supply_normal"


def _water_supply_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float = 0.0,
	hour: float | None = None,
) -> float:
	"""Cap nuoc: Qc(t)+Qmt, tiet giam khi han, xa thua tren Hbt, an toan dap."""
	hs = config.min_level_m
	hc = _supply_restrict_level(config)
	hbt = config.conservation_level_m
	hp = config.flood_control_level_m
	hmax = config.max_level_m
	qc = _supply_qc_at_hour(config, hour)
	qmt = max(config.minimum_release_m3s, 0.0)
	qcap = max(config.outlet_capacity_m3s, qc + qmt)
	demand = min(qc + qmt, qcap)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)

	if level <= hs:
		return 0.0
	if level >= hmax:
		return qcap
	if level > hp:
		frac = (level - hp) / max(hmax - hp, 1e-9)
		return min(qcap, demand + frac * (qcap - demand))
	if level > hbt:
		if not _spillway_gates_open(config):
			return demand
		return min(qcap, max(demand, qin))
	if level <= hc:
		frac = (level - hs) / max(hc - hs, 1e-9)
		return min(demand, qmt + frac * qc)
	return demand


def _environmental_stage(level_m: float, config: ReservoirConfig, inflow_m3s: float) -> str:
	hs = config.min_level_m
	hbt = config.conservation_level_m
	hp = config.flood_control_level_m
	if level_m <= hs:
		return "env_dead"
	if level_m > hp:
		return "dam_safety"
	if level_m > hbt:
		return "env_surplus"
	return "env_min"


def _environmental_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float = 0.0,
	hour: float | None = None,
) -> float:
	"""Dam bao Qmt; gio cao diem cong Q du phat dien; chi vuot Q du khi H > Hp."""
	hs = config.min_level_m
	hbt = config.conservation_level_m
	hp = config.flood_control_level_m
	hmax = config.max_level_m
	qmt = max(config.minimum_release_m3s, 0.0)
	qdu = max(config.normal_release_m3s, qmt)
	qextra = max(qdu - qmt, 0.0)
	qcap = max(config.outlet_capacity_m3s, qdu)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)

	if level <= hs:
		return 0.0
	if level >= hmax:
		return qcap
	if level > hp:
		frac = (level - hp) / max(hmax - hp, 1e-9)
		return min(qcap, qdu + frac * (qcap - qdu))
	if level > hbt:
		if not _spillway_gates_open(config):
			leftover = qextra if _peak_generation_hour(hour, config) else 0.0
			return min(qcap, qmt + leftover)
		return min(qcap, max(qmt, min(qin, qdu)))
	if level < hs + 0.3 * max(hbt - hs, 1e-9):
		return qmt
	leftover = qextra if _peak_generation_hour(hour, config) else 0.0
	return min(qcap, qmt + leftover)


def _turbine_capacity(config: ReservoirConfig) -> float:
	"""Qmax qua turbine: dinh muc, khong dung het cong xa lu."""
	return max(config.normal_release_m3s, config.minimum_release_m3s, 0.0)


def _flood_power_stage(
	level_m: float,
	config: ReservoirConfig,
	inflow_m3s: float,
	prev_inflow_m3s: float | None = None,
	hour: float | None = None,
) -> str:
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	qp = max(config.flood_release_m3s, 0.0)
	qin = max(float(inflow_m3s), 0.0)
	qin_prev = qin if prev_inflow_m3s is None else max(float(prev_inflow_m3s), 0.0)
	if level_m <= hs:
		return "fp_stop"
	if level_m > hp:
		return "dam_safety"
	if level_m > hx or qin > qp:
		if qin + 1e-9 < qin_prev:
			return "fp_recede"
		if qin > qp:
			return "fp_crest"
		return "fp_flood"
	return "fp_gen" if _peak_generation_hour(hour, config) else "fp_store"


def _flood_power_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float = 0.0,
	prev_inflow_m3s: float | None = None,
	prev_release_m3s: float | None = None,
	hour: float | None = None,
) -> float:
	"""Xa lu ket hop phat dien: Q = Qphat + Qxa, cat dinh tai Qp, an toan dap."""
	hs = config.min_level_m
	hx = config.conservation_level_m
	hp = config.flood_control_level_m
	hmax = config.max_level_m
	qrated = _turbine_capacity(config)
	qp = max(config.flood_release_m3s, qrated)
	qcap = max(config.outlet_capacity_m3s, qp)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)
	qin_prev = qin if prev_inflow_m3s is None else max(float(prev_inflow_m3s), 0.0)
	qprev = 0.0 if prev_release_m3s is None else max(float(prev_release_m3s), 0.0)

	if level <= hs:
		return 0.0
	if level >= hmax:
		return qcap
	if level > hp:
		frac = (level - hp) / max(hmax - hp, 1e-9)
		return min(qcap, qp + frac * (qcap - qp))
	if not _spillway_gates_open(config):
		return _generation_flow(level, qin, config, hour)
	if level > hx or qin > qp:
		target = max(qrated, min(max(qin, qrated), qp))
		if level > hx:
			target = qp
		if qin + 1e-9 < qin_prev:
			target = max(qrated, min(qin, qp))
			if qprev + 1e-9 < target:
				target = min(target, qprev + max(qp * 0.15, 10.0))
				target = max(target, qrated)
		return min(qcap, target)
	return _generation_flow(level, qin, config, hour)


def _generation_flow(
	level_m: float,
	inflow_m3s: float,
	config: ReservoirConfig,
	hour: float | None = None,
) -> float:
	"""Q tuabin lien tuc giua Qmin va Qdm theo cot nuoc va Q vao."""
	hs = config.min_level_m
	hx = config.conservation_level_m
	qmin = max(config.minimum_release_m3s, 0.0)
	qrated = _turbine_capacity(config)
	level = float(level_m)
	qin = max(float(inflow_m3s), 0.0)
	if level <= hs:
		return 0.0
	frac = min(max((level - hs) / max(hx - hs, 1e-9), 0.0), 1.0)
	q_head = qmin + frac * (qrated - qmin)
	q_in = min(max(qin, qmin), qrated)
	if _peak_generation_hour(hour, config):
		return min(qrated, 0.35 * q_in + 0.65 * q_head)
	return min(q_head, 0.7 * q_in + 0.3 * qmin)


def _turbine_limit(config: ReservoirConfig) -> float:
	"""Cong suat xa qua turbine; phat dien dung Qmax, cac kich ban khac dung Qdm."""
	if config.operation_set == "hydropower":
		return max(config.outlet_capacity_m3s, 0.0)
	return _turbine_capacity(config)


def _power_release(
	release_m3s: float,
	config: ReservoirConfig,
	*,
	level_m: float,
	inflow_m3s: float = 0.0,
	hour: float | None = None,
) -> float:
	"""Phan qua tuabin: theo Qdm/Qmin va cot nuoc, khong vuot Q xa."""
	q_out = max(float(release_m3s), 0.0)
	if q_out <= 0:
		return 0.0
	q_cap = min(q_out, _turbine_limit(config))
	q_gen = _generation_flow(level_m, inflow_m3s, config, hour)
	if q_gen <= 0:
		return 0.0
	return min(q_cap, q_gen)


def _dam_release(
	release_m3s: float,
	config: ReservoirConfig,
	*,
	level_m: float,
	inflow_m3s: float = 0.0,
	hour: float | None = None,
) -> float:
	"""Phan xa qua dap (cong xa), khong qua turbine."""
	flow = max(float(release_m3s), 0.0)
	return max(flow - _power_release(
		flow, config, level_m=level_m, inflow_m3s=inflow_m3s, hour=hour,
	), 0.0)


def hydropower_mw(level_m: float, turbine_m3s: float, config: ReservoirConfig) -> float:
	"""P = ρ g Q H_net η, don vi MW. H_net = H − H_ha luu."""
	eta = min(max(config.turbine_efficiency, 0.0), 1.0)
	tail = config.tailwater_m if config.tailwater_m else config.bed_m
	head = max(float(level_m) - float(tail), 0.0)
	power = 1000.0 * 9.81 * max(float(turbine_m3s), 0.0) * head * eta / 1e6
	installed = max(float(config.installed_mw), 0.0)
	if installed <= 0.0:
		installed = max(float(config.unit_count), 0.0) * max(float(config.unit_rated_mw), 0.0)
	if installed > 0.0:
		power = min(power, installed)
	return power


def _spillway_gates_open(config: ReservoirConfig) -> bool:
	"""So cua > 0 va tong be rong > 0: cua xa tran dang mo."""
	return max(config.spillway_width_m, 0.0) > 1e-9


def _weir_q(coefficient: float, width_m: float, head_m: float) -> float:
	if head_m <= 0.0 or width_m <= 0.0:
		return 0.0
	return coefficient * width_m * head_m ** 1.5


def _orifice_q(coefficient: float, width_m: float, opening_m: float, head_m: float) -> float:
	"""Q = Cd * B * a * sqrt(2 g H). Cd khong thu nguyen, mac dinh 0.6."""
	if coefficient <= 0.0 or width_m <= 0.0 or opening_m <= 0.0 or head_m <= 0.0:
		return 0.0
	return coefficient * width_m * opening_m * math.sqrt(2.0 * 9.80665 * head_m)


def _controlled_gate_flow(
	level_m: float,
	config: ReservoirConfig,
	*,
	width_m: float | None = None,
	opening_m: float | None = None,
) -> float:
	"""Cua van HEC-RAS / HEC-ResSim.

	H = Z - Z_day. a = do mo.
	H <= a: dap tran Q = C B H^1.5.
	H >= 1.25 a: lo chay Q = Cd B a sqrt(2 g H).
	a < H < 1.25 a: noi suy tuyen tinh hai cong thuc.
	Ha luu ngap (0.67-0.8) thi noi suy lo chay tu do va lo chay ngap.
	"""
	opening = max(
		float(config.spillway_opening_m if opening_m is None else opening_m),
		0.0,
	)
	width = max(
		float(config.spillway_width_m if width_m is None else width_m),
		0.0,
	)
	if opening <= 1e-9 or width <= 1e-9:
		return 0.0
	head = float(level_m) - float(config.outlet_sill_m)
	if head <= 0.0:
		return 0.0
	weir = _weir_q(float(config.spillway_coefficient), width, head)
	orifice = _orifice_q(float(config.spillway_orifice_coefficient), width, opening, head)
	ratio = head / opening
	if ratio <= 1.0:
		flow = weir
	elif ratio >= 1.25:
		flow = orifice
	else:
		weight = (ratio - 1.0) / 0.25
		flow = weir * (1.0 - weight) + orifice * weight
	tail = float(config.tailwater_m)
	sill = float(config.outlet_sill_m)
	if tail <= sill or head <= 0.0:
		return flow
	submergence = (tail - sill) / head
	if submergence >= 1.0:
		return 0.0
	if submergence <= 0.67:
		return flow
	downstream_head = max(float(level_m) - tail, 0.0)
	free = _orifice_q(float(config.spillway_orifice_coefficient), width, opening, 3.0 * downstream_head)
	submerged = _orifice_q(float(config.spillway_orifice_coefficient), width, opening, downstream_head)
	if submergence >= 0.8:
		return submerged
	weight = (submergence - 0.67) / 0.13
	return free * (1.0 - weight) + submerged * weight


def _spillway_from_gates(level_m: float, config: ReservoirConfig) -> float:
	"""Tong Q tran khi moi cua co che do rieng: tu do, dieu khien, dong."""
	level = float(level_m)
	sill = float(config.outlet_sill_m)
	crest = float(config.spillway_crest_m)
	coefficient = float(config.spillway_coefficient)
	dam_width = max(float(config.dam_width_m), 0.0)
	gates = config.spillway_gates or ()
	flow = 0.0
	add_dam = False

	def sill_weir(width: float) -> float:
		if level <= sill or width <= 1e-9:
			return 0.0
		return _weir_q(coefficient, width, level - sill)

	def crest_weir(width: float) -> float:
		if level < crest or width <= 1e-9:
			return 0.0
		return _weir_q(coefficient, width, level - crest)

	for width, control, opening in gates:
		bay = max(float(width), 0.0)
		mode = str(control or "free").strip().lower()
		gap = max(float(opening), 0.0)
		if bay <= 1e-9:
			continue
		if mode == "closed":
			flow += crest_weir(bay)
			continue
		full = crest - sill
		if mode == "controlled" and not (full > 1e-9 and gap + 1e-4 >= full):
			flow += _controlled_gate_flow(level, config, width_m=bay, opening_m=gap)
			if level > crest:
				add_dam = True
			continue
		if level > crest:
			if mode == "controlled":
				flow += sill_weir(bay)
			add_dam = True
			continue
		flow += sill_weir(bay)
	if add_dam and level > crest:
		flow += _weir_q(coefficient, dam_width, level - crest)
	return flow


def _spillway_flow(level_m: float, config: ReservoirConfig) -> float:
	"""Q tran.

	Tu do:
	  Z_day < Z <= Z_dinh: Q = C B_cua (Z - Z_day)^1.5.
	  Z > Z_dinh: Q = C B_dap (Z - Z_dinh)^1.5.
	Dong het:
	  Z < Z_dinh tran: Q = 0.
	  Z >= Z_dinh tran: tran dinh HEC-ResSim Q = C L H^1.5,
	  H = Z - Z_dinh tran, L = tong be rong cua tran.
	Co dieu khien: cong thuc cua van HEC-ResSim, cong them tran qua dinh dap.
	Do mo = 0: khong co dong qua cua.
	Do mo = Z_dinh - Z_day: chay tu do, Q = Q cua + Q dap.
	Moi cua co the dung mot che do rieng.
	"""
	if config.spillway_gates is not None:
		return _spillway_from_gates(level_m, config)
	level = float(level_m)
	sill = float(config.outlet_sill_m)
	crest = float(config.spillway_crest_m)
	coefficient = float(config.spillway_coefficient)
	gate_width = max(float(config.spillway_width_m), 0.0)
	dam_width = max(float(config.dam_width_m), 0.0)
	control = str(config.spillway_control or "free").strip().lower()
	opening = max(float(config.spillway_opening_m), 0.0)
	full_opening = crest - sill

	def dam_overflow() -> float:
		if level <= crest:
			return 0.0
		return _weir_q(coefficient, dam_width, level - crest)

	def gate_from_sill() -> float:
		if level <= sill or not _spillway_gates_open(config):
			return 0.0
		return _weir_q(coefficient, gate_width, level - sill)

	def free_spill() -> float:
		if level > crest:
			return gate_from_sill() + dam_overflow()
		if level > sill and _spillway_gates_open(config):
			return gate_from_sill()
		return 0.0

	if control == "closed":
		if level < crest:
			return 0.0
		length = gate_width if gate_width > 1e-9 else dam_width
		return _weir_q(coefficient, length, level - crest)
	if control != "controlled":
		if level > crest:
			return dam_overflow()
		if level > sill and _spillway_gates_open(config):
			return gate_from_sill()
		return 0.0
	if full_opening > 1e-9 and opening + 1e-4 >= full_opening:
		return free_spill()
	return _controlled_gate_flow(level, config) + dam_overflow()


def _startup_turbine_release(
	release_m3s: float,
	config: ReservoirConfig,
	*,
	level_m: float,
	inflow_m3s: float,
	hour: float | None,
) -> float:
	"""Bao dam Q tuabin theo cot nuoc khi ho con tren muc chet.

	Cat lu dung o moi gio: ho van phat dien khi H < Hx va Qin chua vuot Qp.
	Ba kich ban con lai dung cho moc dau.
	"""
	if config.operation_set not in (
		"normal", "flood_control", "hydropower", "flood_power",
	):
		return max(float(release_m3s), 0.0)
	level = float(level_m)
	if level <= config.min_level_m:
		return max(float(release_m3s), 0.0)
	generated = _generation_flow(level, inflow_m3s, config, hour)
	if generated <= 0.0:
		return max(float(release_m3s), 0.0)
	plant = min(generated, _turbine_limit(config))
	return max(float(release_m3s), plant, 0.0)


def _controlled_release(
	level_m: float,
	config: ReservoirConfig,
	*,
	inflow_m3s: float | None = None,
	prev_inflow_m3s: float | None = None,
	prev_release_m3s: float | None = None,
	hour: float | None = None,
	dt_s: float | None = None,
	has: ReservoirHAS | None = None,
) -> float:
	"""Luu luong xa lien tuc; phong lu va phat dien dung quy tac rieng."""
	if config.operation_set == "flood_control":
		if inflow_m3s is None:
			release = min(_flood_cut_qh(level_m, config), config.outlet_capacity_m3s)
		else:
			release = _flood_cut_release(
				level_m,
				config,
				inflow_m3s=inflow_m3s,
				prev_inflow_m3s=prev_inflow_m3s,
				prev_release_m3s=prev_release_m3s,
				dt_s=dt_s,
				has=has,
			)
		return _startup_turbine_release(
			release,
			config,
			level_m=level_m,
			inflow_m3s=inflow_m3s or 0.0,
			hour=hour,
		)
	if config.operation_set == "hydropower":
		return _hydropower_release(
			level_m, config, inflow_m3s=inflow_m3s or 0.0, hour=hour,
		)
	if config.operation_set == "water_supply":
		return _water_supply_release(
			level_m, config, inflow_m3s=inflow_m3s or 0.0, hour=hour,
		)
	if config.operation_set == "environmental":
		return _environmental_release(
			level_m, config, inflow_m3s=inflow_m3s or 0.0, hour=hour,
		)
	if config.operation_set == "flood_power":
		return _flood_power_release(
			level_m, config,
			inflow_m3s=inflow_m3s or 0.0,
			prev_inflow_m3s=prev_inflow_m3s,
			prev_release_m3s=prev_release_m3s,
			hour=hour,
		)
	level = float(level_m)
	if level < config.conservation_level_m:
		target = config.normal_release_m3s * max(
			(level - config.min_level_m)
			/ max(config.conservation_level_m - config.min_level_m, 1e-9),
			0.0,
		)
	elif level < config.flood_control_level_m:
		fraction = (level - config.conservation_level_m) / max(
			config.flood_control_level_m - config.conservation_level_m, 1e-9
		)
		target = config.normal_release_m3s + fraction * (
			config.flood_release_m3s - config.normal_release_m3s
		)
	else:
		fraction = (level - config.flood_control_level_m) / max(
			config.max_level_m - config.flood_control_level_m, 1e-9
		)
		target = config.flood_release_m3s + fraction * (
			config.outlet_capacity_m3s - config.flood_release_m3s
		)
	return min(max(target, config.minimum_release_m3s), config.outlet_capacity_m3s)


def reservoir_curve(
	config: ReservoirConfig | None = None,
	*,
	step_m: float = 0.25,
) -> ReservoirCurve:
	"""Tao duong H-V-Q dung voi cung quy tac cua mo phong.

	``outlet_m3s`` la xa qua cong theo vung van hanh; ``spillway_m3s`` la
	luu luong tran. ``total_discharge_m3s`` la tong hai thanh phan.
	"""
	cfg = config or ReservoirConfig()
	cfg.validate()
	if not math.isfinite(step_m) or step_m <= 0:
		raise ValueError("Buoc cao trinh phai > 0")
	table = resolve_has(cfg)
	if table is not None and not table.empty():
		levels = [float(v) for v in table.level_m]
	else:
		levels = []
		level = cfg.bed_m
		while level < cfg.max_level_m - 1e-9:
			levels.append(level)
			level += step_m
		levels.append(cfg.max_level_m)
	outlet = [_controlled_release(value, cfg) for value in levels]
	spill = [_spillway_flow(value, cfg) for value in levels]
	return ReservoirCurve(
		level_m=levels,
		area_m2=[area_from_level(value, cfg, table) for value in levels],
		storage_m3=[storage_from_level(value, cfg, table) for value in levels],
		outlet_m3s=outlet,
		spillway_m3s=spill,
		total_discharge_m3s=[a + b for a, b in zip(outlet, spill)],
	)


def simulate_reservoir(
	inflow: Sequence[ReservoirPoint] | Iterable[ReservoirPoint],
	config: ReservoirConfig | None = None,
	has: ReservoirHAS | None = None,
) -> ReservoirResult:
	"""Chay mo phong ho bang phuong trinh S2 = S1 + (Qin-Qout)dt.

	``hour`` phai tang dan va co the khong cach deu; moi doan dung khoang
	thoi gian giua hai moc lien tiep. Giu lai moc dau tien trong ket qua.
	"""
	cfg = config or ReservoirConfig()
	cfg.validate()
	table = resolve_has(cfg, has)
	points = list(inflow)
	if not points:
		raise ValueError("Chuoi luu luong vao dang rong")
	if any(not math.isfinite(p.hour) or not math.isfinite(p.inflow_m3s)
		   or p.inflow_m3s < 0 for p in points):
		raise ValueError("Du lieu inflow phai huu han va khong am")
	if any(points[index].hour <= points[index - 1].hour
		   for index in range(1, len(points))):
		raise ValueError("Cot hour phai tang dan")

	level = min(max(cfg.initial_level_m, cfg.min_level_m), cfg.max_level_m)
	storage = storage_from_level(level, cfg, table)
	release = _startup_turbine_release(
		_controlled_release(
			level, cfg, inflow_m3s=points[0].inflow_m3s,
			prev_inflow_m3s=points[0].inflow_m3s, prev_release_m3s=0.0,
			hour=points[0].hour, has=table,
		),
		cfg,
		level_m=level,
		inflow_m3s=points[0].inflow_m3s,
		hour=points[0].hour,
	)
	result = ReservoirResult([], [], [], [], [], [], [], [], [], [], [])
	result.hour.append(points[0].hour)
	result.inflow_m3s.append(points[0].inflow_m3s)
	result.level_m.append(level)
	result.storage_m3.append(storage)
	result.release_m3s.append(release)
	result.turbine_m3s.append(_power_release(
		release, cfg, level_m=level, inflow_m3s=points[0].inflow_m3s,
		hour=points[0].hour,
	))
	result.dam_release_m3s.append(_dam_release(
		release, cfg, level_m=level, inflow_m3s=points[0].inflow_m3s,
		hour=points[0].hour,
	))
	result.spill_m3s.append(_spillway_flow(level, cfg))
	result.evaporation_m3s.append(0.0)
	result.zone.append(operation_zone(
		level, cfg, inflow_m3s=points[0].inflow_m3s,
		prev_inflow_m3s=points[0].inflow_m3s, hour=points[0].hour,
	))
	result.power_mw.append(hydropower_mw(level, _power_release(
		release, cfg, level_m=level, inflow_m3s=points[0].inflow_m3s,
		hour=points[0].hour,
	), cfg))

	for previous, current in zip(points, points[1:]):
		dt_s = (current.hour - previous.hour) * SECONDS_PER_HOUR
		evaporation = (cfg.evaporation_mm_day / 1000.0
					   * area_from_level(level, cfg, table) / 86400.0)
		release = _controlled_release(
			level, cfg, inflow_m3s=current.inflow_m3s,
			prev_inflow_m3s=previous.inflow_m3s, prev_release_m3s=release,
			hour=current.hour, dt_s=dt_s, has=table,
		)
		trial_storage = storage + (current.inflow_m3s - release - evaporation) * dt_s
		spill = 0.0
		trial_level = level_from_storage(trial_storage, cfg, table)
		if trial_level > cfg.outlet_sill_m or trial_level > cfg.spillway_crest_m:
			spill = _spillway_flow(trial_level, cfg)
			trial_storage -= spill * dt_s
		if trial_storage > storage_from_level(cfg.max_level_m, cfg, table):
			excess = (trial_storage - storage_from_level(cfg.max_level_m, cfg, table)) / dt_s
			spill += max(excess, 0.0)
			trial_storage = storage_from_level(cfg.max_level_m, cfg, table)
		if trial_storage < storage_from_level(cfg.min_level_m, cfg, table):
			trial_storage = storage_from_level(cfg.min_level_m, cfg, table)
		storage = trial_storage
		level = level_from_storage(storage, cfg, table)
		result.hour.append(current.hour)
		result.inflow_m3s.append(current.inflow_m3s)
		result.level_m.append(level)
		result.storage_m3.append(storage)
		result.release_m3s.append(release)
		result.turbine_m3s.append(_power_release(
			release, cfg, level_m=level, inflow_m3s=current.inflow_m3s,
			hour=current.hour,
		))
		result.dam_release_m3s.append(_dam_release(
			release, cfg, level_m=level, inflow_m3s=current.inflow_m3s,
			hour=current.hour,
		))
		result.spill_m3s.append(spill)
		result.evaporation_m3s.append(evaporation)
		result.zone.append(operation_zone(
			level, cfg, inflow_m3s=current.inflow_m3s,
			prev_inflow_m3s=previous.inflow_m3s, hour=current.hour,
		))
		result.power_mw.append(hydropower_mw(level, _power_release(
			release, cfg, level_m=level, inflow_m3s=current.inflow_m3s,
			hour=current.hour,
		), cfg))
	return result


def _dam_site(station_km: float = 0.5, reach: str = "main") -> dict[str, float]:
	"""Noi suy toa do va huong dong tai moc nha dap."""
	if not GEOM_CSV.is_file():
		raise FileNotFoundError(f"Khong tim thay hinh hoc song: {GEOM_CSV}")
	stations: list[float] = []
	lons: list[float] = []
	lats: list[float] = []
	with GEOM_CSV.open("r", encoding="utf-8-sig", newline="") as stream:
		for row in csv.DictReader(stream):
			rid = str(row.get("reach_id") or row.get("reach") or "main").strip().lower()
			if rid != str(reach).strip().lower():
				continue
			try:
				stations.append(float(row["station_km"]))
				lons.append(float(row["lon"]))
				lats.append(float(row["lat"]))
			except (KeyError, TypeError, ValueError):
				continue
	if len(stations) < 2:
		raise ValueError("Khong du diem hinh hoc de dat nha dap")
	s = float(station_km)
	lon = _interp_series(s, stations, lons)
	lat = _interp_series(s, stations, lats)
	i = min(range(len(stations)), key=lambda k: abs(stations[k] - s))
	i0 = max(0, i - 1)
	i1 = min(len(stations) - 1, i + 1)
	if i0 == i1:
		i1 = min(len(stations) - 1, i0 + 1)
	dlat = math.radians(lat)
	fe = (lons[i1] - lons[i0]) * (111320.0 * max(math.cos(dlat), 1e-6))
	fn = (lats[i1] - lats[i0]) * 110540.0
	length = math.hypot(fe, fn) or 1.0
	return {"lon": lon, "lat": lat, "flow_east": fe / length, "flow_north": fn / length}


def _offset_lonlat(lon: float, lat: float, nx: float, ny: float, offset_m: float) -> tuple[float, float]:
	denom = max(111320.0 * math.cos(math.radians(lat)), 1e-6)
	return lon + (nx * offset_m) / denom, lat + (ny * offset_m) / 110540.0


def has_from_dem(
	*,
	dem_path: Path | str | None = None,
	station_km: float = 0.5,
	reach: str = "main",
	lon: float | None = None,
	lat: float | None = None,
	bed_m: float | None = None,
	max_level_m: float = 18.0,
	step_m: float = 0.25,
	storage_area_m2: float | None = 2_500_000.0,
	max_side: int = 720,
) -> ReservoirHAS:
	"""Lap bang H–A–S tu DEM: flood-fill thuong luu dap, S = Σ(H − z)·ΔA."""
	import numpy as np
	import rasterio
	from rasterio.enums import Resampling
	from rasterio.transform import Affine, rowcol, xy
	from rasterio.windows import Window, from_bounds as window_from_bounds

	from flood_model.gis import dem_xy_to_lonlat, lonlat_to_dem_xy, lonlat_to_mercator

	path = Path(dem_path) if dem_path else DEFAULT_DEM
	if not path.is_file():
		raise FileNotFoundError(f"Khong tim thay DEM: {path}")
	site = _dam_site(station_km, reach)
	if lon is None or lat is None:
		lon, lat = site["lon"], site["lat"]
	fe, fn = site["flow_east"], site["flow_north"]
	nx, ny = -fn, fe

	if storage_area_m2 is not None and storage_area_m2 > 0:
		radius_m = float(max(800.0, min(20000.0, math.sqrt(storage_area_m2 / math.pi) * 4.0 + 800.0)))
	else:
		radius_m = 2500.0
	dlat = radius_m / 110540.0
	dlon = radius_m / max(111320.0 * math.cos(math.radians(float(lat))), 1e-6)

	with rasterio.open(path) as src:
		xs_b, ys_b = lonlat_to_dem_xy(
			src,
			[float(lon) - dlon, float(lon) + dlon, float(lon) + dlon, float(lon) - dlon],
			[float(lat) - dlat, float(lat) - dlat, float(lat) + dlat, float(lat) + dlat],
		)
		win = window_from_bounds(
			float(np.min(xs_b)), float(np.min(ys_b)),
			float(np.max(xs_b)), float(np.max(ys_b)),
			transform=src.transform,
		).round_offsets().round_lengths()
		clip = Window.from_slices((0, int(src.height)), (0, int(src.width)))
		col0 = max(float(win.col_off), float(clip.col_off))
		row0 = max(float(win.row_off), float(clip.row_off))
		col1 = min(float(win.col_off + win.width), float(clip.col_off + clip.width))
		row1 = min(float(win.row_off + win.height), float(clip.row_off + clip.height))
		win = Window.from_slices((int(row0), int(row1)), (int(col0), int(col1)))
		if win.width < 8 or win.height < 8:
			raise ValueError("Cua so DEM quanh dap qua nho")

		scale = max(float(win.width), float(win.height)) / float(max(48, max_side))
		out_w = max(16, int(round(float(win.width) / max(scale, 1.0))))
		out_h = max(16, int(round(float(win.height) / max(scale, 1.0))))
		z = np.empty((out_h, out_w), dtype=np.float32)
		src.read(
			1, window=win, out=z,
			resampling=Resampling.average if scale > 1.05 else Resampling.nearest,
		)
		if src.nodata is not None:
			z = np.where(z == src.nodata, np.nan, z)
		z[~np.isfinite(z)] = np.nan
		transform = src.window_transform(win) * Affine.scale(
			float(win.width) / float(out_w), float(win.height) / float(out_h),
		)
		x0, y0 = xy(transform, out_h // 2, out_w // 2, offset="center")
		x1, y1 = xy(transform, out_h // 2, out_w // 2 + 1, offset="center")
		lo0, la0 = dem_xy_to_lonlat(src, [x0], [y0])
		lo1, la1 = dem_xy_to_lonlat(src, [x1], [y1])
		mx0, my0 = lonlat_to_mercator(float(lo0[0]), float(la0[0]))
		mx1, my1 = lonlat_to_mercator(float(lo1[0]), float(la1[0]))
		cell_m = max(math.hypot(mx1 - mx0, my1 - my0), 1.0)
		# EPSG:3857: dien tich that = Δx·Δy·cos²(lat).
		cell_m2 = (cell_m ** 2) * (max(math.cos(math.radians(float(lat))), 1e-6) ** 2)

		barrier = np.zeros(z.shape, dtype=bool)
		half = min(max(80.0, 0.35 * radius_m), radius_m * 0.85)
		b_lon, b_lat = _offset_lonlat(float(lon), float(lat), fe, fn, max(0.4 * cell_m, 4.0))
		a_lon, a_lat = _offset_lonlat(b_lon, b_lat, nx, ny, -half)
		c_lon, c_lat = _offset_lonlat(b_lon, b_lat, nx, ny, half)
		pxs, pys = lonlat_to_dem_xy(src, [a_lon, c_lon], [a_lat, c_lat])
		nr, nc = z.shape
		pts: list[tuple[int, int]] = []
		for x, y in zip(pxs, pys):
			rr, cc = rowcol(transform, float(x), float(y))
			if 0 <= rr < nr and 0 <= cc < nc:
				pts.append((int(rr), int(cc)))
		if len(pts) >= 1:
			for i in range(max(len(pts) - 1, 1)):
				r0, c0 = pts[min(i, len(pts) - 1)]
				r1, c1 = pts[min(i + 1, len(pts) - 1)]
				n = max(abs(r1 - r0), abs(c1 - c0), 1)
				for k in range(n + 1):
					t = k / float(n)
					rr = int(round(r0 + (r1 - r0) * t))
					cc = int(round(c0 + (c1 - c0) * t))
					for dr in range(-1, 2):
						for dc in range(-1, 2):
							r2, c2 = rr + dr, cc + dc
							if 0 <= r2 < nr and 0 <= c2 < nc:
								barrier[r2, c2] = True

		seed_lon, seed_lat = _offset_lonlat(float(lon), float(lat), -fe, -fn, max(cell_m, 12.0))
		sxs, sys_ = lonlat_to_dem_xy(src, [seed_lon], [seed_lat])
		sr, sc = rowcol(transform, float(sxs[0]), float(sys_[0]))

	finite = np.isfinite(z)
	zmin = float(np.nanmin(z)) if finite.any() else float("nan")
	if not math.isfinite(zmin):
		raise ValueError("DEM khong co gia tri hop le quanh dap")
	h0 = float(bed_m) if bed_m is not None and math.isfinite(float(bed_m)) else zmin
	h1 = float(max_level_m)
	if h1 <= h0 + step_m:
		h1 = h0 + max(step_m * 8.0, 4.0)
	levels = []
	h = h0
	while h < h1 - 1e-9:
		levels.append(round(h, 4))
		h += step_m
	levels.append(round(h1, 4))

	def can_wet(r: int, c: int, level: float) -> bool:
		if r < 0 or c < 0 or r >= nr or c >= nc or barrier[r, c]:
			return False
		zv = float(z[r, c])
		return math.isfinite(zv) and zv <= level + 1e-3

	seed: tuple[int, int] | None = None
	for rad in range(0, min(max(nr, nc), 80)):
		found: list[tuple[int, int]] = []
		for dr in range(-rad, rad + 1):
			for dc in range(-rad, rad + 1):
				if rad > 0 and abs(dr) != rad and abs(dc) != rad:
					continue
				r, c = int(sr) + dr, int(sc) + dc
				if can_wet(r, c, levels[-1]):
					found.append((r, c))
		if found:
			found.sort(key=lambda rc: (rc[0] - sr) ** 2 + (rc[1] - sc) ** 2)
			seed = found[0]
			break
	if seed is None:
		raise ValueError("Khong tim thay o hat giong thuong luu tren DEM")

	areas: list[float] = []
	storages: list[float] = []
	max_cells = 0
	for level in levels:
		wet = np.zeros(z.shape, dtype=bool)
		if not can_wet(seed[0], seed[1], level):
			areas.append(0.0)
			storages.append(0.0 if not storages else storages[-1])
			continue
		q: deque[tuple[int, int]] = deque([seed])
		wet[seed] = True
		while q:
			r, c = q.popleft()
			for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
				rr, cc = r + dr, c + dc
				if wet[rr, cc] if 0 <= rr < nr and 0 <= cc < nc else True:
					continue
				if not can_wet(rr, cc, level):
					continue
				wet[rr, cc] = True
				q.append((rr, cc))
		mask = wet & finite
		n_wet = int(mask.sum())
		max_cells = max(max_cells, n_wet)
		area = n_wet * cell_m2
		depth = np.where(mask, level - z, 0.0)
		storage = float(np.clip(depth, 0.0, None).sum()) * cell_m2
		areas.append(area)
		storages.append(storage)

	return ReservoirHAS(
		level_m=levels,
		area_m2=[round(v, 1) for v in areas],
		storage_m3=[round(v, 1) for v in storages],
		source=str(path),
		lon=round(float(lon), 6),
		lat=round(float(lat), 6),
		cell_m2=round(cell_m2, 3),
		n_cells=max_cells,
		bed_m=round(h0, 3),
		note="Flood-fill thuong luu dap tren DEM; S = tong (H - z)·dien tich o",
	).keep_positive_area()


def read_has_csv(path: Path | str) -> ReservoirHAS:
	with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
		reader = csv.DictReader(stream)
		levels, areas, storages = [], [], []
		for row in reader:
			try:
				levels.append(float(row["level_m"]))
				areas.append(float(row.get("area_m2") or row.get("area") or 0))
				storages.append(float(row.get("storage_m3") or row.get("storage") or 0))
			except (KeyError, TypeError, ValueError):
				continue
	if len(levels) < 2:
		raise ValueError("Bang H-A-S can it nhat 2 moc")
	return ReservoirHAS(level_m=levels, area_m2=areas, storage_m3=storages, source=str(path)).keep_positive_area()


def write_has_csv(path: Path | str, table: ReservoirHAS) -> None:
	fields = ["level_m", "area_m2", "storage_m3"]
	Path(path).parent.mkdir(parents=True, exist_ok=True)
	with Path(path).open("w", encoding="utf-8", newline="") as stream:
		writer = csv.DictWriter(stream, fieldnames=fields)
		writer.writeheader()
		for values in zip(table.level_m, table.area_m2, table.storage_m3):
			writer.writerow(dict(zip(fields, values)))


def read_inflow_csv(path: Path | str) -> list[ReservoirPoint]:
	"""Doc CSV inflow voi cot hour va inflow_m3s/q_in_m3s/inflow."""
	aliases = ("inflow_m3s", "q_in_m3s", "inflow", "q_m3s", "q")
	with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
		reader = csv.DictReader(stream)
		fields = {str(field).strip().lower(): field for field in reader.fieldnames or []}
		hour_field = fields.get("hour") or fields.get("time")
		flow_field = next((fields[name] for name in aliases if name in fields), None)
		if not hour_field or not flow_field:
			raise ValueError("CSV can cot hour va inflow_m3s (hoac q_in_m3s)")
		return [ReservoirPoint(float(row[hour_field]), float(row[flow_field]))
				for row in reader if row.get(hour_field, "").strip()]


def write_result_csv(path: Path | str, result: ReservoirResult) -> None:
	fields = ["hour", "inflow_m3s", "level_m", "storage_m3", "release_m3s",
			  "turbine_m3s", "dam_release_m3s", "spill_m3s",
			  "evaporation_m3s", "power_mw", "operation_zone"]
	with Path(path).open("w", encoding="utf-8", newline="") as stream:
		writer = csv.DictWriter(stream, fieldnames=fields)
		writer.writeheader()
		for values in zip(result.hour, result.inflow_m3s, result.level_m,
						  result.storage_m3, result.release_m3s,
						  result.turbine_m3s, result.dam_release_m3s,
						  result.spill_m3s, result.evaporation_m3s,
						  result.power_mw, result.zone):
			writer.writerow(dict(zip(fields, values)))


def write_curve_csv(path: Path | str, curve: ReservoirCurve) -> None:
	"""Ghi bang quan he H-V-Q de kiem tra hoac nhap vao mo hinh khac."""
	fields = ["level_m", "area_m2", "storage_m3", "outlet_m3s", "spillway_m3s", "total_discharge_m3s"]
	with Path(path).open("w", encoding="utf-8", newline="") as stream:
		writer = csv.DictWriter(stream, fieldnames=fields)
		writer.writeheader()
		for values in zip(curve.level_m, curve.area_m2, curve.storage_m3, curve.outlet_m3s,
						  curve.spillway_m3s, curve.total_discharge_m3s):
			writer.writerow(dict(zip(fields, values)))


def main() -> None:
	parser = argparse.ArgumentParser(description="Mo phong ho chua kieu HEC-ResSim")
	parser.add_argument("--inflow", type=Path, required=True,
						help="CSV co cot hour va inflow_m3s")
	parser.add_argument("--output", type=Path, default=Path("reservoir_result.csv"))
	parser.add_argument("--curve-output", type=Path, default=None,
						help="Ghi duong quan he muc nuoc - dung tich - luu luong")
	parser.add_argument("--has-from-dem", action="store_true",
						help="Lap bang H-A-S tu DEM Sông Hồng rồi dùng cho mô phỏng")
	parser.add_argument("--has-output", type=Path, default=DEFAULT_HAS_CSV)
	parser.add_argument("--initial-level", type=float, default=12.0)
	args = parser.parse_args()
	config = ReservoirConfig(initial_level_m=args.initial_level)
	if args.has_from_dem:
		table = has_from_dem(max_level_m=config.max_level_m)
		write_has_csv(args.has_output, table)
		config = ReservoirConfig(
			initial_level_m=args.initial_level,
			has_file=str(args.has_output),
			bed_m=table.bed_m if table.bed_m is not None else config.bed_m,
			storage_area_m2=table.area_m2[-1] if table.area_m2 else config.storage_area_m2,
		)
	result = simulate_reservoir(read_inflow_csv(args.inflow), config)
	write_result_csv(args.output, result)
	if args.curve_output:
		write_curve_csv(args.curve_output, reservoir_curve(config))
	print(f"Da ghi {len(result.hour)} buoc mo phong vao {args.output}")


if __name__ == "__main__":
	main()

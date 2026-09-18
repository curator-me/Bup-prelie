"""Optimizer QA: cost minimisation, aggregate consistency and battery-action contract.

Runs entirely offline (no LLM calls): directives are constructed directly and fed to
``app.optimizer.solve_energy_schedule`` / ``optimize_plan``.
"""

from __future__ import annotations

import itertools
import random

import pytest

from app import optimizer
from app.schemas import (
    BatteryAction,
    BatteryConfig,
    DirectiveInterpretation,
    DirectiveType,
    HourlyInput,
    MaxGridAdjustment,
    OptimizeEnergyRequest,
    ReserveAdjustment,
    SolarAdjustment,
    WindowAdjustment,
)

TOL = 0.01
HORIZON = 24
VALID_ACTIONS = {"charge", "discharge", "idle"}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _battery(**overrides: float) -> BatteryConfig:
    base = {
        "capacity_kwh": 100.0,
        "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 0.0,
        "max_charge_kwh_per_hour": 25.0,
        "max_discharge_kwh_per_hour": 25.0,
    }
    base.update(overrides)
    return BatteryConfig(**base)


def _hours(demand: list[float], solar: list[float], tariff: list[float]) -> list[HourlyInput]:
    return [
        HourlyInput(hour=h, demand_kwh=demand[h], solar_kwh=solar[h], tariff_bdt_per_kwh=tariff[h])
        for h in range(HORIZON)
    ]


def _directive(note_index: int, dtype: DirectiveType, adjustment) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=dtype,
        structured_adjustment=adjustment,
        explanation="test directive",
    )


def _no_op(note_index: int = 0) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index, applies=False, directive_type=DirectiveType.NO_OP, structured_adjustment=None, explanation="none"
    )


def _decompose(plan):
    rows = []
    for item in sorted(plan, key=lambda i: i.hour):
        charge = item.battery_kwh if item.battery_action is BatteryAction.CHARGE else 0.0
        discharge = item.battery_kwh if item.battery_action is BatteryAction.DISCHARGE else 0.0
        rows.append((item, charge, discharge))
    return rows


def _assert_physically_valid(schedule, hours, battery, solar_factor=None, min_energy=None, max_grid=None, no_charge=(), no_discharge=()):
    solar_factor = solar_factor or [1.0] * HORIZON
    min_energy = min_energy or [battery.minimum_energy_kwh] * HORIZON
    max_grid = max_grid or [None] * HORIZON
    energy = battery.initial_energy_kwh
    for item, charge, discharge in _decompose(schedule.hourly_plan):
        h = item.hour
        inp = hours[h]
        assert item.grid_kwh + item.solar_used_kwh + discharge == pytest.approx(inp.demand_kwh + charge, abs=TOL), f"h={h} balance"
        assert item.solar_used_kwh <= inp.solar_kwh * solar_factor[h] + TOL, f"h={h} solar"
        assert charge <= battery.max_charge_kwh_per_hour + TOL and discharge <= battery.max_discharge_kwh_per_hour + TOL, f"h={h} rate"
        energy = energy + charge - discharge
        assert item.battery_energy_after_kwh == pytest.approx(energy, abs=TOL), f"h={h} state"
        assert min_energy[h] - TOL <= item.battery_energy_after_kwh <= battery.capacity_kwh + TOL, f"h={h} bounds"
        if max_grid[h] is not None:
            assert item.grid_kwh <= max_grid[h] + TOL, f"h={h} grid cap"
        if h in no_charge:
            assert charge == pytest.approx(0.0, abs=TOL), f"h={h} no-charge"
        if h in no_discharge:
            assert discharge == pytest.approx(0.0, abs=TOL), f"h={h} no-discharge"
    assert energy == pytest.approx(battery.initial_energy_kwh, abs=TOL), "neutrality"


# ---------------------------------------------------------------------------
# 1. Cost minimisation
# ---------------------------------------------------------------------------


class TestCostMinimisation:
    """Two-tier tariff mock scenario with an analytically known optimum.

    demand 10 kWh/h, no solar, tariff 1 (h 0-11) then 10 (h 12-23).
    Battery: capacity 100, initial 50, min 0, 25 kWh/h limits.
    Cheap-hour headroom is 50 kWh; every cheap kWh shifted into the expensive period
    saves 9 BDT, and neutrality forbids ending below 50 kWh. Optimum:
        cheap:     12*10*1 + 50*1 = 170
        expensive: (12*10 - 50) * 10 = 700
        total      870 BDT
    """

    DEMAND = [10.0] * HORIZON
    SOLAR = [0.0] * HORIZON
    TARIFF = [1.0] * 12 + [10.0] * 12
    OPTIMUM = 870.0
    NAIVE = 12 * 10 * 1.0 + 12 * 10 * 10.0  # 1320

    def test_solver_reaches_analytic_optimum_without_violations(self):
        battery = _battery()
        hours = _hours(self.DEMAND, self.SOLAR, self.TARIFF)
        schedule = optimizer.solve_energy_schedule(hours, battery, [_no_op()])
        _assert_physically_valid(schedule, hours, battery)
        assert schedule.total_cost_bdt == pytest.approx(self.OPTIMUM, abs=0.5)
        assert schedule.total_cost_bdt < self.NAIVE
        assert not schedule.relaxed

    def test_solver_shifts_exactly_the_available_headroom(self):
        battery = _battery()
        hours = _hours(self.DEMAND, self.SOLAR, self.TARIFF)
        schedule = optimizer.solve_energy_schedule(hours, battery, [])
        rows = _decompose(schedule.hourly_plan)
        cheap_charge = sum(c for item, c, _ in rows if item.hour < 12)
        expensive_discharge = sum(d for item, _, d in rows if item.hour >= 12)
        expensive_charge = sum(c for item, c, _ in rows if item.hour >= 12)
        cheap_discharge = sum(d for item, _, d in rows if item.hour < 12)
        assert cheap_charge == pytest.approx(50.0, abs=TOL)
        assert expensive_discharge == pytest.approx(50.0, abs=TOL)
        assert expensive_charge == pytest.approx(0.0, abs=TOL)
        assert cheap_discharge == pytest.approx(0.0, abs=TOL)

    def test_cost_never_exceeds_naive_solar_then_grid_dispatch(self):
        rng = random.Random(2026)
        for _ in range(8):
            demand = [rng.uniform(10, 60) for _ in range(HORIZON)]
            solar = [rng.uniform(0, 40) if 6 <= h <= 17 else 0.0 for h in range(HORIZON)]
            tariff = [rng.choice([3.0, 5.0, 8.0, 12.0]) for _ in range(HORIZON)]
            battery = _battery(minimum_energy_kwh=10.0)
            hours = _hours(demand, solar, tariff)
            schedule = optimizer.solve_energy_schedule(hours, battery, [_no_op()])
            _assert_physically_valid(schedule, hours, battery)
            naive = sum(max(demand[h] - solar[h], 0.0) * tariff[h] for h in range(HORIZON))
            assert schedule.total_cost_bdt <= naive + TOL
            assert schedule.total_cost_bdt == pytest.approx(sum(i.grid_kwh * tariff[i.hour] for i in schedule.hourly_plan), abs=TOL)

    def test_flat_tariff_leaves_battery_idle(self):
        battery = _battery()
        hours = _hours(self.DEMAND, self.SOLAR, [5.0] * HORIZON)
        schedule = optimizer.solve_energy_schedule(hours, battery, [])
        assert all(i.battery_action is BatteryAction.IDLE for i in schedule.hourly_plan)
        assert schedule.total_cost_bdt == pytest.approx(HORIZON * 10.0 * 5.0, abs=TOL)

    def test_directives_are_honoured_and_only_raise_cost(self):
        battery = _battery(minimum_energy_kwh=10.0)
        hours = _hours(self.DEMAND, [0.0] * 6 + [20.0] * 12 + [0.0] * 6, self.TARIFF)
        free = optimizer.solve_energy_schedule(hours, battery, [])
        directives = [
            _directive(0, DirectiveType.SOLAR_REDUCTION, SolarAdjustment(hours=[12, 13, 14], factor=0.25)),
            _directive(1, DirectiveType.NO_CHARGE_WINDOW, WindowAdjustment(hours=[8, 9, 10])),
            _directive(2, DirectiveType.MINIMUM_BATTERY_RESERVE, ReserveAdjustment(hours=[18, 19, 20], minimum_energy_kwh=40.0)),
            _directive(3, DirectiveType.MAX_GRID_WINDOW, MaxGridAdjustment(hours=[20, 21], max_grid_kwh=8.0)),
            _directive(4, DirectiveType.NO_DISCHARGE_WINDOW, WindowAdjustment(hours=[22, 23])),
        ]
        constrained = optimizer.solve_energy_schedule(hours, battery, directives)
        solar_factor = [0.25 if h in (12, 13, 14) else 1.0 for h in range(HORIZON)]
        min_energy = [40.0 if h in (18, 19, 20) else 10.0 for h in range(HORIZON)]
        max_grid = [8.0 if h in (20, 21) else None for h in range(HORIZON)]
        _assert_physically_valid(
            constrained, hours, battery, solar_factor, min_energy, max_grid, no_charge=(8, 9, 10), no_discharge=(22, 23)
        )
        assert not constrained.relaxed
        assert constrained.total_cost_bdt >= free.total_cost_bdt - TOL


# ---------------------------------------------------------------------------
# 2. Root aggregates match the 24h plan
# ---------------------------------------------------------------------------


class TestAggregateConsistency:
    @pytest.fixture(params=["flat", "two_tier", "solar_heavy"])
    def response(self, request):
        demand = [30.0 + 20.0 * (12 <= h <= 20) for h in range(HORIZON)]
        solar = [35.0 if 8 <= h <= 16 else 0.0 for h in range(HORIZON)]
        tariff = {
            "flat": [6.0] * HORIZON,
            "two_tier": [4.0] * 16 + [12.0] * 8,
            "solar_heavy": [3.0 + (h % 5) for h in range(HORIZON)],
        }[request.param]
        req = OptimizeEnergyRequest(
            scenario_id=f"agg-{request.param}",
            operator_notes=["Solar panels cleaned 1 PM to 3 PM, 80% reduction."],
            hours=_hours(demand, solar, tariff),
            battery=_battery(minimum_energy_kwh=10.0),
        )
        directives = [_directive(0, DirectiveType.SOLAR_REDUCTION, SolarAdjustment(hours=[13, 14], factor=0.2))]
        return req, optimizer.optimize_plan(req, directives)

    def test_total_grid_matches_hourly_sum(self, response):
        _, resp = response
        assert resp.total_grid_kwh == pytest.approx(sum(i.grid_kwh for i in resp.hourly_plan), abs=TOL)

    def test_total_cost_matches_hourly_recomputation(self, response):
        req, resp = response
        tariff = {h.hour: h.tariff_bdt_per_kwh for h in req.hours}
        assert resp.total_cost_bdt == pytest.approx(sum(i.grid_kwh * tariff[i.hour] for i in resp.hourly_plan), abs=TOL)

    def test_peak_grid_matches_hourly_max(self, response):
        _, resp = response
        assert resp.peak_grid_kwh == pytest.approx(max(i.grid_kwh for i in resp.hourly_plan), abs=TOL)
        assert resp.peak_grid_kwh <= resp.total_grid_kwh + TOL

    def test_plan_has_24_hours_in_order_and_echoes_scenario(self, response):
        req, resp = response
        assert resp.scenario_id == req.scenario_id
        assert [i.hour for i in resp.hourly_plan] == list(range(HORIZON))
        assert len(resp.directive_interpretation) == len(req.operator_notes)


# ---------------------------------------------------------------------------
# 3. battery_action contract
# ---------------------------------------------------------------------------


class TestBatteryActionContract:
    @pytest.mark.parametrize(
        "tariff",
        [[5.0] * HORIZON, [1.0] * 12 + [10.0] * 12, [10.0] * 12 + [1.0] * 12, [float(1 + (h * 7) % 11) for h in range(HORIZON)]],
        ids=["flat", "cheap-then-dear", "dear-then-cheap", "jagged"],
    )
    def test_actions_are_exact_strings_with_non_negative_kwh(self, tariff):
        battery = _battery(minimum_energy_kwh=5.0)
        hours = _hours([20.0] * HORIZON, [10.0 if 7 <= h <= 17 else 0.0 for h in range(HORIZON)], tariff)
        schedule = optimizer.solve_energy_schedule(hours, battery, [])
        for item in schedule.hourly_plan:
            dumped = item.model_dump(mode="json")
            assert dumped["battery_action"] in VALID_ACTIONS
            assert isinstance(dumped["battery_kwh"], (int, float)) and not isinstance(dumped["battery_kwh"], bool)
            assert dumped["battery_kwh"] >= 0.0
            assert dumped["grid_kwh"] >= 0.0 and dumped["solar_used_kwh"] >= 0.0 and dumped["battery_energy_after_kwh"] >= 0.0
            if item.battery_action is BatteryAction.IDLE:
                assert item.battery_kwh == 0.0
            else:
                assert item.battery_kwh > 0.0

    def test_action_direction_matches_energy_delta(self):
        battery = _battery()
        hours = _hours([10.0] * HORIZON, [0.0] * HORIZON, [1.0] * 12 + [10.0] * 12)
        schedule = optimizer.solve_energy_schedule(hours, battery, [])
        energy = battery.initial_energy_kwh
        seen = set()
        for item in schedule.hourly_plan:
            delta = item.battery_energy_after_kwh - energy
            if item.battery_action is BatteryAction.CHARGE:
                assert delta == pytest.approx(item.battery_kwh, abs=TOL) and delta > 0
            elif item.battery_action is BatteryAction.DISCHARGE:
                assert delta == pytest.approx(-item.battery_kwh, abs=TOL) and delta < 0
            else:
                assert delta == pytest.approx(0.0, abs=TOL)
            seen.add(item.battery_action.value)
            energy = item.battery_energy_after_kwh
        assert {"charge", "discharge"} <= seen
        assert seen <= VALID_ACTIONS

    def test_enum_values_are_exactly_the_three_contract_strings(self):
        assert {a.value for a in BatteryAction} == VALID_ACTIONS

    def test_no_hour_charges_and_discharges_simultaneously(self):
        battery = _battery()
        for tariff in itertools.islice(itertools.permutations([1.0, 4.0, 9.0]), 3):
            hours = _hours([15.0] * HORIZON, [0.0] * HORIZON, [tariff[h % 3] for h in range(HORIZON)])
            schedule = optimizer.solve_energy_schedule(hours, battery, [])
            for item in schedule.hourly_plan:
                assert item.battery_action in (BatteryAction.CHARGE, BatteryAction.DISCHARGE, BatteryAction.IDLE)

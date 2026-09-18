"""Physical-constraint QA for ``hourly_plan`` returned by ``POST /optimize-energy``.

Each scenario is posted exactly once (module-level cache), so the whole suite performs at
most ``MAX_LLM_CALLS`` (10) live LLM requests. A hard guard fails the run if that budget is
exceeded. Set ``CONSTRAINT_TESTS_OFFLINE=1`` to stub the LLM and run fully offline; the
suite also switches itself offline automatically when no API key is configured.

Per hour ``h`` of every plan the suite asserts:
    1. Ground-truth directives were interpreted and applied.
    2. Balance:      grid + solar_used + discharge == demand + charge          (tol 0.01)
    3. Solar:        solar_used <= solar_forecast * solar_factor(h)             (tol 0.01)
    4. State:        E_after[h] == E_before[h] + charge - discharge             (tol 0.01)
    5. Bounds:       min_energy(h) <= E_after[h] <= capacity                    (tol 0.01)
    6. Rates:        charge <= max_charge, discharge <= max_discharge           (tol 0.01)
    7. Neutrality:   E_after[23] == initial_energy                              (tol 0.01)
"""

from __future__ import annotations

import os
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app import config, interpreter
from app.main import app
from app.schemas import BatteryAction, DirectiveType, OptimizeEnergyResponse

# ---------------------------------------------------------------------------
# Budget and tolerances
# ---------------------------------------------------------------------------

MAX_LLM_CALLS = 10
TOL = 0.01
ENDPOINT = "/optimize-energy"
ALL_HOURS = list(range(24))

OFFLINE = os.getenv("CONSTRAINT_TESTS_OFFLINE") == "1" or not config.LLM_API_KEY

# ---------------------------------------------------------------------------
# Shared physical inputs
# ---------------------------------------------------------------------------

BATTERY: dict[str, float] = {
    "capacity_kwh": 100.0,
    "initial_energy_kwh": 50.0,
    "minimum_energy_kwh": 10.0,
    "max_charge_kwh_per_hour": 25.0,
    "max_discharge_kwh_per_hour": 25.0,
}

DEMAND_KWH = [30, 28, 27, 27, 28, 32, 38, 45, 50, 52, 52, 50, 48, 47, 47, 48, 52, 58, 60, 58, 52, 45, 38, 33]
SOLAR_KWH = [0, 0, 0, 0, 0, 0, 5, 15, 25, 32, 38, 40, 40, 38, 32, 25, 15, 5, 0, 0, 0, 0, 0, 0]
TARIFF_BDT = [4, 4, 4, 4, 4, 5, 6, 7, 7, 7, 7, 7, 7, 7, 7, 7, 9, 12, 12, 12, 10, 8, 6, 5]


def _hours_payload() -> list[dict[str, float]]:
    return [
        {"hour": h, "demand_kwh": float(DEMAND_KWH[h]), "solar_kwh": float(SOLAR_KWH[h]), "tariff_bdt_per_kwh": float(TARIFF_BDT[h])}
        for h in range(24)
    ]


# ---------------------------------------------------------------------------
# Scenarios: notes + ground-truth directives (<= MAX_LLM_CALLS scenarios)
# ---------------------------------------------------------------------------


def _gt(directive_type: DirectiveType, hours: Optional[list[int]] = None, **values: float) -> dict[str, Any]:
    return {"directive_type": directive_type, "hours": hours, **values}


SCENARIOS: dict[str, dict[str, Any]] = {
    "baseline_no_directive": {
        "notes": ["The cafeteria will host a birthday party at noon."],
        "truth": [_gt(DirectiveType.NO_OP)],
    },
    "solar_reduction_13_15": {
        "notes": ["Solar panels are being cleaned from 1 PM to 3 PM, expect 80% reduction."],
        "truth": [_gt(DirectiveType.SOLAR_REDUCTION, [13, 14], factor=0.2)],
    },
    "no_charge_14_17": {
        "notes": ["Do not charge the battery from 2 PM to 5 PM."],
        "truth": [_gt(DirectiveType.NO_CHARGE_WINDOW, [14, 15, 16])],
    },
    "no_discharge_18_21": {
        "notes": ["Do not discharge the battery between 6 PM and 9 PM."],
        "truth": [_gt(DirectiveType.NO_DISCHARGE_WINDOW, [18, 19, 20])],
    },
    "reserve_30kwh_18_22": {
        "notes": ["Keep at least 30% battery reserve from 6 PM to 10 PM."],
        "truth": [_gt(DirectiveType.MINIMUM_BATTERY_RESERVE, [18, 19, 20, 21], minimum_energy_kwh=30.0)],
    },
    "max_grid_45kwh_17_20": {
        "notes": ["Grid import must not exceed 45 kWh per hour between 5 PM and 8 PM."],
        "truth": [_gt(DirectiveType.MAX_GRID_WINDOW, [17, 18, 19], max_grid_kwh=45.0)],
    },
    "combo_solar_nocharge_distractor": {
        "notes": [
            "Solar output drops to 50% of normal from 11 AM until 2 PM.",
            "Battery charging is prohibited between 14:00 and 17:00.",
            "Library seminar runs from 2 PM to 4 PM.",
        ],
        "truth": [
            _gt(DirectiveType.SOLAR_REDUCTION, [11, 12, 13], factor=0.5),
            _gt(DirectiveType.NO_CHARGE_WINDOW, [14, 15, 16]),
            _gt(DirectiveType.NO_OP),
        ],
    },
    "combo_reserve_maxgrid_nodischarge": {
        "notes": [
            "Maintain a minimum battery charge of 40 kWh between 17:00 and 20:00.",
            "Cap grid draw at 50 kWh from 17:00 to 20:00.",
            "Avoid discharging the battery from 10 PM until midnight.",
        ],
        "truth": [
            _gt(DirectiveType.MINIMUM_BATTERY_RESERVE, [17, 18, 19], minimum_energy_kwh=40.0),
            _gt(DirectiveType.MAX_GRID_WINDOW, [17, 18, 19], max_grid_kwh=50.0),
            _gt(DirectiveType.NO_DISCHARGE_WINDOW, [22, 23]),
        ],
    },
    "solar_offline_all_day": {
        "notes": ["Solar panels offline all day for maintenance."],
        "truth": [_gt(DirectiveType.SOLAR_REDUCTION, ALL_HOURS, factor=0.0)],
    },
}

assert len(SCENARIOS) <= MAX_LLM_CALLS, "scenario count must stay within the LLM call budget"

SCENARIO_IDS = list(SCENARIOS)


# ---------------------------------------------------------------------------
# Fixtures: LLM call budget guard + one cached POST per scenario
# ---------------------------------------------------------------------------


class _CallCounter:
    def __init__(self) -> None:
        self.calls = 0


@pytest.fixture(scope="module", autouse=True)
def llm_budget():
    """Count live LLM requests (or stub them offline) and enforce the budget at teardown."""
    counter = _CallCounter()
    original_request = interpreter._request_structured_directives
    original_call = interpreter._call_llm

    if OFFLINE:
        interpreter._call_llm = lambda notes, battery: None  # type: ignore[assignment]
    else:

        def _counted(notes, battery):
            counter.calls += 1
            if counter.calls > MAX_LLM_CALLS:
                raise RuntimeError(f"LLM call budget of {MAX_LLM_CALLS} exceeded")
            return original_request(notes, battery)

        interpreter._request_structured_directives = _counted  # type: ignore[assignment]

    try:
        yield counter
    finally:
        interpreter._request_structured_directives = original_request  # type: ignore[assignment]
        interpreter._call_llm = original_call  # type: ignore[assignment]
    assert counter.calls <= MAX_LLM_CALLS, f"suite made {counter.calls} LLM calls (budget {MAX_LLM_CALLS})"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def _cache() -> dict[str, OptimizeEnergyResponse]:
    return {}


@pytest.fixture
def scenario(request, client: TestClient, _cache: dict[str, OptimizeEnergyResponse], llm_budget):
    """(scenario_id, spec, validated response) - each scenario is posted exactly once per session."""
    scenario_id: str = request.param
    if scenario_id not in _cache:
        spec = SCENARIOS[scenario_id]
        payload = {
            "scenario_id": scenario_id,
            "operator_notes": spec["notes"],
            "hours": _hours_payload(),
            "battery": dict(BATTERY),
        }
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, f"{scenario_id}: {response.status_code} {response.text}"
        _cache[scenario_id] = OptimizeEnergyResponse.model_validate(response.json())
    return scenario_id, SCENARIOS[scenario_id], _cache[scenario_id]


def _parametrize_scenarios(func):
    return pytest.mark.parametrize("scenario", SCENARIO_IDS, indirect=True, ids=SCENARIO_IDS)(func)


# ---------------------------------------------------------------------------
# Helpers: ground-truth constraint envelope + plan decomposition
# ---------------------------------------------------------------------------


def _truth_envelope(truth: list[dict[str, Any]]) -> dict[str, Any]:
    solar_factor = [1.0] * 24
    min_energy = [BATTERY["minimum_energy_kwh"]] * 24
    max_grid: list[Optional[float]] = [None] * 24
    no_charge = [False] * 24
    no_discharge = [False] * 24
    for item in truth:
        dt = item["directive_type"]
        if dt is DirectiveType.NO_OP:
            continue
        for h in item["hours"]:
            if dt is DirectiveType.SOLAR_REDUCTION:
                solar_factor[h] = min(solar_factor[h], item["factor"])
            elif dt is DirectiveType.MINIMUM_BATTERY_RESERVE:
                min_energy[h] = max(min_energy[h], item["minimum_energy_kwh"])
            elif dt is DirectiveType.MAX_GRID_WINDOW:
                cap = item["max_grid_kwh"]
                max_grid[h] = cap if max_grid[h] is None else min(max_grid[h], cap)
            elif dt is DirectiveType.NO_CHARGE_WINDOW:
                no_charge[h] = True
            elif dt is DirectiveType.NO_DISCHARGE_WINDOW:
                no_discharge[h] = True
    return {
        "solar_factor": solar_factor,
        "min_energy": min_energy,
        "max_grid": max_grid,
        "no_charge": no_charge,
        "no_discharge": no_discharge,
    }


def _rows(response: OptimizeEnergyResponse) -> list[dict[str, float]]:
    """Per-hour rows in hour order with explicit charge/discharge/E_before/E_after."""
    plan = sorted(response.hourly_plan, key=lambda item: item.hour)
    rows: list[dict[str, float]] = []
    energy_before = BATTERY["initial_energy_kwh"]
    for item in plan:
        charge = item.battery_kwh if item.battery_action is BatteryAction.CHARGE else 0.0
        discharge = item.battery_kwh if item.battery_action is BatteryAction.DISCHARGE else 0.0
        rows.append(
            {
                "hour": item.hour,
                "grid": item.grid_kwh,
                "solar_used": item.solar_used_kwh,
                "charge": charge,
                "discharge": discharge,
                "energy_before": energy_before,
                "energy_after": item.battery_energy_after_kwh,
                "demand": float(DEMAND_KWH[item.hour]),
                "solar": float(SOLAR_KWH[item.hour]),
            }
        )
        energy_before = item.battery_energy_after_kwh
    return rows


# ---------------------------------------------------------------------------
# 0. Plan structure
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_plan_covers_each_hour_exactly_once(scenario):
    _, _, response = scenario
    assert [item.hour for item in response.hourly_plan] == ALL_HOURS
    for item in response.hourly_plan:
        assert item.grid_kwh >= 0.0
        assert item.solar_used_kwh >= 0.0
        assert item.battery_kwh >= 0.0
        assert item.battery_energy_after_kwh >= 0.0
        if item.battery_action is BatteryAction.IDLE:
            assert item.battery_kwh == pytest.approx(0.0, abs=TOL)
        else:
            assert item.battery_kwh > 0.0


# ---------------------------------------------------------------------------
# 1. Ground-truth directives interpreted and applied
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_ground_truth_directives_are_interpreted(scenario):
    scenario_id, spec, response = scenario
    truth = spec["truth"]
    entries = response.directive_interpretation
    assert [e.note_index for e in entries] == list(range(len(truth)))
    for expected, entry in zip(truth, entries):
        dt = expected["directive_type"]
        assert entry.directive_type is dt, f"{scenario_id} note {entry.note_index}: {entry.directive_type} != {dt}"
        if dt is DirectiveType.NO_OP:
            assert entry.applies is False
            assert entry.structured_adjustment is None
            continue
        assert entry.applies is True
        adj = entry.structured_adjustment
        assert adj is not None
        assert adj.hours == expected["hours"], f"{scenario_id} note {entry.note_index}: hours {adj.hours}"
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in expected:
                assert getattr(adj, key) == pytest.approx(expected[key], abs=1e-6), f"{scenario_id} note {entry.note_index}: {key}"


@_parametrize_scenarios
def test_ground_truth_directives_are_applied_to_plan(scenario):
    scenario_id, spec, response = scenario
    env = _truth_envelope(spec["truth"])
    for row in _rows(response):
        h = int(row["hour"])
        if env["no_charge"][h]:
            assert row["charge"] == pytest.approx(0.0, abs=TOL), f"{scenario_id} h={h}: charged in no-charge window"
        if env["no_discharge"][h]:
            assert row["discharge"] == pytest.approx(0.0, abs=TOL), f"{scenario_id} h={h}: discharged in no-discharge window"
        if env["max_grid"][h] is not None:
            assert row["grid"] <= env["max_grid"][h] + TOL, f"{scenario_id} h={h}: grid {row['grid']} > cap {env['max_grid'][h]}"
        assert row["energy_after"] >= env["min_energy"][h] - TOL, f"{scenario_id} h={h}: E_after {row['energy_after']} < reserve {env['min_energy'][h]}"
        assert row["solar_used"] <= row["solar"] * env["solar_factor"][h] + TOL, f"{scenario_id} h={h}: solar_used exceeds reduced solar"
    assert "relaxed" not in response.plan_summary.lower() and "could not" not in response.plan_summary.lower(), (
        f"{scenario_id}: plan relaxed a directive: {response.plan_summary}"
    )


# ---------------------------------------------------------------------------
# 2. Hourly energy balance
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_hourly_energy_balance(scenario):
    scenario_id, _, response = scenario
    for row in _rows(response):
        supply = row["grid"] + row["solar_used"] + row["discharge"]
        usage = row["demand"] + row["charge"]
        assert supply == pytest.approx(usage, abs=TOL), f"{scenario_id} h={int(row['hour'])}: supply {supply} != usage {usage}"


# ---------------------------------------------------------------------------
# 3. Solar usage bounded by effective (reduced) solar
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_solar_used_within_effective_solar(scenario):
    scenario_id, spec, response = scenario
    factors = _truth_envelope(spec["truth"])["solar_factor"]
    for row in _rows(response):
        h = int(row["hour"])
        effective = row["solar"] * factors[h]
        assert 0.0 <= row["solar_used"] <= effective + TOL, f"{scenario_id} h={h}: solar_used {row['solar_used']} > effective {effective}"


# ---------------------------------------------------------------------------
# 4. Battery state transition
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_battery_state_transition(scenario):
    scenario_id, _, response = scenario
    for row in _rows(response):
        expected = row["energy_before"] + row["charge"] - row["discharge"]
        assert row["energy_after"] == pytest.approx(expected, abs=TOL), (
            f"{scenario_id} h={int(row['hour'])}: E_after {row['energy_after']} != {row['energy_before']} + {row['charge']} - {row['discharge']}"
        )


# ---------------------------------------------------------------------------
# 5. Energy bounds
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_battery_energy_within_bounds(scenario):
    scenario_id, spec, response = scenario
    min_energy = _truth_envelope(spec["truth"])["min_energy"]
    capacity = BATTERY["capacity_kwh"]
    for row in _rows(response):
        h = int(row["hour"])
        assert row["energy_after"] >= BATTERY["minimum_energy_kwh"] - TOL, f"{scenario_id} h={h}: below battery minimum"
        assert row["energy_after"] >= min_energy[h] - TOL, f"{scenario_id} h={h}: below directive reserve {min_energy[h]}"
        assert row["energy_after"] <= capacity + TOL, f"{scenario_id} h={h}: above capacity"


# ---------------------------------------------------------------------------
# 6. Charge / discharge rate limits
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_battery_rate_limits(scenario):
    scenario_id, _, response = scenario
    for row in _rows(response):
        h = int(row["hour"])
        assert 0.0 <= row["charge"] <= BATTERY["max_charge_kwh_per_hour"] + TOL, f"{scenario_id} h={h}: charge {row['charge']} exceeds limit"
        assert 0.0 <= row["discharge"] <= BATTERY["max_discharge_kwh_per_hour"] + TOL, f"{scenario_id} h={h}: discharge {row['discharge']} exceeds limit"
        assert not (row["charge"] > TOL and row["discharge"] > TOL), f"{scenario_id} h={h}: simultaneous charge and discharge"


# ---------------------------------------------------------------------------
# 7. End-of-day neutrality
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_end_of_day_neutrality(scenario):
    scenario_id, _, response = scenario
    rows = _rows(response)
    assert int(rows[-1]["hour"]) == 23
    assert rows[-1]["energy_after"] == pytest.approx(BATTERY["initial_energy_kwh"], abs=TOL), (
        f"{scenario_id}: E_after[23]={rows[-1]['energy_after']} != initial {BATTERY['initial_energy_kwh']}"
    )
    net = sum(r["charge"] for r in rows) - sum(r["discharge"] for r in rows)
    assert net == pytest.approx(0.0, abs=TOL), f"{scenario_id}: net battery throughput {net} != 0"


# ---------------------------------------------------------------------------
# Aggregates and call budget
# ---------------------------------------------------------------------------


@_parametrize_scenarios
def test_aggregates_match_hourly_plan(scenario):
    scenario_id, _, response = scenario
    rows = _rows(response)
    total_grid = sum(r["grid"] for r in rows)
    total_cost = sum(r["grid"] * TARIFF_BDT[int(r["hour"])] for r in rows)
    peak = max(r["grid"] for r in rows)
    assert response.total_grid_kwh == pytest.approx(total_grid, abs=TOL), scenario_id
    assert response.total_cost_bdt == pytest.approx(total_cost, abs=TOL), scenario_id
    assert response.peak_grid_kwh == pytest.approx(peak, abs=TOL), scenario_id


def test_llm_call_budget_respected(llm_budget, _cache):
    if OFFLINE:
        assert llm_budget.calls == 0
    else:
        assert llm_budget.calls == len(_cache)
        assert llm_budget.calls <= MAX_LLM_CALLS

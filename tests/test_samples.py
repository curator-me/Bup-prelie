"""Runs the public sample cases (BUP CSE Fest 2026 Preli) through the FastAPI app.

For every case the suite verifies:
    1. HTTP status code is 200.
    2. The response body validates against ``OptimizeEnergyResponse`` (Pydantic).
    3. Directive interpretations match the expected ``applies``, ``directive_type`` and
       ``structured_adjustment`` hours/values.
    4. Physical energy constraints hold for the returned ``hourly_plan``.
    5. ``total_cost_bdt`` matches the expected cost within 1.0 %.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app import interpreter
from app.main import app
from app.schemas import (
    DirectiveInterpretation,
    DirectiveType,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)

# ---------------------------------------------------------------------------
# Tolerances (kWh / BDT)
# ---------------------------------------------------------------------------

BALANCE_TOL = 0.05
SOLAR_TOL = 0.01
BATTERY_TOL = 0.01
RATE_TOL = 0.01
NEUTRALITY_TOL = 0.05
COST_REL_TOL = 0.01
VALUE_TOL = 1e-6

ENDPOINT = "/optimize-energy"
OFFICIAL_FILENAME = "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
FALLBACK_FILENAME = "sample_cases.json"

_TESTS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _TESTS_DIR.parent

_NUMERIC_ADJUSTMENT_KEYS = ("factor", "minimum_energy_kwh", "max_grid_kwh")


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.getenv("SAMPLE_CASES_PATH")
    if env_path:
        paths.append(Path(env_path))
    for base in (_TESTS_DIR, _PROJECT_DIR, _PROJECT_DIR.parent, Path.cwd()):
        paths.append(base / OFFICIAL_FILENAME)
    paths.append(_TESTS_DIR / FALLBACK_FILENAME)
    return paths


def _locate_cases_file() -> Optional[Path]:
    for path in _candidate_paths():
        if path.is_file():
            return path
    return None


def _extract_case_list(document: Any) -> list[dict]:
    if isinstance(document, list):
        return [c for c in document if isinstance(c, dict)]
    if isinstance(document, dict):
        for key in ("cases", "sample_cases", "samples", "scenarios", "test_cases", "data"):
            value = document.get(key)
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
        for value in document.values():
            if isinstance(value, list) and value and all(isinstance(c, dict) for c in value):
                return value
    return []


def _load_cases() -> tuple[Optional[Path], list[dict]]:
    path = _locate_cases_file()
    if path is None:
        return None, []
    document = json.loads(path.read_text(encoding="utf-8"))
    return path, _extract_case_list(document)


CASES_PATH, CASES = _load_cases()


def _case_input(case: dict) -> dict:
    for key in ("input", "request", "payload", "request_body"):
        value = case.get(key)
        if isinstance(value, dict):
            return value
    if "hours" in case and "battery" in case:
        return {k: v for k, v in case.items() if k in OptimizeEnergyRequest.model_fields}
    raise AssertionError("sample case has no request payload")


def _case_expected(case: dict) -> dict:
    for key in ("expected_output", "expected", "output", "expected_response"):
        value = case.get(key)
        if isinstance(value, dict):
            return value
    raise AssertionError("sample case has no expected output")


def _case_id(case: dict) -> str:
    for key in ("case_id", "id", "name", "title", "scenario_id"):
        value = case.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    try:
        return str(_case_input(case).get("scenario_id", "case"))
    except AssertionError:
        return "case"


def _expected_directives(expected: dict) -> list[dict]:
    for key in ("directive_interpretation", "directive_interpretations", "directives"):
        value = expected.get(key)
        if isinstance(value, list):
            return [d for d in value if isinstance(d, dict)]
    return []


def _expected_cost(expected: dict) -> Optional[float]:
    value = expected.get("total_cost_bdt")
    if value is None:
        return None
    return float(value)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _offline_interpreter():
    """Force the deterministic heuristic interpreter unless SAMPLE_TESTS_USE_LLM=1."""
    if os.getenv("SAMPLE_TESTS_USE_LLM") == "1":
        yield
        return
    original = interpreter._call_llm
    interpreter._call_llm = lambda notes, battery: None  # type: ignore[assignment]
    try:
        yield
    finally:
        interpreter._call_llm = original  # type: ignore[assignment]


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def response_cache() -> dict:
    return {}


@pytest.fixture
def case_result(case: dict, client: TestClient, response_cache: dict):
    """POST the case once per module and reuse the raw response across checks."""
    key = id(case)
    if key not in response_cache:
        response_cache[key] = client.post(ENDPOINT, json=_case_input(case))
    return response_cache[key]


@pytest.fixture
def parsed(case_result) -> OptimizeEnergyResponse:
    assert case_result.status_code == 200, case_result.text
    return OptimizeEnergyResponse.model_validate(case_result.json())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm_hours(hours: Any) -> list[int]:
    assert isinstance(hours, list), f"hours must be a list, got {type(hours).__name__}"
    return sorted(int(h) for h in hours)


def _adjustment_dict(adjustment: Any) -> Optional[dict]:
    if adjustment is None:
        return None
    if hasattr(adjustment, "model_dump"):
        return adjustment.model_dump(mode="json")
    assert isinstance(adjustment, dict)
    return adjustment


def _assert_adjustment_matches(actual: Any, expected: Any, label: str) -> None:
    actual_adj = _adjustment_dict(actual)
    expected_adj = _adjustment_dict(expected)

    if expected_adj is None:
        assert actual_adj is None, f"{label}: expected no structured_adjustment, got {actual_adj}"
        return
    assert actual_adj is not None, f"{label}: expected structured_adjustment {expected_adj}, got None"

    if "kind" in expected_adj:
        assert actual_adj.get("kind") == expected_adj["kind"], (
            f"{label}: kind {actual_adj.get('kind')!r} != {expected_adj['kind']!r}"
        )

    if "hours" in expected_adj:
        assert "hours" in actual_adj, f"{label}: structured_adjustment has no hours"
        assert _norm_hours(actual_adj["hours"]) == _norm_hours(expected_adj["hours"]), (
            f"{label}: hours {sorted(actual_adj['hours'])} != {sorted(expected_adj['hours'])}"
        )

    for key in _NUMERIC_ADJUSTMENT_KEYS:
        if key not in expected_adj or expected_adj[key] is None:
            continue
        assert key in actual_adj and actual_adj[key] is not None, (
            f"{label}: structured_adjustment missing {key!r}"
        )
        assert abs(float(actual_adj[key]) - float(expected_adj[key])) <= VALUE_TOL, (
            f"{label}: {key} {actual_adj[key]} != {expected_adj[key]}"
        )


def _applied(directives: list[DirectiveInterpretation], directive_type: DirectiveType):
    for d in directives:
        if d.applies and d.directive_type is directive_type and d.structured_adjustment is not None:
            yield d.structured_adjustment


def _effective_solar(request: OptimizeEnergyRequest, response: OptimizeEnergyResponse) -> dict[int, float]:
    factor = {h: 1.0 for h in range(24)}
    for adj in _applied(response.directive_interpretation, DirectiveType.SOLAR_REDUCTION):
        for h in adj.hours:
            factor[h] = min(factor[h], float(adj.factor))
    return {item.hour: float(item.solar_kwh) * factor[item.hour] for item in request.hours}


def _min_bound(request: OptimizeEnergyRequest, response: OptimizeEnergyResponse) -> dict[int, float]:
    base = float(request.battery.minimum_energy_kwh)
    bound = {h: base for h in range(24)}
    for adj in _applied(response.directive_interpretation, DirectiveType.MINIMUM_BATTERY_RESERVE):
        floor = min(float(adj.minimum_energy_kwh), float(request.battery.capacity_kwh))
        for h in adj.hours:
            bound[h] = max(bound[h], floor)
    return bound


def _charge_discharge(item) -> tuple[float, float]:
    amount = float(item.battery_kwh)
    if item.battery_action.value == "charge":
        return amount, 0.0
    if item.battery_action.value == "discharge":
        return 0.0, amount
    return 0.0, 0.0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sample_cases_are_available():
    if CASES_PATH is None:
        pytest.skip(f"{OFFICIAL_FILENAME} not found (set SAMPLE_CASES_PATH to point at it)")
    assert isinstance(CASES, list)
    if CASES_PATH.name == OFFICIAL_FILENAME:
        assert len(CASES) == 10, f"expected 10 public sample cases, found {len(CASES)}"
    elif not CASES:
        pytest.skip(f"{CASES_PATH} contains no sample cases")


@pytest.mark.parametrize("case", CASES, ids=_case_id)
class TestSampleCase:
    def test_status_code_is_200(self, case, case_result):
        assert case_result.status_code == 200, (
            f"expected 200, got {case_result.status_code}: {case_result.text}"
        )

    def test_response_schema(self, case, case_result):
        assert case_result.status_code == 200, case_result.text
        parsed = OptimizeEnergyResponse.model_validate(case_result.json())
        request_payload = _case_input(case)
        assert parsed.scenario_id == request_payload["scenario_id"]
        assert len(parsed.hourly_plan) == 24
        assert [item.hour for item in parsed.hourly_plan] == list(range(24))
        assert len(parsed.directive_interpretation) == len(request_payload["operator_notes"])

    def test_directive_interpretation(self, case, parsed):
        expected_directives = _expected_directives(_case_expected(case))
        if not expected_directives:
            pytest.skip("case has no expected directive_interpretation")

        actual_by_index = {d.note_index: d for d in parsed.directive_interpretation}
        for position, expected in enumerate(expected_directives):
            note_index = int(expected.get("note_index", position))
            label = f"note {note_index}"
            assert note_index in actual_by_index, f"{label}: missing from response"
            actual = actual_by_index[note_index]

            if "applies" in expected:
                assert actual.applies == bool(expected["applies"]), (
                    f"{label}: applies {actual.applies} != {expected['applies']}"
                )
            if "directive_type" in expected:
                assert actual.directive_type.value == str(expected["directive_type"]), (
                    f"{label}: directive_type {actual.directive_type.value!r} "
                    f"!= {expected['directive_type']!r}"
                )
            if "structured_adjustment" in expected:
                _assert_adjustment_matches(
                    actual.structured_adjustment, expected["structured_adjustment"], label
                )

    def test_energy_constraints(self, case, parsed):
        request = OptimizeEnergyRequest.model_validate(_case_input(case))
        battery = request.battery
        demand = {item.hour: float(item.demand_kwh) for item in request.hours}
        effective_solar = _effective_solar(request, parsed)
        min_bound = _min_bound(request, parsed)
        capacity = float(battery.capacity_kwh)
        max_charge = float(battery.max_charge_kwh_per_hour)
        max_discharge = float(battery.max_discharge_kwh_per_hour)

        plan = sorted(parsed.hourly_plan, key=lambda item: item.hour)
        violations: list[str] = []

        for item in plan:
            h = item.hour
            grid = float(item.grid_kwh)
            solar_used = float(item.solar_used_kwh)
            charge, discharge = _charge_discharge(item)
            energy_after = float(item.battery_energy_after_kwh)

            supplied = grid + solar_used + discharge
            required = demand[h] + charge
            if abs(supplied - required) > BALANCE_TOL:
                violations.append(
                    f"h{h}: balance |{supplied:.4f} - {required:.4f}| > {BALANCE_TOL}"
                )

            if solar_used > effective_solar[h] + SOLAR_TOL:
                violations.append(
                    f"h{h}: solar_used {solar_used:.4f} > effective_solar {effective_solar[h]:.4f}"
                )

            if energy_after < min_bound[h] - BATTERY_TOL:
                violations.append(
                    f"h{h}: battery_energy_after {energy_after:.4f} < min_bound {min_bound[h]:.4f}"
                )
            if energy_after > capacity + BATTERY_TOL:
                violations.append(
                    f"h{h}: battery_energy_after {energy_after:.4f} > capacity {capacity:.4f}"
                )

            if charge > max_charge + RATE_TOL:
                violations.append(f"h{h}: charge {charge:.4f} > max_charge {max_charge:.4f}")
            if discharge > max_discharge + RATE_TOL:
                violations.append(
                    f"h{h}: discharge {discharge:.4f} > max_discharge {max_discharge:.4f}"
                )

        final_energy = float(plan[-1].battery_energy_after_kwh)
        initial_energy = float(battery.initial_energy_kwh)
        if abs(final_energy - initial_energy) > NEUTRALITY_TOL:
            violations.append(
                f"neutrality |{final_energy:.4f} - {initial_energy:.4f}| > {NEUTRALITY_TOL}"
            )

        assert not violations, "energy constraint violations:\n  " + "\n  ".join(violations)

    def test_cost_equivalence(self, case, parsed):
        expected_cost = _expected_cost(_case_expected(case))
        if expected_cost is None:
            pytest.skip("case has no expected total_cost_bdt")
        actual_cost = float(parsed.total_cost_bdt)
        tolerance = COST_REL_TOL * abs(expected_cost)
        assert abs(actual_cost - expected_cost) <= tolerance, (
            f"total_cost_bdt {actual_cost:.4f} differs from expected {expected_cost:.4f} "
            f"by more than {COST_REL_TOL:.1%} (tolerance {tolerance:.4f})"
        )

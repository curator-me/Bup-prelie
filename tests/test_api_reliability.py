"""API reliability QA for the FastAPI service.

External LLM usage is capped: every test stubs the LLM except one live contract check,
which runs only when an API key is configured. Total live DeepSeek calls <= MAX_LLM_CALLS (1).
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config, interpreter
from app.main import app
from app.schemas import BatteryAction, DirectiveType, OptimizeEnergyResponse

MAX_LLM_CALLS = 1
MAX_REQUEST_SECONDS = 30.0
ENDPOINT = "/optimize-energy"
HORIZON = 24
LIVE = os.getenv("API_TESTS_SKIP_LIVE") != "1" and bool(config.LLM_API_KEY)

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r'File "[^"]+\.py", line \d+'),
    re.compile(r"api\.deepseek\.com|ANTHROPIC_API_KEY|anthropic\.Anthropic", re.I),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Budget:
    calls = 0


@pytest.fixture(scope="module", autouse=True)
def llm_budget():
    budget = _Budget()
    original = interpreter._request_structured_directives

    def _counted(notes, battery):
        # Only a call that can actually reach the network counts against the budget.
        if interpreter.anthropic is not None and interpreter.LLM_API_KEY:
            budget.calls += 1
            if budget.calls > MAX_LLM_CALLS:
                raise RuntimeError(f"LLM call budget of {MAX_LLM_CALLS} exceeded")
        return original(notes, battery)

    interpreter._request_structured_directives = _counted  # type: ignore[assignment]
    try:
        yield budget
    finally:
        interpreter._request_structured_directives = original  # type: ignore[assignment]
    assert budget.calls <= MAX_LLM_CALLS


@pytest.fixture
def offline_llm(monkeypatch: pytest.MonkeyPatch):
    """Default: no network. The heuristic fallback interprets notes."""
    monkeypatch.setattr(interpreter, "_call_llm", lambda notes, battery: None)


@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scenario_id": "api-reliability-001",
        "operator_notes": ["Solar panels are being cleaned from 1 PM to 3 PM, expect 80% reduction."],
        "hours": [
            {"hour": h, "demand_kwh": 40.0, "solar_kwh": 25.0 if 7 <= h <= 17 else 0.0, "tariff_bdt_per_kwh": 12.0 if 17 <= h <= 21 else 5.0}
            for h in range(HORIZON)
        ],
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 10.0,
            "max_charge_kwh_per_hour": 25.0,
            "max_discharge_kwh_per_hour": 25.0,
        },
    }
    body.update(overrides)
    return body


def _assert_contract(body: dict[str, Any], expected_scenario: str, note_count: int) -> OptimizeEnergyResponse:
    resp = OptimizeEnergyResponse.model_validate(body)
    assert set(body) == {
        "scenario_id",
        "directive_interpretation",
        "hourly_plan",
        "total_grid_kwh",
        "total_cost_bdt",
        "peak_grid_kwh",
        "plan_summary",
    }
    assert resp.scenario_id == expected_scenario
    assert len(resp.directive_interpretation) == note_count
    assert [d.note_index for d in resp.directive_interpretation] == list(range(note_count))
    assert len(resp.hourly_plan) == HORIZON
    assert [i.hour for i in resp.hourly_plan] == list(range(HORIZON))
    for item in resp.hourly_plan:
        assert item.battery_action in (BatteryAction.CHARGE, BatteryAction.DISCHARGE, BatteryAction.IDLE)
        assert item.battery_kwh >= 0.0 and item.grid_kwh >= 0.0 and item.solar_used_kwh >= 0.0
    assert resp.total_grid_kwh == pytest.approx(sum(i.grid_kwh for i in resp.hourly_plan), abs=0.01)
    assert resp.peak_grid_kwh == pytest.approx(max(i.grid_kwh for i in resp.hourly_plan), abs=0.01)
    assert resp.plan_summary.strip()
    return resp


def _assert_no_leak(text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(text), f"response leaks internals: {pattern.pattern}"
    if config.LLM_API_KEY:
        assert config.LLM_API_KEY not in text


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert response.headers["content-type"].startswith("application/json")

    def test_health_is_fast_and_stable(self, client):
        started = time.perf_counter()
        for _ in range(5):
            assert client.get("/health").json() == {"status": "ok"}
        assert time.perf_counter() - started < 5.0


# ---------------------------------------------------------------------------
# POST /optimize-energy contract
# ---------------------------------------------------------------------------


class TestOptimizeContract:
    def test_echoes_scenario_id_and_matches_24h_contract(self, client, offline_llm):
        payload = _payload(scenario_id="echo-me-42")
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        resp = _assert_contract(response.json(), "echo-me-42", len(payload["operator_notes"]))
        assert resp.directive_interpretation[0].directive_type is DirectiveType.SOLAR_REDUCTION
        assert resp.directive_interpretation[0].structured_adjustment.hours == [13, 14]

    def test_three_notes_yield_three_interpretations(self, client, offline_llm):
        payload = _payload(
            scenario_id="three-notes",
            operator_notes=[
                "Do not charge the battery from 2 PM to 5 PM.",
                "Keep at least 30% battery reserve from 6 PM to 10 PM.",
                "Cafeteria birthday party at noon.",
            ],
        )
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        resp = _assert_contract(response.json(), "three-notes", 3)
        assert [d.directive_type for d in resp.directive_interpretation] == [
            DirectiveType.NO_CHARGE_WINDOW,
            DirectiveType.MINIMUM_BATTERY_RESERVE,
            DirectiveType.NO_OP,
        ]

    def test_unordered_hours_are_accepted_and_returned_sorted(self, client, offline_llm):
        payload = _payload(scenario_id="unordered")
        payload["hours"] = list(reversed(payload["hours"]))
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200, response.text
        _assert_contract(response.json(), "unordered", 1)

    @pytest.mark.skipif(not LIVE, reason="no API key configured; live contract check skipped")
    def test_live_llm_contract_single_call(self, client, llm_budget):
        payload = _payload(scenario_id="live-contract")
        started = time.perf_counter()
        response = client.post(ENDPOINT, json=payload)
        assert time.perf_counter() - started < MAX_REQUEST_SECONDS
        assert response.status_code == 200, response.text
        _assert_contract(response.json(), "live-contract", 1)
        _assert_no_leak(response.text)
        assert llm_budget.calls <= MAX_LLM_CALLS


# ---------------------------------------------------------------------------
# Malformed / invalid payloads
# ---------------------------------------------------------------------------


def _bad_payloads() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = []
    cases.append(("empty-body", {}))
    cases.append(("not-an-object", [1, 2, 3]))
    p = _payload(); del p["scenario_id"]; cases.append(("missing-scenario_id", p))
    cases.append(("empty-scenario_id", _payload(scenario_id="")))
    cases.append(("no-notes", _payload(operator_notes=[])))
    cases.append(("too-many-notes", _payload(operator_notes=["a", "b", "c", "d"])))
    cases.append(("notes-not-strings", _payload(operator_notes=[123])))
    p = _payload(); p["hours"] = p["hours"][:23]; cases.append(("23-hours", p))
    p = _payload(); p["hours"].append(p["hours"][0]); cases.append(("25-hours", p))
    p = _payload(); p["hours"][5]["hour"] = 24; cases.append(("hour-out-of-range", p))
    p = _payload(); p["hours"][3]["hour"] = 4; cases.append(("duplicate-hour", p))
    p = _payload(); p["hours"][0]["demand_kwh"] = -1; cases.append(("negative-demand", p))
    p = _payload(); p["hours"][0]["solar_kwh"] = "lots"; cases.append(("non-numeric-solar", p))
    p = _payload(); p["hours"][0]["tariff_bdt_per_kwh"] = None; cases.append(("null-tariff", p))
    p = _payload(); del p["hours"][0]["demand_kwh"]; cases.append(("missing-hour-field", p))
    p = _payload(); p["hours"][0]["extra"] = 1; cases.append(("unknown-hour-field", p))
    p = _payload(); p["battery"]["capacity_kwh"] = -5; cases.append(("negative-capacity", p))
    p = _payload(); p["battery"]["initial_energy_kwh"] = 500; cases.append(("initial-above-capacity", p))
    p = _payload(); p["battery"]["minimum_energy_kwh"] = 60; cases.append(("initial-below-minimum", p))
    p = _payload(); del p["battery"]["max_charge_kwh_per_hour"]; cases.append(("missing-battery-field", p))
    p = _payload(); p["battery"] = "big"; cases.append(("battery-not-object", p))
    p = _payload(); p["unexpected"] = True; cases.append(("unknown-top-level-field", p))
    return cases


@pytest.mark.parametrize("payload", [c[1] for c in _bad_payloads()], ids=[c[0] for c in _bad_payloads()])
def test_invalid_payload_is_rejected_with_4xx(client, offline_llm, payload):
    response = client.post(ENDPOINT, json=payload)
    assert response.status_code in (400, 422), response.text
    body = response.json()
    assert "detail" in body
    _assert_no_leak(response.text)


def test_non_json_body_is_rejected(client, offline_llm):
    response = client.post(ENDPOINT, content=b"this is not json", headers={"content-type": "application/json"})
    assert response.status_code in (400, 422)
    _assert_no_leak(response.text)


def test_wrong_method_is_rejected(client):
    assert client.get(ENDPOINT).status_code == 405
    assert client.put(ENDPOINT, json=_payload()).status_code == 405


# ---------------------------------------------------------------------------
# LLM failure modes: controlled 500 or graceful fallback, no leaks, no hangs
# ---------------------------------------------------------------------------


class TestLLMFailureHandling:
    def _run(self, client, expected_scenario: str) -> tuple[int, dict[str, Any], float]:
        started = time.perf_counter()
        response = client.post(ENDPOINT, json=_payload(scenario_id=expected_scenario))
        elapsed = time.perf_counter() - started
        assert elapsed < MAX_REQUEST_SECONDS, f"request hung for {elapsed:.1f}s"
        _assert_no_leak(response.text)
        assert response.status_code in (200, 500), response.text
        body = response.json()
        if response.status_code == 500:
            assert body == {"detail": "Internal server error"}
        else:
            _assert_contract(body, expected_scenario, 1)
        return response.status_code, body, elapsed

    def test_api_exception_falls_back_gracefully(self, client, monkeypatch):
        def _boom(notes, battery):
            raise RuntimeError("API failure: 503 Service Unavailable; key=sk-secretsecretsecret")

        monkeypatch.setattr(interpreter, "_request_structured_directives", _boom)
        status, body, _ = self._run(client, "llm-api-failure")
        assert status == 200
        assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"

    def test_llm_timeout_is_bounded_and_falls_back(self, client, monkeypatch):
        monkeypatch.setattr(interpreter, "LLM_TIMEOUT_SECONDS", 0.5)

        def _slow(notes, battery):
            time.sleep(5.0)
            return {"directives": []}

        monkeypatch.setattr(interpreter, "_request_structured_directives", _slow)
        status, body, elapsed = self._run(client, "llm-timeout")
        assert status == 200
        assert elapsed < 5.0
        assert body["directive_interpretation"][0]["directive_type"] == "solar_reduction"

    def test_garbage_llm_payload_falls_back(self, client, monkeypatch):
        monkeypatch.setattr(interpreter, "_request_structured_directives", lambda n, b: {"directives": ["??", None, 7]})
        status, body, _ = self._run(client, "llm-garbage")
        assert status == 200
        assert body["directive_interpretation"][0]["applies"] is True

    def test_missing_sdk_falls_back(self, client, monkeypatch):
        monkeypatch.setattr(interpreter, "anthropic", None)
        status, body, _ = self._run(client, "llm-no-sdk")
        assert status == 200

    def test_missing_api_key_falls_back_without_leak(self, client, monkeypatch):
        monkeypatch.setattr(interpreter, "LLM_API_KEY", "")
        status, body, _ = self._run(client, "llm-no-key")
        assert status == 200

    def test_internal_optimizer_failure_returns_controlled_500(self, client, offline_llm, monkeypatch):
        from app import optimizer

        def _crash(*args, **kwargs):
            raise ValueError("solver exploded with secret sk-abcdefghijklmnop in message")

        monkeypatch.setattr(optimizer, "solve_energy_schedule", _crash)
        status, body, _ = self._run(client, "optimizer-crash")
        assert status == 500
        assert body == {"detail": "Internal server error"}

    def test_health_unaffected_by_llm_outage(self, client, monkeypatch):
        monkeypatch.setattr(interpreter, "_request_structured_directives", lambda n, b: (_ for _ in ()).throw(ConnectionError("down")))
        assert client.get("/health").json() == {"status": "ok"}

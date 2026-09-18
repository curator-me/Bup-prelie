"""QA suite for operator-note interpretation (``app.interpreter`` + ``app.guardrails``).

Covered invariants:
    1. Exactly one ``directive_interpretation`` entry per operator note, ordered 0..N-1.
    2. ``no_op`` strictly forces ``applies=False`` and ``structured_adjustment=None``.
    3. Every other directive forces ``applies=True`` and the exact required JSON shape.
    4. Paraphrase robustness: varied phrasings map to identical directives and integers.
    5. Numeric fields are bounded, non-negative; ``hours`` are unique and strictly ascending.

By default the LLM is stubbed so the suite is deterministic and offline. Set
``INTERPRETATION_TESTS_USE_LLM=1`` (or ``SAMPLE_TESTS_USE_LLM=1``) to run the
pipeline-level tests against the configured live model instead.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app import interpreter
from app.main import app
from app.schemas import (
    BatteryConfig,
    DirectiveInterpretation,
    DirectiveType,
    OptimizeEnergyResponse,
)

# ---------------------------------------------------------------------------
# Constants: exact JSON shape required by the problem statement
# ---------------------------------------------------------------------------

CAPACITY_KWH = 100.0
ALL_HOURS = list(range(24))

ENTRY_KEYS = {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}

ADJUSTMENT_KEYS: dict[DirectiveType, set[str]] = {
    DirectiveType.SOLAR_REDUCTION: {"kind", "hours", "factor"},
    DirectiveType.MINIMUM_BATTERY_RESERVE: {"kind", "hours", "minimum_energy_kwh"},
    DirectiveType.NO_CHARGE_WINDOW: {"kind", "hours"},
    DirectiveType.NO_DISCHARGE_WINDOW: {"kind", "hours"},
    DirectiveType.MAX_GRID_WINDOW: {"kind", "hours", "max_grid_kwh"},
}

ADJUSTMENT_KIND: dict[DirectiveType, str] = {
    DirectiveType.SOLAR_REDUCTION: "solar_reduction",
    DirectiveType.MINIMUM_BATTERY_RESERVE: "minimum_battery_reserve",
    DirectiveType.NO_CHARGE_WINDOW: "window",
    DirectiveType.NO_DISCHARGE_WINDOW: "window",
    DirectiveType.MAX_GRID_WINDOW: "max_grid_window",
}

NUMERIC_KEY: dict[DirectiveType, str] = {
    DirectiveType.SOLAR_REDUCTION: "factor",
    DirectiveType.MINIMUM_BATTERY_RESERVE: "minimum_energy_kwh",
    DirectiveType.MAX_GRID_WINDOW: "max_grid_kwh",
}

USE_LIVE_LLM = os.getenv("INTERPRETATION_TESTS_USE_LLM") == "1" or os.getenv("SAMPLE_TESTS_USE_LLM") == "1"


# ---------------------------------------------------------------------------
# Paraphrase corpus: every phrasing in a group must yield the identical directive
# ---------------------------------------------------------------------------

PARAPHRASE_GROUPS: list[dict[str, Any]] = [
    {
        "id": "solar_80pct_13_15",
        "directive_type": DirectiveType.SOLAR_REDUCTION,
        "adjustment": {"kind": "solar_reduction", "hours": [13, 14], "factor": 0.2},
        "notes": [
            "Solar panels are being cleaned from 1 PM to 3 PM, expect 80% reduction.",
            "Between 13:00 and 15:00 PV output drops by 80 percent due to panel cleaning.",
            "Solar drops to 20% of normal from 1pm until 3pm.",
            "From 1 PM to 3 PM solar generation is reduced by 80%.",
        ],
    },
    {
        "id": "solar_offline_all_day",
        "directive_type": DirectiveType.SOLAR_REDUCTION,
        "adjustment": {"kind": "solar_reduction", "hours": ALL_HOURS, "factor": 0.0},
        "notes": [
            "Solar panels offline all day for maintenance.",
            "Solar array is unavailable for the entire day.",
        ],
    },
    {
        "id": "no_charge_14_17",
        "directive_type": DirectiveType.NO_CHARGE_WINDOW,
        "adjustment": {"kind": "window", "hours": [14, 15, 16]},
        "notes": [
            "Do not charge the battery from 2 PM to 5 PM.",
            "Battery charging is prohibited between 14:00 and 17:00.",
            "No charging 2pm-5pm.",
            "Avoid charging the battery from 2 p.m. until 5 p.m.",
        ],
    },
    {
        "id": "no_discharge_18_21",
        "directive_type": DirectiveType.NO_DISCHARGE_WINDOW,
        "adjustment": {"kind": "window", "hours": [18, 19, 20]},
        "notes": [
            "Do not discharge the battery between 6 PM and 9 PM.",
            "Avoid discharging from 18:00 to 21:00.",
            "The battery must not be discharged 6pm-9pm.",
        ],
    },
    {
        "id": "reserve_30kwh_18_22",
        "directive_type": DirectiveType.MINIMUM_BATTERY_RESERVE,
        "adjustment": {"kind": "minimum_battery_reserve", "hours": [18, 19, 20, 21], "minimum_energy_kwh": 30.0},
        "notes": [
            "Keep at least 30% battery reserve from 6 PM to 10 PM.",
            "Maintain a minimum battery charge of 30 kWh between 18:00 and 22:00.",
            "Battery reserve must stay at 30% of capacity from 6pm until 10pm.",
        ],
    },
    {
        "id": "max_grid_40kwh_17_20",
        "directive_type": DirectiveType.MAX_GRID_WINDOW,
        "adjustment": {"kind": "max_grid_window", "hours": [17, 18, 19], "max_grid_kwh": 40.0},
        "notes": [
            "Grid import must not exceed 40 kWh per hour between 5 PM and 8 PM.",
            "Cap grid draw at 40 kWh from 17:00 to 20:00.",
            "Grid intake capped at 40 kWh from 5pm to 8pm.",
        ],
    },
    {
        "id": "no_op_distractors",
        "directive_type": DirectiveType.NO_OP,
        "adjustment": None,
        "notes": [
            "The cafeteria will host a birthday party at noon.",
            "Library seminar runs from 2 PM to 4 PM.",
            "Visitor tour of the facility scheduled for the afternoon.",
        ],
    },
]

ALL_CORPUS_NOTES: list[str] = [note for group in PARAPHRASE_GROUPS for note in group["notes"]]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _battery(capacity: float = CAPACITY_KWH) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=capacity,
        initial_energy_kwh=capacity * 0.5,
        minimum_energy_kwh=capacity * 0.1,
        max_charge_kwh_per_hour=25.0,
        max_discharge_kwh_per_hour=25.0,
    )


@pytest.fixture
def battery() -> BatteryConfig:
    return _battery()


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], list[DirectiveInterpretation]]:
    """Full interpretation pipeline; deterministic (LLM stubbed out) unless live mode is enabled."""
    if not USE_LIVE_LLM:
        monkeypatch.setattr(interpreter, "_call_llm", lambda notes, battery: None)
    bat = _battery()
    return lambda notes: interpreter.interpret_notes(notes, bat)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Install a canned LLM payload (or exception) behind the real ``_call_llm`` plumbing."""

    def install(payload: Any) -> None:
        def _fake(notes: list[str], battery: BatteryConfig) -> Any:
            if isinstance(payload, BaseException):
                raise payload
            return payload

        monkeypatch.setattr(interpreter, "_request_structured_directives", _fake)

    return install


def _llm_item(note_index: int, directive_type: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "note_index": note_index,
        "directive_type": directive_type,
        "applies": directive_type != "no_op",
        "hours": [],
        "factor": None,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "test",
    }
    item.update(overrides)
    return item


def _dump(entry: DirectiveInterpretation) -> dict[str, Any]:
    return entry.model_dump(mode="json")


def _assert_one_per_note_in_order(entries: list[DirectiveInterpretation], note_count: int) -> None:
    assert len(entries) == note_count
    assert [e.note_index for e in entries] == list(range(note_count))
    assert len({e.note_index for e in entries}) == note_count


def _assert_exact_shape(entry: DirectiveInterpretation) -> None:
    data = _dump(entry)
    assert set(data.keys()) == ENTRY_KEYS
    assert isinstance(data["note_index"], int) and not isinstance(data["note_index"], bool)
    assert isinstance(data["applies"], bool)
    assert data["directive_type"] in {dt.value for dt in DirectiveType}
    assert isinstance(data["explanation"], str) and data["explanation"].strip()

    if entry.directive_type is DirectiveType.NO_OP:
        assert data["applies"] is False
        assert data["structured_adjustment"] is None
        return

    assert data["applies"] is True
    adjustment = data["structured_adjustment"]
    assert isinstance(adjustment, dict)
    assert set(adjustment.keys()) == ADJUSTMENT_KEYS[entry.directive_type]
    assert adjustment["kind"] == ADJUSTMENT_KIND[entry.directive_type]
    assert isinstance(adjustment["hours"], list)
    for hour in adjustment["hours"]:
        assert isinstance(hour, int) and not isinstance(hour, bool)
    numeric_key = NUMERIC_KEY.get(entry.directive_type)
    if numeric_key is not None:
        assert isinstance(adjustment[numeric_key], (int, float)) and not isinstance(adjustment[numeric_key], bool)


def _assert_numeric_invariants(entry: DirectiveInterpretation, capacity: float = CAPACITY_KWH) -> None:
    if entry.structured_adjustment is None:
        assert entry.directive_type is DirectiveType.NO_OP
        return
    adjustment = entry.structured_adjustment.model_dump()

    hours = adjustment["hours"]
    assert hours, "an applied directive must cover at least one hour"
    assert all(h >= 0 for h in hours)
    assert all(0 <= h <= 23 for h in hours)
    assert len(set(hours)) == len(hours), f"hours are not unique: {hours}"
    assert hours == sorted(hours), f"hours are not ascending: {hours}"
    assert all(b > a for a, b in zip(hours, hours[1:])), f"hours are not strictly increasing: {hours}"

    if "factor" in adjustment:
        assert math.isfinite(adjustment["factor"])
        assert 0.0 <= adjustment["factor"] <= 1.0
    if "minimum_energy_kwh" in adjustment:
        assert math.isfinite(adjustment["minimum_energy_kwh"])
        assert 0.0 <= adjustment["minimum_energy_kwh"] <= capacity
    if "max_grid_kwh" in adjustment:
        assert math.isfinite(adjustment["max_grid_kwh"])
        assert adjustment["max_grid_kwh"] >= 0.0


# ---------------------------------------------------------------------------
# 1. Exactly one entry per note, in note_index order 0..N-1
# ---------------------------------------------------------------------------


class TestOneEntryPerNoteInOrder:
    @pytest.mark.parametrize("note_count", [1, 2, 3])
    def test_llm_covering_every_note_yields_exactly_n_ordered_entries(self, fake_llm, battery, note_count):
        notes = ALL_CORPUS_NOTES[:note_count]
        fake_llm({"directives": [_llm_item(i, "no_charge_window", hours=[1, 2]) for i in range(note_count)]})
        entries = interpreter.interpret_notes(notes, battery)
        _assert_one_per_note_in_order(entries, note_count)

    def test_out_of_order_llm_output_is_reordered_by_note_index(self, fake_llm, battery):
        notes = ["Do not charge 1 AM to 3 AM.", "Solar down 50% 9 AM to 11 AM.", "Party at noon."]
        fake_llm(
            {
                "directives": [
                    _llm_item(2, "no_op"),
                    _llm_item(0, "no_charge_window", hours=[1, 2]),
                    _llm_item(1, "solar_reduction", hours=[9, 10], factor=0.5),
                ]
            }
        )
        entries = interpreter.interpret_notes(notes, battery)
        _assert_one_per_note_in_order(entries, 3)
        assert [e.directive_type for e in entries] == [
            DirectiveType.NO_CHARGE_WINDOW,
            DirectiveType.SOLAR_REDUCTION,
            DirectiveType.NO_OP,
        ]

    def test_duplicate_and_out_of_range_indices_are_dropped_first_wins(self, fake_llm, battery):
        notes = ["Do not charge 1 AM to 3 AM.", "Party at noon."]
        fake_llm(
            {
                "directives": [
                    _llm_item(0, "no_charge_window", hours=[1, 2]),
                    _llm_item(0, "solar_reduction", hours=[1, 2], factor=0.1),  # duplicate: ignored
                    _llm_item(1, "no_op"),
                    _llm_item(5, "max_grid_window", hours=[3], max_grid_kwh=10),  # out of range: ignored
                    _llm_item(-1, "no_discharge_window", hours=[3]),  # negative: ignored
                ]
            }
        )
        entries = interpreter.interpret_notes(notes, battery)
        _assert_one_per_note_in_order(entries, 2)
        assert entries[0].directive_type is DirectiveType.NO_CHARGE_WINDOW
        assert entries[1].directive_type is DirectiveType.NO_OP

    def test_note_missing_from_llm_output_is_still_emitted_in_position(self, fake_llm, battery):
        notes = ["Party at noon.", "Do not charge the battery from 1 AM to 3 AM.", "Seminar at 4 PM."]
        fake_llm({"directives": [_llm_item(0, "no_op"), _llm_item(2, "no_op")]})  # note 1 missing
        entries = interpreter.interpret_notes(notes, battery)
        _assert_one_per_note_in_order(entries, 3)
        assert entries[1].directive_type is DirectiveType.NO_CHARGE_WINDOW  # filled by fallback

    @pytest.mark.parametrize(
        "payload",
        [
            RuntimeError("provider down"),
            {"unexpected": "shape"},
            {"directives": "not-a-list"},
            {"directives": [None, 42, "junk", {"note_index": "x"}]},
            None,
        ],
        ids=["exception", "wrong-keys", "non-list", "junk-items", "none"],
    )
    def test_unusable_llm_output_still_yields_exactly_n_ordered_entries(self, fake_llm, battery, payload):
        notes = ALL_CORPUS_NOTES[:3]
        fake_llm(payload)
        entries = interpreter.interpret_notes(notes, battery)
        _assert_one_per_note_in_order(entries, 3)

    def test_empty_note_list_yields_no_entries(self, pipeline):
        assert pipeline([]) == []

    @pytest.mark.parametrize("note_count", [1, 2, 3])
    def test_pipeline_emits_one_entry_per_note_in_order(self, pipeline, note_count):
        entries = pipeline(ALL_CORPUS_NOTES[:note_count])
        _assert_one_per_note_in_order(entries, note_count)

    def test_http_response_directive_interpretation_matches_notes(self, monkeypatch):
        if not USE_LIVE_LLM:
            monkeypatch.setattr(interpreter, "_call_llm", lambda notes, battery: None)
        notes = [
            "Solar panels are being cleaned from 1 PM to 3 PM, expect 80% reduction.",
            "Do not charge the battery from 2 PM to 5 PM.",
            "The cafeteria will host a birthday party at noon.",
        ]
        payload = {
            "scenario_id": "qa-interpretation",
            "operator_notes": notes,
            "hours": [
                {"hour": h, "demand_kwh": 40.0, "solar_kwh": 20.0 if 6 <= h <= 17 else 0.0, "tariff_bdt_per_kwh": 8.0 if 17 <= h <= 22 else 5.0}
                for h in range(24)
            ],
            "battery": _battery().model_dump(),
        }
        with TestClient(app) as client:
            response = client.post("/optimize-energy", json=payload)
        assert response.status_code == 200, response.text
        body = OptimizeEnergyResponse.model_validate(response.json())
        _assert_one_per_note_in_order(body.directive_interpretation, len(notes))
        for entry in body.directive_interpretation:
            _assert_exact_shape(entry)
            _assert_numeric_invariants(entry)


# ---------------------------------------------------------------------------
# 2. no_op strictly forces applies=False and structured_adjustment=None
# ---------------------------------------------------------------------------


class TestNoOpInvariants:
    def test_no_op_from_llm_with_contradicting_fields_is_normalised(self, fake_llm, battery):
        fake_llm(
            {
                "directives": [
                    _llm_item(0, "no_op", applies=True, hours=[1, 2, 3], factor=0.5, minimum_energy_kwh=10, max_grid_kwh=20)
                ]
            }
        )
        (entry,) = interpreter.interpret_notes(["Party at noon."], battery)
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.applies is False
        assert entry.structured_adjustment is None
        _assert_exact_shape(entry)

    @pytest.mark.parametrize("alias", ["no_op", "NO_OP", "No Op", "noop", "no-op", "none", ""])
    def test_no_op_aliases_from_llm_force_invariants(self, fake_llm, battery, alias):
        fake_llm({"directives": [_llm_item(0, alias, applies=True, hours=[4])]})
        (entry,) = interpreter.interpret_notes(["Party at noon."], battery)
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.applies is False
        assert entry.structured_adjustment is None

    def test_unknown_directive_type_degrades_to_strict_no_op(self, fake_llm, battery):
        fake_llm({"directives": [_llm_item(0, "teleport_energy", applies=True, hours=[4])]})
        (entry,) = interpreter.interpret_notes(["Party at noon."], battery)
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.applies is False
        assert entry.structured_adjustment is None

    @pytest.mark.parametrize(
        "note",
        next(g["notes"] for g in PARAPHRASE_GROUPS if g["id"] == "no_op_distractors"),
    )
    def test_pipeline_no_op_notes_force_invariants(self, pipeline, note):
        (entry,) = pipeline([note])
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.applies is False
        assert entry.structured_adjustment is None
        _assert_exact_shape(entry)

    def test_schema_rejects_no_op_with_adjustment_only_via_guardrails(self, battery):
        """Direct model construction can carry an adjustment; the guardrail layer must strip it."""
        from app.guardrails import validate_and_sanitize_directives

        raw = [
            {
                "note_index": 0,
                "directive_type": "no_op",
                "applies": True,
                "structured_adjustment": {"kind": "window", "hours": [1]},
                "explanation": "x",
            }
        ]
        (entry,) = validate_and_sanitize_directives(raw, battery, 1)
        assert entry.applies is False
        assert entry.structured_adjustment is None


# ---------------------------------------------------------------------------
# 3. Non-no_op directives force applies=True and the exact JSON shape
# ---------------------------------------------------------------------------


class TestAppliedDirectiveShape:
    CASES = [
        ("solar_reduction", {"hours": [13, 14], "factor": 0.2}, DirectiveType.SOLAR_REDUCTION),
        ("minimum_battery_reserve", {"hours": [18, 19], "minimum_energy_kwh": 30}, DirectiveType.MINIMUM_BATTERY_RESERVE),
        ("no_charge_window", {"hours": [14, 15, 16]}, DirectiveType.NO_CHARGE_WINDOW),
        ("no_discharge_window", {"hours": [18, 19, 20]}, DirectiveType.NO_DISCHARGE_WINDOW),
        ("max_grid_window", {"hours": [17, 18, 19], "max_grid_kwh": 40}, DirectiveType.MAX_GRID_WINDOW),
    ]

    @pytest.mark.parametrize("dtype,fields,expected", CASES, ids=[c[0] for c in CASES])
    def test_llm_directive_forces_applies_true_and_exact_shape(self, fake_llm, battery, dtype, fields, expected):
        fake_llm({"directives": [_llm_item(0, dtype, applies=False, **fields)]})  # applies=False must be overridden
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.directive_type is expected
        assert entry.applies is True
        assert entry.structured_adjustment is not None
        _assert_exact_shape(entry)
        _assert_numeric_invariants(entry)
        adjustment = _dump(entry)["structured_adjustment"]
        assert adjustment["hours"] == fields["hours"]
        numeric_key = NUMERIC_KEY.get(expected)
        if numeric_key:
            assert adjustment[numeric_key] == pytest.approx(float(fields[numeric_key]))

    @pytest.mark.parametrize("dtype,fields,expected", CASES, ids=[c[0] for c in CASES])
    def test_llm_directive_with_no_hours_covers_full_day(self, fake_llm, battery, dtype, fields, expected):
        fields = {k: v for k, v in fields.items() if k != "hours"}
        fake_llm({"directives": [_llm_item(0, dtype, hours=[], **fields)]})
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.applies is True
        assert entry.structured_adjustment.hours == ALL_HOURS

    @pytest.mark.parametrize("dtype", ["solar_reduction", "minimum_battery_reserve", "max_grid_window"])
    def test_directive_missing_its_numeric_value_is_not_applied(self, fake_llm, battery, dtype):
        fake_llm({"directives": [_llm_item(0, dtype, hours=[1, 2])]})  # numeric field left null
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.applies is False
        assert entry.structured_adjustment is None

    def test_provider_field_aliases_are_accepted(self, fake_llm, battery):
        """Providers that ignore the JSON schema may rename fields; the parser must still apply them."""
        fake_llm(
            {
                "directives": [
                    {"note_index": 0, "type": "solar_reduction", "applies": True, "hours": [13, 14], "factor": 0.2, "explanation": "a"},
                    {"note_index": 1, "type": "minimum_battery_reserve", "applies": True, "hours": [18], "minimum_battery_reserve": 30, "explanation": "b"},
                ]
            }
        )
        entries = interpreter.interpret_notes(["n0", "n1"], battery)
        _assert_one_per_note_in_order(entries, 2)
        assert entries[0].directive_type is DirectiveType.SOLAR_REDUCTION
        assert entries[0].structured_adjustment.factor == pytest.approx(0.2)
        assert entries[1].directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE
        assert entries[1].structured_adjustment.minimum_energy_kwh == pytest.approx(30.0)

    @pytest.mark.parametrize("note", ALL_CORPUS_NOTES)
    def test_pipeline_every_entry_has_exact_shape(self, pipeline, note):
        (entry,) = pipeline([note])
        _assert_exact_shape(entry)
        if entry.directive_type is not DirectiveType.NO_OP:
            assert entry.applies is True
            assert entry.structured_adjustment is not None


# ---------------------------------------------------------------------------
# 4. Paraphrase robustness
# ---------------------------------------------------------------------------


class TestParaphraseRobustness:
    @pytest.mark.parametrize("group", PARAPHRASE_GROUPS, ids=[g["id"] for g in PARAPHRASE_GROUPS])
    def test_all_phrasings_map_to_identical_directive(self, pipeline, group):
        results = []
        for note in group["notes"]:
            (entry,) = pipeline([note])
            results.append(entry)

        for note, entry in zip(group["notes"], results):
            assert entry.directive_type is group["directive_type"], f"{note!r} -> {entry.directive_type}"
            adjustment = None if entry.structured_adjustment is None else entry.structured_adjustment.model_dump()
            if adjustment is not None:
                for key, value in adjustment.items():
                    if isinstance(value, float):
                        assert value == pytest.approx(group["adjustment"][key], abs=1e-6), f"{note!r}: {key}"
                    else:
                        assert value == group["adjustment"][key], f"{note!r}: {key}"
            else:
                assert group["adjustment"] is None, f"{note!r} produced no adjustment"

        first = results[0].structured_adjustment
        for entry in results[1:]:
            if first is None:
                assert entry.structured_adjustment is None
            else:
                assert entry.structured_adjustment.model_dump() == pytest.approx(first.model_dump())

    @pytest.mark.parametrize(
        "note,expected_hours",
        [
            ("Do not charge the battery from 1 PM to 3 PM.", [13, 14]),
            ("Do not charge the battery from 13:00 to 15:00.", [13, 14]),
            ("Do not charge the battery 1pm-3pm.", [13, 14]),
            ("Do not charge the battery between 1 p.m. and 3 p.m.", [13, 14]),
            ("Do not charge the battery from 11 AM until 2 PM.", [11, 12, 13]),
            ("Do not charge the battery from 11:00 to 14:00.", [11, 12, 13]),
            ("Do not charge the battery from 10 PM to 2 AM.", [0, 1, 22, 23]),  # wraps midnight; emitted ascending
            ("Do not charge the battery from midnight to 6 AM.", [0, 1, 2, 3, 4, 5]),
            ("Do not charge the battery from noon to 1 PM.", [12]),
            ("Do not charge the battery all day.", ALL_HOURS),
        ],
    )
    def test_equivalent_whole_hour_expressions_map_to_same_integers(self, pipeline, note, expected_hours):
        (entry,) = pipeline([note])
        assert entry.directive_type is DirectiveType.NO_CHARGE_WINDOW, note
        assert entry.structured_adjustment.hours == expected_hours, note

    @pytest.mark.parametrize(
        "note,expected_factor",
        [
            ("Solar output reduced by 80% from 1 PM to 3 PM.", 0.2),
            ("Solar drops to 20% of normal from 1 PM to 3 PM.", 0.2),
            ("Solar panels cut to 20 percent of usual from 1 PM to 3 PM.", 0.2),
            ("Solar reduced by 50% from 1 PM to 3 PM.", 0.5),
            ("Solar drops to 50% of normal from 1 PM to 3 PM.", 0.5),
            ("Solar panels offline from 1 PM to 3 PM.", 0.0),
            ("Solar reduced by 100% from 1 PM to 3 PM.", 0.0),
        ],
    )
    def test_percentage_phrasings_map_to_same_factor(self, pipeline, note, expected_factor):
        (entry,) = pipeline([note])
        assert entry.directive_type is DirectiveType.SOLAR_REDUCTION, note
        assert entry.structured_adjustment.hours == [13, 14], note
        assert entry.structured_adjustment.factor == pytest.approx(expected_factor, abs=1e-6), note

    @pytest.mark.parametrize(
        "note,expected_kwh",
        [
            ("Keep at least 30% battery reserve from 6 PM to 10 PM.", 30.0),
            ("Keep at least 30 kWh in reserve from 6 PM to 10 PM.", 30.0),
            ("Maintain at least 30 percent state of charge from 6 PM to 10 PM.", 30.0),
        ],
    )
    def test_reserve_percent_and_kwh_phrasings_are_equivalent(self, pipeline, note, expected_kwh):
        (entry,) = pipeline([note])
        assert entry.directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE, note
        assert entry.structured_adjustment.hours == [18, 19, 20, 21], note
        assert entry.structured_adjustment.minimum_energy_kwh == pytest.approx(expected_kwh), note

    def test_paraphrases_are_stable_within_a_multi_note_request(self, pipeline):
        group = next(g for g in PARAPHRASE_GROUPS if g["id"] == "solar_80pct_13_15")
        notes = group["notes"][:3]
        entries = pipeline(notes)
        _assert_one_per_note_in_order(entries, len(notes))
        dumps = [e.structured_adjustment.model_dump() for e in entries]
        assert all(d["hours"] == [13, 14] for d in dumps)
        assert all(d["factor"] == pytest.approx(0.2, abs=1e-6) for d in dumps)


# ---------------------------------------------------------------------------
# 5. Numeric invariants: bounded, non-negative, unique, ascending
# ---------------------------------------------------------------------------


class TestNumericInvariants:
    @pytest.mark.parametrize("note", ALL_CORPUS_NOTES)
    def test_pipeline_output_satisfies_numeric_invariants(self, pipeline, note):
        (entry,) = pipeline([note])
        _assert_numeric_invariants(entry)

    @pytest.mark.parametrize(
        "raw_hours,expected",
        [
            ([14, 16, 15], [14, 15, 16]),
            ([16, 16, 14, 15, 14], [14, 15, 16]),
            ([-3, 0, 23, 24, 99], [0, 23]),
            ([5.0, "6", 7], [5, 6, 7]),
            ([True, 2, 3], [2, 3]),
            ([2.5, "x", None, 8], [8]),
        ],
        ids=["unsorted", "duplicates", "out-of-range", "coercible", "bool-dropped", "garbage-dropped"],
    )
    def test_llm_hours_are_normalised_to_unique_sorted_range(self, fake_llm, battery, raw_hours, expected):
        fake_llm({"directives": [_llm_item(0, "no_charge_window", hours=raw_hours)]})
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.applies is True
        assert entry.structured_adjustment.hours == expected
        _assert_numeric_invariants(entry)

    def test_llm_hours_entirely_invalid_falls_back_safely(self, fake_llm, battery):
        fake_llm({"directives": [_llm_item(0, "no_charge_window", hours=[-1, 24, "noon"])]})
        (entry,) = interpreter.interpret_notes(["Party at noon."], battery)
        _assert_exact_shape(entry)
        _assert_numeric_invariants(entry)

    @pytest.mark.parametrize(
        "raw_factor,expected",
        [(1.7, 1.0), (-0.3, 0.0), (0.0, 0.0), (1.0, 1.0), (0.25, 0.25), ("0.4", 0.4)],
        ids=["above-one", "negative", "zero", "one", "interior", "string"],
    )
    def test_factor_is_clamped_to_unit_interval(self, fake_llm, battery, raw_factor, expected):
        fake_llm({"directives": [_llm_item(0, "solar_reduction", hours=[13, 14], factor=raw_factor)]})
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.applies is True
        assert entry.structured_adjustment.factor == pytest.approx(expected)
        _assert_numeric_invariants(entry)

    @pytest.mark.parametrize("raw_factor", [float("nan"), float("inf"), -float("inf"), "abc", True])
    def test_non_finite_or_non_numeric_factor_is_rejected(self, fake_llm, battery, raw_factor):
        fake_llm({"directives": [_llm_item(0, "solar_reduction", hours=[13, 14], factor=raw_factor)]})
        (entry,) = interpreter.interpret_notes(["Party at noon."], battery)
        assert entry.directive_type is DirectiveType.NO_OP
        assert entry.structured_adjustment is None

    @pytest.mark.parametrize(
        "raw_reserve,expected",
        [(-10, 0.0), (0, 0.0), (30, 30.0), (CAPACITY_KWH, CAPACITY_KWH), (CAPACITY_KWH * 5, CAPACITY_KWH)],
        ids=["negative", "zero", "interior", "at-capacity", "above-capacity"],
    )
    def test_reserve_is_clamped_to_battery_capacity(self, fake_llm, battery, raw_reserve, expected):
        fake_llm({"directives": [_llm_item(0, "minimum_battery_reserve", hours=[18], minimum_energy_kwh=raw_reserve)]})
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.applies is True
        assert entry.structured_adjustment.minimum_energy_kwh == pytest.approx(expected)
        _assert_numeric_invariants(entry)

    @pytest.mark.parametrize("raw_grid,expected", [(-5, 0.0), (0, 0.0), (40, 40.0), (1e6, 1e6)])
    def test_max_grid_is_non_negative(self, fake_llm, battery, raw_grid, expected):
        fake_llm({"directives": [_llm_item(0, "max_grid_window", hours=[17, 18, 19], max_grid_kwh=raw_grid)]})
        (entry,) = interpreter.interpret_notes(["some note"], battery)
        assert entry.applies is True
        assert entry.structured_adjustment.max_grid_kwh == pytest.approx(expected)
        assert entry.structured_adjustment.max_grid_kwh >= 0.0
        _assert_numeric_invariants(entry)

    def test_hours_in_multi_note_request_are_each_unique_and_sorted(self, fake_llm, battery):
        fake_llm(
            {
                "directives": [
                    _llm_item(0, "no_charge_window", hours=[3, 1, 2, 1]),
                    _llm_item(1, "solar_reduction", hours=[14, 13, 13], factor=0.2),
                    _llm_item(2, "max_grid_window", hours=[19, 17, 18, 18], max_grid_kwh=40),
                ]
            }
        )
        entries = interpreter.interpret_notes(["a", "b", "c"], battery)
        _assert_one_per_note_in_order(entries, 3)
        for entry in entries:
            _assert_numeric_invariants(entry)
        assert entries[0].structured_adjustment.hours == [1, 2, 3]
        assert entries[1].structured_adjustment.hours == [13, 14]
        assert entries[2].structured_adjustment.hours == [17, 18, 19]

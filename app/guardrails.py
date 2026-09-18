"""Deterministic guardrails applied to LLM-produced directive interpretations.

Everything that leaves this module is a fully validated ``DirectiveInterpretation``
that the optimizer can trust: one entry per operator note, ordered by note index,
with every structured adjustment clamped into the physically meaningful range.
Any record that cannot be repaired is downgraded to a safe ``no_op``.

This module never raises on malformed input.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from pydantic import BaseModel

from app.schemas import (
    BatteryConfig,
    DirectiveInterpretation,
    DirectiveType,
    MaxGridAdjustment,
    ReserveAdjustment,
    SolarAdjustment,
    WindowAdjustment,
)

logger = logging.getLogger(__name__)

MAX_EXPLANATION_CHARS = 500
_MIN_HOUR = 0
_MAX_HOUR = 23

_DIRECTIVE_ALIASES: dict[str, DirectiveType] = {
    dt.value: dt for dt in DirectiveType
}
_DIRECTIVE_ALIASES.update(
    {
        "solar-reduction": DirectiveType.SOLAR_REDUCTION,
        "solarreduction": DirectiveType.SOLAR_REDUCTION,
        "min_battery_reserve": DirectiveType.MINIMUM_BATTERY_RESERVE,
        "minimum-battery-reserve": DirectiveType.MINIMUM_BATTERY_RESERVE,
        "battery_reserve": DirectiveType.MINIMUM_BATTERY_RESERVE,
        "no-charge-window": DirectiveType.NO_CHARGE_WINDOW,
        "no_charge": DirectiveType.NO_CHARGE_WINDOW,
        "no-discharge-window": DirectiveType.NO_DISCHARGE_WINDOW,
        "no_discharge": DirectiveType.NO_DISCHARGE_WINDOW,
        "max-grid-window": DirectiveType.MAX_GRID_WINDOW,
        "max_grid": DirectiveType.MAX_GRID_WINDOW,
        "noop": DirectiveType.NO_OP,
        "no-op": DirectiveType.NO_OP,
        "none": DirectiveType.NO_OP,
        "": DirectiveType.NO_OP,
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_and_sanitize_directives(
    raw_interpretations: list[dict],
    battery: BatteryConfig,
    note_count: int,
) -> list[DirectiveInterpretation]:
    """Validate, clamp and normalise LLM directive output.

    Args:
        raw_interpretations: Whatever the LLM returned, ideally a list of dicts
            shaped like ``DirectiveInterpretation``. Any shape is tolerated.
        battery: Validated battery configuration used to bound reserve values.
        note_count: Number of operator notes the response must cover. The
            returned list always has exactly this many entries.

    Returns:
        Exactly ``note_count`` interpretations, one per note, ordered by
        ``note_index`` from 0 to ``note_count - 1``. Missing, duplicate,
        out-of-range or irreparable records are replaced by a safe ``no_op``.
    """
    records = _as_record_list(raw_interpretations)
    total = max(0, _safe_int(note_count) or 0)
    capacity = _battery_capacity(battery)

    by_index: dict[int, DirectiveInterpretation] = {}
    for position, record in enumerate(records):
        try:
            note_index = _coerce_note_index(record, total)
            if note_index is None:
                logger.warning("guardrails: record %d has unusable note_index; dropped", position)
                continue
            if note_index in by_index:
                logger.warning("guardrails: duplicate note_index %d; keeping first", note_index)
                continue
            by_index[note_index] = _sanitize_record(record, note_index, capacity)
        except Exception:  # noqa: BLE001 - guardrail must never propagate
            logger.exception("guardrails: record %d is corrupted; falling back to no_op", position)
            fallback_index = _safe_int(record.get("note_index")) if isinstance(record, Mapping) else None
            if fallback_index is not None and 0 <= fallback_index < total and fallback_index not in by_index:
                by_index[fallback_index] = _no_op(fallback_index, "Record was corrupted and could not be interpreted.")

    result: list[DirectiveInterpretation] = []
    for idx in range(total):
        item = by_index.get(idx)
        if item is None:
            logger.warning("guardrails: no interpretation for note %d; defaulting to no_op", idx)
            item = _no_op(idx, "No valid interpretation was produced for this note.")
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Record-level sanitization
# ---------------------------------------------------------------------------


def _sanitize_record(record: Mapping[str, Any], note_index: int, capacity: Optional[float]) -> DirectiveInterpretation:
    directive_type = _coerce_directive_type(record.get("directive_type"))
    explanation = _coerce_explanation(record.get("explanation"))

    if directive_type is None:
        return _no_op(
            note_index,
            _with_reason(explanation, "Unrecognised directive type; treated as no_op."),
        )

    if directive_type is DirectiveType.NO_OP:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.NO_OP,
            structured_adjustment=None,
            explanation=explanation or "No actionable directive.",
        )

    adjustment = _build_adjustment(directive_type, record.get("structured_adjustment"), capacity)
    if adjustment is None:
        return _no_op(
            note_index,
            _with_reason(explanation, f"Adjustment for '{directive_type.value}' was missing or invalid; treated as no_op."),
        )

    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=directive_type,
        structured_adjustment=adjustment,
        explanation=explanation or f"Applied {directive_type.value}.",
    )


def _build_adjustment(directive_type: DirectiveType, raw: Any, capacity: Optional[float]):
    payload = _as_mapping(raw)
    if payload is None:
        return None

    hours = _sanitize_hours(payload.get("hours"))
    if not hours:
        return None

    if directive_type is DirectiveType.SOLAR_REDUCTION:
        factor = _safe_float(payload.get("factor"))
        if factor is None:
            return None
        return SolarAdjustment(hours=hours, factor=_clamp(factor, 0.0, 1.0))

    if directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE:
        reserve = _safe_float(payload.get("minimum_energy_kwh"))
        if reserve is None:
            return None
        upper = capacity if capacity is not None else math.inf
        return ReserveAdjustment(hours=hours, minimum_energy_kwh=_clamp(reserve, 0.0, upper))

    if directive_type in (DirectiveType.NO_CHARGE_WINDOW, DirectiveType.NO_DISCHARGE_WINDOW):
        return WindowAdjustment(hours=hours)

    if directive_type is DirectiveType.MAX_GRID_WINDOW:
        max_grid = _safe_float(payload.get("max_grid_kwh"))
        if max_grid is None:
            return None
        return MaxGridAdjustment(hours=hours, max_grid_kwh=max(0.0, max_grid))

    return None


# ---------------------------------------------------------------------------
# Field-level coercion helpers
# ---------------------------------------------------------------------------


def _sanitize_hours(raw: Any) -> list[int]:
    """Return unique ints in [0, 23], strictly ascending. Invalid entries are dropped."""
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Iterable):
        single = _safe_int(raw)
        candidates: list[Any] = [single] if single is not None else []
    else:
        candidates = list(raw)

    valid: set[int] = set()
    for item in candidates:
        hour = _safe_int(item)
        if hour is not None and _MIN_HOUR <= hour <= _MAX_HOUR:
            valid.add(hour)
    return sorted(valid)


def _coerce_directive_type(raw: Any) -> Optional[DirectiveType]:
    if isinstance(raw, DirectiveType):
        return raw
    if raw is None:
        return DirectiveType.NO_OP
    if not isinstance(raw, str):
        return None
    return _DIRECTIVE_ALIASES.get(raw.strip().lower().replace(" ", "_"))


def _coerce_explanation(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        try:
            raw = str(raw)
        except Exception:  # noqa: BLE001
            return ""
    cleaned = "".join(ch for ch in raw if ch.isprintable() or ch in "\n\t").strip()
    if len(cleaned) > MAX_EXPLANATION_CHARS:
        cleaned = cleaned[: MAX_EXPLANATION_CHARS - 1].rstrip() + "…"
    return cleaned


def _coerce_note_index(record: Any, total: int) -> Optional[int]:
    if not isinstance(record, Mapping):
        return None
    idx = _safe_int(record.get("note_index"))
    if idx is None or idx < 0 or idx >= total:
        return None
    return idx


def _safe_int(value: Any) -> Optional[int]:
    """Strictly integral values only: ints, integral floats, integer strings. Never bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            as_float = float(text)
        except ValueError:
            return None
        return int(as_float) if math.isfinite(as_float) and as_float.is_integer() else None
    return None


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _clamp(value: float, low: float, high: float) -> float:
    if high < low:
        high = low
    return min(max(value, low), high)


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def _as_record_list(raw: Any) -> list[Any]:
    if isinstance(raw, Mapping):
        nested = raw.get("directive_interpretation")
        if isinstance(nested, list):
            return nested
        return [raw]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        return []
    try:
        return list(raw)
    except Exception:  # noqa: BLE001
        return []


def _as_mapping(raw: Any) -> Optional[Mapping[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    if isinstance(raw, Mapping):
        return raw
    return None


def _battery_capacity(battery: Any) -> Optional[float]:
    try:
        if not isinstance(battery, BatteryConfig):
            battery = BatteryConfig.model_validate(battery)
        capacity = float(battery.capacity_kwh)
        return capacity if math.isfinite(capacity) and capacity >= 0 else None
    except Exception:  # noqa: BLE001
        logger.warning("guardrails: battery config unusable; reserve upper bound disabled")
        return None


def _with_reason(explanation: str, reason: str) -> str:
    return f"{explanation} [{reason}]" if explanation else reason


def _no_op(note_index: int, explanation: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type=DirectiveType.NO_OP,
        structured_adjustment=None,
        explanation=_coerce_explanation(explanation) or "No actionable directive.",
    )


__all__ = ["validate_and_sanitize_directives", "MAX_EXPLANATION_CHARS"]

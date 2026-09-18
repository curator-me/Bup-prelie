"""Operator-note interpretation: LLM first, deterministic heuristics as a zero-failure fallback.

Pipeline for a request:
    1. One structured-output call to the Anthropic API covering every note (bounded by a hard
       wall-clock timeout, no retries).
    2. Any note the LLM did not cover, or covered with an unusable record, is parsed by
       ``fallback_heuristic_parse``. If the LLM call fails for any reason, every note goes
       through the fallback.
    3. Everything is funnelled through ``app.guardrails`` so the solver only ever sees
       validated, clamped ``DirectiveInterpretation`` objects.

``interpret_notes`` never raises on LLM, network, schema or parsing failures.
"""

from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Optional

try:  # Imported eagerly so the first request does not pay the SDK import cost inside the timeout.
    import anthropic
except ImportError:  # pragma: no cover - the fallback parser still works without the SDK
    anthropic = None  # type: ignore[assignment]

from app import config
from app.guardrails import validate_and_sanitize_directives
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# API key, base URL and model id are read from the environment / .env via app.config.
DEFAULT_MODEL_ID = config.DEFAULT_MODEL_ID
LLM_MODEL_ID = config.LLM_MODEL_ID
LLM_API_KEY = config.LLM_API_KEY
LLM_BASE_URL = config.LLM_BASE_URL
LLM_TIMEOUT_SECONDS = config.LLM_TIMEOUT_SECONDS
LLM_MAX_TOKENS = 1024

ALL_HOURS: list[int] = list(range(24))

_DIRECTIVE_VALUES = [dt.value for dt in DirectiveType]

# Flat schema: no unions or nested optionals, which keeps strict-mode generation reliable
# and cheap. Unused numeric fields are null and get mapped to the typed adjustment below.
_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "directives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {"type": "integer"},
                    "directive_type": {"type": "string", "enum": _DIRECTIVE_VALUES},
                    "applies": {"type": "boolean"},
                    "hours": {"type": "array", "items": {"type": "integer"}},
                    "factor": {"type": ["number", "null"]},
                    "minimum_energy_kwh": {"type": ["number", "null"]},
                    "max_grid_kwh": {"type": ["number", "null"]},
                    "explanation": {"type": "string"},
                },
                "required": [
                    "note_index",
                    "directive_type",
                    "applies",
                    "hours",
                    "factor",
                    "minimum_energy_kwh",
                    "max_grid_kwh",
                    "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["directives"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "Convert facility operator notes into battery/grid directives. Output JSON only.\n"
    "Types: solar_reduction (factor = usable solar fraction: '80% reduction'->0.2, "
    "'drops to 25%'->0.25); minimum_battery_reserve (minimum_energy_kwh; a % means % of "
    "battery capacity); no_charge_window; no_discharge_window; max_grid_window "
    "(max_grid_kwh); no_op (unrelated or no action, applies=false).\n"
    "Hours: integers 0-23, start-inclusive, end-exclusive: '1 PM to 3 PM'->[13,14], "
    "'11 AM until 2 PM'->[11,12,13]. No time window -> all 24 hours.\n"
    "One entry per note, note_index = note position. Unused numeric fields null. "
    "explanation <= 20 words.\n"
    "Output exactly this shape and these field names: {\"directives\": [{\"note_index\": int, "
    "\"directive_type\": string, \"applies\": bool, \"hours\": [int], \"factor\": number|null, "
    "\"minimum_energy_kwh\": number|null, \"max_grid_kwh\": number|null, \"explanation\": string}]}"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def interpret_operator_notes(notes: list[str], battery: BatteryConfig) -> list[dict[str, Any]]:
    """Return one raw (not yet guardrailed) interpretation dict per note.

    The LLM is tried first; any note it does not cover falls back to the heuristic parser.
    Never raises on LLM, network or parsing failure.
    """
    clean_notes = [str(n) if n is not None else "" for n in (notes or [])]
    count = len(clean_notes)
    if count == 0:
        return []

    try:
        llm_records = _call_llm(clean_notes, battery)
    except Exception:  # noqa: BLE001 - belt and braces; _call_llm already swallows errors
        logger.exception("interpreter: unexpected error in LLM path; using fallback for all notes")
        llm_records = None

    by_index: dict[int, dict[str, Any]] = {}
    if llm_records is not None:
        for record in llm_records:
            idx = record.get("note_index")
            if isinstance(idx, int) and 0 <= idx < count and idx not in by_index:
                by_index[idx] = record

    merged: list[dict[str, Any]] = []
    for idx, note in enumerate(clean_notes):
        record = by_index.get(idx)
        if record is None:
            record = _safe_fallback(note, idx, battery).model_dump()
            logger.info("interpreter: note %d parsed by heuristic fallback", idx)
        merged.append(record)
    return merged


def interpret_notes(notes: list[str], battery: BatteryConfig) -> list[DirectiveInterpretation]:
    """Interpret every operator note and run the result through the guardrails. Never raises."""
    count = len(notes or [])
    if count == 0:
        return []
    try:
        raw = interpret_operator_notes(notes, battery)
        return validate_and_sanitize_directives(raw, battery, count)
    except Exception:  # noqa: BLE001 - guardrails should never raise, but stay total
        logger.exception("interpreter: interpretation failed; emitting no_op for every note")
        return [_no_op(i, "Interpretation failed; no directive applied.") for i in range(count)]


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


def _call_llm(notes: list[str], battery: BatteryConfig) -> Optional[list[dict[str, Any]]]:
    """Return guardrail-shaped records from the LLM, or None on any failure."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="note-interpreter")
    try:
        future = executor.submit(_request_structured_directives, notes, battery)
        payload = future.result(timeout=LLM_TIMEOUT_SECONDS + 0.5)
    except FutureTimeoutError:
        logger.warning("interpreter: LLM call exceeded %.1fs; using fallback", LLM_TIMEOUT_SECONDS)
        return None
    except Exception as exc:  # noqa: BLE001 - any API/SDK/auth error degrades to fallback
        logger.warning("interpreter: LLM call failed (%s: %s); using fallback", type(exc).__name__, exc)
        return None
    finally:
        executor.shutdown(wait=False)

    if payload is None:
        return None
    try:
        return _records_from_payload(payload, len(notes))
    except Exception as exc:  # noqa: BLE001
        logger.warning("interpreter: LLM payload unusable (%s); using fallback", exc)
        return None


def _request_structured_directives(notes: list[str], battery: BatteryConfig) -> Optional[dict[str, Any]]:
    """Blocking Anthropic call. Raises on any failure; the caller converts that to fallback."""
    if anthropic is None:
        raise RuntimeError("anthropic SDK is not installed")

    if not LLM_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (check your .env file)")

    client = anthropic.Anthropic(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL or None,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=0,
    )
    user_content = json.dumps(
        {
            "battery_capacity_kwh": battery.capacity_kwh,
            "notes": [{"note_index": i, "text": n} for i, n in enumerate(notes)],
        },
        separators=(",", ":"),
    )
    response = client.messages.create(
        model=LLM_MODEL_ID,
        max_tokens=LLM_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        thinking={"type": "disabled"},
        output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
    )
    if response.stop_reason not in (None, "end_turn", "stop_sequence"):
        raise RuntimeError(f"unexpected stop_reason {response.stop_reason!r}")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return _parse_json_object(text)


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Tolerate a fenced or prefixed block: take the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(text[start : end + 1])
    return data if isinstance(data, dict) else None


def _records_from_payload(payload: dict[str, Any], note_count: int) -> list[dict[str, Any]]:
    items = payload.get("directives")
    if not isinstance(items, list):
        raise ValueError("payload has no 'directives' list")

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("note_index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < note_count):
            continue
        dtype = str(item.get("directive_type") or item.get("type") or "").strip().lower()
        if dtype not in _DIRECTIVE_VALUES:
            continue
        if item.get("minimum_energy_kwh") is None and item.get("minimum_battery_reserve") is not None:
            item["minimum_energy_kwh"] = item.get("minimum_battery_reserve")
        hours = item.get("hours") if isinstance(item.get("hours"), list) else []
        if dtype != DirectiveType.NO_OP.value and not hours:
            hours = ALL_HOURS

        adjustment: Optional[dict[str, Any]]
        if dtype == DirectiveType.SOLAR_REDUCTION.value:
            adjustment = {"hours": hours, "factor": item.get("factor")}
        elif dtype == DirectiveType.MINIMUM_BATTERY_RESERVE.value:
            adjustment = {"hours": hours, "minimum_energy_kwh": item.get("minimum_energy_kwh")}
        elif dtype == DirectiveType.MAX_GRID_WINDOW.value:
            adjustment = {"hours": hours, "max_grid_kwh": item.get("max_grid_kwh")}
        elif dtype in (DirectiveType.NO_CHARGE_WINDOW.value, DirectiveType.NO_DISCHARGE_WINDOW.value):
            adjustment = {"hours": hours}
        else:
            adjustment = None

        records.append(
            {
                "note_index": idx,
                "applies": dtype != DirectiveType.NO_OP.value,
                "directive_type": dtype,
                "structured_adjustment": adjustment,
                "explanation": str(item.get("explanation") or ""),
            }
        )
    return records


# ---------------------------------------------------------------------------
# Deterministic fallback parser
# ---------------------------------------------------------------------------

_MERIDIEM = r"(a\.?m\.?|p\.?m\.?)"
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*" + _MERIDIEM + r"?"
_RANGE_SEP = r"\s*(?:to|until|till|through|thru|and|-|–|—)\s*"

_WINDOW_RE = re.compile(r"(?:from\s+|between\s+)?\b" + _CLOCK + _RANGE_SEP + _CLOCK + r"\b", re.I)
_AFTER_RE = re.compile(r"\b(?:after|past|starting(?:\s+at|\s+from)?|from)\s+" + _CLOCK + r"\b(?:\s+onwards?)?", re.I)
_BEFORE_RE = re.compile(r"\b(?:before|until|till|by|up to)\s+" + _CLOCK + r"\b", re.I)
_UNIT_AFTER_RE = re.compile(r"^\s*(?:%|percent|pct|kwh?|kilowatt)", re.I)

_NAMED_PERIODS: dict[str, list[int]] = {
    "all day": ALL_HOURS,
    "entire day": ALL_HOURS,
    "whole day": ALL_HOURS,
    "24 hours": ALL_HOURS,
    "overnight": list(range(22, 24)) + list(range(0, 6)),
    "night": list(range(22, 24)) + list(range(0, 6)),
    "morning": list(range(6, 12)),
    "afternoon": list(range(12, 18)),
    "evening": list(range(18, 22)),
}

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|pct\b)", re.I)
_KWH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:kwh\b|kw\b|kilowatt(?:-|\s)?hours?\b)", re.I)
_USABLE_PERCENT_RE = re.compile(r"\b(?:to|at|around|only)\s+(?:about|around|roughly|approximately|just)?\s*(\d+(?:\.\d+)?)\s*(?:%|percent\b|pct\b)", re.I)
_PERCENT_OF_NORMAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|pct)\s+of\s+(?:normal|usual|nominal|rated|expected|typical|full)", re.I)

_SOLAR_RE = re.compile(r"\bsolar\b|\bpv\b|\bpanels?\b", re.I)
_SOLAR_EVENT_RE = re.compile(
    r"drop|reduc|clean|cloud|shade|shading|dust|haze|smog|overcast|curtail|offline|outage|maintenance|lower|cut|loss|less"
    r"|limit|unavailab|partial|capped|of\s+(?:normal|usual|nominal|rated|expected|typical|full)|only\s+\d",
    re.I,
)
_SOLAR_ZERO_RE = re.compile(r"clean|offline|outage|maintenance|unavailable|disconnect|shut|zero|no solar|off\b", re.I)

_NO_CHARGE_RE = re.compile(r"do(?:es)?\s*n[o']t\s+charge|don't\s+charge|no\s+charg(?:e|ing)|must\s+not\s+charge|should\s+not\s+charge|charging\s+circuit|battery\s+charger|charger\b.{0,40}\bisolat|isolat.{0,40}\bcharger|not\s+(?:be\s+)?charged|avoid\s+charging|stop\s+charging|charging\s+(?:is\s+)?(?:disabled|prohibited|forbidden|suspended)|pause\s+charging", re.I | re.S)
_NO_DISCHARGE_RE = re.compile(r"do(?:es)?\s*n[o']t\s+discharge|don't\s+discharge|not\s+(?:be\s+)?discharg|no\s+discharg|avoid\s+discharging|stop\s+discharging|discharg(?:e|ing)\s+(?:is\s+)?(?:disabled|prohibited|forbidden|suspended)|pause\s+discharging|discharge\s+lockout", re.I)
_RESERVE_RE = re.compile(r"\breserve|\bstore\b|\bstored\b|keep\s+at\s+least|maintain\s+at\s+least|hold\s+at\s+least|keep\s+(?:the\s+)?battery\s+(?:at|above)|minimum\s+(?:battery|charge|soc|state\s+of\s+charge)|at\s+least\s+\d+(?:\.\d+)?\s*(?:%|percent|kwh)", re.I)
_GRID_RE = re.compile(r"grid\s+import|grid\s+draw|grid\s+intake|\bintake\b|transformer(?:\s+limit)?|\bfeeder\b|grid\s+(?:supply|power|usage|consumption)|import\s+from\s+(?:the\s+)?grid|utility\s+(?:import|supply)", re.I)
_GRID_LIMIT_RE = re.compile(r"not\s+exceed|no\s+more\s+than|\bcap(?:ped|s)?\b|stay\s+(?:at\s+or\s+)?below|(?:at\s+or\s+)?under|limit(?:ed)?\s+to|maximum|max\b|ceiling|below|restrict", re.I)

_DISTRACTOR_RE = re.compile(r"cafeteria|canteen|library|sports?|seminar|workshop|lecture|meeting|party|festival|holiday|parking|garden|lunch|dinner|visitor|tour|cleaning\s+(?:crew|staff)|hr\b|payroll|birthday", re.I)


def fallback_heuristic_parse(note: str, index: int, battery: BatteryConfig) -> DirectiveInterpretation:
    """Deterministic, regex-only interpretation of one note. Never raises."""
    try:
        return _heuristic_parse(str(note or ""), int(index), battery)
    except Exception:  # noqa: BLE001
        logger.exception("interpreter: heuristic parser failed on note %s", index)
        return _no_op(int(index) if isinstance(index, int) else 0, "Note could not be parsed; no directive applied.")


def _safe_fallback(note: str, index: int, battery: BatteryConfig) -> DirectiveInterpretation:
    return fallback_heuristic_parse(note, index, battery)


def _heuristic_parse(note: str, index: int, battery: BatteryConfig) -> DirectiveInterpretation:
    text = _normalise(note)
    if not text:
        return _no_op(index, "Empty note.")

    hours = _extract_hours(text)
    window = _hours_label(hours)
    capacity = _capacity(battery)

    # 1. Solar availability changes.
    if _SOLAR_RE.search(text) and _SOLAR_EVENT_RE.search(text):
        factor = _solar_factor(text)
        return DirectiveInterpretation(
            note_index=index,
            applies=True,
            directive_type=DirectiveType.SOLAR_REDUCTION,
            structured_adjustment=SolarAdjustment(hours=hours, factor=factor),
            explanation=f"Solar limited to {factor:.0%} of forecast {window} (heuristic).",
        )

    # 2. Grid import caps.
    if _GRID_RE.search(text) and _GRID_LIMIT_RE.search(text):
        limit = _first_number(_KWH_RE, text)
        if limit is None:
            return _no_op(index, "Grid limit mentioned without a kWh value; no directive applied (heuristic).")
        limit = max(0.0, limit)
        return DirectiveInterpretation(
            note_index=index,
            applies=True,
            directive_type=DirectiveType.MAX_GRID_WINDOW,
            structured_adjustment=MaxGridAdjustment(hours=hours, max_grid_kwh=limit),
            explanation=f"Grid import capped at {limit:g} kWh/h {window} (heuristic).",
        )

    # 3. Charging lockout.
    if _NO_CHARGE_RE.search(text):
        return DirectiveInterpretation(
            note_index=index,
            applies=True,
            directive_type=DirectiveType.NO_CHARGE_WINDOW,
            structured_adjustment=WindowAdjustment(hours=hours),
            explanation=f"Battery charging blocked {window} (heuristic).",
        )

    # 4. Discharging lockout.
    if _NO_DISCHARGE_RE.search(text):
        return DirectiveInterpretation(
            note_index=index,
            applies=True,
            directive_type=DirectiveType.NO_DISCHARGE_WINDOW,
            structured_adjustment=WindowAdjustment(hours=hours),
            explanation=f"Battery discharging blocked {window} (heuristic).",
        )

    # 5. Minimum reserve.
    if _RESERVE_RE.search(text):
        reserve = _reserve_kwh(text, capacity)
        if reserve is None:
            return _no_op(index, "Reserve mentioned without a usable amount; no directive applied (heuristic).")
        return DirectiveInterpretation(
            note_index=index,
            applies=True,
            directive_type=DirectiveType.MINIMUM_BATTERY_RESERVE,
            structured_adjustment=ReserveAdjustment(hours=hours, minimum_energy_kwh=reserve),
            explanation=f"Battery kept at or above {reserve:g} kWh {window} (heuristic).",
        )

    # 6. Everything else is noise.
    if _DISTRACTOR_RE.search(text):
        return _no_op(index, "Non-energy operational note; no directive applied (heuristic).")
    return _no_op(index, "No recognised energy directive; no directive applied (heuristic).")


# --- fallback helpers ---------------------------------------------------------


def _normalise(note: str) -> str:
    text = note.replace("’", "'").replace("–", "-").replace("—", "-")
    text = re.sub(r"\bmid-?night\b", "12 am", text, flags=re.I)
    text = re.sub(r"\b(?:noon|mid-?day)\b", "12 pm", text, flags=re.I)
    text = re.sub(r"(\d)\s*(?:h|hrs|hours)\b(?!\s*(?:to|until|-))", r"\1:00", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _extract_hours(text: str) -> list[int]:
    text = _normalise(text)
    for match in _WINDOW_RE.finditer(text):
        sh, sm, smer, eh, em, emer = match.groups()
        if not smer and not emer and not sm and not em and _UNIT_AFTER_RE.match(text[match.end():]):
            continue  # "50 to 60 percent" is not a time window
        window = _window_hours(int(sh), int(sm or 0), smer, int(eh), int(em or 0), emer)
        if window:
            return window

    match = _AFTER_RE.search(text)
    if match:
        start = _to_24h(int(match.group(1)), match.group(3))
        if start is not None:
            return list(range(min(start, 23), 24))

    match = _BEFORE_RE.search(text)
    if match:
        end = _to_24h(int(match.group(1)), match.group(3))
        if end is not None:
            end = end + (1 if match.group(2) and int(match.group(2)) > 0 else 0)
            return list(range(0, max(1, min(end, 24))))

    lowered = text.lower()
    for name, hours in _NAMED_PERIODS.items():
        if name in lowered:
            return list(hours)
    return list(ALL_HOURS)


def _window_hours(sh: int, sm: int, smer: Optional[str], eh: int, em: int, emer: Optional[str]) -> list[int]:
    smer, emer = _infer_meridiems(sh, smer, eh, emer)
    start = _to_24h(sh, smer)
    end = _to_24h(eh, emer)
    if start is None or end is None:
        return []
    end_hour = end + (1 if em > 0 else 0)  # end-exclusive: 3:30 PM still covers the 15:00 slot
    if end_hour > 24:
        return []
    if end_hour <= start:
        if end_hour == start and sm == 0 and em == 0:
            return list(ALL_HOURS)  # "12 AM to 12 AM"
        return list(range(start, 24)) + list(range(0, end_hour))  # wraps past midnight
    return list(range(start, end_hour))


def _infer_meridiems(sh: int, smer: Optional[str], eh: int, emer: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    def base(h: int) -> int:
        return 0 if h == 12 else h

    if smer and not emer and eh <= 12:
        emer = smer if base(sh) < base(eh) else _flip(smer)
    elif emer and not smer and sh <= 12:
        smer = emer if base(sh) < base(eh) else _flip(emer)
    return smer, emer


def _flip(meridiem: str) -> str:
    return "pm" if meridiem.lower().startswith("a") else "am"


def _to_24h(hour: int, meridiem: Optional[str]) -> Optional[int]:
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem.lower().startswith("a"):
            return 0 if hour == 12 else hour
        return 12 if hour == 12 else hour + 12
    return hour if 0 <= hour <= 24 else None


def _hours_label(hours: list[int]) -> str:
    if hours == ALL_HOURS:
        return "all day"
    if not hours:
        return "at no hours"
    if hours == list(range(hours[0], hours[-1] + 1)):
        return f"{hours[0]:02d}:00-{hours[-1] + 1:02d}:00"
    return f"during hours {hours}"


def _solar_factor(text: str) -> float:
    match = _PERCENT_OF_NORMAL_RE.search(text) or _USABLE_PERCENT_RE.search(text)
    if match:
        return _clamp01(float(match.group(1)) / 100.0)
    match = _PERCENT_RE.search(text)
    if match:
        return _clamp01(1.0 - float(match.group(1)) / 100.0)
    lowered = text.lower()
    if "half" in lowered:
        return 0.5
    if "quarter" in lowered:
        return 0.75 if "by" in lowered else 0.25
    if _SOLAR_ZERO_RE.search(text):
        return 0.0
    return 0.5


def _reserve_kwh(text: str, capacity: float) -> Optional[float]:
    kwh = _first_number(_KWH_RE, text)
    percent = _first_number(_PERCENT_RE, text)
    if percent is not None and (kwh is None or re.search(r"capacity|soc|state of charge|charge\b|battery", text, re.I)):
        value = capacity * _clamp01(percent / 100.0)
    elif kwh is not None:
        value = kwh
    else:
        return None
    if not math.isfinite(value):
        return None
    return max(0.0, min(value, capacity))


def _first_number(pattern: re.Pattern[str], text: str) -> Optional[float]:
    match = pattern.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _capacity(battery: Any) -> float:
    try:
        capacity = float(battery.capacity_kwh)
        return capacity if math.isfinite(capacity) and capacity >= 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return round(min(1.0, max(0.0, value)), 6)


def _no_op(index: int, explanation: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=index,
        applies=False,
        directive_type=DirectiveType.NO_OP,
        structured_adjustment=None,
        explanation=explanation,
    )


__all__ = [
    "interpret_operator_notes",
    "interpret_notes",
    "fallback_heuristic_parse",
    "LLM_MODEL_ID",
    "LLM_TIMEOUT_SECONDS",
]

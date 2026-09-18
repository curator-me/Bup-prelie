"""24-hour battery/grid dispatch as a linear program solved with HiGHS.

Decision variables per hour h (all continuous, >= 0):
    grid[h]        energy imported from the grid
    solar_used[h]  solar energy consumed
    charge[h]      energy put into the battery
    discharge[h]   energy drawn from the battery

Battery state: E[h] = initial + sum_{i<=h} (charge[i] - discharge[i]).

Objective: minimise sum_h grid[h] * tariff[h].

Solve strategy:
    1. Strict LP with every directive enforced.
    2. If infeasible, re-solve with penalised slack on the reserve floor and grid caps so a
       plan is always produced; the relaxation is reported in ``plan_summary``.
    3. If the solver itself fails, fall back to a solar-then-grid plan with an idle battery.

``optimize_plan`` never raises.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.optimize import linprog

from app.schemas import (
    BatteryAction,
    BatteryConfig,
    DirectiveInterpretation,
    DirectiveType,
    HourlyInput,
    HourlyPlanItem,
    MaxGridAdjustment,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    ReserveAdjustment,
    SolarAdjustment,
    WindowAdjustment,
)

logger = logging.getLogger(__name__)

HORIZON = 24
ACTION_TOLERANCE = 1e-4
_VIOLATION_TOLERANCE = 1e-6
_TIE_BREAK = 1e-5  # relative to the tariff scale; far below the 2-decimal reporting precision

_GRID = 0
_SOLAR = HORIZON
_CHARGE = 2 * HORIZON
_DISCHARGE = 3 * HORIZON
_N_CORE = 4 * HORIZON


# ---------------------------------------------------------------------------
# Directive -> per-hour constraint arrays
# ---------------------------------------------------------------------------


@dataclass
class HourlyConstraints:
    """Per-hour constraint parameters after all applicable directives are merged."""

    solar_factor: list[float]
    min_energy_kwh: list[float]
    max_grid_kwh: list[Optional[float]]
    no_charge: list[bool]
    no_discharge: list[bool]
    applied_directives: int


def build_hourly_constraints(
    directives: list[DirectiveInterpretation], battery: BatteryConfig
) -> HourlyConstraints:
    """Merge applicable directives; overlapping directives take the most restrictive value."""
    hc = HourlyConstraints(
        solar_factor=[1.0] * HORIZON,
        min_energy_kwh=[float(battery.minimum_energy_kwh)] * HORIZON,
        max_grid_kwh=[None] * HORIZON,
        no_charge=[False] * HORIZON,
        no_discharge=[False] * HORIZON,
        applied_directives=0,
    )

    for directive in directives or []:
        adj = directive.structured_adjustment
        if not directive.applies or adj is None or directive.directive_type is DirectiveType.NO_OP:
            continue
        hours = [h for h in adj.hours if 0 <= h < HORIZON]
        if not hours:
            continue

        if isinstance(adj, SolarAdjustment):
            for h in hours:
                hc.solar_factor[h] = min(hc.solar_factor[h], _clamp(adj.factor, 0.0, 1.0))
        elif isinstance(adj, ReserveAdjustment):
            floor = _clamp(adj.minimum_energy_kwh, 0.0, float(battery.capacity_kwh))
            for h in hours:
                hc.min_energy_kwh[h] = max(hc.min_energy_kwh[h], floor)
        elif isinstance(adj, MaxGridAdjustment):
            cap = max(0.0, float(adj.max_grid_kwh))
            for h in hours:
                current = hc.max_grid_kwh[h]
                hc.max_grid_kwh[h] = cap if current is None else min(current, cap)
        elif isinstance(adj, WindowAdjustment):
            if directive.directive_type is DirectiveType.NO_CHARGE_WINDOW:
                for h in hours:
                    hc.no_charge[h] = True
            elif directive.directive_type is DirectiveType.NO_DISCHARGE_WINDOW:
                for h in hours:
                    hc.no_discharge[h] = True
            else:
                continue
        else:
            continue
        hc.applied_directives += 1

    return hc


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class ScheduleResult:
    """Solver output before it is wrapped into the API response."""

    hourly_plan: list[HourlyPlanItem]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
    relaxed: bool


def solve_energy_schedule(
    hours: list[HourlyInput],
    battery: BatteryConfig,
    directives: list[DirectiveInterpretation],
) -> ScheduleResult:
    """Solve the dispatch LP for one 24-hour scenario. Never raises."""
    ordered = sorted(hours, key=lambda item: item.hour)
    demand = np.array([float(x.demand_kwh) for x in ordered])
    solar = np.array([float(x.solar_kwh) for x in ordered])
    tariff = np.array([float(x.tariff_bdt_per_kwh) for x in ordered])

    hc = build_hourly_constraints(directives, battery)
    effective_solar = solar * np.array(hc.solar_factor)

    relaxed_notes: list[str] = []
    solution: Optional[np.ndarray] = None
    try:
        solution = _solve(demand, effective_solar, tariff, battery, hc, relax=False)
        if solution is None:
            logger.warning("optimizer: strict LP infeasible; relaxing soft constraints")
            solution = _solve(demand, effective_solar, tariff, battery, hc, relax=True)
            if solution is not None:
                relaxed_notes = _describe_relaxation(solution, hc)
                solution = solution[:_N_CORE]
    except Exception:  # noqa: BLE001 - solver must never take the API down
        logger.exception("optimizer: LP solve raised")
        solution = None

    if solution is None:
        logger.error("optimizer: falling back to naive dispatch")
        solution = _naive_dispatch(demand, effective_solar)
        relaxed_notes = ["the optimizer could not find a feasible schedule, so a solar-then-grid plan with an idle battery was used"]

    return _build_schedule(solution, tariff, battery, hc, relaxed_notes)


def optimize_plan(
    request: OptimizeEnergyRequest, directives: list[DirectiveInterpretation]
) -> OptimizeEnergyResponse:
    """Convenience wrapper: solve and assemble the full API response. Never raises."""
    schedule = solve_energy_schedule(request.hours, request.battery, directives)
    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=list(directives or []),
        hourly_plan=schedule.hourly_plan,
        total_grid_kwh=schedule.total_grid_kwh,
        total_cost_bdt=schedule.total_cost_bdt,
        peak_grid_kwh=schedule.peak_grid_kwh,
        plan_summary=schedule.plan_summary,
    )


# ---------------------------------------------------------------------------
# LP construction
# ---------------------------------------------------------------------------


def _solve(
    demand: np.ndarray,
    effective_solar: np.ndarray,
    tariff: np.ndarray,
    battery: BatteryConfig,
    hc: HourlyConstraints,
    relax: bool,
) -> Optional[np.ndarray]:
    """Return the variable vector, or None when the LP is infeasible/unbounded."""
    initial = float(battery.initial_energy_kwh)
    capacity = float(battery.capacity_kwh)
    n_slack = 2 * HORIZON if relax else 0  # [reserve slack | grid-cap slack]
    n_vars = _N_CORE + n_slack
    slack_reserve = _N_CORE
    slack_grid = _N_CORE + HORIZON

    # Objective ---------------------------------------------------------------
    # A negligible throughput cost breaks ties between equal-cost plans so the solver does
    # not cycle the battery pointlessly inside a flat-tariff period.
    tariff_scale = max(1.0, float(tariff.max()) if tariff.size else 1.0)
    c = np.zeros(n_vars)
    c[_GRID:_GRID + HORIZON] = tariff
    c[_CHARGE:_CHARGE + HORIZON] = _TIE_BREAK * tariff_scale
    c[_DISCHARGE:_DISCHARGE + HORIZON] = _TIE_BREAK * tariff_scale
    if relax:
        penalty = 1e4 * tariff_scale
        c[slack_reserve:slack_reserve + HORIZON] = penalty
        c[slack_grid:slack_grid + HORIZON] = penalty

    # Bounds ------------------------------------------------------------------
    bounds: list[tuple[float, Optional[float]]] = []
    for h in range(HORIZON):  # grid
        cap = hc.max_grid_kwh[h]
        bounds.append((0.0, None if (relax or cap is None) else cap))
    for h in range(HORIZON):  # solar_used
        bounds.append((0.0, max(0.0, float(effective_solar[h]))))
    for h in range(HORIZON):  # charge
        bounds.append((0.0, 0.0 if hc.no_charge[h] else float(battery.max_charge_kwh_per_hour)))
    for h in range(HORIZON):  # discharge
        bounds.append((0.0, 0.0 if hc.no_discharge[h] else float(battery.max_discharge_kwh_per_hour)))
    bounds.extend([(0.0, None)] * n_slack)

    # Equalities: hourly balance + end-of-day neutrality -----------------------
    a_eq = np.zeros((HORIZON + 1, n_vars))
    b_eq = np.zeros(HORIZON + 1)
    for h in range(HORIZON):
        a_eq[h, _GRID + h] = 1.0
        a_eq[h, _SOLAR + h] = 1.0
        a_eq[h, _DISCHARGE + h] = 1.0
        a_eq[h, _CHARGE + h] = -1.0
        b_eq[h] = demand[h]
    a_eq[HORIZON, _CHARGE:_CHARGE + HORIZON] = 1.0
    a_eq[HORIZON, _DISCHARGE:_DISCHARGE + HORIZON] = -1.0
    b_eq[HORIZON] = 0.0

    # Inequalities: cumulative energy bounds (+ relaxed grid caps) --------------
    ub_rows: list[np.ndarray] = []
    ub_rhs: list[float] = []
    for h in range(HORIZON):
        # min_bound[h] <= E[h]  ->  -sum c + sum d (- slack) <= initial - min_bound[h]
        row = np.zeros(n_vars)
        row[_CHARGE:_CHARGE + h + 1] = -1.0
        row[_DISCHARGE:_DISCHARGE + h + 1] = 1.0
        if relax:
            row[slack_reserve + h] = -1.0
        ub_rows.append(row)
        ub_rhs.append(initial - hc.min_energy_kwh[h])

        # E[h] <= capacity  ->  sum c - sum d <= capacity - initial
        row = np.zeros(n_vars)
        row[_CHARGE:_CHARGE + h + 1] = 1.0
        row[_DISCHARGE:_DISCHARGE + h + 1] = -1.0
        ub_rows.append(row)
        ub_rhs.append(capacity - initial)

        if relax and hc.max_grid_kwh[h] is not None:
            row = np.zeros(n_vars)
            row[_GRID + h] = 1.0
            row[slack_grid + h] = -1.0
            ub_rows.append(row)
            ub_rhs.append(hc.max_grid_kwh[h])

    result = linprog(
        c,
        A_ub=np.vstack(ub_rows),
        b_ub=np.array(ub_rhs),
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if result.status != 0 or result.x is None:
        logger.info("optimizer: linprog status=%s (%s), relax=%s", result.status, result.message, relax)
        return None
    return np.asarray(result.x, dtype=float)


def _describe_relaxation(solution: np.ndarray, hc: HourlyConstraints) -> list[str]:
    reserve_slack = solution[_N_CORE:_N_CORE + HORIZON]
    grid_slack = solution[_N_CORE + HORIZON:_N_CORE + 2 * HORIZON]
    notes: list[str] = []
    reserve_hours = [h for h in range(HORIZON) if reserve_slack[h] > _VIOLATION_TOLERANCE]
    grid_hours = [h for h in range(HORIZON) if grid_slack[h] > _VIOLATION_TOLERANCE]
    if reserve_hours:
        notes.append(f"the battery reserve floor could not be held at hours {reserve_hours}")
    if grid_hours:
        notes.append(f"the grid import cap was exceeded at hours {grid_hours}")
    if not notes:
        notes.append("soft constraints were relaxed to obtain a feasible schedule")
    return notes


def _naive_dispatch(demand: np.ndarray, effective_solar: np.ndarray) -> np.ndarray:
    x = np.zeros(_N_CORE)
    solar_used = np.minimum(demand, np.maximum(effective_solar, 0.0))
    x[_SOLAR:_SOLAR + HORIZON] = solar_used
    x[_GRID:_GRID + HORIZON] = np.maximum(demand - solar_used, 0.0)
    return x


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------


def _build_schedule(
    x: np.ndarray,
    tariff: np.ndarray,
    battery: BatteryConfig,
    hc: HourlyConstraints,
    relaxed_notes: list[str],
) -> ScheduleResult:
    grid = np.maximum(x[_GRID:_GRID + HORIZON], 0.0)
    solar_used = np.maximum(x[_SOLAR:_SOLAR + HORIZON], 0.0)
    net_battery = x[_CHARGE:_CHARGE + HORIZON] - x[_DISCHARGE:_DISCHARGE + HORIZON]

    energy = float(battery.initial_energy_kwh)
    plan: list[HourlyPlanItem] = []
    charge_hours = 0
    discharge_hours = 0
    for h in range(HORIZON):
        net = float(net_battery[h])
        if net > ACTION_TOLERANCE:
            action, battery_kwh = BatteryAction.CHARGE, round(net, 4)
            charge_hours += 1
        elif net < -ACTION_TOLERANCE:
            action, battery_kwh = BatteryAction.DISCHARGE, round(-net, 4)
            discharge_hours += 1
        else:
            action, battery_kwh, net = BatteryAction.IDLE, 0.0, 0.0
        energy = _clamp(energy + net, 0.0, float(battery.capacity_kwh))
        plan.append(
            HourlyPlanItem(
                hour=h,
                grid_kwh=round(float(grid[h]), 4),
                solar_used_kwh=round(float(solar_used[h]), 4),
                battery_action=action,
                battery_kwh=max(0.0, battery_kwh),
                battery_energy_after_kwh=round(energy, 4),
            )
        )

    total_grid = round(float(grid.sum()), 2)
    total_cost = round(float((grid * tariff).sum()), 2)
    peak_grid = round(float(grid.max()) if grid.size else 0.0, 2)

    return ScheduleResult(
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=_summarise(total_grid, total_cost, peak_grid, charge_hours, discharge_hours, hc, relaxed_notes),
        relaxed=bool(relaxed_notes),
    )


def _summarise(
    total_grid: float,
    total_cost: float,
    peak_grid: float,
    charge_hours: int,
    discharge_hours: int,
    hc: HourlyConstraints,
    relaxed_notes: list[str],
) -> str:
    directive_text = (
        "no operator directives"
        if hc.applied_directives == 0
        else f"{hc.applied_directives} operator directive{'s' if hc.applied_directives != 1 else ''}"
    )
    battery_text = (
        "keeping the battery idle"
        if charge_hours == 0 and discharge_hours == 0
        else f"charging the battery in {charge_hours} hour{'s' if charge_hours != 1 else ''} "
        f"and discharging in {discharge_hours} hour{'s' if discharge_hours != 1 else ''}"
    )
    sentence = (
        f"The plan imports {total_grid:.2f} kWh from the grid for {total_cost:.2f} BDT with a peak draw of "
        f"{peak_grid:.2f} kWh, {battery_text} while honouring {directive_text}"
    )
    if relaxed_notes:
        sentence += ", although " + "; ".join(relaxed_notes)
    return sentence + "."


def _clamp(value: float, low: float, high: float) -> float:
    if not math.isfinite(value):
        return low
    if high < low:
        high = low
    return min(max(float(value), low), high)


__all__ = [
    "solve_energy_schedule",
    "optimize_plan",
    "build_hourly_constraints",
    "HourlyConstraints",
    "ScheduleResult",
    "ACTION_TOLERANCE",
]

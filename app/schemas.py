"""Pydantic v2 request/response models for the GridWise energy optimization API."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DirectiveType(str, Enum):
    """Category of operator directive extracted from a free-text note."""

    SOLAR_REDUCTION = "solar_reduction"
    MINIMUM_BATTERY_RESERVE = "minimum_battery_reserve"
    NO_CHARGE_WINDOW = "no_charge_window"
    NO_DISCHARGE_WINDOW = "no_discharge_window"
    MAX_GRID_WINDOW = "max_grid_window"
    NO_OP = "no_op"


class BatteryAction(str, Enum):
    """Battery behaviour scheduled for a single hour."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"


# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

Hour = Annotated[int, Field(ge=0, le=23, description="Hour of day, 0..23.")]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]


class _StrictModel(BaseModel):
    """Base model: reject unknown fields and validate on attribute assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class BatteryConfig(_StrictModel):
    """Physical and operational limits of the battery storage system."""

    capacity_kwh: NonNegativeFloat = Field(
        ..., description="Total usable battery capacity in kWh."
    )
    initial_energy_kwh: NonNegativeFloat = Field(
        ..., description="Energy stored in the battery at the start of hour 0 (kWh)."
    )
    minimum_energy_kwh: NonNegativeFloat = Field(
        ..., description="Energy level the battery must never fall below (kWh)."
    )
    max_charge_kwh_per_hour: NonNegativeFloat = Field(
        ..., description="Maximum energy that can be added to the battery in one hour (kWh)."
    )
    max_discharge_kwh_per_hour: NonNegativeFloat = Field(
        ..., description="Maximum energy that can be drawn from the battery in one hour (kWh)."
    )

    @model_validator(mode="after")
    def _check_energy_bounds(self) -> "BatteryConfig":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh must not exceed capacity_kwh")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh must not exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError("initial_energy_kwh must not be below minimum_energy_kwh")
        return self


class HourlyInput(_StrictModel):
    """Forecast demand, solar generation and tariff for a single hour."""

    hour: Hour
    demand_kwh: NonNegativeFloat = Field(..., description="Site demand for the hour (kWh).")
    solar_kwh: NonNegativeFloat = Field(
        ..., description="Forecast solar generation available for the hour (kWh)."
    )
    tariff_bdt_per_kwh: NonNegativeFloat = Field(
        ..., description="Grid import tariff for the hour in BDT per kWh."
    )


class OptimizeEnergyRequest(_StrictModel):
    """Full optimization request covering a single 24-hour scenario."""

    scenario_id: str = Field(..., min_length=1, description="Caller-supplied scenario identifier.")
    operator_notes: list[str] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="Free-text operator notes to be interpreted as directives (1..3).",
    )
    hours: list[HourlyInput] = Field(
        ...,
        min_length=24,
        max_length=24,
        description="Exactly 24 hourly inputs, one per hour of the day.",
    )
    battery: BatteryConfig

    @model_validator(mode="after")
    def _check_hours_cover_full_day(self) -> "OptimizeEnergyRequest":
        seen = [item.hour for item in self.hours]
        if sorted(seen) != list(range(24)):
            raise ValueError("hours must contain each hour 0..23 exactly once")
        return self


# ---------------------------------------------------------------------------
# Response schemas: structured adjustments
# ---------------------------------------------------------------------------


class SolarAdjustment(_StrictModel):
    """Scale solar availability by `factor` during the listed hours."""

    kind: Literal["solar_reduction"] = "solar_reduction"
    hours: list[Hour] = Field(..., description="Hours the adjustment applies to.")
    factor: float = Field(
        ..., ge=0.0, le=1.0, description="Multiplier applied to solar_kwh (0.0..1.0)."
    )


class ReserveAdjustment(_StrictModel):
    """Raise the minimum battery reserve during the listed hours."""

    kind: Literal["minimum_battery_reserve"] = "minimum_battery_reserve"
    hours: list[Hour] = Field(..., description="Hours the adjustment applies to.")
    minimum_energy_kwh: NonNegativeFloat = Field(
        ..., description="Minimum battery energy to maintain during these hours (kWh)."
    )


class WindowAdjustment(_StrictModel):
    """Forbid a battery action (charge or discharge) during the listed hours."""

    kind: Literal["window"] = "window"
    hours: list[Hour] = Field(..., description="Hours the restriction applies to.")


class MaxGridAdjustment(_StrictModel):
    """Cap grid import during the listed hours."""

    kind: Literal["max_grid_window"] = "max_grid_window"
    hours: list[Hour] = Field(..., description="Hours the cap applies to.")
    max_grid_kwh: NonNegativeFloat = Field(
        ..., description="Maximum grid import allowed per hour in the window (kWh)."
    )


StructuredAdjustment = Optional[
    Annotated[
        Union[SolarAdjustment, ReserveAdjustment, WindowAdjustment, MaxGridAdjustment],
        Field(discriminator="kind"),
    ]
]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class DirectiveInterpretation(_StrictModel):
    """How a single operator note was interpreted and whether it was applied."""

    note_index: int = Field(..., ge=0, description="Zero-based index into operator_notes.")
    applies: bool = Field(..., description="Whether the directive was applied to the plan.")
    directive_type: DirectiveType
    structured_adjustment: StructuredAdjustment = Field(
        default=None, description="Machine-readable form of the directive, if any."
    )
    explanation: str = Field(..., description="Human-readable rationale for the interpretation.")


class HourlyPlanItem(_StrictModel):
    """Optimized dispatch for a single hour."""

    hour: Hour
    grid_kwh: NonNegativeFloat = Field(..., description="Energy imported from the grid (kWh).")
    solar_used_kwh: NonNegativeFloat = Field(..., description="Solar energy consumed (kWh).")
    battery_action: BatteryAction
    battery_kwh: NonNegativeFloat = Field(
        ..., description="Energy charged into or discharged from the battery this hour (kWh)."
    )
    battery_energy_after_kwh: NonNegativeFloat = Field(
        ..., description="Battery energy level at the end of the hour (kWh)."
    )


class OptimizeEnergyResponse(_StrictModel):
    """Complete optimization result for a scenario."""

    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanItem] = Field(..., min_length=24, max_length=24)
    total_grid_kwh: NonNegativeFloat = Field(..., description="Sum of grid_kwh over all hours.")
    total_cost_bdt: NonNegativeFloat = Field(..., description="Total grid import cost in BDT.")
    peak_grid_kwh: NonNegativeFloat = Field(..., description="Largest single-hour grid import (kWh).")
    plan_summary: str = Field(..., description="Short human-readable summary of the plan.")


class HealthResponse(_StrictModel):
    """Liveness probe response."""

    status: str = "ok"


__all__ = [
    "BatteryAction",
    "BatteryConfig",
    "DirectiveInterpretation",
    "DirectiveType",
    "HealthResponse",
    "HourlyInput",
    "HourlyPlanItem",
    "MaxGridAdjustment",
    "OptimizeEnergyRequest",
    "OptimizeEnergyResponse",
    "ReserveAdjustment",
    "SolarAdjustment",
    "StructuredAdjustment",
    "WindowAdjustment",
]

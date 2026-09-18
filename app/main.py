"""FastAPI entry point for the GridWise energy optimization service.

Flow for ``POST /optimize-energy``:
    operator notes -> interpreter (LLM + heuristic fallback)
                   -> guardrails (deterministic validation and clamping)
                   -> optimizer (HiGHS linear program)
                   -> OptimizeEnergyResponse

Malformed payloads are rejected with 422 by FastAPI/Pydantic. Any internal failure is
logged server-side and surfaced as a generic 500 without traceback or secret leakage.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import guardrails, interpreter, optimizer
from app.schemas import HealthResponse, OptimizeEnergyRequest, OptimizeEnergyResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="GridWise Energy Optimizer",
    description="Interprets operator notes and produces a cost-minimal 24-hour battery/grid dispatch plan.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_INTERNAL_ERROR = {"detail": "Internal server error"}


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence: never leak tracebacks or environment details to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content=_INTERNAL_ERROR)


@app.get("/health", response_model=HealthResponse, tags=["ops"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/optimize-energy",
    response_model=OptimizeEnergyResponse,
    responses={500: {"description": "Internal server error"}},
    tags=["optimization"],
)
def optimize_energy(request: OptimizeEnergyRequest) -> OptimizeEnergyResponse | JSONResponse:
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        raw_interpretations = interpreter.interpret_operator_notes(request.operator_notes, request.battery)

        valid_directives = guardrails.validate_and_sanitize_directives(
            raw_interpretations, request.battery, note_count=len(request.operator_notes)
        )

        schedule = optimizer.solve_energy_schedule(request.hours, request.battery, valid_directives)

        response = OptimizeEnergyResponse(
            scenario_id=request.scenario_id,
            directive_interpretation=valid_directives,
            hourly_plan=schedule.hourly_plan,
            total_grid_kwh=schedule.total_grid_kwh,
            total_cost_bdt=schedule.total_cost_bdt,
            peak_grid_kwh=schedule.peak_grid_kwh,
            plan_summary=schedule.plan_summary,
        )
    except Exception:  # noqa: BLE001 - convert every internal failure into a clean 500
        logger.exception(
            "optimize-energy failed [request_id=%s scenario_id=%s]", request_id, request.scenario_id
        )
        return JSONResponse(status_code=500, content=_INTERNAL_ERROR)

    logger.info(
        "optimize-energy ok [request_id=%s scenario_id=%s applied=%d cost_bdt=%.2f relaxed=%s elapsed_ms=%.0f]",
        request_id,
        request.scenario_id,
        sum(1 for d in valid_directives if d.applies),
        response.total_cost_bdt,
        schedule.relaxed,
        (time.perf_counter() - started) * 1000,
    )
    return response


def main() -> None:
    """Run the service with uvicorn (used by ``python -m app.main`` and the Dockerfile)."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()

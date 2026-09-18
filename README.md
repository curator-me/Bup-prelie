# Bup-prelie

FastAPI service that interprets free-text operator notes and produces a cost-minimal 24-hour battery/grid dispatch plan.

## Quickstart (local)

```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # then fill in your key
python -m app.main                                   # serves on http://0.0.0.0:8000
pytest
```

## Environment variables

Copy `.env.example` to `.env` and set:

```
ANTHROPIC_API_KEY=...                                    # provider API key (never committed)
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic    # Anthropic-compatible endpoint
LLM_MODEL_ID=deepseek-v4-pro
LLM_TIMEOUT_SECONDS=8                                    # per-call limit before heuristic fallback
HOST=0.0.0.0
PORT=8000
LOG_LEVEL=INFO
```

Real environment variables override `.env`. In Docker pass them at run time (`--env-file .env`); the image contains no credentials.

## Model / provider

Notes are interpreted by an LLM through the Anthropic Messages API (`anthropic` SDK). The default provider is DeepSeek's Anthropic-compatible endpoint with model `deepseek-v4-pro`; any Anthropic-compatible provider works by changing `ANTHROPIC_BASE_URL` and `LLM_MODEL_ID`. Extended thinking is disabled for latency. When the key is missing, the call fails, or the timeout is hit, notes fall back to a deterministic heuristic parser, so the API never depends on the LLM being up.

## Solver architecture

```
operator notes -> interpreter (LLM + heuristic fallback)
              -> guardrails  (validate, clamp, one directive per note)
              -> optimizer   (linear program solved with HiGHS via scipy.optimize.linprog)
              -> OptimizeEnergyResponse
```

The optimizer minimises `sum(grid_kwh * tariff)` subject to hourly energy balance, solar availability (scaled by solar_reduction directives), battery capacity / reserve floors, charge and discharge rate limits, no-charge / no-discharge windows, grid import caps and end-of-day neutrality (hour 23 energy equals the initial energy). If the strict LP is infeasible it re-solves with penalised slack and reports the relaxation in `plan_summary`.

## Layout

```
app/
  config.py       loads .env and exposes LLM settings
  schemas.py      request/response models
  guardrails.py   validation and safety checks
  interpreter.py  raw request -> structured representation
  optimizer.py    structured representation -> final result
  main.py         entry point
tests/
  test_samples.py          official sample cases end-to-end
  test_interpretation.py   directive interpretation invariants
  test_constraints.py      physical constraints on hourly_plan
  test_optimization.py     cost minimisation and aggregates
  test_api_reliability.py  HTTP contract and failure handling
verify_deployment.sh       build, run, health-check and README audit
```

## Docker

```
docker build -t bup-prelie .
docker run --rm -p 8000:8000 --env-file .env bup-prelie
./verify_deployment.sh
```

## Sample test curl

```
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "demo-1",
    "operator_notes": ["Solar panels are being cleaned from 1 PM to 3 PM, expect 80% reduction."],
    "hours": [ {"hour": 0, "demand_kwh": 40, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}, "... one object per hour 0-23 ..." ],
    "battery": {"capacity_kwh": 100, "initial_energy_kwh": 50, "minimum_energy_kwh": 10,
                "max_charge_kwh_per_hour": 25, "max_discharge_kwh_per_hour": 25}
  }'
```

Health check: `curl http://localhost:8000/health` returns `{"status": "ok"}`.

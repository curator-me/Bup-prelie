# ⚡ GridWise — Smart Campus Energy Management System

**Event:** BUP CSE FEST 2026 (Preliminary Round)  
**Track:** Smart Campus Energy Optimization  
**Service Type:** Unified REST API (FastAPI + Pydantic v2 + SciPy HiGHS)  

| Submission Deliverable | Value / Reference Link |
| :--- | :--- |
| **Docker Fallback Image** | `docker pull <dockerhub-username>/gridwise:latest` (or `ghcr.io/<username>/gridwise:latest`) |
| **Docker Hub Registry Link** | [hub.docker.io/akulbiswas/bup-preli) |
| **Live Deployed API URL** | `(https://bup-prelie.vercel.app/)` |
| **Endpoints Exposed** | `GET /health` & `POST /optimize-energy` (Port `8000`) |

GridWise is an end-to-end smart campus energy optimization service that translates unstructured natural language operator notes into mathematical constraints and schedules 24-hour battery and grid dispatch via a high-performance Linear Programming solver.

---

## 🏛️ System Architecture

```
[ Operator Request (JSON) ]
            │
            ▼
   ┌──────────────────────────────────────────────┐
   │  FastAPI Gateway & Exception Shield          │  (app/main.py)
   │  POST /optimize-energy  |  GET /health       │
   └──────────────────────┬───────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────┐
   │  Dual-Track Interpretation Engine            │  (app/interpreter.py)
   │  • Track A: LLM Structured Output (8s limit) │
   │  • Track B: Zero-Failure Regex Heuristic     │
   └──────────────────────┬───────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────┐
   │  Deterministic Guardrails Validator          │  (app/guardrails.py)
   │  Bounds checking, window clamping, audits    │
   └──────────────────────┬───────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────┐
   │  SciPy HiGHS Linear Program Solver           │  (app/optimizer.py)
   │  96 decision variables, global cost minimum  │
   └──────────────────────┬───────────────────────┘
                          │
                          ▼
[ 200 OK: 24h Hourly Dispatch Plan + Directives Audit ]
```

---

## 🧠 Core Components

### 1. LLM Role & Provider Setup
- **Role:** Extracts operational constraints (solar reduction, battery reserve levels, charge/discharge lockouts, grid caps) from free-form, unstructured operator notes into structured Pydantic objects.
- **Provider / Model:** Anthropic-compatible API endpoint (default: `deepseek-v4-pro` via DeepSeek / Anthropic).
- **Zero-Failure Redundancy:** The interpreter executes with a strict 8-second wall-clock timeout. If the LLM times out, rate limits, or if an API key is not configured, the engine automatically falls back to a deterministic, regex-based heuristic parser (`fallback_heuristic_parse`). The system **never crashes** due to LLM or network failures.

### 2. Deterministic Guardrails (`app/guardrails.py`)
- **Hourly Window Clamping:** Sanitizes all time ranges to valid integer hours in `[0, 24)` and enforces start-inclusive, end-exclusive interval semantics.
- **Physical Capacity Bounds:** Clamps minimum reserve energy to the physical battery capacity (`0 <= reserve <= capacity_kwh`), bounds solar multipliers to `[0.0, 1.0]`, and ensures non-negative grid import caps.
- **Transparency & Audit Trail:** Produces a machine-checkable `DirectiveInterpretation` entry for every input note in original `note_index` order, indicating `applies: true/false` and an explanatory reason for auditability.

### 3. Optimizer & Solver (`app/optimizer.py`)
- **Solver Engine:** Uses the `HiGHS` solver via `scipy.optimize.linprog(method="highs")`.
- **Formulation:** Linear Program (LP) with 96 continuous variables across 24 hours:
  - $G_t$: Energy imported from grid (kWh)
  - $S_t$: Solar energy utilized (kWh)
  - $C_t$: Battery charge energy (kWh)
  - $D_t$: Battery discharge energy (kWh)
- **Objective:** $\min \sum_{t=0}^{23} (G_t \times \text{Tariff}_t)$
- **Constraints Enforced:**
  1. *Hourly Energy Balance:* $G_t + S_t + D_t = \text{Demand}_t + C_t$
  2. *Solar Availability:* $0 \le S_t \le \text{Solar}_t \times \alpha_t$
  3. *Battery SoC Evolution:* $E_t = E_{t-1} + C_t - D_t$ with $\max(E_{\min}, R_t) \le E_t \le E_{\text{cap}}$
  4. *Charge / Discharge Rate Limits:* $0 \le C_t \le R_{\text{charge}}$, $0 \le D_t \le R_{\text{discharge}}$
  5. *End-of-Day Neutrality:* $E_{23} \ge E_{\text{initial}}$ (ensures sustainable day-to-day operation)
  6. *Lockout Windows:* $C_t = 0$ during no-charge windows; $D_t = 0$ during no-discharge windows; $G_t \le M_t$ during grid cap windows.
- **Global Optimality:** Guaranteed true mathematical global optimum solved in under 50ms.

---

## ⚙️ Environment Variables

Copy `.env.example` to `.env`. Real environment variables override `.env`.

| Variable | Required | Default | Description |
| :--- | :---: | :--- | :--- |
| `ANTHROPIC_API_KEY` | Optional | `""` | API key for LLM provider (fallback works if omitted) |
| `ANTHROPIC_BASE_URL` | Optional | `https://api.deepseek.com/anthropic` | Anthropic-compatible API endpoint |
| `LLM_MODEL_ID` | Optional | `deepseek-v4-pro` | Model identifier used for note interpretation |
| `LLM_TIMEOUT_SECONDS` | Optional | `8` | Hard timeout in seconds before falling back to heuristics |
| `LOG_LEVEL` | Optional | `INFO` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `PORT` | Optional | `8000` | Port for the HTTP service |
| `HOST` | Optional | `0.0.0.0` | Host interface to bind to |

> [!NOTE]
> **No secret values are required to run or test the service.** When `ANTHROPIC_API_KEY` is omitted or unavailable, the deterministic heuristic parser immediately processes all operator directives with 100% test pass rates.

---

## 🚀 Local Installation & Run

### Prerequisites
- Python 3.10, 3.11, or 3.12
- `pip` and `virtualenv`

### Step-by-Step Setup

```bash
# 1. Clone repository and navigate to root
git clone <repo-url>
cd Bup-prelie

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Copy configuration
cp .env.example .env

# 5. Run the service
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service is now listening at `http://0.0.0.0:8000`.

---

## 🧪 Automated Testing

Run the full public sample test suite covering all official test cases (`SAMPLE-01` through `SAMPLE-10`):

```bash
pytest tests/test_samples.py -v
```

This verifies:
1. HTTP 200 responses and Pydantic schema validation.
2. Directive interpretation (`applies`, `directive_type`, `structured_adjustment`).
3. Energy balance and physical battery constraints.
4. Total BDT electricity cost equivalence within 1.0% margin.

---

## 📡 API Usage & Curl Examples

### 1. Readiness Probe (`GET /health`)

```bash
curl -s http://127.0.0.1:8000/health
```

**Expected Response (HTTP 200):**
```json
{
  "status": "ok"
}
```

---

### 2. Energy Optimization (`POST /optimize-energy`)

```bash
curl -s -X POST http://127.0.0.1:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "DEMO-01",
    "battery": {
      "capacity_kwh": 100.0,
      "initial_energy_kwh": 40.0,
      "minimum_energy_kwh": 20.0,
      "max_charge_kwh_per_hour": 25.0,
      "max_discharge_kwh_per_hour": 25.0
    },
    "hours": [
      {"hour": 0, "demand_kwh": 30.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.0},
      {"hour": 1, "demand_kwh": 30.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.0},
      {"hour": 2, "demand_kwh": 30.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0},
      {"hour": 3, "demand_kwh": 30.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0},
      {"hour": 4, "demand_kwh": 30.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0},
      {"hour": 5, "demand_kwh": 35.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 5.0},
      {"hour": 6, "demand_kwh": 40.0, "solar_kwh": 5.0, "tariff_bdt_per_kwh": 6.0},
      {"hour": 7, "demand_kwh": 50.0, "solar_kwh": 15.0, "tariff_bdt_per_kwh": 6.0},
      {"hour": 8, "demand_kwh": 60.0, "solar_kwh": 30.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 9, "demand_kwh": 70.0, "solar_kwh": 45.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 10, "demand_kwh": 80.0, "solar_kwh": 55.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 11, "demand_kwh": 85.0, "solar_kwh": 60.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 12, "demand_kwh": 90.0, "solar_kwh": 65.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 13, "demand_kwh": 90.0, "solar_kwh": 60.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 14, "demand_kwh": 85.0, "solar_kwh": 50.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 15, "demand_kwh": 75.0, "solar_kwh": 35.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 16, "demand_kwh": 70.0, "solar_kwh": 20.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 17, "demand_kwh": 75.0, "solar_kwh": 5.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 18, "demand_kwh": 90.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0},
      {"hour": 19, "demand_kwh": 95.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0},
      {"hour": 20, "demand_kwh": 90.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0},
      {"hour": 21, "demand_kwh": 70.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 9.0},
      {"hour": 22, "demand_kwh": 50.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 8.0},
      {"hour": 23, "demand_kwh": 40.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.0}
    ],
    "operator_notes": [
      "Solar output will be 50% from 11 AM to 2 PM due to panel cleaning.",
      "Annual sports day registration starts next Monday."
    ]
  }'
```

**Expected Response Structure (HTTP 200):**
```json
{
  "scenario_id": "DEMO-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "kind": "solar_reduction",
        "hours": [11, 12, 13],
        "factor": 0.5
      },
      "explanation": "Solar limited to 50% of forecast 11:00-14:00 (heuristic)."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "Non-energy operational note; no directive applied (heuristic)."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 30.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_charge_kwh": 0.0,
      "battery_discharge_kwh": 0.0,
      "battery_level_kwh": 40.0,
      "hourly_cost_bdt": 180.0
    }
  ],
  "total_grid_kwh": 1085.0,
  "total_cost_bdt": 8490.0,
  "peak_grid_kwh": 95.0,
  "plan_summary": "Solved optimally with HiGHS."
}
```

---

## 🐳 Docker Container & Fallback Image

A multi-stage, rootless Dockerfile is included that binds to `0.0.0.0:8000` and contains a built-in health check.

### Building & Running Locally

```bash
# Build the image
docker build -t gridwise:latest .

# Run the container (binds port 8000)
docker run -d --name gridwise -p 8000:8000 gridwise:latest

# Verify health
curl -s http://127.0.0.1:8000/health
```

### Pullable Fallback Image Reference

For organizers requiring a pre-built fallback container:

```bash
# Pull tested image
docker pull docker.io/username/gridwise:latest

# Run fallback container
docker run -p 8000:8000 docker.io/username/gridwise:latest
```

*(Replace `username/gridwise:latest` with the published registry repository and tag).*

---

## 📦 Dependencies

| Package | Purpose |
| :--- | :--- |
| `fastapi` | High-performance asynchronous HTTP REST API framework |
| `uvicorn` | Production ASGI web server |
| `pydantic` (v2) | Data validation, type safety, and schema contracts |
| `scipy` | Linear programming solver (`HiGHS` solver engine) |
| `numpy` | Vector and matrix manipulations for LP formulation |
| `python-dotenv` | Environment variable loader from `.env` |
| `anthropic` | Anthropic-compatible client SDK for LLM-based note parsing |
| `pytest` | Automated test runner for public sample scenarios |
| `httpx` | Fast test client for endpoint validation |

---

## ⚠️ Known Limitations & Assumptions

1. **Deterministic 1-Hour Horizon Step:** Dispatches are planned in discrete 1-hour intervals (hours 0 through 23). Intra-hour fluctuation (e.g. 15-minute solar drops) is averaged across the hour slot.
2. **Efficiency Model:** Battery storage is modelled with 100% round-trip efficiency in accordance with the problem statement parameters.
3. **Overlapping Directives Priority:** If multiple directives specify conflicting bounds for the same hour window, stricter safety bounds (higher reserve floor, lower grid ceiling) are enforced by the deterministic guardrails.
4. **LLM Offline Tolerance:** In the absence of an internet connection or valid API credentials, the system automatically falls back to regex heuristics without service disruption.

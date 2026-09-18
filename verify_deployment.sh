#!/usr/bin/env bash
# verify_deployment.sh - build the image, run the container, probe /health, audit README.
#
# Usage: ./verify_deployment.sh [image-tag]
# Env:   PORT (default 8000), HEALTH_TIMEOUT (default 60), ENV_FILE (default .env, optional)
#
# Credentials are never baked into the image: they are injected at `docker run` time from
# ENV_FILE (if present). No external LLM call is made by this script.

set -euo pipefail

IMAGE="${1:-bup-prelie:verify}"
CONTAINER="bup-prelie-verify-$$"
PORT="${PORT:-8000}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
ENV_FILE="${ENV_FILE:-.env}"
HEALTH_URL="http://localhost:${PORT}/health"
README="README.md"

PASS=0
FAIL=0

log()  { printf '[verify] %s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '  [PASS] %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$*" >&2; }

cleanup() {
    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        log "stopping container $CONTAINER"
        docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 2; }
command -v curl   >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }

# ---------------------------------------------------------------------------
# 1. No baked-in credentials
# ---------------------------------------------------------------------------
log "auditing build context for credentials"
if grep -qE '^\s*\.env\s*$' .dockerignore 2>/dev/null; then
    ok ".dockerignore excludes .env"
else
    bad ".dockerignore must exclude .env"
fi
if grep -nEi 'sk-[A-Za-z0-9]{8,}|ANTHROPIC_API_KEY\s*=|api_key' Dockerfile >/dev/null 2>&1; then
    bad "Dockerfile contains credential material"
else
    ok "Dockerfile contains no credential material"
fi

# ---------------------------------------------------------------------------
# 2. Build image
# ---------------------------------------------------------------------------
log "building image $IMAGE"
if docker build -t "$IMAGE" . >/tmp/verify_build.log 2>&1; then
    ok "image built"
else
    bad "docker build failed (see /tmp/verify_build.log)"
    tail -n 30 /tmp/verify_build.log >&2
    exit 1
fi

if docker history --no-trunc "$IMAGE" 2>/dev/null | grep -qEi 'sk-[A-Za-z0-9]{8,}|ANTHROPIC_API_KEY='; then
    bad "image layers contain an API key"
else
    ok "image layers contain no API key"
fi

# ---------------------------------------------------------------------------
# 3. Run container bound to 0.0.0.0:PORT, credentials injected at runtime only
# ---------------------------------------------------------------------------
RUN_ARGS=(-d --name "$CONTAINER" -p "0.0.0.0:${PORT}:8000" -e HOST=0.0.0.0 -e PORT=8000)
if [[ -f "$ENV_FILE" ]]; then
    RUN_ARGS+=(--env-file "$ENV_FILE")
    log "injecting runtime environment from $ENV_FILE"
else
    log "no $ENV_FILE found; running without LLM credentials (heuristic fallback)"
fi
log "starting container $CONTAINER"
docker run "${RUN_ARGS[@]}" "$IMAGE" >/dev/null
ok "container started"

# ---------------------------------------------------------------------------
# 4. Poll /health until HTTP 200 (timeout HEALTH_TIMEOUT seconds)
# ---------------------------------------------------------------------------
log "polling $HEALTH_URL (timeout ${HEALTH_TIMEOUT}s)"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
healthy=0
while [[ $(date +%s) -lt $deadline ]]; do
    code=$(curl -s -o /tmp/verify_health.json -w '%{http_code}' "$HEALTH_URL" || true)
    if [[ "$code" == "200" ]]; then
        healthy=1
        break
    fi
    if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        bad "container exited before becoming healthy"
        docker logs "$CONTAINER" 2>&1 | tail -n 30 >&2
        exit 1
    fi
    sleep 2
done
if [[ $healthy -eq 1 ]]; then
    ok "/health returned 200: $(cat /tmp/verify_health.json)"
    if grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' /tmp/verify_health.json; then
        ok "/health body is {\"status\": \"ok\"}"
    else
        bad "/health body is not {\"status\": \"ok\"}"
    fi
else
    bad "/health did not return 200 within ${HEALTH_TIMEOUT}s"
    docker logs "$CONTAINER" 2>&1 | tail -n 30 >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. Smoke the optimize endpoint (offline-safe: falls back if no key is present)
# ---------------------------------------------------------------------------
HOURS=$(for h in $(seq 0 23); do
    printf '{"hour":%d,"demand_kwh":40,"solar_kwh":%d,"tariff_bdt_per_kwh":%d}' "$h" \
        $(( h >= 7 && h <= 17 ? 25 : 0 )) $(( h >= 17 && h <= 21 ? 12 : 5 ))
    [[ $h -lt 23 ]] && printf ','
done)
BODY="{\"scenario_id\":\"verify-deploy\",\"operator_notes\":[\"Do not charge the battery from 2 PM to 5 PM.\"],\"hours\":[${HOURS}],\"battery\":{\"capacity_kwh\":100,\"initial_energy_kwh\":50,\"minimum_energy_kwh\":10,\"max_charge_kwh_per_hour\":25,\"max_discharge_kwh_per_hour\":25}}"
code=$(curl -s -o /tmp/verify_optimize.json -w '%{http_code}' -X POST "http://localhost:${PORT}/optimize-energy" \
    -H 'Content-Type: application/json' --data "$BODY" || true)
if [[ "$code" == "200" ]] && grep -q '"scenario_id"[[:space:]]*:[[:space:]]*"verify-deploy"' /tmp/verify_optimize.json \
    && [[ $(grep -o '"hour"' /tmp/verify_optimize.json | wc -l) -eq 24 ]]; then
    ok "/optimize-energy returned a 24-hour plan for scenario verify-deploy"
else
    bad "/optimize-energy smoke failed (HTTP $code)"
fi
if grep -qE 'sk-[A-Za-z0-9]{8,}|Traceback' /tmp/verify_optimize.json; then
    bad "response leaks secrets or tracebacks"
else
    ok "response contains no secrets or tracebacks"
fi

# ---------------------------------------------------------------------------
# 6. README audit
# ---------------------------------------------------------------------------
log "auditing $README"
check_readme() {
    local label="$1" pattern="$2"
    if grep -qiE "$pattern" "$README" 2>/dev/null; then
        ok "README documents $label"
    else
        bad "README is missing $label (pattern: $pattern)"
    fi
}
[[ -f "$README" ]] || { bad "$README not found"; }
check_readme "local quickstart"      'quick ?start|pip install -r requirements\.txt'
check_readme "environment variables" 'ANTHROPIC_API_KEY|environment variables|\.env'
check_readme "model/provider"        'LLM_MODEL_ID|deepseek|anthropic|provider'
check_readme "solver architecture"   'solver|linear program|HiGHS|optimi[sz]er'
check_readme "sample test curl"      'curl .*optimize-energy|curl -X POST'

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\n[verify] %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]

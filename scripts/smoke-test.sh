#!/usr/bin/env bash
# End-to-end smoke test of the running Docker stack (local and CI).
#
#   docker compose up --build -d --wait
#   scripts/smoke-test.sh
#
# It verifies that each infrastructure piece works for real, not just that its container is up:
#   - liveness + readiness (PostgreSQL, Redis, object storage)
#   - migrations were applied
#   - the error envelope and request-ID echo on a real request
#   - Celery: a task goes API container -> Redis broker -> worker -> result backend, and the
#     request ID set by the caller is the one the worker logs and returns
#   - Celery beat is running and scheduling
#   - no secret from .env appears anywhere in the stack's logs
set -euo pipefail

cd "$(dirname "$0")/.."
API="http://127.0.0.1:${API_PORT:-8000}"
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
dc() { docker compose "$@"; }

echo "== HTTP"
live=$(curl -fsS "$API/api/v1/health/live") || fail "liveness endpoint unreachable"
if [[ "$live" == '{"status":"ok"}' ]]; then pass "liveness: $live"; else fail "liveness: $live"; fi

ready=$(curl -sS -w '\n%{http_code}' "$API/api/v1/health/ready")
code=$(tail -n1 <<<"$ready"); body=$(head -n1 <<<"$ready")
[[ "$code" == 200 ]] || fail "readiness HTTP $code: $body"
for check in database cache storage; do
  grep -Eq "\"$check\": *\"ok\"" <<<"$body" || fail "readiness check '$check' not ok: $body"
done
pass "readiness: $body"

headers=$(curl -sS -D - -o /tmp/eduflow-404.json -H 'X-Request-ID: smoke-test-request-0001' "$API/api/v1/nope")
grep -qi '^x-request-id: smoke-test-request-0001' <<<"$headers" || fail "request ID not echoed"
if ! grep -Eq '"code": *"not_found"' /tmp/eduflow-404.json \
  || ! grep -Eq '"request_id": *"smoke-test-request-0001"' /tmp/eduflow-404.json; then
  fail "404 is not the JSON error envelope: $(cat /tmp/eduflow-404.json)"
fi
pass "unknown route -> JSON envelope with request ID"

echo "== Database"
dc exec -T backend python manage.py migrate --check >/dev/null || fail "unapplied migrations"
pass "all migrations applied"

echo "== Celery (broker round trip + request-ID propagation)"
RID="smoke-celery-$(date +%s)"
result=$(dc exec -T -e RID="$RID" backend python - <<'PY'
import os, json, django
django.setup()
from eduflow.core.request_context import bind_request_id
from eduflow.core.tasks import ping
bind_request_id(os.environ["RID"])
print(json.dumps(ping.apply_async().get(timeout=30)))
PY
) || fail "ping task did not complete"
grep -Eq "\"request_id\": *\"$RID\"" <<<"$result" || fail "worker did not receive request ID: $result"
pass "worker returned $result"
sleep 1
dc logs --no-color worker | grep "$RID" | grep -q task_started || fail "worker log has no task_started line for $RID"
pass "worker log lines carry request_id=$RID"

echo "== Celery beat"
dc logs --no-color beat | grep -qi "beat: Starting" || fail "beat did not start"
# Beat sends the heartbeat as soon as it starts; the worker must then run it.
for _ in $(seq 1 30); do
  dc logs --no-color worker | grep -q "eduflow.core.tasks.heartbeat.*succeeded" && break
  sleep 2
done
dc logs --no-color beat | grep -q "Sending due task core.heartbeat" || fail "beat never dispatched core.heartbeat"
dc logs --no-color worker | grep -q "eduflow.core.tasks.heartbeat.*succeeded" || fail "worker never ran the scheduled heartbeat"
pass "beat scheduled core.heartbeat and the worker ran it"

echo "== Secrets never logged"
logs=$(dc logs --no-color)
for var in DJANGO_SECRET_KEY POSTGRES_PASSWORD STORAGE_SECRET_KEY; do
  value=$(grep -E "^${var}=" .env | cut -d= -f2-)
  [[ -n "$value" ]] || continue
  if grep -qF -- "$value" <<<"$logs"; then fail "value of $var appears in logs"; fi
done
pass "no .env secret values in any container log"

echo "All smoke checks passed."

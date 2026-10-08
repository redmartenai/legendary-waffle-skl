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
#   - Phase 2: sign-in, forced password change, tenant resolution, refresh rotation and reuse detection,
#     and PostgreSQL RLS as the application role
#   - no secret from .env, and no token issued during the run, appears anywhere in the stack's logs
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
# The dev stack schedules the heartbeat every 20 s (first run 20 s after beat starts); the worker
# must then run it.
for _ in $(seq 1 45); do
  dc logs --no-color worker | grep -q "eduflow.core.tasks.heartbeat.*succeeded" && break
  sleep 2
done
dc logs --no-color beat | grep -q "Sending due task core.heartbeat" || fail "beat never dispatched core.heartbeat"
dc logs --no-color worker | grep -q "eduflow.core.tasks.heartbeat.*succeeded" || fail "worker never ran the scheduled heartbeat"
pass "beat scheduled core.heartbeat and the worker ran it"

echo "== Phase 2: authentication, tenancy, RLS"
jget() { dc exec -T backend python -c "import json,sys; print(json.load(sys.stdin)$1)"; }
post() { curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H 'Content-Type: application/json' "$@"; }

code=$(curl -sS -o /dev/null -w '%{http_code}' "$API/api/v1/me")
[[ "$code" == 401 ]] || fail "unauthenticated /me returned $code"
pass "unauthenticated request -> 401"

code=$(post -d '{"identifier":"nobody@example.com","password":"wrong-password-1"}' "$API/api/v1/auth/password/login")
[[ "$code" == 401 ]] && grep -q '"invalid_credentials"' /tmp/eduflow-body.json || fail "bad login: $code"
pass "bad credentials -> generic 401"

RUN=$(date +%s)
STAFF_EMAIL="smoke-staff-$RUN@example.com"; STAFF_PW="Smoke-staff-$RUN-pass"
dc exec -T -e EDUFLOW_ADMIN_PASSWORD="$STAFF_PW" backend   python manage.py create_platform_admin --email "$STAFF_EMAIL" --full-name "Smoke staff" >/dev/null
code=$(post -d "{\"identifier\":\"$STAFF_EMAIL\",\"password\":\"$STAFF_PW\"}" "$API/api/v1/auth/password/login")
[[ "$code" == 200 ]] || fail "platform admin login: $code $(cat /tmp/eduflow-body.json)"
STAFF_ACCESS=$(jget '["access"]' </tmp/eduflow-body.json)
pass "platform admin signed in"

ADMIN_EMAIL="smoke-admin-$RUN@example.com"; TEMP_PW="Temporary-$RUN-pass"
code=$(post -H "Authorization: Bearer $STAFF_ACCESS"   -d "{\"code\":\"smoke-$RUN\",\"name\":\"Smoke School\",\"admin\":{\"full_name\":\"Smoke Admin\",\"email\":\"$ADMIN_EMAIL\",\"temporary_password\":\"$TEMP_PW\"}}"   "$API/api/v1/platform/schools")
[[ "$code" == 201 ]] || fail "create school: $code $(cat /tmp/eduflow-body.json)"
SCHOOL_ID=$(jget '["id"]' </tmp/eduflow-body.json)
pass "school created with system roles and a first admin"

code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $STAFF_ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/school")
[[ "$code" == 403 ]] || fail "platform admin got $code on a school it is not a member of"
pass "platform admin has no implicit school access (403)"

code=$(post -d "{\"identifier\":\"$ADMIN_EMAIL\",\"password\":\"$TEMP_PW\"}" "$API/api/v1/auth/password/login")
[[ "$code" == 200 ]] || fail "school admin login: $code"
ACCESS=$(jget '["access"]' </tmp/eduflow-body.json); REFRESH=$(jget '["refresh"]' </tmp/eduflow-body.json)
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H "Authorization: Bearer $ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/school")
[[ "$code" == 403 ]] && grep -q password_change_required /tmp/eduflow-body.json || fail "temporary password not enforced: $code"
NEW_PW="Changed-$RUN-password"
code=$(post -H "Authorization: Bearer $ACCESS" -d "{\"current_password\":\"$TEMP_PW\",\"new_password\":\"$NEW_PW\"}" "$API/api/v1/auth/password/change")
[[ "$code" == 204 ]] || fail "password change: $code $(cat /tmp/eduflow-body.json)"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H "Authorization: Bearer $ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/memberships")
[[ "$code" == 200 ]] || fail "school admin cannot list members: $code"
pass "forced password change, then tenant-scoped access"

code=$(post -d "{\"refresh\":\"$REFRESH\"}" "$API/api/v1/auth/token/refresh")
[[ "$code" == 200 ]] || fail "refresh: $code"
ROTATED=$(jget '["refresh"]' </tmp/eduflow-body.json)
code=$(post -d "{\"refresh\":\"$REFRESH\"}" "$API/api/v1/auth/token/refresh")
[[ "$code" == 401 ]] || fail "reused refresh token accepted: $code"
code=$(post -d "{\"refresh\":\"$ROTATED\"}" "$API/api/v1/auth/token/refresh")
[[ "$code" == 401 ]] || fail "token family not revoked after reuse: $code"
pass "refresh rotation; reuse revokes the whole family"

rls=$(dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAqc "SET ROLE eduflow_app; SELECT count(*) FROM tenancy_membership;"')
[[ "$(tr -d '[:space:]' <<<"$rls")" == 0 ]] || fail "RLS: app role without tenant context sees $rls memberships"
pass "RLS: the application role sees no tenant rows without a tenant context"

echo "== Secrets never logged"
logs=$(dc logs --no-color)
for token in "$STAFF_ACCESS" "$ACCESS" "$REFRESH" "$ROTATED" "$TEMP_PW" "$NEW_PW" "$STAFF_PW"; do
  if grep -qF -- "$token" <<<"$logs"; then fail "a token or password issued during the smoke test appears in logs"; fi
done
pass "no token or password from this run in any container log"
for var in DJANGO_SECRET_KEY POSTGRES_PASSWORD STORAGE_SECRET_KEY; do
  value=$(grep -E "^${var}=" .env | cut -d= -f2-)
  [[ -n "$value" ]] || continue
  if grep -qF -- "$value" <<<"$logs"; then fail "value of $var appears in logs"; fi
done
pass "no .env secret values in any container log"

echo "All smoke checks passed."

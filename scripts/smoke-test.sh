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
#   - Phase 3: academic year (overlap refused), grade, section, student and enrollment through the API,
#     and RLS on the new tables
#   - Phase 4: a guardian invitation from creation to acceptance, the parent's child-only access, no replay
#   - no secret from .env, and no token issued during the run, appears anywhere in the stack's logs
set -euo pipefail

cd "$(dirname "$0")/.."
API="http://127.0.0.1:${API_PORT:-8000}"
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
dc() { docker compose "$@"; }
# Matches are counted rather than tested with "grep -q": under pipefail, grep -q exiting early kills
# "docker compose logs" with SIGPIPE once the logs are long (a stack that has been up for hours).
logs() { dc logs --no-color "$@"; }
has_log() { [[ "$(logs "$1" | grep -Ec -- "$2")" -gt 0 ]]; }

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
has_log worker "$RID.*task_started|task_started.*$RID" || fail "worker log has no task_started line for $RID"
pass "worker log lines carry request_id=$RID"

echo "== Celery beat"
has_log beat "[Bb]eat: Starting" || fail "beat did not start"
# The dev stack schedules the heartbeat every 20 s (first run 20 s after beat starts); the worker
# must then run it.
for _ in $(seq 1 45); do
  has_log worker "eduflow.core.tasks.heartbeat.*succeeded" && break
  sleep 2
done
has_log beat "Sending due task core.heartbeat" || fail "beat never dispatched core.heartbeat"
has_log worker "eduflow.core.tasks.heartbeat.*succeeded" || fail "worker never ran the scheduled heartbeat"
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

echo "== Phase 3: core school domain"
dc exec -T backend python manage.py sync_rbac >/dev/null || fail "sync_rbac failed"
pass "sync_rbac: permission catalogue and system roles up to date"
# The reuse-detection check above revoked the admin's session: sign in again.
code=$(post -d "{\"identifier\":\"$ADMIN_EMAIL\",\"password\":\"$NEW_PW\"}" "$API/api/v1/auth/password/login")
[[ "$code" == 200 ]] || fail "school admin re-login: $code"
ACCESS=$(jget '["access"]' </tmp/eduflow-body.json)
auth=(-H "Authorization: Bearer $ACCESS" -H "X-School-Id: $SCHOOL_ID")
code=$(post "${auth[@]}" -d '{"name":"2026-27","start_date":"2026-06-01","end_date":"2027-03-31"}' "$API/api/v1/academic-years")
[[ "$code" == 201 ]] || fail "create academic year: $code $(cat /tmp/eduflow-body.json)"
YEAR_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d '{"name":"2026-27b","start_date":"2026-12-01","end_date":"2027-06-30"}' "$API/api/v1/academic-years")
[[ "$code" == 400 ]] || fail "overlapping academic year accepted: $code"
code=$(post "${auth[@]}" -d '{"name":"Grade 5","code":"g5","display_order":5}' "$API/api/v1/grades")
[[ "$code" == 201 ]] || fail "create grade: $code"
GRADE_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"academic_year_id\":\"$YEAR_ID\",\"grade_id\":\"$GRADE_ID\",\"name\":\"A\",\"code\":\"5a\"}" "$API/api/v1/sections")
[[ "$code" == 201 ]] || fail "create section: $code $(cat /tmp/eduflow-body.json)"
SECTION_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d '{"admission_number":"SMOKE-1","first_name":"Smoke","last_name":"Student"}' "$API/api/v1/students")
[[ "$code" == 201 ]] || fail "create student: $code"
STUDENT_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"student_id\":\"$STUDENT_ID\",\"section_id\":\"$SECTION_ID\",\"roll_number\":\"1\"}" "$API/api/v1/enrollments")
[[ "$code" == 201 ]] || fail "enroll: $code $(cat /tmp/eduflow-body.json)"
grep -q "\"$GRADE_ID\"" /tmp/eduflow-body.json || fail "enrollment grade not taken from the section"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' "${auth[@]}" "$API/api/v1/students?section_id=$SECTION_ID")
[[ "$code" == 200 ]] && grep -q "$STUDENT_ID" /tmp/eduflow-body.json || fail "student list by section: $code"
pass "year (overlap refused), grade, section, student and enrollment through the API"
rls=$(dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAqc "SET ROLE eduflow_app; SELECT (SELECT count(*) FROM people_student) + (SELECT count(*) FROM people_enrollment) + (SELECT count(*) FROM academics_section);"')
[[ "$(tr -d '[:space:]' <<<"$rls")" == 0 ]] || fail "RLS: app role without tenant context sees $rls Phase 3 rows"
pass "RLS: Phase 3 tables hidden from the application role without a tenant context"

echo "== Phase 4: invitations and account linking"
# Email channel: the development console email backend prints the raw message to stdout (the SMS console
# adapter logs through the redacting logger, which masks the link's token).
GUARDIAN_EMAIL="smoke-parent-$RUN@example.com"
code=$(post "${auth[@]}" -d "{\"full_name\":\"Smoke Parent\",\"email\":\"$GUARDIAN_EMAIL\"}" "$API/api/v1/guardians")
[[ "$code" == 201 ]] || fail "create guardian: $code $(cat /tmp/eduflow-body.json)"
GUARDIAN_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"student_id\":\"$STUDENT_ID\",\"guardian_id\":\"$GUARDIAN_ID\",\"relationship\":\"mother\"}" "$API/api/v1/student-guardians")
[[ "$code" == 201 ]] || fail "link guardian to student: $code"
code=$(post "${auth[@]}" -d "{\"kind\":\"guardian\",\"channel\":\"email\",\"recipient\":\"$GUARDIAN_EMAIL\",\"full_name\":\"Smoke Parent\",\"guardian_id\":\"$GUARDIAN_ID\"}" "$API/api/v1/invitations")
[[ "$code" == 201 ]] || fail "create invitation: $code $(cat /tmp/eduflow-body.json)"
if grep -q '"token' /tmp/eduflow-body.json; then fail "invitation response contains a token"; fi
# Development delivers through the console email backend, so the link and code are in the backend output.
sleep 1
INVITE_TOKEN=$(logs backend | grep -o 'token=[A-Za-z0-9_-]\+' | tail -1 | cut -d= -f2)
[[ -n "$INVITE_TOKEN" ]] || fail "no invitation link was delivered"
code=$(post -d "{\"token\":\"$INVITE_TOKEN\"}" "$API/api/v1/invitations/preview")
[[ "$code" == 200 ]] && grep -q "Smoke School" /tmp/eduflow-body.json || fail "invitation preview: $code"
code=$(post -d "{\"token\":\"$INVITE_TOKEN\"}" "$API/api/v1/invitations/verification")
[[ "$code" == 200 ]] || fail "invitation verification: $code $(cat /tmp/eduflow-body.json)"
CHALLENGE_ID=$(jget '["challenge_id"]' </tmp/eduflow-body.json)
sleep 1
INVITE_CODE=$(logs backend | grep -o '[0-9]\{6\} is your EduFlow invitation code' | tail -1 | cut -c1-6)
[[ -n "$INVITE_CODE" ]] || fail "no invitation code was delivered"
code=$(post -d "{\"token\":\"$INVITE_TOKEN\",\"challenge_id\":\"$CHALLENGE_ID\",\"code\":\"$INVITE_CODE\"}" "$API/api/v1/invitations/accept")
[[ "$code" == 200 ]] || fail "accept invitation: $code $(cat /tmp/eduflow-body.json)"
PARENT_ACCESS=$(jget '["session"]["access"]' </tmp/eduflow-body.json)
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H "Authorization: Bearer $PARENT_ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/students")
[[ "$code" == 200 ]] && grep -q "$STUDENT_ID" /tmp/eduflow-body.json || fail "parent cannot see their child: $code"
[[ "$(jget '["results"].__len__()' </tmp/eduflow-body.json)" == 1 ]] || fail "parent sees more than their child"
code=$(post -d "{\"token\":\"$INVITE_TOKEN\",\"challenge_id\":\"$CHALLENGE_ID\",\"code\":\"$INVITE_CODE\"}" "$API/api/v1/invitations/accept")
[[ "$code" == 404 ]] || fail "accepted invitation replayed: $code"
pass "guardian invitation: link delivered, preview, verify, accept as new account; child-only scope; no replay"

echo "== Secrets never logged"
logs=$(logs)
# (Invitation links and codes are printed to the log on purpose by the development console adapter.)
for token in "$STAFF_ACCESS" "$ACCESS" "$REFRESH" "$ROTATED" "$TEMP_PW" "$NEW_PW" "$STAFF_PW" "$PARENT_ACCESS"; do
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

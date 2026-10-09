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
#   - Phase 5: a timetable built and published through the API, a cross-timetable teacher clash refused,
#     the parent's view of the child's schedule with a recorded lesson, and RLS on the new tables
#   - Attendance: a register taken with exceptions only, an idempotent retry, the parent's month view,
#     a correction that its requester cannot approve, and RLS on the attendance tables
#   - White-label: colours, a logo stored in object storage and served publicly with safe headers, SVG
#     refused, branding in the public school lookup, a pending custom domain that does not resolve
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
code=$(post "${auth[@]}" -d "{\"student_id\":\"$STUDENT_ID\",\"section_id\":\"$SECTION_ID\",\"roll_number\":\"1\",\"start_date\":\"2026-06-01\"}" "$API/api/v1/enrollments")
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

echo "== Phase 5: academic engine (timetables, clashes, schedules, lessons)"
code=$(post "${auth[@]}" -d "{\"full_name\":\"Smoke Teacher\",\"email\":\"smoke-teacher-$RUN@example.com\"}" "$API/api/v1/memberships")
[[ "$code" == 201 ]] || fail "add teacher member: $code $(cat /tmp/eduflow-body.json)"
TEACHER_MEMBERSHIP=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"membership_id\":\"$TEACHER_MEMBERSHIP\",\"employee_id\":\"T-$RUN\"}" "$API/api/v1/staff")
[[ "$code" == 201 ]] || fail "create teacher profile: $code $(cat /tmp/eduflow-body.json)"
STAFF_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d '{"name":"Mathematics","code":"maths"}' "$API/api/v1/subjects")
[[ "$code" == 201 ]] || fail "create subject: $code"
SUBJECT_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"academic_year_id\":\"$YEAR_ID\",\"grade_id\":\"$GRADE_ID\",\"name\":\"B\",\"code\":\"5b\"}" "$API/api/v1/sections")
[[ "$code" == 201 ]] || fail "create section B: $code"
SECTION_B=$(jget '["id"]' </tmp/eduflow-body.json)
# Helpers set globals (not "$(...)"), so that "fail" inside them stops the script with its message.
assign() {
  code=$(post "${auth[@]}" -d "{\"staff_id\":\"$STAFF_ID\",\"section_id\":\"$1\",\"subject_id\":\"$SUBJECT_ID\"}" "$API/api/v1/teacher-assignments")
  [[ "$code" == 201 ]] || fail "assign teacher: $code $(cat /tmp/eduflow-body.json)"
  ASSIGNMENT=$(jget '["id"]' </tmp/eduflow-body.json)
}
assign "$SECTION_ID"; ASSIGN_A=$ASSIGNMENT
assign "$SECTION_B"; ASSIGN_B=$ASSIGNMENT
# timetable NAME START END SECTION ASSIGNMENT -> TT and SLOT (a timetable with one Monday slot)
timetable() {
  code=$(post "${auth[@]}" -d "{\"academic_year_id\":\"$YEAR_ID\",\"name\":\"$1\"}" "$API/api/v1/timetables")
  [[ "$code" == 201 ]] || fail "create timetable $1: $code $(cat /tmp/eduflow-body.json)"
  local tt; tt=$(jget '["id"]' </tmp/eduflow-body.json)
  code=$(post "${auth[@]}" -d "{\"timetable_id\":\"$tt\",\"number\":1,\"name\":\"P1\",\"start_time\":\"$2\",\"end_time\":\"$3\"}" "$API/api/v1/timetable-periods")
  [[ "$code" == 201 ]] || fail "create period: $code $(cat /tmp/eduflow-body.json)"
  local period; period=$(jget '["id"]' </tmp/eduflow-body.json)
  code=$(post "${auth[@]}" -d "{\"timetable_id\":\"$tt\",\"period_id\":\"$period\",\"weekday\":1,\"section_id\":\"$4\",\"assignment_id\":\"$5\"}" "$API/api/v1/timetable-slots")
  [[ "$code" == 201 ]] || fail "create slot: $code $(cat /tmp/eduflow-body.json)"
  TT=$tt; SLOT=$(jget '["id"]' </tmp/eduflow-body.json)
}
timetable Main 09:00 09:45 "$SECTION_ID" "$ASSIGN_A"; MAIN_TT=$TT; MAIN_SLOT=$SLOT
timetable Wing 09:30 10:15 "$SECTION_B" "$ASSIGN_B"; WING_TT=$TT
code=$(post "${auth[@]}" -d "{}" "$API/api/v1/timetables/$MAIN_TT/publish")
[[ "$code" == 200 ]] || fail "publish timetable: $code $(cat /tmp/eduflow-body.json)"
code=$(post "${auth[@]}" -d "{}" "$API/api/v1/timetables/$WING_TT/publish")
[[ "$code" == 409 ]] && grep -q "Smoke Teacher" /tmp/eduflow-body.json || fail "teacher double-booked across timetables: $code"
pass "timetable built and published; a teacher clash across timetables with different bells is refused (409)"
code=$(post "${auth[@]}" -d "{\"slot_id\":\"$MAIN_SLOT\",\"date\":\"2026-07-13\",\"topic\":\"Fractions\"}" "$API/api/v1/lessons")
[[ "$code" == 201 ]] || fail "record lesson: $code $(cat /tmp/eduflow-body.json)"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H "Authorization: Bearer $PARENT_ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/students/$STUDENT_ID/schedule?date_from=2026-07-13&date_to=2026-07-19")
[[ "$code" == 200 ]] && grep -q "$MAIN_SLOT" /tmp/eduflow-body.json && grep -q "Fractions" /tmp/eduflow-body.json \
  || fail "parent cannot see the child's schedule with its lesson: $code $(cat /tmp/eduflow-body.json)"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PARENT_ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/sections/$SECTION_B/schedule")
[[ "$code" == 404 ]] || fail "parent sees another section's schedule: $code"
pass "parent sees the child's schedule and recorded lesson, and no other section's (404)"
rls=$(dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAqc "SET ROLE eduflow_app; SELECT (SELECT count(*) FROM timetable_timetable) + (SELECT count(*) FROM timetable_slot) + (SELECT count(*) FROM timetable_lesson) + (SELECT count(*) FROM academics_room);"')
[[ "$(tr -d '[:space:]' <<<"$rls")" == 0 ]] || fail "RLS: app role without tenant context sees $rls Phase 5 rows"
pass "RLS: Phase 5 tables hidden from the application role without a tenant context"

echo "== Attendance: registers and corrections"
REGISTER="{\"entries\":[{\"student_id\":\"$STUDENT_ID\",\"status\":\"absent\"}],\"client_id\":\"smoke-$RUN\",\"date\":\"2026-07-14\"}"
code=$(post "${auth[@]}" -d "$REGISTER" "$API/api/v1/classes/$SECTION_ID/attendance")
[[ "$code" == 200 ]] && grep -q '"absent": *1' /tmp/eduflow-body.json || fail "take register: $code $(cat /tmp/eduflow-body.json)"
code=$(post "${auth[@]}" -d "{\"entries\":[],\"client_id\":\"smoke-$RUN\",\"date\":\"2026-07-14\"}" "$API/api/v1/classes/$SECTION_ID/attendance")
[[ "$code" == 200 ]] && grep -q '"replayed": *true' /tmp/eduflow-body.json && grep -q '"absent": *1' /tmp/eduflow-body.json \
  || fail "retry with the same client_id was not a replay: $code $(cat /tmp/eduflow-body.json)"
pass "register taken with exceptions only; a retry with the same client_id changes nothing"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -H "Authorization: Bearer $PARENT_ACCESS" -H "X-School-Id: $SCHOOL_ID" "$API/api/v1/students/$STUDENT_ID/attendance?month=2026-07")
[[ "$code" == 200 ]] && grep -Eq '"date": *"2026-07-14", *"status": *"absent"' /tmp/eduflow-body.json \
  || fail "parent month view: $code $(cat /tmp/eduflow-body.json)"
pass "parent sees the child's month with the absence"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' "${auth[@]}" "$API/api/v1/attendance/records?student_id=$STUDENT_ID&date=2026-07-14")
[[ "$code" == 200 ]] || fail "list records: $code"
RECORD_ID=$(jget '["results"][0]["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d "{\"record_id\":\"$RECORD_ID\",\"new_status\":\"present\",\"reason\":\"Arrived after roll call\"}" "$API/api/v1/attendance/corrections")
[[ "$code" == 201 ]] || fail "request correction: $code $(cat /tmp/eduflow-body.json)"
CORRECTION_ID=$(jget '["id"]' </tmp/eduflow-body.json)
code=$(post "${auth[@]}" -d '{}' "$API/api/v1/attendance/corrections/$CORRECTION_ID/approve")
[[ "$code" == 403 ]] || fail "a requester approved their own correction: $code"
pass "correction requested on the locked register; its requester cannot approve it (403)"
rls=$(dc exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAqc "SET ROLE eduflow_app; SELECT (SELECT count(*) FROM attendance_session) + (SELECT count(*) FROM attendance_record) + (SELECT count(*) FROM attendance_correction);"')
[[ "$(tr -d '[:space:]' <<<"$rls")" == 0 ]] || fail "RLS: app role without tenant context sees $rls attendance rows"
pass "RLS: attendance tables hidden from the application role without a tenant context"

echo "== White-label: branding and domains"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -X PATCH "${auth[@]}" -H 'Content-Type: application/json' -d '{"primary_color":"#FFD700"}' "$API/api/v1/branding")
[[ "$code" == 200 ]] && grep -q '"on_primary": *"#000000"' /tmp/eduflow-body.json || fail "change colours: $code $(cat /tmp/eduflow-body.json)"
# Relative paths: they work with every curl and Python (Windows ones do not see Git Bash's /tmp).
PYTHON=$(command -v python3 || command -v python)
"$PYTHON" - .smoke-logo.png .smoke-logo.svg <<'PYCODE'
import struct, sys, zlib
def chunk(k, d): return struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xFFFFFFFF)
rows = b"".join(b"\x00" + b"\xff\xd7\x00" * 32 for _ in range(32))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
open(sys.argv[2], "wb").write(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
PYCODE
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -X PUT "${auth[@]}" -F "file=@.smoke-logo.png;type=image/png" "$API/api/v1/branding/logo")
[[ "$code" == 200 ]] || fail "upload logo: $code $(cat /tmp/eduflow-body.json)"
LOGO_URL=$(jget '["logo_url"]' </tmp/eduflow-body.json)
headers=$(curl -sS -D - -o .smoke-logo-served.png "http://127.0.0.1:${API_PORT:-8000}$LOGO_URL")
grep -qi '^content-type: image/png' <<<"$headers" && grep -qi "^content-security-policy:.*sandbox" <<<"$headers" \
  && cmp -s .smoke-logo.png .smoke-logo-served.png || fail "logo not served intact with safe headers"
pass "logo stored in object storage and served publicly, byte for byte, as image/png with a sandboxing CSP"
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' -X PUT "${auth[@]}" -F "file=@.smoke-logo.svg;type=image/png" "$API/api/v1/branding/logo")
[[ "$code" == 400 ]] || fail "SVG logo accepted: $code"
pass "SVG (script-capable) upload refused, whatever its declared type"
rm -f .smoke-logo.png .smoke-logo.svg .smoke-logo-served.png
code=$(curl -sS -o /tmp/eduflow-body.json -w '%{http_code}' "$API/api/v1/schools/lookup?code=smoke-$RUN")
[[ "$code" == 200 ]] && grep -q '"primary_color": *"#FFD700"' /tmp/eduflow-body.json && grep -q "$LOGO_URL" /tmp/eduflow-body.json \
  || fail "public lookup lacks branding: $code $(cat /tmp/eduflow-body.json)"
pass "public school lookup carries the school's branding"
code=$(post "${auth[@]}" -d "{\"hostname\":\"portal-$RUN.example.org\"}" "$API/api/v1/domains")
[[ "$code" == 201 ]] && grep -q '"status": *"pending"' /tmp/eduflow-body.json || fail "register domain: $code $(cat /tmp/eduflow-body.json)"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$API/api/v1/branding/resolve?host=portal-$RUN.example.org")
[[ "$code" == 404 ]] || fail "an unverified domain resolved: $code"
pass "custom domain registered as pending; it does not resolve until verified"

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

# EduFlow: Implementation Plan

This plan is based on [CURRENT_STATE.md](CURRENT_STATE.md). Decisions referenced as ADR-nnn are in [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md).

**Guiding rule:** build the smallest complete vertical slice first, then expand. Each phase ends with a report, and the next phase starts only once that report is approved.

---

## 0. Before Phase 1: owner actions and decisions

| # | Item | Owner | Blocking? |
|---|---|---|---|
| D1 | Delete `origin/master` from the remote (malware), and audit any machine that opened or ran it | Repo owner | Not blocking, but urgent |
| D2 | Install **Docker Desktop** on the development machine. Docker, PostgreSQL and Redis are not installed here today. Without Docker, Phase 1 can be verified only partly locally (lint, unit tests against a remote or CI database) and fully only in CI. | Developer | Blocks local end-to-end verification |
| D3 | Approve the monorepo layout (ADR-002) | Owner | Phase 1 |
| D4 | Approve the tenancy, authorization and authentication approach (ADR-003/004/005) | Owner | Phase 2 |
| D5 | Decide which UI the MVP admin steps go through (see §2) | Owner | Phase 6 |
| D6 | Choose realtime transport: Channels or Centrifugo (ADR-011) | Owner | Phase 9 |
| D7 | Decide whether `admin` should use the web console (the client currently restricts it to `principal`) | Product | Phase 6 |

## 1. Phases (from the brief), with concrete exit criteria

Each phase's Definition of Done is: model, migration, API, authentication, authorization, tenant isolation, validation, error handling, tests, indexes, documentation, logging, and audit where it applies.

| Phase | Scope | Exit criteria |
|---|---|---|
| **1. Foundation** | Django project, settings split, Docker Compose, CI, health endpoints, error envelope, request IDs, structured logging, docs skeleton | See §3 |
| **2. Security foundation** | `identity` (User, UserSession, OTP challenge, push devices); `tenancy` (Organization, School, Membership, UserRole, Role, Permission, RolePermission + data scope); auth endpoints from the client contract; tenant middleware; permission classes; throttles; AuditLog with append-only trigger; seed of system roles | Login (password and OTP, with a dev SMS adapter), refresh rotation and reuse detection, logout, `/me`, `/schools/lookup`. The isolation-matrix test harness exists and passes. |
| **3. School structure** | Campus, AcademicYear, Term, Department, Grade, Section, Subject, Room. Canonical CRUD. A minimal `/platform/schools` POST that creates a school with its year, terms, grades, sections and principal, in one transaction. | An admin creates the structure through the API. A school-B admin gets 404 on every school-A object. |
| **4. People** | Student, Parent (guardian), StudentParent, Staff, Teacher profile, StudentEnrollment, TeacherAssignment. Services for enrolment and assignment (transactional, audited). Invites and temporary passwords for staff and parents. | Create teacher, student and parent, link parent to student, enrol, assign, with audit entries. Parent and teacher accounts can sign in. |
| **5. Academic engine** | Timetable, TimetableSlot (section × weekday × period → subject, teacher, room), with clash constraints. Teacher and student schedule selectors. Lesson. | Teacher schedule and student schedule APIs, with clash rejection enforced by the database and the service |
| **6. Attendance + MVP integration** | AttendanceSession and AttendanceRecord (ADR-008), LeaveRequest, AttendanceCorrection. Experience endpoints `/staff/classes`, `/teacher/classes`, `/classes/{id}/roster`, `POST /classes/{id}/attendance`, `/parent/children`, `/students/{id}/attendance`, `/realtime/connection` (disabled). **Import the client into `apps/mobile/`** and fix its attendance bugs. | **The MVP slice works end to end** (§2) on real PostgreSQL, with real login, from the Expo client |
| 7. Learning | Assignments, materials, submissions, feedback (files via ADR-009) | Per Definition of Done |
| 8. Assessment | Exams, schedules, mark sheets, moderation, results, report cards (transactional publish) | Per Definition of Done |
| 9. Communication | Notifications (outbox → push/SMS/email adapters), announcements, chat; realtime per ADR-011 | Per Definition of Done |
| 10. Operations | Fees and payments (real gateway, webhooks, idempotency), HR, transport, library, hostel, inventory, documents, approvals | Per Definition of Done |
| 11. Intelligence | Analytics read models, risk rules (thresholds from the prototype), interventions, principal insights | Per Definition of Done |
| 12. Advanced | Chrome extension, AI assistant and tutor through a scoped tool gateway, 3D campus, advanced realtime | Per Definition of Done |

Kubernetes, OpenSearch, NATS and a data warehouse are **not** introduced until a phase needs them.

## 2. The MVP vertical slice: how each step is served

| Step | Backend (phase) | UI for the MVP |
|---|---|---|
| School exists | `POST /platform/schools` (3) or a `bootstrap_school` management command | Platform wizard (it already exists in the client), or the command |
| Admin login | `/auth/password/login` (2) | Client `sign-in` |
| Academic year → grade → section → subject | Canonical REST (3) | **D5:** either (a) the API through Swagger UI and a scripted seed for the MVP, or (b) build minimal console screens. The current console has no screens for subject creation or explicit parent creation. **Recommendation: (a)**, with console work in a later slice. |
| Teacher, student, parent | Canonical REST (4). `POST /console/students` (creates the student, guardian and enrolment) and `POST /console/staff` are added because the client already calls them. | Console Students/Staff "add" dialogs, if D7 allows admin access, else as a principal |
| Enroll and assign teacher | Canonical REST (4); `POST /console/academics/allocate` | API / console Academics |
| Timetable | Canonical REST (5) | API (console draft/publish later) |
| Teacher login → sees classes → takes attendance | Experience endpoints (6) | Client `staff/attendance` |
| Parent login → child's attendance | Experience endpoints (6) | Client `parent/attendance` |

An automated **end-to-end API test** walks the whole slice. It is the acceptance test for Phase 6.

### Client fixes scheduled with the MVP (Phase 6)

These are small and targeted, with no rewrites:

1. Keep one `client_id` per attendance draft and reuse it on retry. Generate a new one only after the server accepts the submission.
2. Stop collapsing `excused` and `half_day` statuses in `toMark()`.
3. Call `POST /auth/logout` and `unregisterPushDevice` on sign-out.
4. `downloadFile`/`openFile`: attach the `Authorization` header only to `API_URL` origins.
5. Gate the `simulate` payment buttons behind `__DEV__`. The server ignores `simulate` regardless.
6. Pick one package manager (npm, matching the existing lockfile) and add `typecheck` and lint to CI.

Larger client changes, each proposed before it is implemented:

- an offline attendance queue
- httpOnly cookie authentication for the web console
- generated OpenAPI types
- a realtime rewrite if ADR-011 picks Channels

## 3. Phase 1 proposal (awaiting approval)

### Files to create

```
backend/
  pyproject.toml                  # uv-managed; ruff, mypy, pytest config
  uv.lock
  manage.py
  config/
    settings/{__init__,base,dev,test,prod}.py
    urls.py  asgi.py  wsgi.py  celery.py
  eduflow/
    core/                         # cross-cutting, no domain logic
      apps.py
      middleware.py               # request ID / correlation ID, structured access log
      logging.py                  # structlog config + redaction processor
      exceptions.py               # DRF exception handler -> {"error": {...}} envelope
      api/health.py               # /api/v1/health/live, /api/v1/health/ready
      tasks.py                    # Celery ping task (proves worker + request-id propagation)
      tests/{test_health,test_errors,test_request_id,test_settings_prod}.py
  conftest.py
  Dockerfile                      # multi-stage, non-root, healthcheck
  .dockerignore
infra/docker/
  docker-compose.yml              # backend, worker, beat, postgres:17, redis:7, minio
  postgres/init/                  # least-privilege app role
.env.example
.github/workflows/backend.yml     # ruff, mypy, makemigrations --check, check --deploy, pytest (PG+Redis services), schema export, docker build, pip-audit
docs/
  architecture/overview.md
  api/conventions.md              # error envelope, no trailing slash, IDs, dates, pagination, versioning
  database/conventions.md         # keys, constraints, index rules, migration rules
  security/model.md               # tenancy/authz summary (filled in Phase 2)
  deployment/local-setup.md       # Docker + non-Docker setup
  deployment/backup-restore.md    # pg_dump/pgBackRest plan (skeleton)
  ONBOARDING.md
README.md
```

### Dependencies

- **Runtime:**
  - Django 5.2 LTS, djangorestframework, drf-spectacular
  - django-environ, psycopg[binary] 3
  - redis, celery, django-redis
  - structlog, django-prometheus
  - django-storages[s3] and boto3 (configured, but unused until Phase 7)
  - argon2-cffi (wired in Phase 2)
  - uuid-utils for UUIDv7
- **Development:** pytest, pytest-django, pytest-cov, factory_boy, ruff, mypy, django-stubs, djangorestframework-stubs, pip-audit.

Exact versions are pinned in `uv.lock` at creation time.

### Behaviour delivered

- **Health endpoints:**
  - `GET /api/v1/health/live` returns `200 {"status":"ok"}`.
  - `GET /api/v1/health/ready` checks the database and Redis. It returns `200`, or `503` naming the failing check but not leaking its host.
- **Error envelope:** every error is a JSON envelope with a stable `code` and a `request_id`. A 404 on an unknown route is JSON, not HTML.
- **Request IDs:** `X-Request-ID` is generated or accepted, echoed in the response, included in logs, and propagated into Celery.
- **Production settings:** they fail fast when a setting is insecure, and `check --deploy` is clean.
- **Local environment:** `docker compose up` brings up the API, worker, beat, PostgreSQL, Redis and MinIO, all with healthchecks.
- **OpenAPI:** the schema is served at `/api/v1/schema` and `/api/v1/docs`. Docs are enabled only in development.

### Verification plan

1. **Locally, without Docker:**
   - `uv sync` (after `pip install uv`)
   - `ruff format --check`, `ruff check`, `mypy`
   - `makemigrations --check`
   - Tests run only if PostgreSQL is reachable. They will **not** fall back to SQLite.
2. **With Docker (after D2):**
   - `docker compose up --build`
   - curl both health endpoints
   - `docker compose exec backend pytest`
3. **In CI:** the full pipeline on push. This step needs the repo pushed to a branch; it will not be pushed without approval.

### Out of scope for Phase 1

Users, authentication, tenancy and every domain model. Phase 1 creates **no** domain migrations; it only verifies that the migration check runs.

## 4. Working agreements

- **Branches:** one branch per phase (`phase-1/foundation`, …). Commits are grouped logically. Nothing is pushed or merged without approval.
- **API changes:** every API change updates the OpenAPI schema and `docs/api/`. Breaking changes need either a `/api/v2` path or a documented migration.
- **Tests:** every new tenant-scoped endpoint is registered with the isolation-matrix test, and CI fails if one is not.
- **Commands:** each phase report lists the files created and changed, the commands run, the test results, the remaining work, and the decisions taken.

---

## 5. Phase 1 status (2026-10-08)

Implemented on branch `phase-1/foundation`. These are the deviations from the §3 proposal:

| Item | Proposal | Actual | Reason |
|---|---|---|---|
| Object storage | MinIO | RustFS (S3-compatible) | MinIO images are no longer publicly available (ADR-017) |
| Compose file | `infra/docker/docker-compose.yml` | `infra/docker/compose.yaml`, plus a root `compose.yaml` that includes it | Lets you run `docker compose up` from the repo root with the root `.env` |
| Least-privilege database role | `infra/docker/postgres/init/` | Deferred to Phase 2 | Belongs with the append-only audit trigger and grants (ADR-015). Phase 1 has no domain tables. |
| Prometheus metrics endpoint | Not in the §3 list | Not added | Observability is prepared through structured logs and request IDs. The metrics endpoint comes with the first deployed environment. |
| Smoke test | Not listed | `scripts/smoke-test.sh`, run locally and in CI | Verifies the stack end to end, not just that containers start |

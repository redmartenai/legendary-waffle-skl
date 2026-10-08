# EduFlow: Architecture Decisions

Each record uses the format **Context → Decision → Consequences**.

Status values:

- **Accepted:** decided in the project brief, or follows directly from it.
- **Proposed:** the recommendation from analysis; it needs sign-off.
- **Open:** a decision is needed before the phase named in the record.

New decisions are appended to this file. A decision that has been made is never edited in place; a later ADR supersedes it.

---

## ADR-001: Modular monolith on Django + DRF. *Accepted*

**Context**

The brief fixes the stack as Python, Django, DRF, PostgreSQL, Redis, Celery, Channels and Docker. The PDF's NestJS suggestion is superseded.

**Decision**

One Django project, `backend/`, with one Django app per domain module under `backend/eduflow/`:

- `identity`
- `tenancy`
- `academics`
- `people`
- `timetable`
- `attendance`
- …and later modules.

Each module follows the same internal layout:

```
models.py       data + DB constraints only
services.py     write-side business operations (transactions, audit, events)
selectors.py    read-side query functions (always tenant-scoped)
policies.py     authorization/data-scope rules for this module
api/            serializers, views, urls (thin)
tests/
```

The rules:

- Views call services and selectors. Serializers validate shape only.
- Modules talk to each other through `services`, `selectors` and domain events. They never write directly to another module's models.

**Consequences**

- A module can later be extracted along its service boundary.
- Cross-module workflows that need ACID stay in one transaction while the system is a monolith.

## ADR-002: Monorepo layout. *Proposed*

**Decision**

This repository becomes the monorepo:

```
backend/            Django project
apps/mobile/        the eduflow-new Expo client (imported with history at MVP integration)
docs/               architecture/ api/ database/ security/ deployment/ + top-level plans
infra/              docker/ compose files, later deploy manifests
.github/workflows/  CI
```

The client is imported from `origin/eduflow-new` with `git merge --allow-unrelated-histories` into `apps/mobile/`. It is imported only when the backend can serve its first screens, which happens in Phase 6. **Nothing is ever taken from `origin/master`.**

**Consequences**

- One PR can change the API, the generated types and the client together.
- CI runs per path: backend jobs on `backend/**`, client jobs on `apps/mobile/**`.

## ADR-003: Tenancy is a shared database and schema with a mandatory school key, enforced in the application layer. *Proposed*

**Context**

The client sends `X-School-Id` on every authenticated call. One user can belong to several schools. The tenant hierarchy is Organization → School → Campus → AcademicYear → Term → Grade → Section.

**Decision**

- `School` is the tenant boundary. `Organization` groups schools for billing and ownership; it does not by itself grant data access.
- Every school-owned table has a non-null `school_id` foreign key and an index that leads with `school_id`.
- A middleware/authentication step resolves `request.tenant`:
  1. Read `X-School-Id`.
  2. Require an **active `Membership`** for `(request.user, school)`, and require `school.is_active`.
  3. Otherwise return `403 {"error":{"code":"tenant_forbidden"}}`.
  4. Platform staff never get implicit school access. They use `/platform/*` endpoints, which are separately audited.
- Every selector takes the tenant explicitly (`selectors.students_for(tenant, actor, …)`). Model managers expose `.for_school(school)`.
- A test helper asserts that each API endpoint 404s on a different school's object IDs. 404 rather than 403, so the endpoint does not leak whether the object exists.
- Object lookups use `get_object_or_404(Model.objects.for_school(tenant), pk=…)`, never `Model.objects.get(pk=…)`.
- IDs are UUIDv7: time-ordered, so indexes stay compact, and not enumerable.
- **PostgreSQL Row-Level Security** is defence in depth, evaluated in Phase 2:
  - The app sets `SET LOCAL app.school_id` per request or transaction, and policies check it.
  - It is adopted only if it proves workable with Celery and admin tooling. Application-layer enforcement plus isolation tests remain the primary control either way.

**Consequences**

- Simple operations: one database and standard migrations.
- Isolation correctness depends on discipline, so it is backed by mandatory isolation tests for every endpoint (see ADR-013).

## ADR-004: Authorization is Role → Permission (module.action) → Data Scope → Record. *Proposed*

**Context**

The console already models roles as a module × action matrix with a per-module data scope (`school | classes | own | none`). The client does not send the active role.

**Decision**

- **Roles** are per school.
  - System roles (`principal, admin, teacher, accountant, transport_manager, parent, student, driver, attendant`) are seeded from a versioned default matrix.
  - Custom roles are cloned from a system role (`based_on`).
- **Permissions** are codenames `"<module>.<action>"`, for example `attendance.create`.
- **RolePermission** maps a role to a permission, with a `data_scope` for that module.
- **UserRole** (inside `Membership`) gives a user roles in a school, with optional `title` and `department`.
- **Effective permission** for a request:
  1. Take the union of the user's roles in `request.tenant`.
  2. The broadest scope wins for each module.
  3. The result is cached per `(user, school, roles_version)`.
- **Data scope** is resolved by each module's `policies.py` into a queryset filter. For example:
  - `classes` (teacher) → sections where the teacher has an active `TeacherAssignment` or is class teacher.
  - `own` for parents → students linked by `StudentParent`.
  - `own` for students → the student themself.
- Each DRF view declares `required_permission = "attendance.create"`. A shared permission class checks it.
- Object-level checks call the module policy (`can_mark_attendance(actor, section, date)`).
- Role-shaped endpoint families (`/parent/*`, `/teacher/*`, `/staff/*`, `/driver/*`) also require that the user holds the matching role.
- Platform staff authorization is a separate flag (`User.is_platform_staff`) with its own permission class.

**Consequences**

- One model serves both the console's role editor and backend enforcement.
- Editing a role invalidates the cached permissions.
- Role changes are audited.

## ADR-005: Authentication uses phone OTP and password with JWT access + rotating refresh tokens. *Proposed*

**Context**

The client expects:

- `/auth/otp/{request,verify}`, `/auth/password/login`, `/auth/token/refresh {refresh} → {access, refresh}`
- An `AuthSession {access, refresh, user, memberships}`
- Invite and forced-password-change flows

**Decision**

- **Tokens:** `djangorestframework-simplejwt`.
  - Access tokens last 10 minutes and carry only `sub` and `jti`. They carry no school and no role; those are resolved per request.
  - Refresh tokens rotate on every use and are blacklisted after use. A reused refresh token revokes the whole token family.
  - "Remember me" gives a 30-day refresh lifetime; otherwise 1 day.
- **Logout and sessions:**
  - `POST /auth/logout` blacklists the refresh token and optionally unregisters the push token.
  - A `UserSession` (device) table lets users and admins list and revoke sessions.
- **Passwords:**
  - Hashed with Argon2 (`argon2-cffi`), plus Django's password validators. Minimum length 10, matching the client.
  - Login is rate-limited per identifier and per IP.
- **OTP:**
  - The code is 6 digits. Only its HMAC is stored.
  - It expires in 5 minutes and allows at most 5 attempts per challenge.
  - Requests are rate-limited per phone number and per IP.
  - SMS goes through an `SmsGateway` interface; development uses a logging adapter.
  - `dev_code` is returned only when `DEBUG and OTP_ECHO_DEV_CODE`. A startup check refuses that combination in production settings.
- **Enumeration:** login and OTP endpoints give identical responses whether or not the account exists.
- **Web console:** before any production web deploy, move to httpOnly `Secure` `SameSite=Lax` cookies plus a CSRF token. Native apps keep bearer tokens in SecureStore.
- **Audit:** every security event (login success/failure, OTP issue/verify, refresh-token reuse, password change, logout) writes an audit record.

## ADR-006: The API has canonical resource endpoints plus a thin "experience" layer for client-shaped reads. *Proposed*

**Context**

The brief asks for REST resources (`/api/v1/students/`, `/api/v1/attendance/`). The existing client calls screen-shaped endpoints such as `/staff/classes`, `/classes/{id}/roster` and `/students/{id}/attendance?month=`, plus about 120 console aggregates.

**Decision**

- **Canonical resources** live in each domain module, for example `/api/v1/academic-years`, `/grades`, `/sections`, `/subjects`, `/students`, `/attendance/sessions`. They:
  - are used by administration, integrations and tests;
  - get full CRUD where it is appropriate;
  - use cursor pagination.
- **Experience endpoints** serve the existing client paths, starting with the MVP set in CURRENT_STATE §5. They:
  - are read models composed from module selectors;
  - delegate every write to the same services the canonical endpoints use, so business logic is never duplicated.
- Experience endpoints are built **only when a slice needs them**. The console aggregates come in their phases.

**Conventions (both kinds of endpoint)**

| Topic | Convention |
|---|---|
| Path | `/api/v1/` prefix. **No trailing slash**, with routers configured `trailing_slash=False` to match the client. |
| Field names | `snake_case` |
| IDs | UUID strings |
| Dates | ISO dates. Times of day are school-local `HH:MM`. Timestamps are UTC ISO-8601. |
| Money | Decimal strings |
| Errors | One exception handler emits `{"error":{"code","message","fields"?,"retry_after_seconds"?}}`. `code` is a stable machine string (`validation_error`, `not_authenticated`, `permission_denied`, `tenant_forbidden`, `not_found`, `conflict`, `rate_limited`, `server_error`). Every error carries the request ID. |
| Request bodies | Unknown fields are rejected, to block mass assignment. Writable fields are explicitly allow-listed per serializer. |
| Pagination | Canonical lists use cursor pagination. Experience endpoints keep the client's existing page/limit envelopes. |
| Contract | OpenAPI 3.1 via `drf-spectacular`. The schema is checked in CI; a breaking diff fails the build unless it is labelled. The client generates types with `openapi-typescript`. |

## ADR-007: Domain vocabulary is Section (homeroom), not a separate "Class" model, for the MVP. *Proposed*

**Context**

In the client, "class" means a homeroom section (`label "Grade 9 · B"`); `/classes/{id}` takes a section ID. The brief lists both `Section` and `Class`.

**Decision**

- `Grade` and `Section` belong to an `AcademicYear`. Sections are re-created per year, which supports promotion.
- `StudentEnrollment(student, section, academic_year, roll_no, status, start/end dates)`. At most one active enrollment per student per year, enforced by a partial unique constraint.
- `TeacherAssignment(teacher, section, subject, academic_year, is_class_teacher)`. A partial unique index enforces one class teacher per section.
- A teaching "class" is `(section, subject)` via `TeacherAssignment`. A separate `ClassGroup` model for electives and cross-section groups is deferred until electives are needed (ADR to follow).
- The client's `/classes/{id}` maps to section ID.

## ADR-008: The MVP attendance model is a daily section register with idempotent full-replace submission. *Proposed*

**Decision**

- **Models:**
  - `AttendanceSession(school, section, date, period NULL, taken_by, submitted_at, locked_at, client_id)` with a unique constraint on `(section, date, period)` using `NULLS NOT DISTINCT`.
  - `AttendanceRecord(session, student, status, note)` with a unique constraint on `(session, student)`.
  - Status is a DB `CHECK` limited to the five statuses.
- **Submission** (`POST /classes/{id}/attendance`):
  - Upserts the session and records in one transaction.
  - It is a **full replacement** in which any enrolled student who is missing counts as `present`. This matches the client's exceptions-only payload.
  - It is naturally idempotent because the same payload gives the same state.
  - `client_id` (and/or an `Idempotency-Key` header) is stored to return the original response on replay.
- **Server-side rules:**
  - Each `student_id` must be actively enrolled in that section.
  - The actor must be in scope for the section (assigned teacher or class teacher, or a school-scope role).
  - The date may not be in the future.
  - After the school's cutoff or lock, changes go through `AttendanceCorrection` (old value, new value, reason, approval) rather than in-place edits.
- **Audit and events:** corrections and post-lock edits are audited. `AttendanceMarked` is emitted via the outbox (ADR-010).
- **Period-wise attendance** is a later extension using the same tables, with `period` set.

## ADR-009: File storage uses S3-compatible private buckets behind an authorization check. *Accepted (brief) / details Proposed*

- **Storage backend:** `django-storages` with an S3 backend. MinIO in development; S3, R2 or similar in production, chosen by environment only.
- **Database:** files are never stored in PostgreSQL. A `StoredFile` row holds `school_id`, owner, a server-generated key (`<school>/<module>/<uuid>`), the original name (sanitised for display only), MIME type (sniffed, not trusted from the client), size, checksum and scan status.
- **Uploads:**
  - Validated against a per-purpose allow-list of types and sizes.
  - A file stays `pending_scan` until an `AntivirusScanner` interface clears it (no-op in development, ClamAV later).
- **Downloads:**
  - Go through an API endpoint that authorizes the actor.
  - The endpoint audits the access where it is sensitive.
  - It then returns a 302 to a presigned URL that expires in 60 seconds.
- **Client fix:** the client must stop sending the bearer token to absolute URLs (CURRENT_STATE §12).

## ADR-010: Background work uses Celery + Redis with a transactional outbox. *Proposed*

- **Outbox:** services write an `OutboxEvent` row in the same transaction as the business change. A relay (Celery beat plus `on_commit` kick) publishes these events to Celery tasks.
- **Idempotency:** tasks are idempotent and keyed by event ID. A processed-event table makes retries safe.
- **Retries:** exponential backoff, with dead events kept for inspection.
- **Tracing:** request and correlation IDs are propagated into task headers and logs.
- **Queues:** `default`, `notifications`, `reports`.
- **Scope:** Celery is not used for plain synchronous CRUD.
- **Event bus:** NATS or Kafka is not introduced until scale demands it; the outbox interface keeps that change local.

## ADR-011: Realtime transport. *Open (decide before Phase 9)*

**Context**

The brief specifies Django Channels. The existing client is built on **Centrifugo's** client (`centrifuge`): connection tokens, `personal:#<user>` and `trip:<id>` channels. Every realtime feature already degrades to polling when `/realtime/connection` returns `{enabled:false}`.

**Options**

| Option | Upside | Downside |
|---|---|---|
| A. Django Channels (brief) | One runtime | The client's realtime module must be rewritten for a plain WebSocket protocol (about 3 files) |
| B. Self-hosted Centrifugo, with Django publishing through its HTTP API | Zero client change; scales independently | Another service that is not in the brief |

**Interim decision**

The MVP needs no realtime. The backend returns `{enabled:false, poll_interval_seconds}` and the client polls.

**Recommendation**

Option A, keeping the brief's stack, unless push to many concurrent bus trackers makes B clearly better. This is to be confirmed by the project owner. The requirement is not changed silently.

## ADR-012: Observability. *Proposed*

- **Logging:** `structlog` JSON logs carrying `request_id`, `correlation_id`, `school_id`, `user_id`, route and latency.
  - A redaction processor strips `password`, `token`, `refresh`, `access`, `otp`, `code` and `authorization`.
  - Personal data (names, phone numbers) is not logged by default.
- **Request IDs:**
  - `X-Request-ID` is accepted (when well-formed) or generated, and echoed in every response and error body.
  - It is propagated into Celery tasks and outbox events.
- **Metrics:** `django-prometheus` exposes `/metrics` on an internal port.
- **Tracing:** OpenTelemetry instrumentation is wired but disabled until a collector exists.
- **Health:**
  - `/api/v1/health/live` is process-only.
  - `/api/v1/health/ready` checks PostgreSQL, Redis and (later) storage.
  - Neither endpoint requires authentication, and neither leaks versions or hosts.

## ADR-013: Testing strategy and quality gates. *Proposed*

- **Framework:** `pytest` + `pytest-django` + `factory_boy`, against real PostgreSQL (no SQLite) and Redis.
- **Required test types:**
  - Service tests for each module.
  - API tests for every endpoint covering:
    - unauthenticated → 401
    - wrong role → 403
    - out of scope → 404
    - other school → 404
    - invalid input → 400 with `fields`
    - the happy path
  - A reusable **tenant isolation matrix** (School A actor × School B objects) that runs over every registered tenant-scoped endpoint, so new endpoints are covered automatically.
  - Query-count assertions on list endpoints, to catch N+1 queries.
- **CI gates:**
  - `ruff format --check` and `ruff check`
  - `mypy` (django-stubs), strict on new modules
  - `manage.py makemigrations --check`
  - `manage.py check --deploy` with production settings
  - Tests with a coverage floor
  - OpenAPI schema generation and diff
  - Docker image build
  - Dependency audit (`pip-audit`)
- **Client:** `tsc --noEmit` plus lint. Jest tests are added for the session, HTTP and attendance code paths when the client is imported.

## ADR-014: Configuration, secrets and environments. *Proposed*

- **Settings:** split into `base`, `dev`, `test` and `prod`. All values come from environment variables via `django-environ`.
  - `prod` refuses to start if `DEBUG`, the default `SECRET_KEY`, `OTP_ECHO_DEV_CODE`, or a wildcard `ALLOWED_HOSTS` is set.
- **Secrets:** `.env` is git-ignored and `.env.example` is committed. Production secrets come from the platform's secret store. SOPS or OpenBao are adopted only when needed.
- **Python dependencies:** locked with **uv** (`uv.lock`) on Python 3.13. Docker images are multi-stage, run as non-root, have a pinned base digest, and include a healthcheck.
- **Versions:** Django is held at the **5.2 LTS** line, with an upgrade path to the next LTS.

## ADR-015: Audit logging. *Proposed*

- **Table:** `AuditLog` is append-only. It is enforced by a PostgreSQL trigger that rejects `UPDATE` and `DELETE`, and the app role has no delete grant.
- **Fields:**
  - `id`, `school_id` (nullable for platform and security events), `actor_id`, `actor_role(s)`
  - `action` (`attendance.correction.approved`), `target_type`, `target_id`
  - `before` and `after` as JSONB, filtered through a per-model field allow-list
  - `request_id`, `ip`, `user_agent`, `at`
- **Writing:** services write audit entries inside the same transaction as the change. There are no signal-based implicit audits, which keeps them explicit and testable.
- **Never logged:** secrets, tokens, OTPs or password hashes.
- **Indexes:** `(school_id, at DESC)`, `(school_id, target_type, target_id)` and `(actor_id, at DESC)`.
- **Retention:** the policy is configurable. Partitioning by month comes when volume requires it.

## ADR-016: Rate limiting. *Proposed*

- **Mechanism:** DRF throttles backed by Redis.
  - Anonymous: per IP.
  - Authenticated: per user.
  - Sensitive scopes have tighter limits:
    - `otp_request`: per phone and per IP
    - `otp_verify`
    - `password_login`: per identifier and per IP
    - `token_refresh`
    - `exports`
    - `uploads`
- **Response:** `429` with `retry_after_seconds`, which the client already handles.
- **Edge:** limits at the edge (reverse proxy or WAF) are added at deployment, not in the app.

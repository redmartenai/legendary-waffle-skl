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

## ADR-017: Local object storage is RustFS, because MinIO images are unavailable. *Accepted (Phase 1, deviation recorded)*

**Context**

- The brief says to use MinIO for development (ADR-009).
- During Phase 1 (2026-10-08), official MinIO images could not be obtained:
  - `docker pull minio/minio` returns "repository does not exist".
  - `quay.io/minio/minio` returns `401 UNAUTHORIZED`.
  - The `minio/mc` image is also unavailable.
- MinIO stopped distributing community builds and images in 2025. Using MinIO now would mean building an unmaintained source tree ourselves.

**Decision**

- Local and CI object storage use **RustFS** (`rustfs/rustfs:1.0.1`, Apache-2.0, pinned by digest).
  - RustFS is an S3-compatible server designed as a MinIO drop-in.
  - It was verified for this project before adoption:
    - a missing bucket returns 404;
    - a created bucket is private, and anonymous reads return 403;
    - presigned GET works;
    - a wrong secret returns 403.
- Bucket creation uses the backend's own `ensure_storage_bucket` command (boto3), not a vendor CLI.
- **The application depends only on the S3 API** (`django-storages` S3 backend, boto3). Storage is selected by `STORAGE_ENDPOINT_URL` / `STORAGE_*` variables, so MinIO (if a licensed image becomes available), AWS S3, Cloudflare R2 or another S3-compatible service can replace RustFS without code changes.

**Consequences**

- Swapping the storage server is a Compose/infrastructure change only.
- If the owner obtains MinIO images (for example, a commercial subscription), revert by changing the `storage` service image and its environment variable names.

## ADR-011 follow-up: realtime remains open

Per the owner's Phase 1 instruction, Centrifugo is **not** replaced and Channels is **not** introduced in Phase 1. A technical comparison and recommendation will be delivered before the realtime phase (Phase 9).

## ADR-018: PostgreSQL Row-Level Security through an application role and per-request session settings. *Accepted (Phase 2)*

**Context**

ADR-003 made the application layer the tenant boundary and asked Phase 2 to evaluate RLS as defence in depth, adopting it only if it works with Celery and admin tooling. Two constraints shaped the design:

- Requests run in autocommit (`ATOMIC_REQUESTS=False`), so `SET LOCAL` would last for a single query.
- The local database login is a superuser, and owners and superusers bypass RLS.

**Decision**

- Migration `core.0001` creates `eduflow_app` (`NOLOGIN NOBYPASSRLS`), grants it DML on all tables (now and by default privileges), and makes the migration role a member.
- Every request (except health probes) and every Celery task switches to `eduflow_app` and writes `eduflow.school_id`, `eduflow.user_id` and `eduflow.rls_bypass` with `set_config(…, false)`. Only server-verified values are written. The context starts empty (fail closed) and is reset at the end; it is re-applied on reconnect, and the connection is closed if a reset fails.
- School-owned tables get `tenant_rw` policies, plus SELECT-only policies for a user's own memberships and roles. The audit table gets read and insert policies only.
- `TenantTask` gives background jobs an explicit, cross-checked tenant.
- Migrations, management commands and test fixtures run as the owner, outside RLS.

**Consequences**

- A query that forgets its school filter returns nothing from other schools, and a write to another school fails.
- One extra round trip per request to set the context.
- RLS does not stop SQL injection (the app role can call `set_config`) and knows nothing of roles or scopes. Application authorization remains mandatory.
- RLS is adopted, satisfying ADR-003's evaluation clause.

## ADR-019: Session-bound access tokens and opaque rotating refresh tokens. *Accepted (Phase 2)*

**Context**

ADR-005 chose `djangorestframework-simplejwt` with rotation and reuse detection. simplejwt's own blacklist app stores tokens by `jti` and has no notion of a token family, so it cannot revoke "everything descended from this login".

**Decision**

- **Access tokens:** simplejwt JWTs, HS256, 10 minutes, with claims `sub`, `sid`, `jti`, `iss`, `aud`, `token_type`. The signing key is `JWT_SIGNING_KEY` or a key derived from `SECRET_KEY`. Every request checks the `sid` session (unrevoked, unexpired, active user), so revocation is immediate.
- **Refresh tokens:** 48 random bytes from `secrets`, opaque to the client, stored as SHA-256 only. Rotated on every use under a row lock. The family is an `AuthSession`; presenting a used token revokes the session.
- Lifetimes: 1 day sliding (30 with "remember me"), capped at 90 days from sign-in.
- simplejwt's `token_blacklist` app is not installed.

**Consequences**

- Logout, logout-all, password change, deactivation and reuse detection all take effect on the next request.
- One primary-key query per authenticated request (the session check). Caching it would weaken immediate revocation, so it is not cached.
- No grace window for lost refresh responses: such a client is signed out.
- No cryptography is implemented here: JWT signing is simplejwt's, randomness is `secrets`, hashing is `hashlib`.

## ADR-020: Data scopes combine as a union and are resolved by per-resource rules that fail closed. *Accepted (Phase 2)*

**Context**

ADR-004 said "broadest scope wins". Scopes such as `child` and `section` are not comparable, though: a teacher who is also a parent needs both.

**Decision**

- A grant holds a **set** of scopes (`RolePermission.scopes`). The effective grant for a permission is the union across the member's roles.
- Each resource declares a `ScopedResource` with one `Q` rule per supported scope. `school` (or `platform`) means school-wide. Rules are OR-ed, the school filter is always applied first, and an unsupported scope contributes nothing.
- `platform` scope cannot be granted to school roles. Platform access is `User.is_platform_admin` only.

**Consequences**

- Mixed roles work naturally, and adding a scope to a resource is one function.
- A mis-configured scope grants nothing rather than everything.

## ADR-021: OTP through a provider adapter, with codes and phone numbers stored only as HMACs. *Accepted (Phase 2)*

**Decision**

- The `SmsProvider` protocol has these adapters:
  - `ConsoleSmsProvider` (development; requires `DEBUG`)
  - `MemorySmsProvider` (tests)
  - `DisabledSmsProvider` (the default, so production does not send until a gateway is configured)
- Production refuses the console and memory providers.
- Codes are stored as `HMAC-SHA256(k, challenge_id:code)` and numbers as `HMAC-SHA256(k, phone)`. The keys are derived from `SECRET_KEY` with purpose labels.
- Rules: 5 attempts, 5 minutes, single use, 30-second resend cooldown, rate limits per number and per IP. A challenge is also created for unknown numbers, without sending an SMS.
- No commercial gateway is chosen. MSG91 or Twilio can be added as an adapter.

**Consequences**

- Phone sign-in works locally with no external service, and tests never send SMS.
- Rotating `SECRET_KEY` invalidates outstanding codes, which is harmless because they live for 5 minutes.
- Response timing for registered numbers includes the gateway call (recorded in the threat model).

## ADR-022: Grades are school-level academic levels; sections belong to a grade and an academic year. *Accepted (Phase 3)*

**Context**

ADR-007 placed both `Grade` and `Section` under `AcademicYear`. The Phase 3 brief instead defines Class/Grade as the school's stable academic level (school, name, code, display order) and Section as belonging to a school, an academic year and a class.

**Decision**

- `Grade` is per school and stable across years: "Grade 5" is the same row in every year. Names and codes are the school's own.
- `Section` is per (academic year, grade); sections are still created for each year, which keeps promotion simple.
- The API uses `/grades` and `/sections`. `/classes` is not used, because the existing client's `/classes/{id}` means a section (ADR-007); those experience endpoints arrive with attendance (Phase 6).
- Terms and rooms are deferred to the phases that need them (assessment and timetable).

**Consequences**

- Grade-level reporting across years needs no mapping table.
- Enrollments and teacher assignments copy the year (and grade) from their section, and composite foreign keys keep them consistent.

## ADR-023: Domain profiles link to the school membership, not to the user. *Accepted (Phase 3)*

**Context**

A user can belong to several schools (Phase 2). Staff, student and guardian records are school data. Linking them to `User` would let a profile in school A point at someone with no access to A, and every scope rule would need a membership join.

**Decision**

- `StaffProfile.membership` is required. `Student.membership` and `Guardian.membership` are optional (for people who do not sign in). All are one-to-one.
- A composite FK `(membership_id, school_id) → tenancy_membership (id, school_id)` guarantees that the membership belongs to the same school.
- Teacher = the `teacher` role (permissions) plus a `teaching` staff profile (domain record).
- Data-scope rules match on `actor.membership` directly.

**Consequences**

- No second identity table and no credentials in the domain.
- Deactivating a membership removes a person's access without touching their domain history.

## ADR-024: School-owned resources share one scoped endpoint implementation. *Accepted (Phase 3)*

**Decision**

- `ResourceListView` and `ResourceDetailView` (`authz/api/resources.py`) implement list, create, retrieve, update and delete on top of `TenantAPIView` and `ScopedResource`.
  - Reads come only from the read permission's scoped queryset.
  - Writes load the target through the write permission's scope, then call a service.
  - Lists are cursor-paginated, with typed, validated filters that can only narrow.
- Permissions per method are derived from `read_permission` / `write_permission` (deny by default). The OpenAPI description is generated from the same declaration.
- The domain uses `<resource>.read` and `<resource>.manage` permissions. `manage` covers create, update, archive and delete. The existing `student.*` and `staff.*` codenames are reused.

**Consequences**

- Twelve resources share one tested authorization path, instead of twelve hand-written ones.
- Lifecycle operations that are not CRUD (enrollment end and transfer) are explicit actions with their own permission check.

## ADR-025: Accounts join schools and link to student or guardian records only through verified invitations. *Accepted (Phase 4)*

**Context**

Phase 3 let an administrator set `membership_id` on a student or guardian record directly. Nothing proved that the person behind the membership controlled the contact details of that student or guardian, so a mistyped or malicious link would give a stranger a child's data (child scope follows `StudentGuardian`).

**Decision**

- `membership_id` is removed from the student and guardian write APIs. A record is linked to an account only by accepting an invitation for that record (`people.services.link_student_account` / `link_guardian_account`, called by `invitations`).
- An invitation stores only the SHA-256 digest of a 32-byte random secret. The link carries the secret after `#`, so it stays out of server logs. Public endpoints find the invitation by digest under a named, logged RLS bypass, then work in that school's context.
- Acceptance requires a one-time code sent to the invited address (OTP purpose `invitation`, bound to the invitation). It verifies exactly that channel.
- Roles and links are applied with the inviter's **current** authority at acceptance and resend (Phase 2 escalation rule). An inviter who lost access voids their pending invitations.
- Anonymous acceptance never takes over an account someone can sign in to (`409 account_exists`). An account nobody can reach (no password, nothing verified) is claimed by the verified recipient.

**Consequences**

- Guardian and student access always rests on a proven contact channel.
- One more public surface (preview, verification, accept), rate limited per IP and per secret.
- Delivery happens inside the transaction until the outbox (ADR-010) exists; a provider failure rolls the invitation back.
- Unlinking an account from a record needs its own audited flow (deferred).

## ADR-026: Timetable clashes are rejected by the database, using copies kept in sync by cascading foreign keys. *Accepted (Phase 5)*

**Context**

A timetable must never put a teacher, a section or a room in two places at once. Service checks alone race: two administrators can publish clashing timetables at the same moment. Schools also run several timetables at once (wings with different bell times, a new term starting mid-year), so "same period number" is not the same as "same time".

**Decision**

- A **timetable** is a version of the weekly plan with effective dates inside one academic year: `draft → published → archived`. Only published timetables produce schedules and lessons.
- A **slot** (period x weekday x section) references a **teacher assignment**, which gives it its teacher and subject. A composite foreign key `(assignment, staff, section, subject)` makes a slot naming anyone else impossible.
- A slot stores **copies** of its period's times and its timetable's dates and live flag. Composite foreign keys with `ON UPDATE CASCADE` keep them equal to their sources: wrong copies are refused, and changing a period's times or a timetable's dates rewrites every slot.
- **Within a timetable**, unique constraints allow one slot per (period, weekday) for each section, teacher and room. Periods never overlap (exclusion constraint), so this is a time check.
- **Across published timetables**, three exclusion constraints (`btree_gist`) refuse two live slots with the same section, teacher or room, the same weekday, overlapping times (`eduflow_timerange`) and overlapping dates. Because of the cascades, the constraints re-run when publishing, moving dates or changing bell times.
- The services pre-check the same rules to name the clashes in the `409`. The database stays the guarantee.

**Consequences**

- No clash can be written by any code path: API, ORM, admin scripts or concurrent requests (tested).
- Slots carry seven copied columns, which only the services set; responses never expose them.
- Changing a period's times on a large published timetable updates all its slots in one statement, and the clash check covers them all.

## ADR-027: Narrow-scope writes check the scope and then the relationship; schedules are authorised by their subject. *Accepted (Phase 5)*

**Context**

Until Phase 5, every domain write required a school-wide grant (ADR-024). Lessons are the first record a teacher must write for their own classes only. Schedules combine several resources (slots, sections, enrollments), so it must be clear whose permission decides.

**Decision**

- **Lessons:** `lesson.manage` with `self` scope.
  1. The view loads the slot or lesson through the caller's `lesson.manage` scope: `404` outside it.
  2. The service then requires the caller to be that slot's teacher with an **active** assignment, unless the grant is school-wide. A scope rule written for reading (for example the `self` rule's "a student's own section" branch) can therefore never authorise a write.
- **Schedules:** a teacher's, student's or section's schedule is shown when that record is visible under **both** its own read permission (`staff.read`, `student.read`, `section.read`) and `timetable.read`, each through that resource's scope rules.
  - The schedule then shows the subject's whole plan for the dates asked, including a student's earlier section after a transfer.
  - A school-wide `timetable.read` (teachers, by default) does not reveal which section any student is in.

**Consequences**

- Attendance (Phase 6) will follow the same two steps for teachers' writes.
- Schedule rules live in one place (`timetable/api/views.py`), and they reuse the existing scope rules.

## ADR-028: Attendance rules the sources left open: lock at the end of the day, corrections approved by someone else. *Accepted (attendance), open for review*

**Context**

ADR-008 fixes the register model and says that changes after "the school's cutoff or lock" go through corrections with approval. Neither the cutoff, the approver, nor the response shapes beyond `ClassRoster` and `AttendanceMonth` are defined anywhere. The source list and every open point are in [architecture/attendance.md](architecture/attendance.md#decisions-for-review).

**Decision**

- **Lock.** A register locks at the end of its date in the school's time zone (`locked_at`). Before that it is re-submitted (full replacement, ADR-008); afterwards each change is a correction. A register first taken for an earlier date is accepted and locked at once.
- **Corrections.**
  - Requested with `attendance.update` (the Phase 2 "Correct attendance" permission) on a locked record, with a reason. At most one pending per record.
  - Decided with a new permission, **`attendance.approve`**: school-wide, held by the school admin and principal.
  - The approver is never the requester.
  - Approval applies the change only if the record still has the old status.
- **Integrity in the database.**
  - A record's enrollment must belong to that student in that section (composite FK to a new `people_enrollment (id, student, section, school)` key).
  - A record's section and date are its register's.
  - Statuses are a `CHECK`.
- **Access** follows ADR-027:
  - Narrow writes are scoped, then re-checked against an active assignment.
  - The month view needs `student.read` and `attendance.read` over the student.
  - Registers and corrections are whole-class views that parents and students do not see.
- **Not yet:** period-wise registers, the `AttendanceMarked` outbox event (ADR-010 is not built), holidays (no calendar).

**Consequences**

- A configurable cutoff time, backfill limits or a different approval chain can replace these rules without migrating data.
- Schools with a single administrator need a second approver for corrections.

## ADR-029: White-label: branding beside the school record, domains proven by DNS, hosts that select but never authorise. *Accepted*

**Context**

Schools on the shared platform need their own name, colours, logo and favicon, and their own address. The client already expects `branding` on the public school record (CURRENT_STATE §7). Hosts and uploaded images are classic attack surfaces: host-header spoofing, domain takeover, and script-carrying images.

**Decision**

- **Branding** lives in a new `branding` module:
  - colours and logo/favicon references in `SchoolBranding`;
  - images in `BrandAsset`, private in object storage;
  - the name stays on `School` and is not copied.
- **Public branding is a fixed set of public fields** (`selectors.payload`). It is exposed:
  - in `GET /branding/resolve?host=`;
  - in `GET /schools/lookup`;
  - in the invitation preview;
  - images through `GET /branding/assets/{id}`.
- **Images are validated by content.** PNG, JPEG and WebP logos; PNG and ICO favicons. SVG and GIF are refused.
  - They are served by the API with an exact type, `nosniff` and a sandboxing CSP, under immutable IDs.
  - They are not re-encoded: no image library is added.
- **Hosts:**
  - A school is reachable at `<code>.<base domain>` (the code is immutable) and at **verified** custom domains.
  - Verification is a DNS TXT challenge checked by a pluggable `DOMAIN_VERIFIER`: none by default (manual by EduFlow staff), or DNS-over-HTTPS with the standard library.
  - A hostname belongs to one school platform-wide.
  - A daily job disables domains whose proof has lapsed.
  - The platform can suspend a domain, and the school cannot lift the suspension.
- **Host binding:** a request on a school's host acts only in that school, and membership is still required. Platform endpoints refuse to run on school hosts.
- **`ALLOWED_HOSTS` stays wildcard-free.** Custom domains serve the web app by default; serving the API on them requires an explicit host entry.
- **Permissions:** `branding.manage` and `domain.manage` (school-wide; school admin and principal). Platform staff manage any school through `/platform/...`. Every change is audited.

**Consequences**

- Custom-domain TLS (on-demand certificates) is an edge concern, gated on `GET /branding/resolve`. It is not built.
- Without a configured verifier, each custom domain needs a manual, audited verification by platform staff.
- Separate per-school store apps are out of scope: the shared app is themed at run time.

## ADR-030: The ERP modules: one module per domain, the people model reused, approvals aggregated, rules the sources leave open made settings. *Accepted*

**Context**

The product scope (screen documentation, HLD, prototype) covers admissions, homework, conduct, examinations, fees, HR and payroll, library, hostel, transport, inventory and procurement, visitors and alumni. Several of their rules are not stated anywhere: statutory deductions, depreciation, library fines, recruitment stages, leave policy, grading scales.

**Decision**

- **One Django app per domain**, each with models, services, policies (data-scope rules), an API package, RLS and same-school foreign keys (migration `0002_tenant_integrity_and_rls`), and tests including an isolation matrix and an RLS check. No second student, staff or identity model: every module references `people` and `tenancy`.
- **Shared scope rules** (`people.scoping`): a record about a student, a section or a staff member gets the same section / child / self rules everywhere. `may_write` and `may_write_for_student` implement ADR-027 narrow writes; `ResourceView.narrow_writes` lets a resource view accept a narrow write grant and leave the relationship check to the service.
- **The approvals queue aggregates; modules decide.** Admission offers, mark sheets, marks corrections, refunds, leave, outpasses and purchase requisitions register providers. Each provider names its school-wide permission (`admission.approve`, `assessment.approve`, `fee.approve`, `leave.approve`, `hostel.approve`, `procurement.approve`).
- **Files** reuse `documents.files` (content-identified types, 10 MB, private storage, attachment download with a sandboxing CSP).
- **Unstated rules are not invented.** PF, ESI and TDS are entered amounts. Assets keep cost and date but no depreciation. Loan period, fine rate, leave types and grade bands are the school's settings, with no defaults where no source gives one. Recruitment stages are provisional.
- **Money** is `Decimal` everywhere. Fee and donation receipts are sequential per school under a lock. Payments are idempotent on a client key.
- **No fabricated data.** GPS positions are stored only as reported (validated range and clock skew); there is no estimate. There are no online payments: no gateway is integrated.
- A **Security** system role is added for the visitor gate.

**Consequences**

- Every module is independently testable and isolated by the same mechanisms as Phases 2 to 6.
- A school must configure fine rates, leave types and grade bands before those features compute anything.
- Statutory payroll needs a later decision (an integration or rules the school confirms).

## ADR-031: Monitoring is a rule engine over the live modules, producing one deduplicated alert per rule with a lifecycle. *Accepted*

**Context**

The Monitoring Intelligence Layer is the product's core (HLD; prototype `engine/monitoring.ts`). The prototype computes alerts on the client from demo data, with fixed thresholds, owners and escalations (README "First alert set").

**Decision**

- **Rules** (`monitoring/rules.py`) read the modules' own tables through their selectors, inside the school's RLS context. Thresholds are a per-school `MonitoringSettings` row whose defaults are the prototype's constants.
- **One live alert per rule per school**, enforced by a partial unique constraint. Each evaluation updates the alert's rows and `last_seen_at`; a rule that stops firing resolves its alert automatically. A resolved alert stays as history; if the rule fires again, a new alert opens.
- **Lifecycle:** open, acknowledged, resolved. Acknowledging stops escalation. Escalation happens once, after the README's time ("after 7 days", "48h", "live"); rules without a stated time are not escalated automatically (the principal sees them from the start).
- **Owners:** each alert row carries the staff member who owns it (class teacher, subject teacher, the teacher waiting on a reply). New alerts notify the owners and the school's principals and administrators.
- **Role-scoped views:** school scope sees every alert; self scope sees the alerts with a row it owns, trimmed to those rows. Finance alerts have no staff owner.
- **Scheduling:** a beat task enqueues one tenant task per active school (`tenancy.jobs`, every 15 minutes by default); `POST /monitoring/evaluate` runs it on demand.
- **Risk, scorecards and pulse** use the prototype's weights and formula. Components without data score as the prototype does, and the API lists them in `no_data`.
- **Ask EduFlow** is deterministic intent matching (no language model). Each intent reads through the caller's permissions and data scopes; an intent the caller may not use answers that they have no access, without revealing data.
- **Not built:** "Class without a teacher" (no substitutions); a holiday calendar (school days come from the published timetable and from registers taken).

**Consequences**

- Alerts are explainable and reproducible from the data at a point in time.
- An event outbox (ADR-010) would allow near-real-time evaluation; until then alerts lag by up to the evaluation interval.

## ADR-032: LMS without hosted video, live-class lock-in or invented AI output. *Accepted*

**Decision**

- Lessons are notes (text or a file), a link to a recorded video hosted wherever the school hosts it (https only), or a quiz. Families see published content only.
- Quizzes are scored on submission; the best attempt counts; answer keys are shown to a student only after their first attempt; lists never carry keys.
- Live classes store any provider's join link; joining through EduFlow records attendance.
- AI question drafting goes through `AI_QUESTION_PROVIDER` (empty by default: `503 ai_unavailable`). Provider output is validated and malformed questions are dropped, never repaired. Drafts are returned for review and are never saved or published automatically.

**Consequences**

- Choosing a video host, a meeting provider or an AI provider needs no schema change.

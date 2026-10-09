# Architecture Overview

EduFlow is a multi-tenant School Operating System. The backend is a **modular monolith** on Django and DRF (ADR-001), and serves the Expo mobile and web client (and, later, a Chrome extension) over a versioned REST API.

```
 Expo app (iOS / Android / web)   Chrome extension (later)
                 │  HTTPS, Bearer token, X-School-Id, X-Request-ID
                 ▼
        ┌──────────────────────────────┐
        │ Django + DRF  (/api/v1)      │  request context · error envelope · authn · tenancy · authz
        │  eduflow/<module>/           │  models · services · selectors · policies · api
        └───────┬───────────┬──────────┘
                │           │ enqueue (request_id in headers)
                ▼           ▼
          PostgreSQL 17   Redis 7 ──► Celery worker(s)  ◄── Celery beat
          (system of      (cache,      (retry-safe jobs:
           record)         broker)      notifications, reports, imports)
                │
                ▼
          S3-compatible object storage (private bucket; signed URLs)
```

Realtime transport (Django Channels vs Centrifugo) is **an open decision** (ADR-011). The client currently polls.

## Repository layout

```
backend/                Django project (uv-managed)
  config/               settings (base/dev/test/prod), urls, celery, wsgi/asgi
  eduflow/core/         cross-cutting infrastructure only, no domain logic
  eduflow/identity/     users, sessions, tokens, OTP (Phase 2)
  eduflow/tenancy/      schools, memberships, tenant resolution, tenant tasks (Phase 2)
  eduflow/authz/        permissions, roles, data scopes, base API views (Phase 2)
  eduflow/audit/        append-only audit trail (Phase 2)
  eduflow/academics/    campuses, academic years, departments, grades, sections, subjects (Phase 3)
  eduflow/people/       staff, students, guardians, enrollments, teacher assignments (Phase 3)
  eduflow/invitations/  verified invitations: staff, student and guardian onboarding and account linking (Phase 4)
  eduflow/timetable/    timetables, periods, slots, lessons and schedules (Phase 5)
  eduflow/attendance/   daily section registers, records and corrections (Phase 6, attendance)
  eduflow/branding/     white-label: school colours, logo, favicon, subdomains and custom domains
  eduflow/<module>/     later domain modules
apps/mobile/            Expo client (imported in Phase 6 from origin/eduflow-new)
infra/docker/           Compose stack
scripts/                smoke test and dev scripts
docs/                   architecture, api, database, security, deployment
.github/workflows/      CI
```

## Module rules

Each domain module has this layout:

| File | Responsibility |
|---|---|
| `models.py` | Data and database constraints only |
| `services.py` | Writes: transactions, audit, events |
| `selectors.py` | Reads: always tenant-scoped, N+1-safe |
| `policies.py` | Authorization and data scope |
| `api/` | Thin serializers, views and URLs |
| `tests/` | The module's tests |

Two rules govern how modules interact:

- Modules call each other through `services`, `selectors` and events, never by writing another module's tables.
- Views never contain business rules.

## Cross-cutting infrastructure (Phase 1, `eduflow/core`)

| Concern | Where | Behaviour |
|---|---|---|
| Request ID | `request_context.py`, `middleware.py` | Accepted from `X-Request-ID` when safe, else generated. Echoed in the response and bound to every log line. |
| Structured logging and redaction | `logging.py` | One structlog pipeline for our code, Django, Celery and libraries. Secrets are masked. |
| Error envelope | `exceptions.py`, `views.py` | `{"error": {code, message, fields?, retry_after_seconds?, request_id}}` everywhere |
| Health | `health.py` | `live` (no I/O) and `ready` (PostgreSQL, Redis, storage) |
| Background context | `celery_context.py` | The request ID travels in task headers, and the worker logs carry it |
| Production guard | `config_validation.py` | Insecure production configuration refuses to start |
| Database tenant context (RLS) | `db_context.py`, `middleware.py` | Every request and task runs as `eduflow_app` with a verified tenant context (Phase 2, [rls.md](../security/rls.md)) |
| Security headers | `middleware.py` | CSP and `no-store` on API responses |
| Client IP | `client_ip.py` | `X-Forwarded-For` trusted only for configured proxies |

The Phase 2 security architecture is described in [phase-2.md](phase-2.md).

## Environments

| Settings | Used by | Key differences |
|---|---|---|
| `config.settings.dev` | Local Compose and host runs | `DEBUG` on, API docs on, console logs allowed |
| `config.settings.test` | pytest and CI | Real PostgreSQL and Redis, eager Celery, JSON logs, fast hasher |
| `config.settings.prod` | Any shared environment | Secure defaults plus fail-fast validation, docs off, HTTPS, HSTS |

# Phase 2: Security, Identity, Authentication, RBAC and Multitenancy

Phase 2 builds the security foundation every later module relies on. It adds no school domain (students, attendance and so on); it adds the machinery those modules will use.

## What was built

| Area | Module | Key pieces |
|---|---|---|
| Identity | `eduflow/identity` | `User` (email/phone), `AuthSession`, `RefreshToken`, `OtpChallenge`; password and OTP sign-in; token rotation; rate limits |
| Tenancy | `eduflow/tenancy` | `School`, `Membership`, `TenantModel`/`TenantQuerySet`, tenant resolution, `TenantTask`, platform endpoints |
| Authorization | `eduflow/authz` | Permission catalogue, 12 system roles, custom roles, `MembershipRole`, effective grants, `DataScope`, `ScopedResource`, base API views, escalation guards |
| Audit | `eduflow/audit` | Append-only `AuditEvent`, `record()`, school-scoped read API |
| Cross-cutting | `eduflow/core` | RLS database context, UUIDv7, trusted client IP, CSP / no-store headers, strict serializers, typed API errors |

## Request path

```
HTTP request
 │ RequestContextMiddleware   request ID, client IP (trusted proxies), user agent
 │ DatabaseContextMiddleware  SET ROLE eduflow_app, empty tenant context (fail closed)
 │ SecurityMiddleware / SecurityHeadersMiddleware   HSTS, nosniff, CSP, no-store …
 ▼
DRF view
 │ AccessTokenAuthentication  JWT + live session + active user  → eduflow.user_id
 │ NoPendingPasswordChange
 │ resolve_actor (TenantAPIView)  membership in X-School-Id school → eduflow.school_id
 │ required_permissions[method]   role grants (cached per rbac_version)
 │ throttles (auth endpoints)
 ▼
selectors / ScopedResource   school filter + data-scope filter (404 outside)
services                     transaction + audit.record(...)
 ▼
PostgreSQL                   RLS policies on every school-owned table
 ▲
end of request: context released, RESET ROLE
```

## Security boundaries

1. **Client → API.** Tenant, role and ownership are never taken from the client. `X-School-Id` is a request, checked against memberships.
2. **Authentication → authorization.** Tokens carry identity only (`sub`, `sid`); everything else is looked up per request.
3. **Application → database.** RLS repeats the school boundary. The application role cannot bypass it; the owner role is for migrations and operators.
4. **API → worker.** Tasks start with no tenant. `TenantTask` establishes one from an explicit, cross-checked `school_id`.
5. **Platform → school.** Platform administrators reach schools only through `/platform/*`.

## Decisions

| ADR | Decision |
|---|---|
| ADR-018 | RLS through a `NOLOGIN` app role plus per-request session settings, with application authorization remaining primary |
| ADR-019 | Short JWT access tokens bound to a revocable session; opaque, hashed, rotating refresh tokens with family revocation on reuse |
| ADR-020 | Data scopes are a union across roles, resolved by per-resource rules that fail closed |
| ADR-021 | OTP through a provider adapter; codes and phone numbers stored only as HMACs |

ADR-003, ADR-004, ADR-005, ADR-015 and ADR-016 (Phase 0) are implemented as proposed, refined by the ADRs above.

## Detailed documentation

- [security/authentication.md](../security/authentication.md): identity model, endpoints, generic failures, rate limits, web-session plan, headers
- [security/token-lifecycle.md](../security/token-lifecycle.md)
- [security/authorization.md](../security/authorization.md): RBAC, permissions, roles, escalation guards, data scopes
- [security/multitenancy.md](../security/multitenancy.md)
- [security/rls.md](../security/rls.md): policies, context lifetime, Celery, migrations
- [security/otp.md](../security/otp.md)
- [security/audit.md](../security/audit.md)
- [security/threat-model.md](../security/threat-model.md)

## API surface (all under `/api/v1`)

| Group | Endpoints |
|---|---|
| Authentication | `auth/password/login`, `auth/password/change`, `auth/otp/request`, `auth/otp/verify`, `auth/token/refresh`, `auth/logout`, `auth/logout-all`, `auth/sessions[/{id}]` |
| Identity | `me` |
| Tenancy | `schools/lookup` (public), `school`, `memberships[/{id}]`, `memberships/{id}/roles[/{role_id}]` |
| Authorization | `roles[/{id}]`, `permissions`, `me/permissions` |
| Audit | `audit-events` |
| Platform | `platform/schools[/{id}]`, `platform/users/{id}` |

All of them are in [`docs/api/openapi.yaml`](../api/openapi.yaml), with the bearer scheme, the `X-School-Id` header and the error responses for each.

## Local development behaviour

| Concern | Development (`dev`) | Production (`prod`) |
|---|---|---|
| OTP delivery | `ConsoleSmsProvider` writes codes to the backend log | A real adapter is required (the default disables OTP) |
| `dev_code` in OTP responses | Only with `OTP_ECHO_DEV_CODE=true` | Refused at startup |
| Rate limits | On (can be turned off with `RATE_LIMITS_ENABLED=false`) | Must be on |
| HTTPS, HSTS, secure cookies | Off | On, enforced |
| Swagger UI | On | Refused at startup |
| RLS | On (the dev database login is a superuser; requests still switch to `eduflow_app`) | On; `DATABASE_RLS_ROLE` must be set |

First steps on a fresh stack:

```bash
docker compose exec -e EDUFLOW_ADMIN_PASSWORD='choose-a-long-one' backend \
  python manage.py create_platform_admin --email you@example.com --full-name "You"
# Sign in via POST /api/v1/auth/password/login, then POST /api/v1/platform/schools
```

## Operations

| Step | When |
|---|---|
| `python manage.py migrate` | every deployment (creates the `eduflow_app` role on first run if permitted) |
| `python manage.py sync_rbac` | every deployment, after `migrate` |
| Beat schedule `identity.purge_expired_auth_records` | every 6 hours, automatic |

## Known limits and next steps

See [threat-model.md](../security/threat-model.md) for residual risks. In short:

- Edge rate limiting or a WAF is still required in production.
- A production SMS adapter is still to be written; its provider is not yet chosen.
- The httpOnly-cookie flow for the web console is designed, not built.
- Campus, section, child and self scope rules over real domain models arrived in Phase 3 ([phase-3.md](phase-3.md)).
- Platform-level audit reading, security alerting and MFA for staff are later work.

# Security Model

This is a summary of how EduFlow is secured. The decisions behind it are in [ARCHITECTURE_DECISIONS.md](../ARCHITECTURE_DECISIONS.md). Each item below is marked as implemented now or as planned for a named phase.

## Principles

1. **The backend is the only security boundary.** Hidden menus, client route guards and client-side filtering are UX, not security.
2. **Nothing identifying the tenant, role or ownership is trusted from the client.** `X-School-Id`, IDs in paths and bodies, and claimed roles are all validated against server-side state.
3. **Deny by default.** An endpoint is public only if it explicitly opts in.
4. **Least data.** Responses contain what the screen needs. Errors and logs contain no secrets.

## Status by area

| Area | Status | Notes |
|---|---|---|
| Deny-by-default API | **Implemented (Phase 1)** | The default DRF permission is `IsAuthenticated`, with no authenticators until Phase 2. Only the health probes are public. Tested. |
| Error hygiene | **Implemented** | One JSON envelope. Internal exception text never reaches clients. Tested for 4xx, 5xx and unknown routes. |
| Secret redaction in logs | **Implemented** | Applies to every log line, including those from Django, Celery and libraries. See "Logging" below. Tested, and verified against live container logs by the smoke test. |
| Production config guard | **Implemented** | `config.settings.prod` refuses to start with `DEBUG`, a weak or placeholder `SECRET_KEY`, empty or wildcard `ALLOWED_HOSTS`, `OTP_ECHO_DEV_CODE`, API docs enabled, insecure cookies, no HTTPS enforcement, or a default database password. Django `check --deploy` passes at `WARNING` level. |
| Security headers | **Implemented** | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, COOP `same-origin`. In production: HSTS for 1 year with subdomains, an HTTPS redirect (health probes exempt), and secure cookies. HSTS preload is an explicit per-domain opt-in. |
| CSRF | **Implemented (middleware)** | Protects any cookie-authenticated endpoint. Token-authenticated API calls are CSRF-exempt by design. |
| Password hashing | **Configured** | Argon2 primary, minimum length 10. Used from Phase 2. |
| Container hardening | **Implemented** | The runtime image is non-root (uid 10001), contains no dev tools, uses pinned base digests, and runs with `no-new-privileges`. All local ports bind to `127.0.0.1`. |
| Supply chain | **Implemented** | `uv.lock` with `--locked` installs. `pip-audit` runs in CI. GitHub Actions are pinned to commit SHAs. Images are pinned by digest. See also [SECURITY_INCIDENT.md](../SECURITY_INCIDENT.md). |
| Authentication | Phase 2 | Phone OTP and password. JWT access for 10 minutes plus rotating refresh tokens with reuse detection. Server-side logout (ADR-005). |
| Tenant isolation | Phase 2 | Membership-validated `X-School-Id` and school-scoped selectors. An isolation-matrix test runs over every endpoint (ADR-003, ADR-013). |
| Authorization and data scope | Phase 2 | Role → permission (`module.action`) → data scope → record (ADR-004) |
| Rate limiting | Phase 2 | Redis-backed throttles, tighter on OTP, login and refresh (ADR-016). The error format for it already exists. |
| Audit log | Phase 2 | Append-only, enforced by a database trigger (ADR-015) |
| Files | Phase 7 | A private bucket is already provisioned, and anonymous reads are denied (verified). Authorization happens before download, then a 60-second signed URL, a type and size allow-list, and a scan hook (ADR-009). |

## Logging

**Rendering.** Logs are structured JSON (or console format in development). Each line carries:

- `request_id`
- `task_id` / `task_name` for background jobs
- `user_id` once authentication exists

**Never logged:**

- Passwords, tokens, cookies, authorization headers, OTP codes or secrets. They are masked by key name anywhere in a log event, and by pattern inside text (`Bearer …`, JWTs, `password=…`, `scheme://user:pass@host`).
- Query strings. The access log records only the path, because query strings can carry phone numbers or other personal data.

**Personal data.** Names, phone numbers and emails should not be logged at all. Log IDs instead.

**Background tasks.** Task arguments should be IDs, not personal data. Celery logs task arguments on completion, and redaction only masks secret-shaped values.

## Reporting a vulnerability

Report privately to the repository owner. Do not open a public issue. See [SECURITY_INCIDENT.md](../SECURITY_INCIDENT.md) for how incidents are recorded.

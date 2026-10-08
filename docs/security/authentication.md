# Authentication

How a person proves who they are. What they may then do is in [authorization.md](authorization.md); which school they act in is in [multitenancy.md](multitenancy.md).

Decisions: ADR-005 (approach), ADR-019 (token design). Code: `backend/eduflow/identity/`.

## Identity model

| Concept | Table | Notes |
|---|---|---|
| User | `identity_user` | One person, across all schools. UUIDv7 primary key. |
| Auth session | `identity_auth_session` | One sign-in on one device: a refresh-token *family*. Revocable. |
| Refresh token | `identity_refresh_token` | SHA-256 of each token in a family, with rotation links. |
| OTP challenge | `identity_otp_challenge` | One code sent by SMS. HMACs only. See [otp.md](otp.md). |

The user model is a custom `AbstractBaseUser` (`AUTH_USER_MODEL = "identity.User"`). It reuses Django's password hashing (Argon2), password validators and `last_login`. It deliberately does **not** include Django's `PermissionsMixin` (groups and per-user permissions): EduFlow's RBAC replaces it, and a second, parallel permission system would be a source of bugs.

### Email and phone strategy

- **Sign-in identifiers:** email or mobile number. A user has at least one; a database `CHECK` enforces this.
- **Email** is stored lower-case (`CHECK email = lower(email)`) and is unique, so `Asha@School.in` and `asha@school.in` are the same account.
- **Phone** is stored as E.164 (`+919812345678`) and is unique. A 10-digit national number gets the default country code `PHONE_DEFAULT_COUNTRY_CODE` (91), as the client assumes. Numbering-plan validation is not done; `eduflow/identity/phone.py` can be swapped for a library without changing callers.
- **No usernames.** The client never had them, and they add an enumeration surface.

### Verified identifiers

`email_verified_at` and `phone_verified_at` record that the person proved control of an identifier (a successful OTP sign-in verifies the phone) or that platform staff entered it. Schools can attach existing accounts only through verified identifiers ([multitenancy.md](multitenancy.md#adding-members)). Email verification arrives with the invite flow.

### Active and inactive users

- `User.is_active = false` (platform action, `PATCH /platform/users/{id}`) revokes every session at once. Every request checks the session and the user, so the user's current access tokens stop working immediately, not when they expire.
- Membership deactivation (`PATCH /memberships/{id}`) removes access to **one school** only. See [multitenancy.md](multitenancy.md).
- Both are audited.

### Platform administrators

`User.is_platform_admin` marks EduFlow staff. It grants the `/api/v1/platform/*` endpoints and **nothing inside a school** (ADR-003). Create the first one with:

```bash
EDUFLOW_ADMIN_PASSWORD='…' python manage.py create_platform_admin --email ops@example.com --full-name "Ops"
```

The password comes from the environment or a prompt, never from a command-line argument.

### Forced password change

A user created by platform staff with a `temporary_password` (school onboarding) has `must_change_password = true`. Schools cannot set passwords on the accounts they create ([multitenancy.md](multitenancy.md#adding-members)). Until they call `POST /auth/password/change`, every endpoint except `/me`, `/auth/password/change`, `/auth/logout*` and `/auth/sessions*` returns `403 password_change_required`.

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /auth/password/login` | public, rate-limited | `{identifier, password, remember}` → session |
| `POST /auth/otp/request` | public, rate-limited | `{phone}` → `{challenge_id, expires_in, resend_in}` |
| `POST /auth/otp/verify` | public, rate-limited | `{challenge_id, code}` → session |
| `POST /auth/token/refresh` | refresh token, rate-limited | `{refresh}` → new `{access, refresh}` |
| `POST /auth/logout` | access token | Revoke this session (and optionally the session of a given `refresh`) |
| `POST /auth/logout-all` | access token | Revoke every session of the user |
| `POST /auth/password/change` | access token, rate-limited | Revokes every *other* session. `current_password` may be omitted only by an account that has no password yet. |
| `GET /auth/sessions`, `DELETE /auth/sessions/{id}` | access token | List and revoke your own sessions |
| `GET /me` | access token | User plus active memberships with roles (no `X-School-Id` needed) |

A successful sign-in returns:

```json
{
  "access": "<JWT, 10 minutes>",
  "refresh": "<opaque, single use>",
  "token_type": "Bearer",
  "access_expires_in": 600,
  "refresh_expires_at": "2026-10-09T10:00:00Z",
  "user": {"id": "…", "full_name": "…", "email": "…", "phone": "…", "language": "en",
           "is_platform_admin": false, "must_change_password": false},
  "memberships": [{"id": "…", "school": {"id": "…", "code": "…", "name": "…"},
                   "roles": [{"id": "…", "key": "teacher", "name": "Teacher", "title": "", "department": ""}]}]
}
```

Token mechanics (rotation, reuse detection, revocation) are in [token-lifecycle.md](token-lifecycle.md).

## Generic failures (no account enumeration)

| Situation | Response |
|---|---|
| Unknown identifier, wrong password, inactive account, account without a password | Same `401 invalid_credentials`, same message. Unknown identifiers and accounts without a password still run an Argon2 hash, so the timing matches a wrong password. |
| OTP for a number with or without an account | Same `200` body. No SMS is sent without an account. |
| Wrong, expired, used or locked OTP | Same `401 invalid_code` |
| Any bad, expired or revoked token | Same `401 not_authenticated`. No reason is given. |

The audit log records the real reason (for example `{"reason": "reused"}`) for investigators. Responses never do.

## Rate limits

Redis-backed sliding windows (`eduflow/identity/throttles.py`, ADR-016). A blocked request gets `429 rate_limited` with `retry_after_seconds` and a `Retry-After` header.

| Scope | Default | Key |
|---|---|---|
| `login_ip` | 20 / 5 min | client IP |
| `login_identifier` | 5 / 5 min | hashed, normalised email or phone, across all IPs |
| `refresh_ip` | 60 / min | client IP |
| `otp_request_ip` | 10 / hour | client IP |
| `otp_request_phone` | 3 / 10 min | hashed phone, plus a 30-second resend cooldown |
| `otp_verify_ip` | 30 / 10 min | client IP |
| `otp_verify_challenge` | 10 / 10 min | challenge ID (attempts are also capped at 5 per challenge) |
| `password_change_user` | 5 / hour | user |
| `school_lookup_ip` | 30 / min | client IP |
| `member_create_user` | 60 / hour | the admin adding members |

- Rates live in `settings.RATE_LIMITS`. They are read per request, so tests and environments can change them, and windows like `5/15m` are allowed.
- `RATE_LIMITS_ENABLED=false` turns them off for local experiments. Production settings refuse to start that way.
- Identifiers are hashed before becoming Redis keys. Numeric JSON values (`{"phone": 9812345678}`) are counted the same as strings.
- **Client IP:** `X-Forwarded-For` is trusted only for the number of proxies set in `TRUSTED_PROXY_COUNT` (default 0: the header is ignored). A client cannot dodge an IP limit by sending its own header. Set this to the real number of proxies in each deployment.

These limits slow down online guessing per account and per IP. They are **not** a defence against distributed attacks from many IPs; an edge rate limiter or WAF is still needed in production (ADR-016).

## Web sessions (prepared, not implemented)

The Expo apps keep tokens in SecureStore and send `Authorization: Bearer`. The browser console must not keep tokens in `localStorage` (CURRENT_STATE §12). Before any production web deployment, the planned approach is:

1. A web-only login variant sets the **refresh token** in an `httpOnly`, `Secure`, `SameSite=Lax` (or `Strict`) cookie scoped to `/api/v1/auth/`, instead of returning it in the body.
2. The access token stays in memory in the page, or is exchanged per request from the cookie.
3. Every cookie-authenticated, state-changing request requires a CSRF token. Django's `CsrfViewMiddleware` is already installed and returns the envelope `403 csrf_failed`.
4. The API stays **same-origin** with the web console behind the reverse proxy, so no CORS is needed. No CORS headers are sent today, so browsers refuse cross-origin calls. If a cross-origin client becomes necessary, add an explicit origin allow-list (for example `django-cors-headers`) with credentials limited to that list.

What already supports this:

- Session and CSRF cookies are `HttpOnly`/`SameSite=Lax` in all environments and `Secure` in production (enforced at startup).
- The refresh-token lifecycle (rotation, reuse detection, revocation) is independent of transport, so a cookie is only a different carrier for the same token.
- API responses are `Cache-Control: no-store`.

No second authentication mechanism (for example Django sessions) is added.

## Security headers

| Header | Value | Where |
|---|---|---|
| `Strict-Transport-Security` | 1 year, `includeSubDomains` (preload opt-in) | production |
| HTTPS redirect | on (health probes exempt), proxy header trusted | production |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` | every response except Swagger UI (development only) |
| `Cache-Control` | `no-store` | every `/api/` response except docs |
| `X-Content-Type-Options` | `nosniff` | all |
| `X-Frame-Options` | `DENY` | all |
| `Referrer-Policy` | `same-origin` | all |
| `Cross-Origin-Opener-Policy` / `Cross-Origin-Resource-Policy` | `same-origin` | all |

Development (`config.settings.dev`) keeps HTTP, insecure cookies and Swagger UI, so the local Docker stack works. Production (`config.settings.prod`) refuses to start with any of these unsafe, or with the console or test SMS provider, disabled rate limits, an empty `DATABASE_RLS_ROLE` or a short `JWT_SIGNING_KEY`.

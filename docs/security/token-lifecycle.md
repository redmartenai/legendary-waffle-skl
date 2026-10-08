# Token Lifecycle

Decision: ADR-019. Code: `backend/eduflow/identity/tokens.py`, `authentication.py`.

## The pieces

```
AuthSession (one sign-in on one device; its id is the "sid" claim)
 ├── RefreshToken #1   used_at=10:00  replaced_by=#2
 ├── RefreshToken #2   used_at=10:09  replaced_by=#3
 └── RefreshToken #3   used_at=null   ← the only one that works
```

| Token | Format | Lifetime | Stored server-side as |
|---|---|---|---|
| Access | JWT, HS256, signed by `djangorestframework-simplejwt` | 10 minutes (`ACCESS_TOKEN_MINUTES`) | nothing (stateless), but its `sid` must be a live session |
| Refresh | 64 random URL-safe characters (`secrets.token_urlsafe(48)`) | 1 day sliding, 30 days with "remember me", never past the session's absolute end (90 days) | SHA-256 hex only |

Access-token claims: `sub` (user ID), `sid` (session ID), `jti`, `iat`, `exp`, `iss=eduflow`, `aud=eduflow-api`, `token_type=access`. **No school, no role, no permission**: those are resolved from the database on every request, so they can never be stale or forged in a token.

The signing key is `JWT_SIGNING_KEY`, or by default `sha256("eduflow.jwt-signing:" + SECRET_KEY)`. A separate key lets every access token be invalidated without rotating `SECRET_KEY`.

## Lifecycle

```
login / otp verify ──► AuthSession + RefreshToken#1 ──► {access, refresh#1}
                                    │
refresh(#1) ──► #1.used_at = now, #2 created ──► {access', refresh#2}
                                    │
refresh(#1) again ──► REUSE: session revoked (reason reuse_detected), audited, 401
                       #2 and every access token with this sid stop working
                                    │
logout ──► session revoked (reason logout): access and refresh die immediately
```

### Every request

`AccessTokenAuthentication`:

1. simplejwt verifies the signature, `exp` (no leeway), `iss`, `aud` and `token_type`.
2. The `sid` session is loaded with its user in one query. It must be unrevoked, unexpired, and its user active, and `sub` must match the session's user.
3. Any failure gives a generic `401 not_authenticated`.

Step 2 is what makes logout and revocation immediate. It costs one indexed primary-key query per request.

### Refresh (rotation)

`POST /auth/token/refresh {refresh}`:

1. The token row is looked up **by hash** and locked (`SELECT … FOR UPDATE`) together with its session, so two concurrent refreshes cannot both succeed.
2. It is rejected if: unknown; session revoked; **already used** (reuse); expired; user inactive.
3. Otherwise it is marked `used_at`, linked to its replacement, and a new token is issued in the same session.

### Reuse detection

A rotated token being presented again means two parties hold it: the client and someone who copied it. Which one is legitimate is unknowable, so the **whole session** is revoked:

- every refresh token of the family stops working, including the newest;
- every access token carrying that `sid` stops working at once;
- `auth.refresh.reuse_detected` is audited with the user and session;
- other sessions of the same user (other devices) are not affected.

The revocation is committed **before** the 401 is returned, so an error response can never roll it back.

**Known trade-off:** a client that sends a refresh, loses the response (network drop) and retries with the old token is signed out. No grace window is given, because a grace window is exactly what an attacker racing the client would use. The Expo client already does single-flight refresh.

### Revocation and logout

| Action | Effect | Reason recorded |
|---|---|---|
| `POST /auth/logout` | This session | `logout` |
| `POST /auth/logout {refresh}` | Also the session of that refresh token, if it is the caller's | `logout` |
| `POST /auth/logout-all` | All the user's sessions | `logout_all` |
| `DELETE /auth/sessions/{id}` | One of the caller's own sessions (404 for anyone else's) | `session_revoked` |
| Password change | All sessions except the current one | `password_changed` |
| Platform deactivates the user | All sessions | `user_deactivated` |
| Reuse detected | That session | `reuse_detected` |

Logout needs a valid access token. A client whose access token has expired refreshes first, or simply discards its tokens; the session then expires on its own.

### Clean-up

`eduflow.identity.tasks.purge_expired_auth_records` (Celery beat, every 6 hours) deletes sessions, with their refresh tokens, and OTP challenges that ended more than `AUTH_RECORD_RETENTION_DAYS` (30) ago. The audit trail keeps the history.

## What is never stored or logged

- Raw refresh tokens: only SHA-256. The tokens carry 384 bits of entropy, so an unsalted hash cannot be brute-forced.
- Access tokens: not stored at all.
- Any token in logs or audit records: masked by key name (`access`, `refresh`, `token`, `authorization`) and by pattern (`Bearer …`, `eyJ…` JWTs). The smoke test checks the live container logs for the tokens it issued.

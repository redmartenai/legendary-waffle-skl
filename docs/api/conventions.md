# API Conventions

These rules apply to every endpoint. They are based on what the existing Expo client already expects (see [CURRENT_STATE.md §5](../CURRENT_STATE.md)) and on ADR-006.

## Paths and versions

- Every route is under `/api/v1/`.
- **No trailing slash.** For example, `/api/v1/classes/{id}/roster`. `APPEND_SLASH` is off, and a trailing-slash request returns 404 rather than a redirect.
- A breaking change requires a new major version (`/api/v2/...`) or a documented migration path. Additive changes, such as new optional fields or new endpoints, are not breaking.
- Unknown paths return the JSON error envelope (`404 not_found`) in every environment.

## Data formats

| Kind | Format |
|---|---|
| Field names | `snake_case` |
| IDs | UUID strings |
| Dates | `YYYY-MM-DD` |
| Time of day | School-local `HH:MM` (24-hour) |
| Timestamps | ISO-8601 in UTC, e.g. `2026-10-08T11:41:37Z` |
| Money | Decimal strings, e.g. `"1250.00"` |
| Request bodies | JSON. Multipart only for uploads. |

## Errors

Every error, from DRF, Django or a crash, uses one envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Some fields need attention.",
    "fields": {"name": ["Ensure this field has no more than 5 characters."]},
    "retry_after_seconds": 13,
    "request_id": "3a56600b3c38491e9a319f0cb3755697"
  }
}
```

- `fields` appears only for validation errors.
- `retry_after_seconds` appears only for `rate_limited`, and is mirrored in a `Retry-After` header.

| `code` | HTTP | Meaning |
|---|---|---|
| `validation_error` | 400 | Input failed validation. Per-field messages are in `fields`, and non-field messages are under `non_field_errors`. |
| `parse_error` | 400 | The body could not be parsed |
| `bad_request` | 400 | A malformed request was rejected before reaching a view |
| `not_authenticated` | 401 | Missing or invalid credentials |
| `permission_denied` | 403 | Authenticated, but not allowed |
| `csrf_failed` | 403 | CSRF check failed (cookie-authenticated requests only) |
| `not_found` | 404 | The resource does not exist **or is outside the caller's tenant or scope**. These are deliberately the same, so the API does not leak whether an object exists. |
| `method_not_allowed` | 405 | |
| `not_acceptable` / `unsupported_media_type` | 406 / 415 | |
| `rate_limited` | 429 | See `retry_after_seconds` |
| `server_error` | 500 | A bug. No internal detail is ever returned. Quote `request_id` to support. |

Rules:

- `message` is safe to show to users. It never contains exception text, SQL, stack traces or hostnames.
- `code` is stable. Clients branch on `code`, never on `message`.

## Request IDs

- Send `X-Request-ID` (8–128 characters from `[A-Za-z0-9._:-]`) to correlate a client action with server logs. Otherwise the server generates one.
- The response always echoes `X-Request-ID`, and every error body includes it as `request_id`.
- The same ID is attached to every log line of the request and to background jobs it enqueues.

## Authentication and tenancy (from Phase 2)

- Requests carry `Authorization: Bearer <access token>` and `X-School-Id: <school uuid>`.
- The server validates `X-School-Id` against the caller's active memberships on every request. Nothing about tenancy or role is trusted from the client (ADR-003/004).
- Until Phase 2, endpoints are **deny-by-default**: only endpoints that explicitly opt in (the health probes) are public.

## Pagination

- Canonical resource lists use cursor pagination.
- Experience endpoints keep the envelopes the client already uses: `{items, page, page_size, pages, total}`, or `{items, limit}`.

## Idempotency

- Retried writes send `Idempotency-Key: <uuid>`, or a `client_id` in the body where the client already does so (attendance, chat).
- The server returns the original result for a replay.

## Health endpoints

| Endpoint | Auth | Behaviour |
|---|---|---|
| `GET /api/v1/health/live` | none | `200 {"status":"ok"}` if the process is serving. No I/O. |
| `GET /api/v1/health/ready` | none | `200` if PostgreSQL, Redis (and storage, when enabled) respond. Otherwise `503` with the failing check named, e.g. `{"status":"unavailable","checks":{"database":"ok","cache":"failed","storage":"ok"}}`. |

## OpenAPI contract

- The schema is generated from code with drf-spectacular. The committed copy is [`openapi.yaml`](openapi.yaml).
- CI regenerates the schema and fails if the committed file differs, so API changes are always visible in review.
- Swagger UI (`/api/v1/docs`) and the live schema (`/api/v1/schema`) are served only when `API_DOCS_ENABLED=true`. They are on in dev and test. Production refuses to start with them enabled.

To regenerate after changing the API:

```bash
cd backend
DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py spectacular \
  --file ../docs/api/openapi.yaml --validate --fail-on-warn
```

Client types are generated from this file with `openapi-typescript` once the client is imported (Phase 6).

### Changelog

| Date | Change |
|---|---|
| 2026-10-08 | v1 created: `health/live`, `health/ready`, error envelope, request IDs |

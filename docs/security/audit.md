# Audit Trail

Decisions: ADR-015. Code: `backend/eduflow/audit/`.

## Model

`audit_event` (append-only):

| Field | Content |
|---|---|
| `id` | UUIDv7 (time-ordered; the API sorts by it) |
| `occurred_at` | UTC timestamp |
| `school_id` | the tenant, or `NULL` for sign-in and platform events |
| `actor_id` | the user who acted (`NULL` if unknown, e.g. a failed sign-in for an unknown account) |
| `action` | stable dotted name, below |
| `outcome` | `success`, `failure` or `denied` (database `CHECK`) |
| `target_type`, `target_id` | what was acted on |
| `request_id`, `ip`, `user_agent` | from the request context; `ip` honours `TRUSTED_PROXY_COUNT` |
| `metadata` | small JSON with details; redacted and limited to 4 KB |

References are plain UUIDs, not foreign keys: the trail outlives the rows it describes, and deleting a user never rewrites history.

## How events are written

`eduflow.audit.services.record(action, outcome=…, target_type=…, target_id=…, metadata=…)`:

- Called **explicitly** by services, in the same transaction as the change, so a rolled-back change leaves no event and a committed change always has one. There are no signal-based implicit audits.
- Failure events (failed sign-in, refresh-token reuse) are committed **before** the error response is sent.
- `request_id`, `ip`, `user_agent`, and by default `actor_id` and `school_id`, come from the request context.
- `metadata` passes through the same redactor as the logs. Keys like `password`, `token`, `refresh`, `access`, `otp` and `code` become `[REDACTED]`, and `Bearer …` or JWT-shaped strings are masked. Oversized metadata is replaced by `{"truncated": true}`.

## Events

| Action | Outcome(s) | Notes |
|---|---|---|
| `auth.login` | success, failure | `metadata.method` = `password` / `otp` |
| `auth.logout`, `auth.logout_all`, `auth.session.revoked` | success | |
| `auth.refresh` | success, failure | failure `metadata.reason`: `unknown`, `revoked`, `expired`, `inactive` |
| `auth.refresh.reuse_detected` | failure | the whole session is revoked |
| `auth.otp.requested` | success | `metadata.account_found`, `metadata.delivered` (internal only) |
| `auth.otp.verified` | success, failure | failure `metadata.reason`: `wrong_code`, `expired`, `used`, `locked`, `unknown` |
| `identity.password.change` | success, failure | `metadata.other_sessions_revoked` |
| `identity.user.created` | success | account created while adding a member; `metadata.by_platform` |
| `identity.phone.verified` | success | first successful OTP sign-in to that phone |
| `identity.user.activated` / `.deactivated` | success | platform action; deactivation revokes all sessions |
| `identity.platform_admin.granted` | success | management command |
| `tenancy.school.created` / `.updated` | success | |
| `tenancy.membership.created` / `.activated` / `.deactivated` | success | |
| `tenancy.access_denied` | denied | `X-School-Id` for a school the caller cannot access; target = attempted school |
| `authz.role.created` / `.updated` / `.deleted` | success | update records added, removed and re-scoped permissions |
| `authz.role.assigned` / `.unassigned` | success | target = membership, `metadata.role` |

Plain permission denials (`403 permission_denied`) are logged (`authz_denied`) but not audited, to keep the trail meaningful. Add an audit event where a denial is security-relevant for a specific feature.

## Protection

| Layer | Effect |
|---|---|
| Trigger `audit_event_append_only` | `UPDATE` and `DELETE` raise an error for **every** role, the owner included |
| Grants | `eduflow_app` has only `SELECT, INSERT` |
| RLS | a school reads only its own events; school-less events are readable only through the platform bypass; inserts for another school are refused |
| Redaction | secrets never reach `metadata` |

`TRUNCATE` is not blocked. It is an operator action (retention, test teardown) and is never issued by the application.

## Reading

`GET /api/v1/audit-events` (permission `audit.read`, default: school admin and principal). It returns the current school's events, newest first, cursor-paginated (`?cursor=`, `?page_size=` up to 200), and can be filtered by `?action=` and `?actor_id=`. Platform-level reading (school-less events) is a later platform endpoint.

## Retention

Not automated yet. Indexes lead with `(school_id, occurred_at)`, `(actor_id, occurred_at)`, `(action, occurred_at)` and `(school_id, target_type, target_id)`. Monthly partitioning comes when volume requires it (ADR-015).

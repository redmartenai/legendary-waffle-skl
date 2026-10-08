# PostgreSQL Row-Level Security

RLS is the **second** tenant boundary. The application already filters every query by school ([multitenancy.md](multitenancy.md)); RLS makes PostgreSQL enforce the same boundary, so a query that forgets its filter still cannot read or write another school's rows.

Decision: ADR-018. Code: `backend/eduflow/core/db_context.py`, migrations `core.0001`, `tenancy.0002`, `authz.0002`, `audit.0002`.

**RLS does not replace application authorization.** It does not know about roles, permissions or data scopes, only schools.

## How PostgreSQL knows the tenant

```
request / task start ─► set_config('role', 'eduflow_app')            (NOLOGIN, NOBYPASSRLS)
                        set_config('eduflow.school_id', '')          (empty: fail closed)
                        set_config('eduflow.user_id',  '')
                        set_config('eduflow.rls_bypass', '')
after authentication ─► eduflow.user_id   = <verified user>
after tenant check   ─► eduflow.school_id = <verified membership's school>
request / task end   ─► every setting cleared, RESET ROLE
```

- **Role.** The app connects with its normal login (the database owner locally). Every request and task switches to `eduflow_app`, a `NOLOGIN NOBYPASSRLS` role created by migration `core.0001`. Because `eduflow_app` does not own the tables, RLS applies to it. This works even when the login role is a superuser, as in the local Docker image.
- **Settings.** The policies read the three settings through SQL helper functions (`eduflow_current_school()`, `eduflow_current_user()`, `eduflow_rls_bypass()`). Empty means `NULL`, and `school_id = NULL` matches nothing.
- **Who writes them.** Only `eduflow.core.db_context`, and only with server-verified values: the user from a valid session, the school from an active membership. The `X-School-Id` header never reaches the database unverified (tested by `test_tenant_header_never_reaches_the_database_unverified`).
- **Lifetime.** Requests run in autocommit (one transaction per query), so the settings are session-level and are reset explicitly at the end of every request and task (`DatabaseContextMiddleware`, Celery `task_postrun`). If Django reconnects mid-request, the `connection_created` hook re-applies the current context. If resetting fails, the connection is closed. Either way no context survives into the next request on a persistent connection.
- **Health probes** are exempt: `live` does no I/O, and `ready` runs `SELECT 1` as the login role.
- **Migrations, management commands and test fixtures** do not engage a context, so they run as the owner and RLS does not apply to them. That is intended: they are operator actions, not request handling.

## Policies

| Table | Policy | Rows allowed |
|---|---|---|
| `tenancy_membership` | `tenant_rw` (all commands) | `school_id = current school`, or bypass |
| | `own_read` (SELECT only) | `user_id = current user` (for `/me` and tenant resolution) |
| `authz_role` | `tenant_rw` | current school, or bypass |
| | `member_read` (SELECT) | roles attached to the current user's own memberships |
| `authz_role_permission` | `tenant_rw` | current school, or bypass |
| `authz_membership_role` | `tenant_rw` | current school, or bypass |
| | `member_read` (SELECT) | assignments on the current user's own memberships |
| `audit_event` | `tenant_read` (SELECT) | current school, or bypass |
| | `append` (INSERT) | current school, school-less events, or bypass. No UPDATE or DELETE policy at all. |

Read-only policies grant no writes: with only a user context (no school), writes to every tenant table are refused. Moving a row to another school (`UPDATE … SET school_id`) fails the `WITH CHECK`.

**Not under RLS:** `identity_*` (user-owned, not school-owned), `tenancy_school` (the registry; public lookup), `authz_permission` (global catalogue). RLS is enabled, not forced, so the table owner (the migration role) is unaffected.

## The bypass

`eduflow.rls_bypass = on` is set in exactly two ways, and each logs `rls_bypass` with a reason:

- `PlatformAPIView.initial()`, for the rest of the request, **after** verifying `is_platform_admin`. Platform administration crosses tenants by definition.
- `db_context.system_context(reason)`, for a block. It is the tool for future platform-level jobs and is unused by Phase 2 request code.

Ordinary tenant code never needs it. A new use should be reviewed as a security change.

## Background jobs

Every Celery task, tenant-aware or not, starts with the RLS role and an **empty** context (`core.celery_context`, `task_prerun`), so a task that touches school data without a tenant sees no rows and cannot write.

A task that works on one school's data uses `TenantTask`:

```python
from celery import shared_task
from eduflow.tenancy.tasks import TenantTask

@shared_task(base=TenantTask, name="eduflow.attendance.tasks.send_absence_alerts")
def send_absence_alerts(*, school_id: str, date: str) -> None:
    ...  # eduflow.school_id = school_id here; RLS limits every query to it

send_absence_alerts.delay(school_id=str(actor.school.pk), date="2026-10-08")
```

| Check before the body runs | Failure |
|---|---|
| `school_id` keyword argument present and a UUID | `MissingTenantContext` |
| If enqueued during a tenant request: the publisher's school (`school_id` message header, added automatically) equals `school_id` | `TenantContextMismatch`: a request in school A cannot start work in school B |
| The school exists and is active | `InactiveTenant` |

- These failures are permanent. The task fails without retry (`dont_autoretry_for`) and the error is logged with the task ID and request ID.
- The context is the school only, never the enqueuing user, in the worker and in eager mode alike.
- The previous context is restored afterwards, so an inline (eager) task never changes the calling request's context.
- Platform-wide maintenance tasks (for example `identity.purge_expired_auth_records`) touch only non-tenant tables. A future platform task that must cross schools uses `system_context(reason)` explicitly.

## Managing policies in migrations

- Policies, roles, grants and triggers live in `RunSQL` migrations with a full reverse, so `migrate <app> zero` works (CI rolls every Phase 2 app back and forward again).
- `core.0001` creates `eduflow_app` if missing, makes the migration role a member, creates the helper functions and grants DML on all tables, **including future ones** (`ALTER DEFAULT PRIVILEGES`).
- **A new school-owned table** gets, in its own migration (depending on `core.0001`):

  ```sql
  ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
  CREATE POLICY tenant_rw ON <t>
      USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())
      WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());
  ```

  and a test in its module that an unfiltered query under another school's context sees nothing.
- **Production.** Roles are cluster-wide. If the migration role may not create roles, the platform team creates `eduflow_app` (`NOLOGIN NOBYPASSRLS`) and grants it to the migration role beforehand; the migration then skips creation. The application may also connect with a dedicated non-owner login and set `DATABASE_RLS_ROLE=""`; production refuses to start with it empty unless that is changed deliberately in code.
- The role name is fixed (`eduflow_app`) because migrations reference it.

## Limits (be honest about what RLS does not do)

- **SQL injection** running as the app role could call `set_config` itself. RLS protects against *missing filters*, not against arbitrary SQL. The application never builds SQL from strings (database conventions).
- **The owner and superusers bypass RLS.** Operator shells and migrations are trusted.
- RLS knows schools, not scopes. A teacher reading another section's students in the same school is stopped by the application's data scopes, not by RLS.
- Policies with sub-selects (`member_read`) cost a little on `/me`. They are bounded by the user's own memberships.

## Tests

`tenancy/tests/test_rls.py`: no context means no rows; the tenant sees only its school, even by primary key; the user context is read-only; inserts into, updates of and deletes from another school are refused; moving rows across schools is refused; the bypass is scoped; composite FKs; audit isolation and append-only; the context is released after a request and after an error; the header never reaches the database unverified; reconnect re-applies the context. `tenancy/tests/test_tasks.py` covers background jobs. The smoke test checks RLS on the real Docker database.

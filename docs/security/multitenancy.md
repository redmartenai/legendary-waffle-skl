# Multitenancy

The **school is the tenant**. One database and schema; every school-owned row carries a non-null `school_id` (ADR-003). Isolation is enforced twice: in the application (primary), and by PostgreSQL Row-Level Security (defence in depth, [rls.md](rls.md)).

Code: `backend/eduflow/tenancy/`.

## Concepts

| Concept | Table | Owner |
|---|---|---|
| Platform | (no table) — EduFlow itself; `User.is_platform_admin` | platform |
| School (tenant) | `tenancy_school` | platform registry: code, name, active flag, `rbac_version` |
| Membership | `tenancy_membership` | school: a user's place in one school, active or not |
| Role assignment | `authz_membership_role` | school: roles of a membership |
| User | `identity_user` | platform: one person across schools |

A user can belong to any number of schools, with different roles in each. The client keeps an "active school" and sends it as `X-School-Id`.

## Tenant resolution

```
Authorization: Bearer …   → user (live session)
X-School-Id: <uuid>        → a *request* to act in that school
            │
            ▼
Membership(user, school, is_active) AND school.is_active ?
    no  → 403 tenant_forbidden (same for malformed, unknown, inactive, not a member); audited
    yes → request actor = (user, school, membership, grants)
          database context eduflow.school_id = school   (RLS)
```

- **The header is never trusted on its own.** It is only used after the membership check, and only the verified school ID reaches the database context.
- **The response does not leak** whether a school exists: every failure is the same `403`. Denied attempts are audited (`tenancy.access_denied`, with the attempted school as target).
- **Platform administrators get no implicit access.** They use `/platform/*`, which is separately guarded and audited.
- **Tokens carry no school.** Switching schools is a header change; revoking a membership takes effect on the next request.

## Queryset discipline

The rules, in order of preference:

1. **List and detail through a `ScopedResource`** (`members.queryset(actor, "user.read")`, `members.get(...)`). It filters by school first, then by data scope. See [authorization.md](authorization.md#data-scopes).
2. Otherwise, start every query from `Model.objects.for_school(actor.school)` (`TenantQuerySet`). Every school-owned model uses this manager.
3. **Never** `Model.objects.get(pk=…)` on school-owned data from a request. A forgotten filter is still caught by RLS, but it is a bug.
4. School-owned models derive from `tenancy.models.TenantModel` (non-null, indexed `school` FK).
5. A child row whose parent is school-owned also stores `school_id`, and composite foreign keys keep the two consistent (for example `authz_membership_role(membership_id, school_id) → tenancy_membership(id, school_id)`).

## Tests that keep it true

- `tenancy/tests/test_isolation.py` — the **isolation matrix**. A school admin of school A sends every tenant endpoint school B's object IDs, for read, update, delete and role assignment, and must get exactly the same response as for a random non-existent ID.
- **The matrix is enforced**: `test_matrix_covers_every_tenant_endpoint_with_an_object_id` walks the URL configuration and fails if a tenant endpoint with an ID in its path is not in the matrix. A new endpoint cannot ship without isolation coverage.
- `test_every_tenant_handler_declares_a_permission` fails if any handler lacks a permission.
- `tenancy/tests/test_rls.py` proves the database refuses cross-tenant reads and writes, even for unfiltered queries.
- `tenancy/tests/test_tasks.py` covers background jobs.

## Background jobs

See [rls.md § Background jobs](rls.md#background-jobs). In short: a task working on one school derives from `TenantTask`, takes `school_id`, and runs with that school's database context. Any other task sees no tenant rows.

## What is deliberately not tenant-owned

| Data | Why |
|---|---|
| Users, auth sessions, refresh tokens, OTP challenges | One person, many schools. Access is by user, not by school. |
| `School` itself | The tenant registry. Public lookup by code must work without a tenant. |
| Permission catalogue | Global vocabulary |
| Audit events with no school (sign-ins, platform actions) | Readable only by platform tooling (RLS) |

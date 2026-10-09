# Authorization

**User → Membership → Role → Permission (+ data scope) → Record.** The server decides everything; hiding a button in the client is not authorization.

Decisions: ADR-004, ADR-020. Code: `backend/eduflow/authz/`.

## The order of checks

Every protected request passes these steps in order. The first failure ends it.

| # | Check | Where | Failure |
|---|---|---|---|
| 1 | Valid access token on a live session, active user | `identity.authentication.AccessTokenAuthentication` | `401 not_authenticated` |
| 2 | No pending forced password change | `authz.api.base.NoPendingPasswordChange` | `403 password_change_required` |
| 3 | Active membership in the `X-School-Id` school, school active | `tenancy.context.resolve_actor` | `400 tenant_required` / `403 tenant_forbidden` |
| 4 | The method's permission is granted by one of the member's roles | `TenantAPIView.check_permissions` | `403 permission_denied` |
| 5 | The record is within the permission's data scope (and the school) | the view's `ScopedResource` | `404 not_found` |
| 6 | Business rules (e.g. "cannot remove the last admin") | services | `403` / `409` |

Platform endpoints replace steps 3–5 with "`is_platform_admin`" (`PlatformAPIView`).

## Base views

```python
class RoleDetailView(TenantAPIView):
    required_permissions = {"GET": "role.read", "PATCH": "role.update", "DELETE": "role.delete"}

    def get(self, request, role_id):
        role = roles.get(self.actor, "role.read", role_id)   # tenant + scope, 404 otherwise
        ...
```

- **Deny by default.** A handler whose method is missing from `required_permissions` is refused at runtime, and `test_every_tenant_handler_declares_a_permission` fails CI.
- `self.actor` is an `Actor(user, school, membership, grants)` built from server-side state only.
- `HEAD` uses the `GET` permission. `OPTIONS` is not declared, so it is refused.

## Permissions

Codenames are `<resource>.<action>`. The catalogue is `authz/catalog.py::PERMISSIONS` (34 from Phase 2, 53 since Phase 3):

| Resource | Actions |
|---|---|
| `school` | `read`, `update` |
| `user` (members of the school) | `read`, `create`, `update`, `disable` |
| `role` | `read`, `create`, `update`, `delete`, `assign` |
| `permission`, `audit` | `read` |
| `student` | `read`, `create`, `update` |
| `staff` | `read`, `update` |
| `attendance`, `assessment` | `read`, `create`, `update` |
| `timetable`, `report` | `read` |
| `fee` | `read`, `update` |
| `library`, `transport`, `hostel` | `read`, `manage` |

Domain permissions such as `student.*` and `attendance.*` exist now so that roles are complete from day one. Their endpoints arrive with their modules.

### Phase 3 domain

| Resource | Actions | Default grants |
|---|---|---|
| `campus`, `academic_year`, `department`, `grade`, `subject` | `read`, `manage` | `read`: every role, school-wide (no personal data). `manage`: school admin, principal. |
| `section` | `read`, `manage` | `read`: teacher `section`, parent `child`, student `self`, office roles `school` |
| `staff` | `read`, `create`, `update` | HR, school admin and principal `school`; teacher and staff `self` |
| `student` | `read`, `create`, `update` | Phase 2 defaults |
| `guardian` | `read`, `manage` | teacher `section`, parent and student `self`, accountant `school` |
| `enrollment` | `read`, `manage` | teacher `section`, parent `child`, student `self`, office roles `school` |
| `teacher_assignment` | `read`, `manage` | teacher `self` + `section`, parent `child`, student `self` |

Phase 3 writes need the write permission with `school` scope, and linking an account to a profile happens only by accepting an invitation (ADR-025) ([phase-3.md](../architecture/phase-3.md#authorization-and-data-scopes)). The scope rules for these resources are in `people/policies.py` (see [phase-3.md](../architecture/phase-3.md#authorization-and-data-scopes)).

### Phase 4 invitations

| Permission | Default grants | Notes |
|---|---|---|
| `invitation.read` | school admin, principal (`school`) | Masked addresses only |
| `invitation.manage` | school admin, principal (`school`) | Create, resend and revoke also need school-wide `user.create`, `user.update` and the target permission (`staff.create`, `student.update` or `guardian.manage`) |

Invitation roles follow the escalation guard below: a principal cannot invite with `school_admin`, because it lacks `role.delete`. The inviter's authority is re-checked at resend and at acceptance ([invitations.md](invitations.md)). No other role holds `invitation.*`, and platform administrators have no implicit school access.

### Phase 5 academic engine

| Permission | Default grants | Notes |
|---|---|---|
| `term.read`, `room.read` | every role, school-wide | School structure, no personal data |
| `term.manage`, `room.manage`, `timetable.manage` | school admin, principal | Writes need `school` scope |
| `timetable.read` (Phase 2) | teacher and staff `school`, parent `child`, student `self` | Covers timetables, periods, slots and schedules |
| `lesson.read` | teacher `self` + `section`, parent `child`, student `self` | |
| `lesson.manage` | teacher `self`; school admin and principal `school` | **The first write with a narrower scope than the school:** a teacher records lessons of their own classes. The service re-checks that the caller is the slot's teacher with an active assignment (ADR-027). |

Schedules are authorized by their subject: a teacher's, student's or section's schedule needs that record to be visible under both its own read permission and `timetable.read` (ADR-027).

### Adding a permission

1. Add `"<resource>.<action>": "description"` to `PERMISSIONS`.
2. Grant it in `SYSTEM_ROLES` to the roles that should have it by default, with scopes.
3. Protect the endpoint: `required_permissions = {"POST": "<resource>.<action>"}`.
4. Deploy, and run `python manage.py sync_rbac` after `migrate`. The table `authz_permission` is synced automatically after every `migrate`; `sync_rbac` also adds the new default grants to every existing school's system roles. It never removes grants or changes scopes. It does re-add a default grant a school removed from a system role, so schools that need a narrower role should use a custom role.
5. Removing a codename from the catalogue marks it `is_deprecated` (kept for history) and it stops granting anything.

## Roles

Roles belong to a school. Every new school gets the 12 system roles, seeded from `SYSTEM_ROLES`:

| Key | Default reach |
|---|---|
| `school_admin` | Every permission, whole school. **Locked**: cannot be edited or deleted. |
| `principal` | Everything except `role.delete`, whole school |
| `teacher` | Students, attendance and assessment for their **sections** (+ individually **assigned** students) |
| `parent` | Their **children's** students, attendance, assessment, timetable, reports, fees, transport |
| `student` | **Self** |
| `accountant` | Fees (read and update), students, reports |
| `hr_manager` | Members (read and update), staff records, reports |
| `librarian`, `transport_manager`, `hostel_manager` | Their area, plus students to look up borrowers and riders |
| `staff` | School profile, timetable, own membership |
| `driver` | **Assigned** riders and routes |

**Platform Admin is not a school role.** It is `User.is_platform_admin` and grants only `/platform/*` (ADR-003).

- **Custom roles** (`POST /roles`, optionally `based_on` a role to copy its grants) get a server-generated key.
- A member can hold several roles (`MembershipRole`, with optional `title` and `department`). Their effective grant for each permission is the **union** of the scopes from all their roles.
- Role assignment, removal and editing are audited, and bump `School.rbac_version`. Cached grants are keyed by that version, so changes take effect on the next request.

### Escalation guards

| Rule | Prevents |
|---|---|
| Granting a permission to a role (create or update) requires holding that permission with `school` scope | A principal giving `role.delete` to staff |
| Giving someone a role requires holding *all* of that role's permissions with `school` scope | A principal making anyone (including themselves) `school_admin` |
| `school_admin` is locked; system roles cannot be deleted | A school locking itself out |
| Activating or deactivating a member requires holding every permission of that member's roles | A principal re-activating a former school admin, or removing co-admins |
| The last active school admin cannot lose the role or be deactivated; the check locks the school row | The same, including two admins demoting each other at once |
| Nobody can deactivate their own membership; platform admins cannot deactivate themselves | Accidental lock-out |
| A role from another school cannot be assigned: service check, plus composite foreign keys in the database | Cross-tenant role injection |

## Data scopes

A grant says *how much* of a resource a permission covers:

| Scope | Meaning | Who has it by default |
|---|---|---|
| `platform` | Everything (platform tooling only; cannot be granted to school roles) | — |
| `school` | Every row of the school | admins, principal, office roles |
| `campus`, `academic_year`, `department` | Rows of the actor's campus / year / department | (rules arrive with the academic modules; `department` uses the role assignment's `department`) |
| `section` | Rows of the sections the actor teaches | teacher |
| `assigned` | Rows individually assigned to the actor | teacher (mentees), driver (riders) |
| `child` | Rows of the actor's children | parent |
| `self` | The actor's own record | student, everyone for `user.read` |
| `own` | Rows the actor created | (for custom roles) |

### How a domain module uses scopes

Each module declares a `ScopedResource` and one rule per scope it supports (see `authz/tests/scopeapp/resources.py`, which the real modules will mirror):

```python
students = ScopedResource("student", Student)

@students.rule(DataScope.SECTION)
def _taught(actor):  return Q(section__teacher_assignments__membership=actor.membership)

@students.rule(DataScope.CHILD)
def _children(actor): return Q(guardians__membership=actor.membership)

students.queryset(actor, "student.read")      # list
students.get(actor, "student.read", pk)       # detail: 404 when outside school or scope
students.can(actor, "attendance.update", obj) # object-level checks in services
```

Resolution is deny-by-default:

- The queryset is **always** filtered to the actor's school first, even with `school` scope.
- `school` scope adds nothing more. Every other scope adds its rule, OR-ed together.
- A scope with **no registered rule grants nothing**, so a role granted `campus` scope on a resource without a campus rule sees no rows. It fails closed.
- No grant at all is `403`. A record outside the scope is `404`, the same as a record that does not exist.
- Rules that follow to-many relations are de-duplicated (filtered by primary key).

## Error semantics

| Situation | Response | Why |
|---|---|---|
| Not signed in | `401` | |
| Signed in, but no access to the school | `403 tenant_forbidden` | identical for unknown, inactive and foreign schools |
| In the school, lacking the permission | `403 permission_denied` | the permission's existence is not secret |
| Has the permission, record out of scope or in another school | `404 not_found` | does not reveal that the record exists |

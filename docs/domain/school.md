# School, Campus, Department

## School

The tenant (`tenancy.School`, Phase 2), extended in Phase 3 with its profile.

| Field | Notes |
|---|---|
| `code` | Public lookup code. Platform-controlled; unique. |
| `name`, `legal_name`, `short_name` | |
| `email`, `phone`, `website` | |
| `address_line1/2`, `city`, `state`, `postal_code`, `country` | `country` is ISO 3166-1 alpha-2 (default `IN`) |
| `timezone` | IANA name (default `Asia/Kolkata`), validated |
| `settings` | School preferences (JSON). Never credentials or personal data. |
| `is_active` | Platform-controlled. An inactive school is unreachable for its members. |

- `PATCH /school` (permission `school.update`) edits the profile. `code` and `is_active` are rejected there.
- `PATCH /platform/schools/{id}` (platform admins) edits the profile and `is_active`.
- Platform admins have no other access to school data.
- Audited: `tenancy.school.updated`, with the changed field names.

## Campus

`name`, `code`, address fields, `status` (`active`/`archived`). Code and name (case-insensitive) are unique per school. A section and a staff profile may name a campus; the `campus` data scope uses the staff profile's campus. Permissions: `campus.read`, `campus.manage`.

## Department

`name`, `code`, `description`, `status`. Code and name are unique per school. Subjects and staff profiles may name a department; the `department` data scope uses the staff profile's department. Phase 3 includes no HR functionality. Permissions: `department.read`, `department.manage`.

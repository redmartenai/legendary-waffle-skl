# Staff and Teachers

A staff member is a **membership** (sign-in and school access, Phase 2) plus a **`StaffProfile`** (the school's record).

| Field | Notes |
|---|---|
| `membership` | Required. An active membership of this school, one profile per membership (composite FK). |
| `employee_id` | Unique per school |
| `staff_type` | `teaching` (teachers) or `non_teaching` |
| `designation`, `department`, `campus`, `joining_date` | Department and campus must be in the school |
| `status` | `active`, `on_leave`, `left`. `left` (or a change to `non_teaching`) ends the person's active teacher assignments; `on_leave` keeps them. |

- **A teacher is the `teacher` role plus a `teaching` profile.** Teacher assignments refer to the profile, never to the role.
- Teachers are listed with `/staff?staff_type=teaching`.
- Responses show the person's `full_name` and `membership_id`, but not their email, phone or any credential.
- Permissions:
  - `staff.read`: school-wide for school admin, principal and HR; `self` for teachers and staff.
  - `staff.create`: school admin, principal, HR.
  - `staff.update`: same.
- Data scopes: `self`, `department`, `campus`.
- To onboard a teacher: add the member (`POST /memberships`, Phase 2) with the `teacher` role, then create the staff profile.

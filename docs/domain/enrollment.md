# Enrollment

An enrollment places a **student** in a **section** for an **academic year**.

| Field | Notes |
|---|---|
| `student`, `section` | Same school |
| `academic_year`, `grade` | **Copied from the section**: the client cannot send them (unknown fields are rejected). A composite FK to `section (id, academic_year_id, grade_id, school_id)` makes a mismatch impossible in the database. |
| `roll_number` | Optional; unique among active enrollments of the section |
| `status` | `active` → `completed` / `withdrawn` / `transferred` (terminal) |
| `start_date`, `end_date` | `end_date` is set exactly when the status is not `active`, and is never before `start_date` |
| `transferred_to`, `status_reason` | Set by a transfer or an ending |

## Rules

- The student must be `active`; the section `active`, in a year that is not closed, and not full.
- **One active enrollment per student per academic year** (partial unique index). Ended enrollments stay as history, so a withdrawn student can be enrolled again.
- Dates fall within the academic year.
- **Closing the academic year** completes every active enrollment of that year. Changing a student's status away from `active` ends their active enrollment.
- Enrollments of a closed year are read-only.

## Lifecycle actions

| Action | Effect |
|---|---|
| `POST /enrollments` | New active enrollment (`start_date` defaults to today) |
| `PATCH /enrollments/{id}` | Only `roll_number`, only while active |
| `POST /enrollments/{id}/end` `{status: completed\|withdrawn, end_date?, reason?}` | Ends it; final |
| `POST /enrollments/{id}/transfer` `{section_id, date?, reason?, roll_number?}` | Marks it `transferred` and creates the new active enrollment in another section **of the same year**; returns the new one |

Promotion to the next academic year is not part of Phase 3.

- Filters: `student_id`, `section_id`, `academic_year_id`, `grade_id`, `status`.
- Data scopes: `section`, `child`, `self`.
- Permissions: `enrollment.read`, `enrollment.manage`.
- Audit: `people.enrollment.created|updated|completed|withdrawn|transferred`.

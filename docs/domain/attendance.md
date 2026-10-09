# Attendance

A daily **register** per section (ADR-008), one **record** per student in it, and **corrections** for changes after the register locks (ADR-028). Design and open decisions: [architecture/attendance.md](../architecture/attendance.md).

## Register (`AttendanceSession`)

| Field | Notes |
|---|---|
| `section`, `date` | One register per section and date. The date is within the section's academic year and not in the future. |
| `taken_by`, `submitted_at` | The last submission |
| `locked_at` | The end of the date in the school's time zone. Until then the register can be submitted again; afterwards it changes only through corrections. |
| `client_id` | The idempotency key of the last accepted submission |

**Taking a register:** `POST /classes/{id}/attendance` with `{entries: [{student_id, status, note?}], client_id?, date?}`.
- Exceptions only: every student on the roster who is not listed is `present`. Each submission replaces the register.
- Every `student_id` must be on the roster that day, at most once (`400` otherwise).
- The same `client_id` (or `Idempotency-Key` header) on a retry returns the stored register unchanged (`replayed: true`).

**Roster for a date:** the students enrolled in the section that day.
- A student transferred on that day belongs to the new section.
- A withdrawn or completed student stays up to and including their last day.
- When a register is retaken, students no longer on the roster leave it.

## Record (`AttendanceRecord`)

| Field | Notes |
|---|---|
| `status` | `present`, `absent`, `late`, `half_day`, `excused` (database `CHECK`) |
| `note` | Optional, up to 200 characters |
| `student`, `enrollment` | The enrollment must be the student's, in the register's section (composite foreign key) |

## Correction (`AttendanceCorrection`)

| Field | Notes |
|---|---|
| `record`, `old_status`, `new_status`, `reason` | Only for a **locked** register; the new status must differ; a reason is required |
| `status` | `pending` → `approved` / `declined` (final). At most one pending per record. |
| `requested_by`, `decided_by`, `decided_at`, `decision_note` | The approver is never the requester |

- Approving applies the new status, provided the record still has the old one (`409` otherwise).
- The approved and declined rows are the record's change history (`GET /attendance/corrections?record_id=`).

## Month view

`GET /students/{id}/attendance?month=YYYY-MM` (default: the current month) returns `{month, days: [{date, status}], summary}`.

- **A day's status:**
  - the mark in the section the student belonged to that day;
  - `not_marked` when no register has it;
  - `upcoming` after today;
  - `null` when the student was not enrolled.
- **`summary`:** per-status counts, `not_marked` and `marked_days`.

## Reference

- Filters:
  - registers: `section_id`, `academic_year_id`, `date`, `date_from`, `date_to`
  - records: `student_id`, `section_id`, `session_id`, `status`, `date`, `date_from`, `date_to`
  - corrections: `status`, `record_id`, `student_id`, `section_id`
- Data scopes (`attendance.read`):
  - Registers and corrections: `section` (an active assignment), `campus`.
  - Records: `section`, `child`, `self`, `campus`.
- Permissions:
  - `attendance.read`
  - `attendance.create`: take a register, read its roster
  - `attendance.update`: request a correction
  - `attendance.approve`: decide a correction (school-wide)
- Audit: `attendance.register.submitted`, `attendance.correction.requested|approved|declined`, `attendance.record.corrected`.

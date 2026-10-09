# Attendance (Phase 6, backend part)

This is the attendance domain of Phase 6 of the [implementation plan](../IMPLEMENTATION_PLAN.md): daily section registers, the records in them, and corrections of locked registers.

The rest of Phase 6 is **not** part of this work:
- the remaining experience endpoints (`/staff/classes`, `/teacher/classes`, `/parent/children`);
- student leave requests;
- importing the client and the end-to-end MVP slice.

Decisions: ADR-008 (the register model), ADR-028 (rules the sources left open), ADR-027 (narrow-scope writes; subject-based reads). Entity reference: [domain/attendance.md](../domain/attendance.md).

## Sources

The implementation follows only these documents:

| Source | What it fixes |
|---|---|
| ADR-008 | The model: `AttendanceSession` (section, date, period NULL, taken_by, submitted_at, locked_at, client_id) and `AttendanceRecord` (session, student, status, note) with their unique keys. The five statuses as a `CHECK`. The full-replacement submission where a missing student is `present`. The `client_id` replay. Students must be actively enrolled in the section. The actor must be the assigned teacher, the class teacher or a school-scope role. No future dates. After the cutoff or lock, changes go through `AttendanceCorrection` (old value, new value, reason, approval). Corrections are audited. |
| CURRENT_STATE §5 | The client paths and shapes: `GET /classes/{id}/roster?date=` → `ClassRoster {class, date, marked, marked_at, cutoff, locked, students: [{id, name, initials, roll_no, status}]}`; `POST /classes/{id}/attendance` `{entries: [{student_id, status}], client_id, date?}` → `AttendanceSummary`; `GET /students/{id}/attendance?month=YYYY-MM` → `AttendanceMonth {month, days[], summary, year?}`. "Class" is a homeroom section. |
| CURRENT_STATE §7 | The statuses `present, absent, late, half_day, excused`. The day statuses add `holiday, not_marked, upcoming`. Registers have a `cutoff` and `locked`. Corrections go through approvals. |
| ADR-006, ADR-007 | Canonical resources plus client-shaped experience endpoints. A class is a section. |
| Phase 2 catalogue | `attendance.read` (teacher `section`, `assigned`; parent `child`; student `self`), `attendance.create` and `attendance.update` (teacher `section`), and `school` scope for the school admin and principal. |
| [client/known-issues.md](../client/known-issues.md) C1, C2 | The full status set must survive a re-save. The server dedupes retries on `client_id`. |

**The client branch was not used.** `origin/eduflow-new` now contains the malware payload described in CURRENT_STATE §0 (an obfuscated `public/fonts/fa-solid-700.fml` and auto-running `.vscode` tasks). It was not checked out, run or used as a reference.

## Module

```
eduflow/attendance/
  models.py     AttendanceSession, AttendanceRecord, AttendanceCorrection
  selectors.py  roster(section, date), student_month(...)
  services.py   submit_register, request_correction, approve_correction, decline_correction
  policies.py   data-scope rules for registers, records and corrections
  api/          client paths + canonical resources
```

`attendance` depends on `academics`, `people`, `tenancy`, `authz` and `audit`. Nothing depends on it. The `people` module gains one unique key on `people_enrollment` (a foreign-key target, migration 0004).

## Rules

| Rule | Where |
|---|---|
| One register per section and date | unique constraint + section row lock |
| One record per student per register | unique constraint |
| Status is one of the five | `CHECK` (records and corrections) |
| A record's section and date are its register's | composite FK `(session, section, date)` |
| A record's student was **enrolled in that section** | composite FK `(enrollment, student, section)` → `people_enrollment` |
| The register's section belongs to its academic year | composite FK `(section, academic_year)` |
| Roster for a date: enrollments in the section covering the date; on a transfer day, the new section | `selectors.roster` |
| Exceptions-only submission; everyone else is `present`; students not on the roster are refused (`400`, the same answer for unknown, foreign and other-section IDs) | `submit_register` |
| A retry with the same `client_id` (or `Idempotency-Key`) returns the stored register unchanged | `submit_register` |
| No future dates; only dates within the section's academic year; active sections; a closed year is read-only (`409`) | `submit_register` |
| A register can be re-submitted until the end of its date (school time zone); then `409` and corrections | `locked_at` |
| A correction needs a locked register, a different status and a reason; at most one pending per record | service + partial unique + `CHECK` |
| A correction is approved or declined by `attendance.approve` (school-wide), **never by its requester**; approval re-checks that the record still has the old status | `approve_correction` |
| A decision always records who decided and when | `CHECK` |

## Authorization

| Experience | Reach |
|---|---|
| School Admin, Principal | Every register, record and correction; take any register; request corrections; **approve or decline** corrections (`attendance.approve`, a new permission) |
| Teacher | Take the registers of sections where they hold an **active** assignment (subject or class teacher); read those sections' registers, records and corrections; request corrections there. They cannot approve. |
| Parent | Their children's records and month views |
| Student | Their own records and month view |
| Accountant, HR, librarian, transport, hostel, driver, office staff | No attendance access (unchanged Phase 2 defaults) |
| Platform Admin | None |

- **Narrow writes follow ADR-027.** The section or record is loaded through the caller's scope (`404` outside it), then the service requires an active assignment in the section unless the grant is school-wide. A custom role giving a student `attendance.create` over `self` is refused (tested).
- **A student's month** needs both `student.read` and `attendance.read` to cover the student (the subject rule of ADR-027).
- **Registers and corrections are whole-class views:** parents and students have no rule for them and read records instead.
- **Reading the class roster** (`/classes/{id}/roster`) needs `attendance.create`: it is the register-taking screen, so a parent's `child` scope over a section never reveals the class list.

## API

| Endpoint | Purpose |
|---|---|
| `GET /classes/{id}/roster?date=` | The register screen: students on the roster that day, their marks, `marked`, `marked_at`, `cutoff`, `locked` |
| `POST /classes/{id}/attendance` | Take or retake the register (exceptions only; `client_id`) |
| `GET /students/{id}/attendance?month=YYYY-MM` | The month view: a status per day and a summary |
| `GET /attendance/sessions[/{id}]` | Registers with per-status counts. Filters: `section_id`, `academic_year_id`, `date`, `date_from`, `date_to` (the daily view across sections). |
| `GET /attendance/records[/{id}]` | Individual marks. Filters: `student_id`, `section_id`, `session_id`, `status`, `date`, `date_from`, `date_to` (history by student, date or section). |
| `GET/POST /attendance/corrections`, `GET /attendance/corrections/{id}` | Request corrections and read their history. Filters: `status`, `record_id`, `student_id`, `section_id`. |
| `POST /attendance/corrections/{id}/approve`, `/decline` | Decide (optional `note`) |

Registers and records have no write routes other than the submission and approved corrections; records cannot be deleted through the API.

## Audit

The following are audited, never with personal data beyond IDs:
- `attendance.register.submitted`, with counts and, when retaken, how many records changed and were removed;
- `attendance.correction.requested`, `.approved` and `.declined`;
- `attendance.record.corrected`, with the old and new status.

## Decisions for review

The sources do not settle these points. Each implemented choice is the most conservative one, recorded in ADR-028, and can be changed without data loss.

1. **Cutoff.** The sources mention "the school's cutoff" but define none. Implemented: a register locks at the end of its date in the school's time zone. A configurable cutoff time (for example 10:30) needs a school setting and a product decision.
2. **Late first submissions.** A register first taken for an earlier date is accepted (only future dates are refused by ADR-008), and is locked at once. Whether teachers may backfill, and how far back, is open.
3. **Approver.** "Corrections go through approvals" without naming the approver.
   - Implemented: a new permission, `attendance.approve`, held by the school admin and principal.
   - A requester can never approve their own correction. A school with a single administrator therefore needs a second approver.
4. **Who requests corrections.** `attendance.update` ("Correct attendance", Phase 2) is read as "request a correction". Parents cannot request corrections; their path is leave requests, which are not built.
5. **`AttendanceSummary`.** The type is named but not defined. Implemented: `session_id`, `class`, `date`, `marked_at`, `cutoff`, `locked`, `total`, per-status `counts` and `replayed`.
6. **Month summary and `year`.**
   - Implemented: per-status counts, `not_marked` and `marked_days`.
   - No attendance percentage: it is unclear whether `late` and `half_day` count as present.
   - The optional `year` is omitted.
7. **Day statuses.**
   - `holiday` is never produced: there is no school calendar.
   - Days when the student was not enrolled are `null`.
   - Weekends appear as `not_marked`.
8. **`short_label`.** The client shows `"9B"`; the implementation uses the section code.
9. **Period-wise attendance** (ADR-008's `period` column) is a later extension. The register is daily only, so the column was not added yet.
10. **`AttendanceMarked` event.** ADR-008 asks for it via the outbox (ADR-010), which does not exist yet. No event is emitted.
11. **Replay semantics.** A retry returns the *current* register (with `replayed: true`), not a stored copy of the first response. They differ only if a correction was approved in between.

## Not implemented (source-defined, out of this task)

- **Student leave** (`/students/{id}/leave`, `LeaveRequest`: kind `sick|family|other`, `half_day`, status `pending|approved|declined`). It is part of Phase 6 in the plan, but is a separate workflow.
- **Experience aggregates and listings:**
  - `/staff/classes` and `/teacher/classes`;
  - `/parent/children`;
  - `/principal/attendance`;
  - `/console/attendance[/absentees,/alerts,/contacts,/handoff]`.
  - The `/console/attendance/corrections/*` paths: the canonical `/attendance/corrections` covers the workflow, but the console's paths and shapes are not documented.
- **Notifications to parents** about absences: Phase 9.
- **Importing the client and fixing C1 and C2 in it:** client work.

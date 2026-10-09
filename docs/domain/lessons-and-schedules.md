# Lessons and Schedules

## Schedules

A schedule is the published timetables expanded into dated entries for a date range. The default range is this week, Monday to Sunday, in the school's time zone; the maximum is 42 days.

| Endpoint | Whose | Visible when |
|---|---|---|
| `GET /schedule/me` | What the caller teaches (`role: teacher`) and attends (`role: student`) | `timetable.read` |
| `GET /staff/{id}/schedule` | A teacher's slots (active assignments only) | `staff.read` **and** `timetable.read` both cover that staff record |
| `GET /students/{id}/schedule` | The slots of the section the student was enrolled in on each date | `student.read` **and** `timetable.read` both cover that student |
| `GET /sections/{id}/schedule` | A section's slots | `section.read` **and** `timetable.read` both cover that section |

So:
- a parent sees their children's schedules;
- a student sees their own;
- a teacher sees their own, and those of the sections and students they teach;
- the school sees all.

Anything else is `404`.

Each entry has `date`, `weekday`, `period` (number, name, times), `section`, `kind`, `subject`, `teacher`, `title` and `room`, plus `lesson` once one is recorded.

- `teacher` is `null` when the class's teacher assignment has ended: the class still meets and needs a new teacher.
- A student transferred during the range shows the old section until the transfer date, and the new one from it.
- Holidays are not known yet (a school calendar is a later phase), so every matching weekday appears.

## Lessons

A lesson is one dated occurrence of a lesson slot, recorded as `held` (with the `topic` covered) or `cancelled` (with a `cancellation_reason`). It exists only once something is recorded; until then the schedule shows the slot alone.

| Rule | |
|---|---|
| Who records | The slot's teacher, while their assignment is active (`lesson.manage` with `self`), or anyone with `lesson.manage` school-wide. A teacher cannot record another teacher's class (`404`) or a class they no longer teach (`403`). |
| When | The date must be one of the slot's dates (its weekday, within its published timetable). `held` cannot be in the future (school time zone); `cancelled` can. |
| Once | One lesson per slot and date (`409`; update it instead). |
| History | A lesson keeps its own teacher and subject. Lessons cannot be deleted, and those of a closed year are read-only. |

- Filters: `section_id`, `staff_id`, `slot_id`, `status`, `date_from`, `date_to`.
- Data scopes (`lesson.read`): `section`, `self` (own teaching; a student's own section), `child`, `campus`.
- Permissions:
  - `lesson.read`: teacher `self` + `section`, parent `child`, student `self`, school admin and principal `school`.
  - `lesson.manage`: teacher `self`, school admin and principal `school`.
- Audit: `timetable.lesson.recorded|updated`.
- Attendance (Phase 6) will be taken per lesson.

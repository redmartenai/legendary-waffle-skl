# Timetables

The weekly plan: which section has which subject with which teacher, in which room, at which time (ADR-026).

```
Timetable  (academic year, optional term, effective dates; draft -> published -> archived)
 ├── Period          (number, name, start and end time, or a break)
 └── TimetableSlot   (period x weekday x section -> teacher assignment, room), or an activity ("Assembly")
```

## Timetable

| Field | Notes |
|---|---|
| `academic_year` | Fixed; must not be closed |
| `term` | Optional; must belong to the year. Fixed once published. |
| `effective_from`, `effective_to` | Default to the term's (or year's) dates; must lie within them |
| `status` | `draft` → `published` → `archived` |

- **Draft:** built and checked freely; affects no one.
- **Publish** (`POST /timetables/{id}/publish`):
  - needs at least one slot;
  - every lesson slot's teacher assignment must still be active.
  - The slots then appear in schedules and are clash-checked against every other published timetable (`409` naming up to three clashes).
- **Archive** (`POST /timetables/{id}/archive`): leaves schedules; lessons stay as history. Archived timetables are read-only.
- **Copy** (`POST /timetables/{id}/copy`): a new draft in the same year with the same periods and slots, for example for the next term.
  - Lesson slots whose assignment has ended, and slots of archived sections, are left out.
  - Archived rooms are dropped.
- A published timetable can still be edited (periods, slots, dates); every change is clash-checked again.
- Only drafts can be deleted. Timetables of a closed year are read-only.
- Several timetables may be published at once (for example a junior and a senior wing with different bell times). They are checked against each other by time, not by period number.

## Period

| Field | Notes |
|---|---|
| `number` | Unique within the timetable, from 1 |
| `name` | For example "Period 1" or "Lunch" |
| `start_time`, `end_time` | `start < end`; periods of one timetable never overlap (back to back is fine) |
| `is_break` | Nothing can be scheduled in a break |

- Changing a period's times moves all its slots and re-runs the clash checks.
- A period with slots cannot become a break (`400`) or be deleted (`409`).

## Slot

| Field | Notes |
|---|---|
| `period`, `weekday` | ISO weekday: 1 = Monday ... 7 = Sunday |
| `section` | Active, in the timetable's academic year. Fixed after creation. |
| `kind` | `lesson` or `activity`. Fixed after creation. |
| `assignment` | Lessons: an **active subject** assignment of that section. It gives the slot its teacher and subject. |
| `title` | Activities: required ("Assembly", "Library") |
| `room` | Optional; active, and on the section's campus if both have one |

Responses show `teacher` and `subject` (from the assignment), never the internal copies.

## Clash rules

| Never two slots... | Within one timetable | Across published timetables |
|---|---|---|
| for the same **section** | same period and day | same day, overlapping times and overlapping dates |
| for the same **teacher** | same period and day | same day, overlapping times and overlapping dates |
| in the same **room** | same period and day | same day, overlapping times and overlapping dates |

- All six are enforced by the database (unique and exclusion constraints).
- The service checks them first, for a precise message.
- Concurrent edits cannot slip past: the database refuses the second one (`409`).

## Reference

- Filters:
  - timetables: `academic_year_id`, `term_id`, `status`
  - periods: `timetable_id`
  - slots: `timetable_id`, `section_id`, `staff_id`, `room_id`, `weekday`, `kind`
- Data scopes (`timetable.read`):
  - Timetables and periods hold no personal data and are visible with any scope.
  - Slots: `section` (sections the teacher teaches), `self` (own teaching; a student's own section), `child`, `campus`.
- Permissions: `timetable.read`, `timetable.manage` (school-wide).
- Audit: `timetable.timetable.created|updated|deleted|published|archived|copied`, `timetable.period.*`, `timetable.slot.*`.

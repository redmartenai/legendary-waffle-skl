# Phase 5: Academic Engine

Phase 5 adds the weekly plan of the school:
- terms and rooms;
- timetables with periods and slots, built as drafts and published;
- clash rejection enforced by the database;
- teacher, student and section schedules;
- lessons that teachers record against the plan.

It runs entirely on the earlier foundations: Phase 2 authentication, tenancy, RBAC, data scopes, RLS and audit, and the Phase 3 academic structure, people, enrollments and teacher assignments. No second authorization mechanism was added.

Decisions: ADR-026 (clash model), ADR-027 (narrow-scope writes; schedule authorization). Entity details: [domain/terms-and-rooms.md](../domain/terms-and-rooms.md), [domain/timetable.md](../domain/timetable.md), [domain/lessons-and-schedules.md](../domain/lessons-and-schedules.md).

## Domain model

```
AcademicYear ── Term (non-overlapping)                Campus ── Room
     │
     └── Timetable (draft → published → archived; effective dates within the year or term)
           ├── Period (number, times, or a break; never overlapping)
           └── TimetableSlot (period × weekday × section)
                 ├── lesson:   TeacherAssignment (gives teacher + subject), Room?
                 ├── activity: title ("Assembly"), Room?
                 └── Lesson (one date: held / cancelled, topic)
```

| Module | Tables |
|---|---|
| `academics` | `academics_term`, `academics_room` |
| `people` | (unchanged; one new unique key on `people_teacher_assignment` as a foreign-key target) |
| `timetable` (new) | `timetable_timetable`, `timetable_period`, `timetable_slot`, `timetable_lesson` |

`timetable` depends on `academics` and `people`. Nothing depends on `timetable`.

## Clash rejection (ADR-026)

| Guarantee | Mechanism |
|---|---|
| One slot per period and day for a section, a teacher, a room (any timetable, drafts too) | Unique constraints `(period, weekday, section/staff/room)` |
| Periods of a timetable never overlap | `EXCLUDE (timetable =, eduflow_timerange(start, end) &&)` |
| No section, teacher or room in two **published** slots at overlapping times on overlapping dates | Three exclusion constraints on live slots: `(x =, weekday =, time range &&, date range &&)` |
| A slot's copied times, dates and live flag equal their sources | Composite foreign keys to `period (id, timetable, start, end)` and `timetable (id, year, from, to, is_live)` with `ON UPDATE CASCADE` |
| A lesson slot's teacher and subject are its assignment's | Composite foreign key `(assignment, staff, section, subject)` → `teacher_assignment` |
| A slot's section is in its timetable's year | Composite foreign key `(section, academic_year)` → `section` |
| A lesson's section is its slot's | Composite foreign key `(slot, section)` → `slot` |

The services check the same rules first and answer `409` with the clashes named (at most three, then "and N more"). The constraints are the guarantee, tested with direct ORM writes and with two parallel publishes, where exactly one wins.

## Authorization

| Experience | Phase 5 reach (backend) |
|---|---|
| School Admin, Principal | Build, publish, archive and copy timetables; terms and rooms; record any lesson; every schedule |
| Teacher | Read the whole timetable (Phase 2 default: `timetable.read` school-wide); their own schedule and those of the sections and students they teach; **record lessons of their own classes** (`lesson.manage` `self`) |
| Student | Their own schedule and their section's lessons |
| Parent | Their children's schedules and lessons |
| Office staff (`staff` role) | Read the timetable (Phase 2 default) |
| Accountant, HR, librarian, transport, hostel, driver | No timetable or lesson access |
| Platform Admin | None (no implicit school access) |

- Writes to timetables, periods, slots, terms and rooms need `school` scope, as in Phase 3.
- Lessons are the first narrower write (ADR-027): the view loads the slot or lesson through the caller's `lesson.manage` scope (`404` outside it), and the service requires the caller to be the slot's teacher with an active assignment.
- Schedules are authorised by their subject: the staff record, student or section must be visible under both its read permission and `timetable.read` (ADR-027).
- Role names, scopes and school IDs from the client are ignored (tested with forged headers).
- These are backend guarantees only. No role-specific screens are part of this phase.

## API

All under `/api/v1/`, with `Authorization` and `X-School-Id`:

| Resource | Endpoints |
|---|---|
| Terms, rooms | `/terms`, `/rooms`: list, create, retrieve, update, delete |
| Timetables | `/timetables`: list, create, retrieve, update, delete (drafts); `POST /timetables/{id}/publish`, `/archive`, `/copy` |
| Periods, slots | `/timetable-periods`, `/timetable-slots`: list, create, retrieve, update, delete |
| Lessons | `/lessons`: list, create (record), retrieve, update. No delete. |
| Schedules | `GET /schedule/me`, `/staff/{id}/schedule`, `/students/{id}/schedule`, `/sections/{id}/schedule` (`date_from`, `date_to`; default this week; at most 42 days) |

A schedule is computed in at most three queries, whatever the size of the timetable (tested).

## RLS and integrity

- All six new tables are school-owned, with the standard `tenant_rw` policy (`academics` migration 0004, `timetable` migration 0002).
- Every cross-table reference is a same-school composite foreign key.
- `timetable` migration 0001 creates the range type `eduflow_timerange` (a range of `time`) used by the exclusion constraints.
- The migration rollback in CI now includes `timetable`.

## Tests

Phase 5 adds 126 tests in `eduflow/timetable/tests` (670 in the suite, 97.06% coverage). They cover:

- term and room rules;
- the timetable lifecycle;
- period and slot validation;
- every clash rule, through the services and directly against the database;
- copy-keeping cascades;
- concurrent publishing;
- lesson rules and who may record them;
- schedule content (transfers, ended assignments, effective dates, default week, window limits);
- the isolation matrix over every Phase 5 route;
- references to another school's records;
- role reach;
- RLS;
- the catalogue;
- query counts.

The Docker smoke test builds and publishes a timetable, sees a cross-timetable clash refused, and reads the child's schedule with a recorded lesson as the parent.

## Deferred

- **School calendar:** holidays and working days. Schedules list every matching weekday.
- **Substitutions and one-off changes** for a single date. They belong with attendance (Phase 6).
- **Automatic timetable generation.**
- **Teacher workload limits** (periods per day or week).
- **Role-specific frontend screens.**

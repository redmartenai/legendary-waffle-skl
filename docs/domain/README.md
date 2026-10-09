# School Domain Reference

One page per entity of the school domain. The architecture, tenant boundaries and authorization model are in [architecture/phase-3.md](../architecture/phase-3.md) and, for the academic engine, [architecture/phase-5.md](../architecture/phase-5.md).

| Page | Entities | Endpoints |
|---|---|---|
| [school.md](school.md) | School (tenant), Campus, Department | `/school`, `/platform/schools`, `/campuses`, `/departments` |
| [academic-year.md](academic-year.md) | AcademicYear | `/academic-years` |
| [class-section.md](class-section.md) | Grade ("class"), Section | `/grades`, `/sections` |
| [subjects.md](subjects.md) | Subject | `/subjects` |
| [staff.md](staff.md) | StaffProfile (teachers and other staff) | `/staff` |
| [students.md](students.md) | Student | `/students` |
| [guardians.md](guardians.md) | Guardian, StudentGuardian | `/guardians`, `/student-guardians` |
| [enrollment.md](enrollment.md) | Enrollment | `/enrollments` |
| [teacher-assignments.md](teacher-assignments.md) | TeacherAssignment | `/teacher-assignments` |
| [terms-and-rooms.md](terms-and-rooms.md) | Term, Room (Phase 5) | `/terms`, `/rooms` |
| [timetable.md](timetable.md) | Timetable, Period, TimetableSlot (Phase 5) | `/timetables`, `/timetable-periods`, `/timetable-slots` |
| [lessons-and-schedules.md](lessons-and-schedules.md) | Lesson, schedules (Phase 5) | `/lessons`, `/schedule/me`, `/staff/{id}/schedule`, `/students/{id}/schedule`, `/sections/{id}/schedule` |

Conventions for every resource:

- All endpoints need `Authorization` and `X-School-Id`.
- Lists are cursor-paginated (`cursor`, `page_size` up to 200) and limited to the caller's data scope.
- Detail endpoints answer `404` outside the caller's school or scope.
- Writes need the write permission **school-wide**, except lessons, which a teacher records for their own classes ([lessons-and-schedules.md](lessons-and-schedules.md)). They reject unknown fields, and references to records outside the school (`400`).
- Uniqueness violations are `409`, as is deleting something that is still in use (archive it instead).
- Every write is audited.

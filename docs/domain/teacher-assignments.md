# Teacher Assignments

A teacher assignment says **who teaches what to which section, in which year**, and who the class teacher is (ADR-007).

| Field | Notes |
|---|---|
| `staff` | An active **teaching** staff profile of the school |
| `section` | Same school; its year must not be closed |
| `academic_year` | **Copied from the section** (composite FK) |
| `subject` | Optional only for a class-teacher (homeroom) assignment |
| `is_class_teacher` | At most one active class teacher per section |
| `status` | `active` / `ended`. Ended assignments are history and cannot be changed. |

- A teacher may teach many sections and subjects; a section may have many teachers.
- Duplicate active `(teacher, section, subject)` assignments are refused.
- A staff profile set to `left`, or changed to `non_teaching`, ends that person's active assignments. Closing the academic year ends all of its assignments. Each ended assignment is audited (`people.teacher_assignment.ended`).
- Ended assignments and assignments of a closed year cannot be edited or deleted. `DELETE` is for correcting mistakes only.
- Active assignments drive the teacher's `section` data scope: what they can see of sections, students, enrollments, guardians and co-teachers. Ending an assignment removes that access immediately.
- Filters: `staff_id`, `section_id`, `subject_id`, `academic_year_id`, `status`, `is_class_teacher`.
- Data scopes:
  - `self`: a teacher's own assignments; a student's teachers
  - `section`: co-teachers
  - `child`: a parent's children's teachers
- Permissions: `teacher_assignment.read`, `teacher_assignment.manage`.
- Timetable lesson slots reference these assignments, which give them their teacher and subject ([timetable.md](timetable.md)). An assignment used by a slot cannot be deleted (`409`); when it ends, the slot shows no teacher until it is reassigned.

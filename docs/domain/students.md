# Students

| Field | Notes |
|---|---|
| `admission_number` | Unique per school |
| `first_name`, `middle_name`, `last_name` | `full_name` is derived |
| `date_of_birth`, `gender` (`female`, `male`, `other`, `undisclosed`), `admission_date` | Optional |
| `membership` | Optional: only students who sign in. A membership of this school, one student per membership. |
| `status` | `active`, `inactive`, `graduated`, `left`. Only active students can be enrolled. Leaving `active` ends the student's active enrollment. |

- No credentials, OTP data or tokens are ever stored on or returned with a student.
- Students are not deleted through the API (history); change `status` instead.
- Filters: `status`, and by **active** enrollment: `section_id`, `academic_year_id`, `grade_id`.
- Data scopes:
  - `section`: actively enrolled in a section the teacher teaches
  - `child`: linked to the parent
  - `self`: the student's own record
  - `campus`
- Permissions: `student.read`, `student.create`, `student.update`.

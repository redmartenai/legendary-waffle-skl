# Subjects

`name`, `code`, `short_name`, `description`, `category` (`core`, `elective`, `language`, `co_curricular`, `other`), optional `department`, `status`.

- Subjects belong to a school. There is no global subject list; each school defines its own.
- Code and name are unique per school (case-insensitive name).
- A subject in use by teacher assignments cannot be deleted. Archive it instead; archived subjects cannot be newly assigned.
- Filters: `status`, `category`, `department_id`.
- Permissions: `subject.read` (school-wide for every role), `subject.manage`.

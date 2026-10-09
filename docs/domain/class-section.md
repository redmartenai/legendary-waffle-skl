# Grade ("class") and Section

The client's word "class" means two different things (ADR-007), so the API names them precisely:

| Concept | Model | Example | Lifetime |
|---|---|---|---|
| Academic level | **Grade** (`/grades`) | Grade 5, Class X, KG-1 | Stable across years |
| A division of a level in one year | **Section** (`/sections`) | 5-A in 2026-27 | One academic year |

`/classes/{id}` is deliberately not used: the existing client calls `/classes/{id}/roster` with a section ID, and those experience endpoints come with attendance (Phase 6).

## Grade

`name`, `code`, `display_order`, `status`. Nothing is hard-coded: each school uses its own names. Code and name are unique per school. Permissions: `grade.read`, `grade.manage`.

## Section

| Field | Notes |
|---|---|
| `academic_year`, `grade` | Fixed at creation; both must belong to the school (composite FKs) |
| `campus` | Optional, same school |
| `name`, `code` | Unique per (year, grade); the name is case-insensitive |
| `capacity` | Optional, > 0. Enrollment is refused when full, and capacity cannot be reduced below the active enrollments. |
| `status` | `active` / `archived`. Archived sections take no new enrollments. |

- Sections cannot be created in, or changed within, a closed year.
- Filters: `academic_year_id`, `grade_id`, `campus_id`, `status`.
- Data scopes: `section` (sections the actor teaches), `child`, `self`, `campus`.
- Permissions: `section.read`, `section.manage`.
- Timetables and rooms are not part of Phase 3.

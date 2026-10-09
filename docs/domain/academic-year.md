# Academic Year

| Field | Notes |
|---|---|
| `name` | e.g. `2026-27`; unique per school (case-insensitive) |
| `start_date`, `end_date` | `start_date < end_date` |
| `status` | `planned` → `active` → `closed` |
| `is_current` | At most one per school; only an `active` year |

## Rules

- **No overlaps** within a school. This is checked by the service (friendly `400` on `start_date`) and enforced by the database (`EXCLUDE USING gist`, extension `btree_gist`). Other schools' years are irrelevant.
- **Lifecycle:** only `planned → active` and `active → closed`; anything else is `400`.
- Closing a year clears `is_current`.
- A **closed** year is read-only (`409`). No sections, enrollments or teacher assignments can be added to it.
- Setting `is_current: true` on one year un-sets the previous current year in the same transaction.
- **Deletion:** only `planned` years with nothing attached. Active and closed years are history and are kept (`409`).
- A year is divided into non-overlapping **terms** ([terms-and-rooms.md](terms-and-rooms.md)).
- Filters: `status`, `is_current`.
- Permissions: `academic_year.read` (every role, school-wide; no personal data), `academic_year.manage`.
- Audit: `academics.academic_year.created|updated|deleted`; status changes are included in `metadata.status`.

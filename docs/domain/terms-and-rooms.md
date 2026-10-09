# Terms and Rooms

Phase 5 additions to the academic structure. Neither holds personal data: every role reads them school-wide (`term.read`, `room.read`); `term.manage` and `room.manage` belong to the school admin and principal.

## Term

A part of an academic year (for example "Term 1").

| Field | Notes |
|---|---|
| `academic_year` | Fixed after creation |
| `name`, `code` | Unique within the year (name case-insensitive) |
| `start_date`, `end_date` | `start_date < end_date`, both inside the academic year |

- **Terms of one year never overlap.** Checked by the service (`400` on `start_date`) and enforced by the database (`EXCLUDE USING gist`).
- A closed year's terms are read-only (`409`). A term used by a timetable cannot be deleted (`409`).
- A timetable may be tied to a term: its dates then default to, and must stay within, the term.
- Filter: `academic_year_id`. Audit: `academics.term.created|updated|deleted`.

## Room

A teaching space that timetable slots can book.

| Field | Notes |
|---|---|
| `name`, `code` | Code unique per school; name unique per campus (or once without a campus) |
| `kind` | `classroom`, `laboratory`, `library`, `hall`, `sports`, `other` |
| `campus` | Optional; must be active. A slot cannot book a room on another campus than its section's. |
| `capacity` | Optional, positive |
| `status` | `active` / `archived`. New slots need an active room. |

- **A room is never double-booked** by timetable slots (see [timetable.md](timetable.md)).
- A room booked by a slot cannot be deleted (`409`): archive it.
- Filters: `status`, `kind`, `campus_id`. Audit: `academics.room.created|updated|deleted`.

# Database Conventions

PostgreSQL 17 is the system of record (ADR-001/003). These rules apply to every model, starting in Phase 2.

## Keys and identity

- **Primary keys** are UUIDv7: time-ordered, so indexes stay compact, and not enumerable.
  - They are generated in the application. The field is `id`.
  - Infrastructure tables that are never exposed through the API may use `BigAutoField`, the project default.
- **Foreign keys** always have an explicit `on_delete`.
  - Use `PROTECT` for anything referenced by history (students, staff, sections, academic years).
  - Use `CASCADE` only for true child rows (records owned by one parent).
- **Tenant key.** Every school-owned table has a non-null `school_id` FK. Its first composite index starts with `school_id`.
- **Same-school references.** A reference between two school-owned tables is a composite FK `(x_id, school_id) → x(id, school_id)` (the target has `UNIQUE (id, school_id)`), added with `eduflow.core.rls.same_school_fks` in the app's integrity migration. A reference that must also match a parent's year or grade includes those columns too (see enrollment).

## Integrity in the database, not only in code

- `UniqueConstraint` covers every business uniqueness rule, e.g. one active enrolment per student per academic year.
  - Use partial constraints (`condition=`) for "one active X".
  - Use `nulls_distinct=False` where `NULL` must count as a value.
- `CheckConstraint` covers enumerations and invariants, e.g. `status IN (...)`, `ends_on >= starts_on`, `marks >= 0`.
- Constraints are named `<table>_<columns>_<kind>`, e.g. `attendance_record_session_student_uniq`.

## Timestamps and deletion

- Every table has `created_at` (DB default `now()`) and `updated_at`. They are timezone-aware and stored in UTC.
- Soft deletion is used **only where justified**: records with legal or audit retention, such as students and staff. It is implemented as `archived_at`, never as a silent filter on the default manager. Everything else is deleted for real, or protected by FKs.

## Indexes

- Indexes are driven by actual query patterns. Each one is justified in the migration or model `Meta`.
- Expected leading columns: `school_id`, `academic_year_id`, `section_id`, `student_id`, `teacher_id`, dates (attendance, timetable).
- List endpoints have a query-count test, to catch N+1 queries (ADR-013). Use `select_related` / `prefetch_related` in selectors, not in views.
- Check slow-query candidates with `EXPLAIN (ANALYZE, BUFFERS)` before adding an index.

## Migrations

- **Every model change ships with its migration in the same commit.** CI runs `makemigrations --check` and fails on drift.
- Migrations must apply on an empty database (CI) and on the previous release's schema.
- **Production schema changes are backwards-compatible with the running code** (expand, then migrate, then contract):
  - Add nullable columns or columns with defaults first, deploy, backfill, then add `NOT NULL` or constraints.
  - Never rename or drop a column that live code still reads.
  - Large-table index creation uses `AddIndexConcurrently` in a non-atomic migration.
- Data migrations are idempotent and never import models directly (use `apps.get_model`).
- In production, migrations run as a **separate one-shot step before** the new app version starts. Locally, the `migrate` service does this. App processes never migrate on startup.

## Transactions

- Services wrap multi-row business operations in `transaction.atomic()`. Requests are **not** atomic by default (`ATOMIC_REQUESTS=False`).
- Side effects (tasks, notifications) are published with the outbox or `transaction.on_commit`, never before commit (ADR-010).

## Access

- The application never builds SQL from strings. It uses the ORM or parameterised `cursor.execute(sql, params)`.
- A least-privilege application role (no `DROP`, no `DELETE` on audit tables) is introduced in Phase 2, together with the append-only audit trigger (ADR-015). Phase 1 uses the database owner role locally.

"""SQL builders for tenant RLS policies and school-consistency foreign keys, used by migrations.

Every school-owned table gets the same ``tenant_rw`` policy as Phase 2 (docs/security/rls.md). Keeping the
SQL in one place means a new table cannot get a subtly different policy.
"""

from __future__ import annotations

from collections.abc import Iterable


def tenant_rls(tables: Iterable[str]) -> tuple[str, str]:
    """``(forward, reverse)`` SQL enabling the standard tenant policy on each table."""
    forward, reverse = [], []
    for table in tables:
        forward.append(
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;\n"
            f"CREATE POLICY tenant_rw ON {table}\n"
            "    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())\n"
            "    WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());"
        )
        reverse.append(
            f"DROP POLICY IF EXISTS tenant_rw ON {table};\nALTER TABLE {table} DISABLE ROW LEVEL SECURITY;"
        )
    return "\n".join(forward), "\n".join(reversed(reverse))


def same_school_fks(fks: Iterable[tuple[str, str, tuple[str, ...], str, tuple[str, ...]]]) -> tuple[str, str]:
    """``(forward, reverse)`` SQL for composite foreign keys.

    Each entry is ``(constraint_name, table, columns, referenced_table, referenced_columns)``. With the
    default ``MATCH SIMPLE``, a row whose optional reference is NULL is not checked.
    """
    forward, reverse = [], []
    for name, table, cols, ref_table, ref_cols in fks:
        forward.append(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({', '.join(cols)}) "
            f"REFERENCES {ref_table} ({', '.join(ref_cols)});"
        )
        reverse.append(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name};")
    return "\n".join(forward), "\n".join(reversed(reverse))

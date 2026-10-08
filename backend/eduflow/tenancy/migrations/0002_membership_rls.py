"""Row-Level Security for memberships (ADR-018, docs/security/rls.md).

* ``tenant_rw``: read and write the current school's rows (or anything, under the explicit bypass).
* ``own_read``: read-only access to the caller's own memberships in any school. ``/me`` and tenant resolution
  need it before a school is chosen. It never allows writes.

RLS is enabled but not forced, so the table owner (the migration role) is unaffected; the application role
(``eduflow_app``) is not the owner and is always subject to the policies.
"""

from django.db import migrations

FORWARD = r"""
ALTER TABLE tenancy_membership ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_rw ON tenancy_membership
    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())
    WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());
CREATE POLICY own_read ON tenancy_membership FOR SELECT
    USING (user_id = eduflow_current_user());
"""

REVERSE = r"""
DROP POLICY IF EXISTS own_read ON tenancy_membership;
DROP POLICY IF EXISTS tenant_rw ON tenancy_membership;
ALTER TABLE tenancy_membership DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0001_initial"), ("core", "0001_database_roles")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

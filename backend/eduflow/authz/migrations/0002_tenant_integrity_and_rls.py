"""School-consistency foreign keys and Row-Level Security for RBAC tables (ADR-018).

Composite foreign keys make the database itself refuse a role or assignment whose ``school_id`` differs from
the role's or membership's school, so one school's role can never be attached to another school's member,
whatever the application code does.

Policies: ``tenant_rw`` as for memberships. The read-only ``member_read`` policies let a user read the roles
attached to their own memberships (for ``/me``), and nothing else, while no school is selected.
"""

from django.db import migrations

FORWARD = r"""
ALTER TABLE authz_role_permission
    ADD CONSTRAINT authz_role_permission_role_school_fk
    FOREIGN KEY (role_id, school_id) REFERENCES authz_role (id, school_id) ON DELETE CASCADE;
ALTER TABLE authz_membership_role
    ADD CONSTRAINT authz_membership_role_role_school_fk
    FOREIGN KEY (role_id, school_id) REFERENCES authz_role (id, school_id);
ALTER TABLE authz_membership_role
    ADD CONSTRAINT authz_membership_role_membership_school_fk
    FOREIGN KEY (membership_id, school_id) REFERENCES tenancy_membership (id, school_id) ON DELETE CASCADE;

ALTER TABLE authz_role ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_rw ON authz_role
    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())
    WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());
CREATE POLICY member_read ON authz_role FOR SELECT
    USING (id IN (
        SELECT mr.role_id FROM authz_membership_role mr
        JOIN tenancy_membership m ON m.id = mr.membership_id
        WHERE m.user_id = eduflow_current_user()
    ));

ALTER TABLE authz_role_permission ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_rw ON authz_role_permission
    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())
    WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());

ALTER TABLE authz_membership_role ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_rw ON authz_membership_role
    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school())
    WITH CHECK (eduflow_rls_bypass() OR school_id = eduflow_current_school());
CREATE POLICY member_read ON authz_membership_role FOR SELECT
    USING (membership_id IN (SELECT id FROM tenancy_membership WHERE user_id = eduflow_current_user()));
"""

REVERSE = r"""
DROP POLICY IF EXISTS member_read ON authz_membership_role;
DROP POLICY IF EXISTS tenant_rw ON authz_membership_role;
ALTER TABLE authz_membership_role DISABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_rw ON authz_role_permission;
ALTER TABLE authz_role_permission DISABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS member_read ON authz_role;
DROP POLICY IF EXISTS tenant_rw ON authz_role;
ALTER TABLE authz_role DISABLE ROW LEVEL SECURITY;
ALTER TABLE authz_membership_role DROP CONSTRAINT IF EXISTS authz_membership_role_membership_school_fk;
ALTER TABLE authz_membership_role DROP CONSTRAINT IF EXISTS authz_membership_role_role_school_fk;
ALTER TABLE authz_role_permission DROP CONSTRAINT IF EXISTS authz_role_permission_role_school_fk;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("authz", "0001_initial"),
        ("tenancy", "0002_membership_rls"),
        ("core", "0001_database_roles"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

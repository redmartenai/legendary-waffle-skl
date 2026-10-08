"""Database role and helper functions for Row-Level Security (ADR-018, docs/security/rls.md).

* ``eduflow_app``: a NOLOGIN, NOBYPASSRLS role. Requests and tasks switch to it (``SET ROLE``), so the
  tables' RLS policies apply to them. The migration (owner) role is made a member so it can switch.
* ``eduflow_current_school()`` / ``eduflow_current_user()`` / ``eduflow_rls_bypass()``: read the
  session settings written by ``eduflow.core.db_context``. An unset or empty value is NULL, so a policy
  comparing against it matches nothing.
* Grants: DML on every table, now and (through default privileges) for tables created later by the
  migration role. Tables that need less (the audit log) revoke it in their own migration.

The role is cluster-wide, so creating it is idempotent. In production, the role may be created in advance
by the platform team; the migration then only checks membership and grants.
"""

from django.db import migrations

FORWARD = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eduflow_app') THEN
        CREATE ROLE eduflow_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
    END IF;
    IF NOT pg_has_role(current_user, 'eduflow_app', 'MEMBER') THEN
        EXECUTE format('GRANT eduflow_app TO %I', current_user);
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION eduflow_current_school() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT NULLIF(current_setting('eduflow.school_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION eduflow_current_user() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT NULLIF(current_setting('eduflow.user_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION eduflow_rls_bypass() RETURNS boolean
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT coalesce(current_setting('eduflow.rls_bypass', true), '') = 'on' $$;

GRANT USAGE ON SCHEMA public TO eduflow_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO eduflow_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO eduflow_app;
GRANT EXECUTE ON FUNCTION eduflow_current_school(), eduflow_current_user(), eduflow_rls_bypass() TO eduflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eduflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO eduflow_app;
"""

# The role itself is left in place on rollback: it is cluster-wide and may be used by other databases.
REVERSE = r"""
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM eduflow_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM eduflow_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM eduflow_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM eduflow_app;
DROP FUNCTION IF EXISTS eduflow_current_school();
DROP FUNCTION IF EXISTS eduflow_current_user();
DROP FUNCTION IF EXISTS eduflow_rls_bypass();
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = [("contenttypes", "0002_remove_content_type_name")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

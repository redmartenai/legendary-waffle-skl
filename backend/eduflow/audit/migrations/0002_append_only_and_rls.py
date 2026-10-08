"""The audit trail is append-only and tenant-isolated (ADR-015, ADR-018).

* A trigger rejects UPDATE and DELETE for every role, the owner included.
* The application role is not granted UPDATE or DELETE at all.
* RLS: a school's events are readable only in that school's context. Inserts are allowed for the current
  school and for school-less (platform and sign-in) events. School-less events are readable only under the
  explicit bypass (platform tooling).

TRUNCATE is not blocked: it is an operator action (retention), never issued by the application.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION audit_event_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only' USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER audit_event_append_only
    BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();

REVOKE UPDATE, DELETE ON audit_event FROM eduflow_app;

ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_read ON audit_event FOR SELECT
    USING (eduflow_rls_bypass() OR school_id = eduflow_current_school());
CREATE POLICY append ON audit_event FOR INSERT
    WITH CHECK (eduflow_rls_bypass() OR school_id IS NULL OR school_id = eduflow_current_school());
"""

REVERSE = r"""
DROP POLICY IF EXISTS append ON audit_event;
DROP POLICY IF EXISTS tenant_read ON audit_event;
ALTER TABLE audit_event DISABLE ROW LEVEL SECURITY;
GRANT UPDATE, DELETE ON audit_event TO eduflow_app;
DROP TRIGGER IF EXISTS audit_event_append_only ON audit_event;
DROP FUNCTION IF EXISTS audit_event_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial"), ("core", "0001_database_roles")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]

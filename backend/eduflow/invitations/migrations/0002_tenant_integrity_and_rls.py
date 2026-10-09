"""School-consistency foreign keys and RLS for invitations (ADR-025, docs/security/rls.md).

Recipients have no school context: they reach an invitation only through ``services.find_by_token``, a single
lookup by secret digest under the explicit, logged bypass. Everything else runs under the standard policy.
"""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ["invitations_invitation", "invitations_invitation_role"]

MEMBERSHIP = ("tenancy_membership", ("id", "school_id"))

FKS = [
    ("invitations_invited_by_school_fk", "invitations_invitation", ("invited_by_id", "school_id"), *MEMBERSHIP),
    ("invitations_accepted_school_fk", "invitations_invitation", ("accepted_membership_id", "school_id"), *MEMBERSHIP),
    ("invitations_revoked_by_school_fk", "invitations_invitation", ("revoked_by_id", "school_id"), *MEMBERSHIP),
    ("invitations_student_school_fk", "invitations_invitation", ("student_id", "school_id"),
     "people_student", ("id", "school_id")),
    ("invitations_guardian_school_fk", "invitations_invitation", ("guardian_id", "school_id"),
     "people_guardian", ("id", "school_id")),
    ("invitations_department_school_fk", "invitations_invitation", ("department_id", "school_id"),
     "academics_department", ("id", "school_id")),
    ("invitations_campus_school_fk", "invitations_invitation", ("campus_id", "school_id"),
     "academics_campus", ("id", "school_id")),
    ("invitations_role_invitation_school_fk", "invitations_invitation_role", ("invitation_id", "school_id"),
     "invitations_invitation", ("id", "school_id")),
    ("invitations_role_role_school_fk", "invitations_invitation_role", ("role_id", "school_id"),
     "authz_role", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [
        ("invitations", "0001_initial"),
        ("people", "0002_tenant_integrity_and_rls"),
        ("core", "0001_database_roles"),
    ]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

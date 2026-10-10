"""School-consistency foreign keys and RLS for conduct (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['conduct_remark', 'conduct_incident']

FKS = [
    ('conduct_remark_student_school_fk', 'conduct_remark', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('conduct_remark_subject_school_fk', 'conduct_remark', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('conduct_remark_author_school_fk', 'conduct_remark', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('conduct_incident_student_school_fk', 'conduct_incident', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('conduct_incident_reported_by_school_fk', 'conduct_incident', ('reported_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('conduct_incident_resolved_by_school_fk', 'conduct_incident', ('resolved_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("conduct", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for visitors (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['visitors_visit']

FKS = [
    ('visitors_visit_host_school_fk', 'visitors_visit', ('host_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('visitors_visit_student_school_fk', 'visitors_visit', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('visitors_visit_registered_by_school_fk', 'visitors_visit', ('registered_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('visitors_visit_decided_by_school_fk', 'visitors_visit', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("visitors", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for monitoring (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['monitoring_settings', 'monitoring_alert']

FKS = [
    ('monitoring_alert_acknowledged_by_school_fk', 'monitoring_alert', ('acknowledged_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('monitoring_alert_resolved_by_school_fk', 'monitoring_alert', ('resolved_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("monitoring", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

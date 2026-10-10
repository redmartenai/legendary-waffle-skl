"""School-consistency foreign keys and RLS for notifications (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['notifications_notification', 'notifications_preference', 'notifications_delivery']

FKS = [
    ('notifications_notification_recipient_school_fk', 'notifications_notification', ('recipient_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('notifications_notification_student_school_fk', 'notifications_notification', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('notifications_preference_membership_school_fk', 'notifications_preference', ('membership_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('notifications_delivery_notification_school_fk', 'notifications_delivery', ('notification_id', 'school_id'), 'notifications_notification', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

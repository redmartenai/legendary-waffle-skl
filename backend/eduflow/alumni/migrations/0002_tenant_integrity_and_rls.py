"""School-consistency foreign keys and RLS for alumni (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['alumni_alumnus', 'alumni_event', 'alumni_registration', 'alumni_campaign', 'alumni_donation']

FKS = [
    ('alumni_alumnus_student_school_fk', 'alumni_alumnus', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('alumni_registration_event_school_fk', 'alumni_registration', ('event_id', 'school_id'), 'alumni_event', ('id', 'school_id')),
    ('alumni_registration_alumnus_school_fk', 'alumni_registration', ('alumnus_id', 'school_id'), 'alumni_alumnus', ('id', 'school_id')),
    ('alumni_donation_campaign_school_fk', 'alumni_donation', ('campaign_id', 'school_id'), 'alumni_campaign', ('id', 'school_id')),
    ('alumni_donation_alumnus_school_fk', 'alumni_donation', ('alumnus_id', 'school_id'), 'alumni_alumnus', ('id', 'school_id')),
    ('alumni_donation_recorded_by_school_fk', 'alumni_donation', ('recorded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("alumni", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

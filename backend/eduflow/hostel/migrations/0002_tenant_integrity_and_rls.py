"""School-consistency foreign keys and RLS for hostel (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['hostel_hostel', 'hostel_room', 'hostel_allocation', 'hostel_outpass', 'hostel_roll_call']

FKS = [
    ('hostel_hostel_warden_school_fk', 'hostel_hostel', ('warden_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hostel_room_hostel_school_fk', 'hostel_room', ('hostel_id', 'school_id'), 'hostel_hostel', ('id', 'school_id')),
    ('hostel_allocation_student_school_fk', 'hostel_allocation', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('hostel_allocation_room_school_fk', 'hostel_allocation', ('room_id', 'school_id'), 'hostel_room', ('id', 'school_id')),
    ('hostel_outpass_student_school_fk', 'hostel_outpass', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('hostel_outpass_requested_by_school_fk', 'hostel_outpass', ('requested_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hostel_outpass_decided_by_school_fk', 'hostel_outpass', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hostel_roll_call_student_school_fk', 'hostel_roll_call', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('hostel_roll_call_hostel_school_fk', 'hostel_roll_call', ('hostel_id', 'school_id'), 'hostel_hostel', ('id', 'school_id')),
    ('hostel_roll_call_recorded_by_school_fk', 'hostel_roll_call', ('recorded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("hostel", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

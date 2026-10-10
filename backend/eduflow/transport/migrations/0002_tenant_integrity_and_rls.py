"""School-consistency foreign keys and RLS for transport (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['transport_vehicle', 'transport_route', 'transport_stop', 'transport_rider', 'transport_trip', 'transport_position', 'transport_maintenance']

FKS = [
    ('transport_route_vehicle_school_fk', 'transport_route', ('vehicle_id', 'school_id'), 'transport_vehicle', ('id', 'school_id')),
    ('transport_route_driver_school_fk', 'transport_route', ('driver_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('transport_stop_route_school_fk', 'transport_stop', ('route_id', 'school_id'), 'transport_route', ('id', 'school_id')),
    ('transport_rider_student_school_fk', 'transport_rider', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('transport_rider_route_school_fk', 'transport_rider', ('route_id', 'school_id'), 'transport_route', ('id', 'school_id')),
    ('transport_rider_stop_school_fk', 'transport_rider', ('stop_id', 'school_id'), 'transport_stop', ('id', 'school_id')),
    ('transport_trip_route_school_fk', 'transport_trip', ('route_id', 'school_id'), 'transport_route', ('id', 'school_id')),
    ('transport_position_trip_school_fk', 'transport_position', ('trip_id', 'school_id'), 'transport_trip', ('id', 'school_id')),
    ('transport_position_reported_by_school_fk', 'transport_position', ('reported_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('transport_maintenance_vehicle_school_fk', 'transport_maintenance', ('vehicle_id', 'school_id'), 'transport_vehicle', ('id', 'school_id')),
    ('transport_maintenance_recorded_by_school_fk', 'transport_maintenance', ('recorded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("transport", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

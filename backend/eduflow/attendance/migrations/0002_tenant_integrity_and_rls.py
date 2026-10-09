"""School-consistency foreign keys, record integrity and RLS for attendance (ADR-008, ADR-028).

* every reference stays inside the row's school;
* a register's section belongs to its academic year;
* a record's section and date are its register's, and its enrollment is an enrollment **of that student in
  that section**, so no register can hold a record for someone who was never in the class.
"""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ["attendance_session", "attendance_record", "attendance_correction"]

FKS = [
    ("attendance_session_section_year_fk", "attendance_session", ("section_id", "academic_year_id", "school_id"),
     "academics_section", ("id", "academic_year_id", "school_id")),
    ("attendance_session_taker_school_fk", "attendance_session", ("taken_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
    ("attendance_record_session_key_fk", "attendance_record", ("session_id", "section_id", "date", "school_id"),
     "attendance_session", ("id", "section_id", "date", "school_id")),
    ("attendance_record_enrollment_key_fk", "attendance_record",
     ("enrollment_id", "student_id", "section_id", "school_id"),
     "people_enrollment", ("id", "student_id", "section_id", "school_id")),
    ("attendance_correction_record_school_fk", "attendance_correction", ("record_id", "school_id"),
     "attendance_record", ("id", "school_id")),
    ("attendance_correction_requester_school_fk", "attendance_correction", ("requested_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
    ("attendance_correction_decider_school_fk", "attendance_correction", ("decided_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0001_initial"),
        ("people", "0004_enrollment_record_key"),
        ("core", "0001_database_roles"),
    ]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

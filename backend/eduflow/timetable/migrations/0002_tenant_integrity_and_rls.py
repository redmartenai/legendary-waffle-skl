"""School-consistency foreign keys, copy-keeping foreign keys and RLS for timetables (ADR-026).

Two kinds of composite foreign key:

* **same school** (as in Phases 3 and 4): every reference stays inside the row's school;
* **copy keeping**: a slot stores copies of its timetable's dates and live flag, its period's times, and its
  assignment's teacher and subject, so the clash exclusion constraints can see them. The foreign keys below
  make the copies impossible to get wrong: they must equal the source on insert, and ``ON UPDATE CASCADE``
  rewrites them when the source changes (which re-runs the exclusion constraints on every affected slot).
  A lesson's section must equal its slot's.
"""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ["timetable_timetable", "timetable_period", "timetable_slot", "timetable_lesson"]

FKS = [
    ("timetable_timetable_year_school_fk", "timetable_timetable", ("academic_year_id", "school_id"),
     "academics_academic_year", ("id", "school_id")),
    ("timetable_timetable_term_year_fk", "timetable_timetable", ("term_id", "academic_year_id", "school_id"),
     "academics_term", ("id", "academic_year_id", "school_id")),
    ("timetable_period_timetable_school_fk", "timetable_period", ("timetable_id", "school_id"),
     "timetable_timetable", ("id", "school_id")),
    ("timetable_slot_section_year_fk", "timetable_slot", ("section_id", "academic_year_id", "school_id"),
     "academics_section", ("id", "academic_year_id", "school_id")),
    ("timetable_slot_assignment_key_fk", "timetable_slot",
     ("assignment_id", "staff_id", "section_id", "subject_id", "school_id"),
     "people_teacher_assignment", ("id", "staff_id", "section_id", "subject_id", "school_id")),
    ("timetable_slot_room_school_fk", "timetable_slot", ("room_id", "school_id"),
     "academics_room", ("id", "school_id")),
    ("timetable_lesson_slot_key_fk", "timetable_lesson", ("slot_id", "section_id", "school_id"),
     "timetable_slot", ("id", "section_id", "school_id")),
    ("timetable_lesson_staff_school_fk", "timetable_lesson", ("staff_id", "school_id"),
     "people_staff_profile", ("id", "school_id")),
    ("timetable_lesson_subject_school_fk", "timetable_lesson", ("subject_id", "school_id"),
     "academics_subject", ("id", "school_id")),
    ("timetable_lesson_recorder_school_fk", "timetable_lesson", ("recorded_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
]

CASCADING = """
ALTER TABLE timetable_slot ADD CONSTRAINT timetable_slot_timetable_copy_fk
    FOREIGN KEY (timetable_id, academic_year_id, effective_from, effective_to, is_live, school_id)
    REFERENCES timetable_timetable (id, academic_year_id, effective_from, effective_to, is_live, school_id)
    ON UPDATE CASCADE;
ALTER TABLE timetable_slot ADD CONSTRAINT timetable_slot_period_copy_fk
    FOREIGN KEY (period_id, timetable_id, start_time, end_time, school_id)
    REFERENCES timetable_period (id, timetable_id, start_time, end_time, school_id)
    ON UPDATE CASCADE;
"""
CASCADING_REVERSE = """
ALTER TABLE timetable_slot DROP CONSTRAINT IF EXISTS timetable_slot_period_copy_fk;
ALTER TABLE timetable_slot DROP CONSTRAINT IF EXISTS timetable_slot_timetable_copy_fk;
"""

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [
        ("timetable", "0001_initial"),
        ("academics", "0004_term_room_integrity_and_rls"),
        ("people", "0003_assignment_slot_key"),
        ("core", "0001_database_roles"),
    ]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(CASCADING, CASCADING_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for the academic structure (docs/architecture/phase-3.md)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = [
    "academics_campus",
    "academics_academic_year",
    "academics_department",
    "academics_grade",
    "academics_section",
    "academics_subject",
]

FKS = [
    ("academics_section_year_school_fk", "academics_section", ("academic_year_id", "school_id"),
     "academics_academic_year", ("id", "school_id")),
    ("academics_section_grade_school_fk", "academics_section", ("grade_id", "school_id"),
     "academics_grade", ("id", "school_id")),
    ("academics_section_campus_school_fk", "academics_section", ("campus_id", "school_id"),
     "academics_campus", ("id", "school_id")),
    ("academics_subject_department_school_fk", "academics_subject", ("department_id", "school_id"),
     "academics_department", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("academics", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

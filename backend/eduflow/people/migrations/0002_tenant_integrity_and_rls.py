"""School-consistency foreign keys and RLS for people, enrollment and teaching (docs/architecture/phase-3.md).

The enrollment key ``(section_id, academic_year_id, grade_id, school_id)`` guarantees that an enrollment's
section belongs to its grade, its academic year and its school. Teacher assignments pin
``(section_id, academic_year_id, school_id)`` the same way.
"""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = [
    "people_staff_profile",
    "people_student",
    "people_guardian",
    "people_student_guardian",
    "people_enrollment",
    "people_teacher_assignment",
]

MEMBERSHIP = ("tenancy_membership", ("id", "school_id"))

FKS = [
    ("people_staff_membership_school_fk", "people_staff_profile", ("membership_id", "school_id"), *MEMBERSHIP),
    ("people_staff_department_school_fk", "people_staff_profile", ("department_id", "school_id"),
     "academics_department", ("id", "school_id")),
    ("people_staff_campus_school_fk", "people_staff_profile", ("campus_id", "school_id"),
     "academics_campus", ("id", "school_id")),
    ("people_student_membership_school_fk", "people_student", ("membership_id", "school_id"), *MEMBERSHIP),
    ("people_guardian_membership_school_fk", "people_guardian", ("membership_id", "school_id"), *MEMBERSHIP),
    ("people_link_student_school_fk", "people_student_guardian", ("student_id", "school_id"),
     "people_student", ("id", "school_id")),
    ("people_link_guardian_school_fk", "people_student_guardian", ("guardian_id", "school_id"),
     "people_guardian", ("id", "school_id")),
    ("people_enrollment_student_school_fk", "people_enrollment", ("student_id", "school_id"),
     "people_student", ("id", "school_id")),
    ("people_enrollment_section_key_fk", "people_enrollment",
     ("section_id", "academic_year_id", "grade_id", "school_id"),
     "academics_section", ("id", "academic_year_id", "grade_id", "school_id")),
    ("people_assignment_staff_school_fk", "people_teacher_assignment", ("staff_id", "school_id"),
     "people_staff_profile", ("id", "school_id")),
    ("people_assignment_section_key_fk", "people_teacher_assignment",
     ("section_id", "academic_year_id", "school_id"),
     "academics_section", ("id", "academic_year_id", "school_id")),
    ("people_assignment_subject_school_fk", "people_teacher_assignment", ("subject_id", "school_id"),
     "academics_subject", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [
        ("people", "0001_initial"),
        ("academics", "0002_tenant_integrity_and_rls"),
        ("core", "0001_database_roles"),
    ]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

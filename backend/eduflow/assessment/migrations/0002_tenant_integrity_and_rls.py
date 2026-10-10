"""School-consistency foreign keys and RLS for assessment (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['assessment_exam', 'assessment_mark_sheet', 'assessment_mark', 'assessment_mark_correction', 'assessment_grade_band']

FKS = [
    ('assessment_exam_academic_year_school_fk', 'assessment_exam', ('academic_year_id', 'school_id'), 'academics_academic_year', ('id', 'school_id')),
    ('assessment_exam_term_school_fk', 'assessment_exam', ('term_id', 'school_id'), 'academics_term', ('id', 'school_id')),
    ('assessment_mark_sheet_exam_school_fk', 'assessment_mark_sheet', ('exam_id', 'school_id'), 'assessment_exam', ('id', 'school_id')),
    ('assessment_mark_sheet_section_school_fk', 'assessment_mark_sheet', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('assessment_mark_sheet_subject_school_fk', 'assessment_mark_sheet', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('assessment_mark_sheet_teacher_school_fk', 'assessment_mark_sheet', ('teacher_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('assessment_mark_sheet_submitted_by_school_fk', 'assessment_mark_sheet', ('submitted_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('assessment_mark_sheet_decided_by_school_fk', 'assessment_mark_sheet', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('assessment_mark_sheet_school_fk', 'assessment_mark', ('sheet_id', 'school_id'), 'assessment_mark_sheet', ('id', 'school_id')),
    ('assessment_mark_student_school_fk', 'assessment_mark', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('assessment_mark_correction_mark_school_fk', 'assessment_mark_correction', ('mark_id', 'school_id'), 'assessment_mark', ('id', 'school_id')),
    ('assessment_mark_correction_requested_by_school_fk', 'assessment_mark_correction', ('requested_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('assessment_mark_correction_decided_by_school_fk', 'assessment_mark_correction', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("assessment", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

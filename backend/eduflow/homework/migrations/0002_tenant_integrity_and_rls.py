"""School-consistency foreign keys and RLS for homework (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['homework_homework', 'homework_submission']

FKS = [
    ('homework_homework_section_school_fk', 'homework_homework', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('homework_homework_subject_school_fk', 'homework_homework', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('homework_homework_teacher_school_fk', 'homework_homework', ('teacher_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('homework_homework_attachment_school_fk', 'homework_homework', ('attachment_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
    ('homework_homework_created_by_school_fk', 'homework_homework', ('created_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('homework_submission_homework_school_fk', 'homework_submission', ('homework_id', 'school_id'), 'homework_homework', ('id', 'school_id')),
    ('homework_submission_student_school_fk', 'homework_submission', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('homework_submission_file_school_fk', 'homework_submission', ('file_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
    ('homework_submission_reviewed_by_school_fk', 'homework_submission', ('reviewed_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("homework", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for lms (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['lms_quiz', 'lms_question', 'lms_attempt', 'lms_lesson', 'lms_progress', 'lms_learning_path', 'lms_path_step', 'lms_live_class', 'lms_live_attendance']

FKS = [
    ('lms_quiz_section_school_fk', 'lms_quiz', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('lms_quiz_subject_school_fk', 'lms_quiz', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('lms_quiz_author_school_fk', 'lms_quiz', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('lms_question_quiz_school_fk', 'lms_question', ('quiz_id', 'school_id'), 'lms_quiz', ('id', 'school_id')),
    ('lms_attempt_quiz_school_fk', 'lms_attempt', ('quiz_id', 'school_id'), 'lms_quiz', ('id', 'school_id')),
    ('lms_attempt_student_school_fk', 'lms_attempt', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('lms_lesson_section_school_fk', 'lms_lesson', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('lms_lesson_subject_school_fk', 'lms_lesson', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('lms_lesson_file_school_fk', 'lms_lesson', ('file_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
    ('lms_lesson_quiz_school_fk', 'lms_lesson', ('quiz_id', 'school_id'), 'lms_quiz', ('id', 'school_id')),
    ('lms_lesson_author_school_fk', 'lms_lesson', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('lms_progress_lesson_school_fk', 'lms_progress', ('lesson_id', 'school_id'), 'lms_lesson', ('id', 'school_id')),
    ('lms_progress_student_school_fk', 'lms_progress', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('lms_learning_path_section_school_fk', 'lms_learning_path', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('lms_learning_path_subject_school_fk', 'lms_learning_path', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('lms_learning_path_author_school_fk', 'lms_learning_path', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('lms_path_step_path_school_fk', 'lms_path_step', ('path_id', 'school_id'), 'lms_learning_path', ('id', 'school_id')),
    ('lms_path_step_lesson_school_fk', 'lms_path_step', ('lesson_id', 'school_id'), 'lms_lesson', ('id', 'school_id')),
    ('lms_live_class_section_school_fk', 'lms_live_class', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('lms_live_class_subject_school_fk', 'lms_live_class', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('lms_live_class_host_school_fk', 'lms_live_class', ('host_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('lms_live_attendance_live_class_school_fk', 'lms_live_attendance', ('live_class_id', 'school_id'), 'lms_live_class', ('id', 'school_id')),
    ('lms_live_attendance_student_school_fk', 'lms_live_attendance', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("lms", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for admissions (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['admissions_application', 'admissions_stage_change', 'admissions_document']

FKS = [
    ('admissions_application_grade_school_fk', 'admissions_application', ('grade_id', 'school_id'), 'academics_grade', ('id', 'school_id')),
    ('admissions_application_academic_year_school_fk', 'admissions_application', ('academic_year_id', 'school_id'), 'academics_academic_year', ('id', 'school_id')),
    ('admissions_application_offer_requested_by_school_fk', 'admissions_application', ('offer_requested_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('admissions_application_offer_decided_by_school_fk', 'admissions_application', ('offer_decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('admissions_application_student_school_fk', 'admissions_application', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('admissions_application_guardian_school_fk', 'admissions_application', ('guardian_id', 'school_id'), 'people_guardian', ('id', 'school_id')),
    ('admissions_application_created_by_school_fk', 'admissions_application', ('created_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('admissions_stage_change_application_school_fk', 'admissions_stage_change', ('application_id', 'school_id'), 'admissions_application', ('id', 'school_id')),
    ('admissions_stage_change_by_school_fk', 'admissions_stage_change', ('by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('admissions_document_application_school_fk', 'admissions_document', ('application_id', 'school_id'), 'admissions_application', ('id', 'school_id')),
    ('admissions_document_file_school_fk', 'admissions_document', ('file_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("admissions", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

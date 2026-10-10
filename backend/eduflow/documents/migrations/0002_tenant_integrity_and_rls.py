"""School-consistency foreign keys and RLS for documents (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['documents_stored_file', 'documents_document']

FKS = [
    ('documents_stored_file_uploaded_by_school_fk', 'documents_stored_file', ('uploaded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('documents_document_file_school_fk', 'documents_document', ('file_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
    ('documents_document_student_school_fk', 'documents_document', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('documents_document_staff_school_fk', 'documents_document', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('documents_document_uploaded_by_school_fk', 'documents_document', ('uploaded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

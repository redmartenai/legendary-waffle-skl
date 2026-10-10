"""School-consistency foreign keys and RLS for library (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['library_settings', 'library_book', 'library_copy', 'library_loan']

FKS = [
    ('library_copy_book_school_fk', 'library_copy', ('book_id', 'school_id'), 'library_book', ('id', 'school_id')),
    ('library_loan_copy_school_fk', 'library_loan', ('copy_id', 'school_id'), 'library_copy', ('id', 'school_id')),
    ('library_loan_student_school_fk', 'library_loan', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('library_loan_member_school_fk', 'library_loan', ('member_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('library_loan_issued_by_school_fk', 'library_loan', ('issued_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("library", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

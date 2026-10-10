"""School-consistency foreign keys and RLS for fees (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['fees_plan', 'fees_instalment', 'fees_student_fee', 'fees_receipt_counter', 'fees_payment', 'fees_refund']

FKS = [
    ('fees_plan_academic_year_school_fk', 'fees_plan', ('academic_year_id', 'school_id'), 'academics_academic_year', ('id', 'school_id')),
    ('fees_plan_grade_school_fk', 'fees_plan', ('grade_id', 'school_id'), 'academics_grade', ('id', 'school_id')),
    ('fees_instalment_plan_school_fk', 'fees_instalment', ('plan_id', 'school_id'), 'fees_plan', ('id', 'school_id')),
    ('fees_student_fee_student_school_fk', 'fees_student_fee', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('fees_student_fee_plan_school_fk', 'fees_student_fee', ('plan_id', 'school_id'), 'fees_plan', ('id', 'school_id')),
    ('fees_payment_student_school_fk', 'fees_payment', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('fees_payment_collected_by_school_fk', 'fees_payment', ('collected_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('fees_refund_payment_school_fk', 'fees_refund', ('payment_id', 'school_id'), 'fees_payment', ('id', 'school_id')),
    ('fees_refund_requested_by_school_fk', 'fees_refund', ('requested_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('fees_refund_decided_by_school_fk', 'fees_refund', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("fees", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

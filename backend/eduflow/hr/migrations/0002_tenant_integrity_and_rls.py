"""School-consistency foreign keys and RLS for hr (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['hr_settings', 'hr_staff_attendance', 'hr_leave_type', 'hr_leave_request', 'hr_salary_structure', 'hr_payroll_run', 'hr_payslip', 'hr_job_opening', 'hr_candidate']

FKS = [
    ('hr_staff_attendance_staff_school_fk', 'hr_staff_attendance', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('hr_staff_attendance_recorded_by_school_fk', 'hr_staff_attendance', ('recorded_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hr_leave_request_staff_school_fk', 'hr_leave_request', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('hr_leave_request_leave_type_school_fk', 'hr_leave_request', ('leave_type_id', 'school_id'), 'hr_leave_type', ('id', 'school_id')),
    ('hr_leave_request_decided_by_school_fk', 'hr_leave_request', ('decided_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hr_salary_structure_staff_school_fk', 'hr_salary_structure', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('hr_payroll_run_processed_by_school_fk', 'hr_payroll_run', ('processed_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('hr_payslip_run_school_fk', 'hr_payslip', ('run_id', 'school_id'), 'hr_payroll_run', ('id', 'school_id')),
    ('hr_payslip_staff_school_fk', 'hr_payslip', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('hr_job_opening_department_school_fk', 'hr_job_opening', ('department_id', 'school_id'), 'academics_department', ('id', 'school_id')),
    ('hr_candidate_opening_school_fk', 'hr_candidate', ('opening_id', 'school_id'), 'hr_job_opening', ('id', 'school_id')),
    ('hr_candidate_resume_school_fk', 'hr_candidate', ('resume_id', 'school_id'), 'documents_stored_file', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("hr", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

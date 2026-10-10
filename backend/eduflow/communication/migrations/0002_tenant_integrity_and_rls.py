"""School-consistency foreign keys and RLS for communication (generated from the models)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ['communication_announcement', 'communication_acknowledgement', 'communication_announcement_response', 'communication_thread', 'communication_message', 'communication_complaint']

FKS = [
    ('communication_announcement_section_school_fk', 'communication_announcement', ('section_id', 'school_id'), 'academics_section', ('id', 'school_id')),
    ('communication_announcement_author_school_fk', 'communication_announcement', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('communication_acknowledgement_announcement_school_fk', 'communication_acknowledgement', ('announcement_id', 'school_id'), 'communication_announcement', ('id', 'school_id')),
    ('communication_acknowledgement_member_school_fk', 'communication_acknowledgement', ('member_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('communication_announcement_response_announcement_school_fk', 'communication_announcement_response', ('announcement_id', 'school_id'), 'communication_announcement', ('id', 'school_id')),
    ('communication_announcement_response_member_school_fk', 'communication_announcement_response', ('member_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('communication_thread_student_school_fk', 'communication_thread', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('communication_thread_staff_school_fk', 'communication_thread', ('staff_id', 'school_id'), 'people_staff_profile', ('id', 'school_id')),
    ('communication_thread_guardian_school_fk', 'communication_thread', ('guardian_id', 'school_id'), 'people_guardian', ('id', 'school_id')),
    ('communication_thread_subject_school_fk', 'communication_thread', ('subject_id', 'school_id'), 'academics_subject', ('id', 'school_id')),
    ('communication_message_thread_school_fk', 'communication_message', ('thread_id', 'school_id'), 'communication_thread', ('id', 'school_id')),
    ('communication_message_author_school_fk', 'communication_message', ('author_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('communication_complaint_student_school_fk', 'communication_complaint', ('student_id', 'school_id'), 'people_student', ('id', 'school_id')),
    ('communication_complaint_raised_by_school_fk', 'communication_complaint', ('raised_by_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
    ('communication_complaint_assigned_to_school_fk', 'communication_complaint', ('assigned_to_id', 'school_id'), 'tenancy_membership', ('id', 'school_id')),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("communication", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

"""School-consistency foreign keys and RLS for branding (ADR-029).

A school's branding can only point at that school's own assets, and every member reference stays inside the
school. Public reads use the named, logged bypasses in selectors.py and hosts.py.
"""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ["branding_asset", "branding_school_branding", "branding_domain"]

FKS = [
    ("branding_school_branding_logo_school_fk", "branding_school_branding", ("logo_id", "school_id"),
     "branding_asset", ("id", "school_id")),
    ("branding_school_branding_favicon_school_fk", "branding_school_branding", ("favicon_id", "school_id"),
     "branding_asset", ("id", "school_id")),
    ("branding_school_branding_editor_school_fk", "branding_school_branding", ("updated_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
    ("branding_asset_uploader_school_fk", "branding_asset", ("uploaded_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
    ("branding_domain_creator_school_fk", "branding_domain", ("created_by_id", "school_id"),
     "tenancy_membership", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("branding", "0001_initial"), ("core", "0001_database_roles")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

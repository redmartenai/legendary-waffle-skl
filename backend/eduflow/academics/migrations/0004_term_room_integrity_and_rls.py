"""School-consistency foreign keys and RLS for terms and rooms (docs/architecture/phase-5.md)."""

from django.db import migrations

from eduflow.core.rls import same_school_fks, tenant_rls

TABLES = ["academics_term", "academics_room"]

FKS = [
    ("academics_term_year_school_fk", "academics_term", ("academic_year_id", "school_id"),
     "academics_academic_year", ("id", "school_id")),
    ("academics_room_campus_school_fk", "academics_room", ("campus_id", "school_id"),
     "academics_campus", ("id", "school_id")),
]

RLS_FORWARD, RLS_REVERSE = tenant_rls(TABLES)
FK_FORWARD, FK_REVERSE = same_school_fks(FKS)


class Migration(migrations.Migration):
    dependencies = [("academics", "0003_room_term")]
    operations = [
        migrations.RunSQL(FK_FORWARD, FK_REVERSE),
        migrations.RunSQL(RLS_FORWARD, RLS_REVERSE),
    ]

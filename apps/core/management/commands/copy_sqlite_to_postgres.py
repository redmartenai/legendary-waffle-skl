"""Copy every row from the local SQLite database into the Postgres ``default`` database, then upload the
files those rows point to.

Seeding straight into a remote Postgres (Supabase) makes one network round trip per row and takes hours;
seeding locally and copying in batches takes minutes. The target's tables are emptied first.
"""

from django.apps import apps
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, models, transaction

from apps.core.tenant import unscoped

BATCH = 1000


class Command(BaseCommand):
    help = "Replace the Postgres data with a copy of the local SQLite database (and upload its files)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Don't ask before emptying the target database")
        parser.add_argument("--skip-files", action="store_true", help="Copy rows only; don't upload files")

    def handle(self, *args, **options):
        if "sqlite" not in connections.databases or connections["default"].vendor != "postgresql":
            raise CommandError("Set DATABASE_URL to the Postgres target; the SQLite file is the source.")
        target = connections["default"]
        if not options["yes"]:
            answer = input(f"This empties every table in {target.settings_dict['HOST']} and copies SQLite into it. Type 'copy': ")
            if answer.strip() != "copy":
                raise CommandError("Cancelled.")

        found = [m for m in apps.get_models(include_auto_created=True) if m._meta.managed and not m._meta.proxy]
        existing = set(target.introspection.table_names())
        todo = [m for m in found if m._meta.db_table in existing]
        tables = [m._meta.db_table for m in todo]

        with unscoped(), transaction.atomic(using="default"):
            with target.cursor() as cursor:
                for sql in target.ops.sql_flush(no_style(), tables, reset_sequences=True, allow_cascade=True):
                    cursor.execute(sql)
            # Django's foreign keys are DEFERRABLE INITIALLY DEFERRED on Postgres, so table order doesn't matter
            # inside this one transaction.
            total = 0
            for model in todo:
                count = self._copy(model)
                total += count
                if count:
                    self.stdout.write(f"  {model._meta.label}: {count}")
            with target.cursor() as cursor:
                for sql in target.ops.sequence_reset_sql(no_style(), todo):
                    cursor.execute(sql)
        self.stdout.write(self.style.SUCCESS(f"Copied {total} rows from {len(todo)} tables."))

        if not options["skip_files"]:
            self._upload_files(todo)

    def _copy(self, model) -> int:
        source = model._base_manager.using("sqlite").order_by("pk")
        count = 0
        batch = []
        for obj in source.iterator(chunk_size=BATCH):
            batch.append(obj)
            if len(batch) >= BATCH:
                model._base_manager.using("default").bulk_create(batch)
                count += len(batch)
                batch = []
        if batch:
            model._base_manager.using("default").bulk_create(batch)
            count += len(batch)
        return count

    def _upload_files(self, todo):
        if isinstance(storages["default"], FileSystemStorage):
            self.stdout.write("Default storage is the local disk; no files to upload.")
            return
        local = FileSystemStorage()
        names = set()
        for model in todo:
            fields = [f.name for f in model._meta.fields if isinstance(f, models.FileField)]
            for field in fields:
                with unscoped():
                    names.update(n for n in model._base_manager.using("default").exclude(**{field: ""}).values_list(field, flat=True) if n)
        uploaded = missing = 0
        for name in sorted(names):
            if not local.exists(name):
                missing += 1
                continue
            if default_storage.exists(name):
                continue
            with local.open(name, "rb") as fh:
                saved = default_storage.save(name, File(fh))
            if saved != name:
                raise CommandError(f"Storage renamed {name} to {saved}; is file_overwrite on?")
            uploaded += 1
        self.stdout.write(self.style.SUCCESS(f"Uploaded {uploaded} files ({len(names)} referenced, {missing} missing locally)."))

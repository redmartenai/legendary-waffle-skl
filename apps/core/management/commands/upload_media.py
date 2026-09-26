"""Copy the local media/ folder into the configured storage (Supabase Storage), keeping each file's path."""

from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Upload every file under MEDIA_ROOT to the default storage (skips files already there)."

    def add_arguments(self, parser):
        parser.add_argument("--source", default=str(settings.MEDIA_ROOT), help="Folder to upload (default: MEDIA_ROOT)")

    def handle(self, *args, **options):
        if isinstance(storages["default"], FileSystemStorage):
            raise CommandError("Default storage is the local disk. Set the SUPABASE_S3_* variables first.")
        root = Path(options["source"])
        if not root.is_dir():
            raise CommandError(f"{root} is not a folder.")
        uploaded = skipped = 0
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            name = path.relative_to(root).as_posix()
            if default_storage.exists(name):
                skipped += 1
                continue
            with path.open("rb") as fh:
                default_storage.save(name, File(fh))
            uploaded += 1
        self.stdout.write(f"Uploaded {uploaded} file(s); {skipped} already there.")

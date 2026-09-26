"""Send scheduled announcements whose time has come. Run every minute from cron (or a task runner)."""

from django.core.management.base import BaseCommand

from apps.announcements.delivery import deliver_due
from apps.core.tenant import use_school
from apps.tenancy.models import School


class Command(BaseCommand):
    help = "Deliver scheduled announcements that are due."

    def handle(self, *args, **options):
        total = 0
        for school in School.objects.all():
            with use_school(school):
                total += deliver_due()
        self.stdout.write(f"Delivered {total} announcement(s).")

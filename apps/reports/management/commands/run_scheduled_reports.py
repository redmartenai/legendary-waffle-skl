"""Generate and deliver the scheduled reports that are due. Run every few minutes from cron (or a task runner)."""

from django.core.management.base import BaseCommand

from apps.core.tenant import use_school
from apps.reports.services import run_due
from apps.tenancy.models import School


class Command(BaseCommand):
    help = "Generate scheduled reports that are due and deliver them in-app (and by email where set up)."

    def handle(self, *args, **options):
        total = 0
        for school in School.objects.all():
            with use_school(school):
                total += run_due()
        self.stdout.write(f"Delivered {total} scheduled report(s).")

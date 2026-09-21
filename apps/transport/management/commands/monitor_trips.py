import time

from django.core.management.base import BaseCommand

from apps.transport.services import monitor_active_trips


class Command(BaseCommand):
    help = "Check running trips for lost GPS signal and close forgotten trips. Run every minute."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, checking every 60 seconds")

    def handle(self, *args, **options):
        while True:
            summary = monitor_active_trips()
            self.stdout.write(str(summary))
            if not options["loop"]:
                break
            time.sleep(60)

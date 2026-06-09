"""Run due HydroDesk schedules (refresh computed fields + raise threshold alerts).

The production trigger: add to cron, e.g. every minute
    * * * * * cd <portal> && tethys db ... ; python manage.py hydrodesk_run_schedules
(the in-app ticker is a dev convenience; cron is authoritative).
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run all DUE HydroDesk schedules (refresh + threshold alerts)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true",
                            help="Run every ENABLED schedule regardless of its interval.")

    def handle(self, *args, **options):
        from tethysapp.hydrodesk.controllers import _run_due_schedules
        summary = _run_due_schedules(force=options.get("force"))
        self.stdout.write("ran %d schedule(s):" % len(summary))
        for s in summary:
            self.stdout.write("  " + str(s))

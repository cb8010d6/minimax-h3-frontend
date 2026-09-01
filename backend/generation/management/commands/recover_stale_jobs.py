"""Run once at qcluster container startup (see docker-compose.yml's
qcluster command) so a job orphaned by a previous restart -- see
generation.tasks.recover_orphaned_processing_jobs()'s docstring -- gets
recovered immediately. This command must run before qcluster starts, never
beside live queue workers.
"""

from django.core.management.base import BaseCommand

from generation.tasks import recover_orphaned_processing_jobs


class Command(BaseCommand):
    help = __doc__

    def handle(self, *args, **options):
        recover_orphaned_processing_jobs()
        self.stdout.write(self.style.SUCCESS("Done."))

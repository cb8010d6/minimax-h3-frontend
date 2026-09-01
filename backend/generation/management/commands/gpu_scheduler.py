import time

from django.core.management.base import BaseCommand
from django_q.tasks import async_task

from generation import gpu_scheduler
from generation.models import GenerationJob, GpuWorker


class Command(BaseCommand):
    help = "Continuously refresh all GPUs, reap idle models, and wake queued jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=5.0)

    def handle(self, *args, **options):
        while True:
            gpu_scheduler.refresh_inventory()
            gpu_scheduler.reap_idle_models()
            queued = GenerationJob.objects.filter(status=GenerationJob.Status.QUEUED).count()
            available = GpuWorker.objects.filter(
                current_job__isnull=True,
                state__in=[GpuWorker.State.FREE, GpuWorker.State.STANDBY, GpuWorker.State.READY],
            ).count()
            for _ in range(min(queued, available)):
                async_task("generation.tasks.process_queue")
            if options["once"]:
                return
            time.sleep(options["interval"])

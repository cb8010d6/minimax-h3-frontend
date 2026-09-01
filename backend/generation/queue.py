"""Privacy-preserving queue ETA estimation for the dynamic GPU pool."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from .models import GenerationJob, GpuWorker

def _remaining_finish(job: GenerationJob, now: datetime) -> datetime:
    if job.started_at:
        return max(now, job.started_at + timedelta(seconds=job.estimated_seconds))
    return now + timedelta(seconds=job.estimated_seconds)


def _schedule() -> tuple[datetime, list[datetime], dict[int, datetime]]:
    """List-schedule queued jobs across currently usable physical GPUs."""
    now = timezone.now()
    processing = list(
        GenerationJob.objects.filter(status=GenerationJob.Status.PROCESSING)
        .select_related("assigned_worker")
        .order_by("created_at", "id")
    )
    queued = list(
        GenerationJob.objects.filter(status=GenerationJob.Status.QUEUED).order_by(
            "created_at", "id"
        )
    )
    slots = [_remaining_finish(job, now) for job in processing]
    idle_count = GpuWorker.objects.filter(
        current_job__isnull=True,
        state__in=[GpuWorker.State.FREE, GpuWorker.State.STANDBY, GpuWorker.State.READY],
    ).count()
    slots.extend([now] * idle_count)

    # Before the first inventory/migration, retain the old single-worker ETA.
    if not slots:
        slots = [now]

    result = {job.id: _remaining_finish(job, now) for job in processing}
    for job in queued:
        slot_index = min(range(len(slots)), key=slots.__getitem__)
        slots[slot_index] = slots[slot_index] + timedelta(seconds=job.estimated_seconds)
        result[job.id] = slots[slot_index]
    return now, slots, result


def estimated_seconds_ahead() -> int:
    """Seconds until a newly appended job could start on the first GPU."""
    now, slots, _ = _schedule()
    return round(max(0.0, (min(slots) - now).total_seconds()))


def estimated_finish_time(additional_seconds: int):
    return timezone.now() + timedelta(seconds=estimated_seconds_ahead() + additional_seconds)


def expected_finish_times() -> dict[int, datetime]:
    """Estimated completion time for each active job without exposing peers."""
    return _schedule()[2]

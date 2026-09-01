"""Dynamic GPU inventory, leases, warm-model preference, and safe control."""

from __future__ import annotations

import json
import logging
import struct
import subprocess
import uuid
import zlib
from binascii import crc32
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import comfyui

from .models import GenerationJob, GpuWorker, Mode, ModelVariant, REFERENCE_FLOW_MODES

logger = logging.getLogger(__name__)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", crc32(kind + payload) & 0xFFFFFFFF)
    )


def prewarm_reference_png() -> bytes:
    """Return a strict, decoder-portable 64x64 RGB PNG for Ref2VA warm-up."""
    width = height = 64
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + (b"\x80\x80\x80" * width)
    pixels = scanline * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(pixels, level=9))
        + _png_chunk(b"IEND", b"")
    )


def model_family(mode: str) -> str:
    return "ref2va" if mode in REFERENCE_FLOW_MODES else "fl2va"


def model_key(mode: str, variant: str) -> str:
    return f"{model_family(mode)}:{variant}"


def model_available(mode: str, variant: str) -> bool:
    return model_key(mode, variant) in settings.GPU_AVAILABLE_MODELS


def model_filename(mode: str, variant: str) -> str:
    family = model_family(mode)
    suffix = "fp8_scaled" if variant == ModelVariant.FP8 else "int8_convrot"
    return f"minimax_h3_{family}_pruned_{suffix}.safetensors"


def _remote(host: str, action: str, gpu: int | None = None, timeout: int = 150) -> dict | list:
    command = [
        settings.GPU_WORKER_SSH_BIN,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        host,
        "python3",
        settings.GPU_WORKER_CONTROLLER,
        action,
    ]
    if gpu is not None:
        command.extend(["--gpu", str(gpu)])
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def refresh_inventory() -> list[GpuWorker]:
    """Discover every physical GPU. No index or host is statically reserved."""
    seen: set[str] = set()
    now = timezone.now()
    for host in settings.GPU_WORKER_HOSTS:
        try:
            rows = _remote(host, "inventory", timeout=30)
        except Exception as exc:  # noqa: BLE001 - persisted for operators
            logger.warning("GPU inventory failed for %s: %s", host, exc)
            GpuWorker.objects.filter(host=host).update(state=GpuWorker.State.OFFLINE, last_error=str(exc))
            continue
        for row in rows:
            seen.add(row["uuid"])
            worker, _ = GpuWorker.objects.get_or_create(
                gpu_uuid=row["uuid"],
                defaults={"host": host, "cuda_index": row["index"], "port": row["port"]},
            )
            previous_managed_pid = worker.managed_pid
            reported_managed_pid = row.get("managed_pid")
            # A fresh ComfyUI process cannot still have the model weights
            # remembered for the old PID.  Clear the DB hint before deriving
            # READY/STANDBY so a process restart never produces a false READY.
            if previous_managed_pid and previous_managed_pid != reported_managed_pid:
                worker.loaded_model = ""
            has_external = bool(row.get("external_processes"))
            if worker.current_job_id:
                state = GpuWorker.State.BUSY
            elif has_external:
                state = GpuWorker.State.EXTERNAL
            elif worker.state == GpuWorker.State.STARTING:
                # A prewarm intentionally has no GenerationJob lease. Keep it
                # unavailable until prewarm() publishes READY/ERROR; otherwise
                # an inventory refresh could hand the card to a real job while
                # the one-step warm-up prompt is still running.
                state = GpuWorker.State.STARTING
            elif row.get("managed_running") and row.get("healthy"):
                state = (
                    GpuWorker.State.READY
                    if worker.loaded_model
                    else GpuWorker.State.STANDBY
                )
            else:
                state = GpuWorker.State.FREE
                worker.loaded_model = ""
            worker.host = host
            worker.cuda_index = row["index"]
            worker.port = row["port"]
            worker.name = row["name"]
            worker.state = state
            worker.managed_pid = reported_managed_pid
            worker.memory_used_mb = row.get("memory_used_mb", 0)
            worker.memory_total_mb = row.get("memory_total_mb", 0)
            worker.utilization_percent = row.get("utilization_percent", 0)
            worker.last_seen_at = now
            worker.last_error = ""
            worker.save()
    if seen:
        GpuWorker.objects.exclude(gpu_uuid__in=seen).update(state=GpuWorker.State.OFFLINE)
    return list(GpuWorker.objects.all())


def _candidate_priority(worker: GpuWorker, wanted_model: str) -> tuple:
    """Prefer warm weights, then an already-running healthy ComfyUI worker.

    A STANDBY worker is cheaper and safer to use than a FREE worker: its
    managed PID and HTTP endpoint are already alive even though no model has
    been confirmed loaded.
    The previous ordering considered only ``last_used_at`` and therefore put
    never-started (NULL timestamp) GPUs ahead of READY GPUs after their model
    had been unloaded. That made jobs unnecessarily skip the warm worker pool
    and start gpu2/gpu3/etc. first.
    """
    return (
        worker.loaded_model != wanted_model,
        worker.state not in [GpuWorker.State.READY, GpuWorker.State.STANDBY],
        worker.last_used_at is not None,
        worker.last_used_at or timezone.now(),
    )


def lease_worker(job) -> GpuWorker | None:
    """Atomically lease one truly available GPU, preferring the warm model."""
    wanted = model_key(job.mode, job.model_variant)
    with transaction.atomic():
        candidates = list(
            GpuWorker.objects.select_for_update(skip_locked=True)
            .filter(
                current_job__isnull=True,
                state__in=[GpuWorker.State.FREE, GpuWorker.State.STANDBY, GpuWorker.State.READY],
            )
            .order_by("host", "cuda_index")
        )
        candidates.sort(key=lambda worker: _candidate_priority(worker, wanted))
        if not candidates:
            return None
        worker = candidates[0]
        worker.current_job = job
        worker.state = GpuWorker.State.STARTING
        worker.save(update_fields=["current_job", "state"])
        job.assigned_worker = worker
        job.save(update_fields=["assigned_worker"])
        return worker


def ensure_started(worker: GpuWorker) -> GpuWorker:
    try:
        row = _remote(worker.host, "start", worker.cuda_index)
    except Exception as exc:
        cleanup_error = ""
        try:
            cleanup_worker_cache(worker)
        except Exception as cleanup_exc:  # noqa: BLE001
            cleanup_error = f"; RAM cache cleanup also failed: {cleanup_exc}"
        release_worker(worker, error=f"{exc}{cleanup_error}")
        raise
    worker.managed_pid = row.get("managed_pid")
    worker.state = GpuWorker.State.BUSY if worker.current_job_id else GpuWorker.State.STARTING
    worker.last_seen_at = timezone.now()
    worker.last_error = ""
    worker.save(update_fields=["managed_pid", "state", "last_seen_at", "last_error"])
    return worker


def release_worker(worker: GpuWorker, *, loaded_model: str | None = None, error: str = "") -> None:
    worker.refresh_from_db()
    worker.current_job = None
    worker.state = (
        GpuWorker.State.ERROR
        if error
        else (GpuWorker.State.READY if loaded_model else GpuWorker.State.STANDBY)
    )
    worker.last_used_at = timezone.now()
    worker.last_error = error
    # ``loaded_model`` is evidence, not intent: callers pass it only after a
    # prompt completed successfully.  A failed/cancelled render is treated
    # conservatively because ComfyUI may have failed before loading weights.
    worker.loaded_model = loaded_model or ""
    worker.save(update_fields=["current_job", "state", "last_used_at", "last_error", "loaded_model"])


def cleanup_worker_cache(worker: GpuWorker) -> None:
    """Clear this card's input/output/temp RAM cache after a task finishes."""
    _remote(worker.host, "cleanup", worker.cuda_index, timeout=45)


def unload_worker(worker: GpuWorker) -> None:
    """Fully release an idle managed GPU, including shared model components.

    ComfyUI's ``/free`` endpoint unloads the selected diffusion model but can
    retain the text encoder/VAE and roughly half of an L20's VRAM.  Stopping
    only this controller-owned process is the reliable definition of unload;
    the next lease starts a fresh worker.
    """
    worker.refresh_from_db()
    if worker.current_job_id:
        raise RuntimeError("Cannot unload a GPU while it is rendering")
    row = _remote(worker.host, "stop", worker.cuda_index, timeout=60)
    worker.loaded_model = ""
    worker.managed_pid = row.get("managed_pid")
    worker.state = (
        GpuWorker.State.EXTERNAL
        if row.get("external_processes")
        else GpuWorker.State.FREE
    )
    worker.memory_used_mb = row.get("memory_used_mb", 0)
    worker.utilization_percent = row.get("utilization_percent", 0)
    worker.last_used_at = timezone.now()
    worker.save(
        update_fields=[
            "loaded_model",
            "managed_pid",
            "state",
            "memory_used_mb",
            "utilization_percent",
            "last_used_at",
        ]
    )


def prewarm(family: str, variant: str) -> GpuWorker:
    """Start one idle worker and execute a one-step render to really load weights."""
    if family not in {"fl2va", "ref2va"} or variant not in ModelVariant.values:
        raise ValueError("Unsupported model family or variant")
    wanted = f"{family}:{variant}"
    if wanted not in settings.GPU_AVAILABLE_MODELS:
        raise RuntimeError(f"{wanted} is not installed in this deployment")
    with transaction.atomic():
        candidates = list(
            GpuWorker.objects.select_for_update(skip_locked=True)
            .filter(
                current_job__isnull=True,
                state__in=[GpuWorker.State.FREE, GpuWorker.State.STANDBY, GpuWorker.State.READY],
            )
            .order_by("host", "cuda_index")
        )
        candidates.sort(key=lambda worker: _candidate_priority(worker, wanted))
        if not candidates:
            raise RuntimeError("No idle GPU is currently available")
        worker = candidates[0]
        if worker.loaded_model == wanted:
            return worker
        worker.state = GpuWorker.State.STARTING
        worker.save(update_fields=["state"])

    mode = Mode.REFERENCE_TO_VIDEO if family == "ref2va" else Mode.TEXT_TO_VIDEO
    try:
        ensure_started(worker)
        from .tasks import SAVE_VIDEO_NODE_ID, build_api_workflow

        with comfyui.use_base_url(worker.base_url):
            refs = None
            if family == "ref2va":
                refs = [comfyui.upload_media(prewarm_reference_png(), "scheduler-prewarm.png")]
            workflow = build_api_workflow(
                mode,
                width=256,
                height=256,
                duration_seconds=1,
                steps=1,
                prompt_text="neutral test frame",
                ref_image_uploads=refs,
                model_variant=variant,
            )
            prompt_id = comfyui.queue_prompt(workflow, str(uuid.uuid4()))
            history = comfyui.wait_for_result(prompt_id, timeout=900)
            comfyui.check_for_error(history)
            output = comfyui.extract_video_output(history, SAVE_VIDEO_NODE_ID)
            comfyui.delete_output_file(output)
            comfyui.clear_history(prompt_id)
        cleanup_worker_cache(worker)
        release_worker(worker, loaded_model=wanted)
        return GpuWorker.objects.get(pk=worker.pk)
    except Exception as exc:
        cleanup_error = ""
        try:
            cleanup_worker_cache(worker)
        except Exception as cleanup_exc:  # noqa: BLE001
            cleanup_error = f"; RAM cache cleanup also failed: {cleanup_exc}"
        release_worker(worker, error=f"{exc}{cleanup_error}")
        raise


def reap_idle_models() -> int:
    cutoff = timezone.now() - timedelta(seconds=settings.GPU_MODEL_IDLE_SECONDS)
    count = 0
    for worker in GpuWorker.objects.filter(
        current_job__isnull=True,
        state__in=[GpuWorker.State.READY, GpuWorker.State.STANDBY],
        managed_pid__isnull=False,
        last_used_at__lt=cutoff,
    ):
        queued_compatible = False
        if worker.loaded_model:
            family, variant = worker.loaded_model.split(":", 1)
            non_reference_modes = {
                choice.value for choice in Mode if choice.value not in REFERENCE_FLOW_MODES
            }
            queued_compatible = GenerationJob.objects.filter(
                status="queued",
                model_variant=variant,
                mode__in=(
                    REFERENCE_FLOW_MODES if family == "ref2va" else non_reference_modes
                ),
            ).exists()
        if not queued_compatible:
            try:
                unload_worker(worker)
                count += 1
            except Exception as exc:  # noqa: BLE001
                worker.last_error = str(exc)
                worker.save(update_fields=["last_error"])
    return count

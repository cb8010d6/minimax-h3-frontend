import json
import struct
import sys
import types
import zlib
from binascii import crc32
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from generation import gpu_scheduler, tasks as generation_tasks
from generation.model_capabilities import capability_for, is_duration_supported
from generation.models import (
    GenerationJob,
    GpuWorker,
    Mode,
    ModelVariant,
    ReferenceAsset,
    RenderDuration,
    RenderPreset,
    ResolutionPolicy,
)
from generation.resolution import compute_h3_native_resolution, compute_resolution
from generation.reference_tokens import expected_primary_reference_tokens, invalid_reference_tokens
from generation.tasks import build_api_workflow
from integrations import comfyui, media_post
from integrations.spectrum import apply_spectrum

# Read the source workflow directly for graph-shape tests. The settings test
# below separately locks down packaged Docker and bare-repository discovery.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class ResourcesSettingsTests(SimpleTestCase):
    def test_default_resources_dir_contains_api_workflows(self):
        self.assertTrue(
            (settings.RESOURCES_DIR / "workflows_api" / "video_minimax_h3_r2v.api.json").is_file()
        )


class ResolutionPreviewTests(TestCase):
    def setUp(self):
        self.native = RenderPreset.objects.create(
            mode=Mode.TEXT_TO_VIDEO,
            label="Native test",
            megapixels=0.98,
            steps=20,
            resolution_policy=ResolutionPolicy.H3_NATIVE,
        )

    def test_matches_official_comfy_resolution_rounding(self):
        self.assertEqual(compute_resolution(0.6, "16:9"), (1056, 608))

    def test_native_16_9_is_1344_by_768(self):
        self.assertEqual(compute_h3_native_resolution("16:9"), (1344, 768))
        response = self.client.get(
            "/api/resolution-preview/",
            {"preset_id": self.native.id, "aspect_ratio": "16:9", "model_variant": "fp8"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["width"], 1344)
        self.assertEqual(response.json()["height"], 768)
        self.assertEqual(response.json()["max_duration_seconds"], 15)

    def test_supports_custom_first_frame_ratio(self):
        response = self.client.get(
            "/api/resolution-preview/",
            {"preset_id": self.native.id, "aspect_ratio": "1366:768"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["width"] % 32, 0)
        self.assertEqual(response.json()["height"] % 32, 0)

    def test_rejects_invalid_inputs(self):
        self.assertEqual(
            self.client.get(
                "/api/resolution-preview/",
                {"preset_id": "bad", "aspect_ratio": "16:9"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/api/resolution-preview/",
                {"preset_id": self.native.id, "aspect_ratio": "bad"},
            ).status_code,
            400,
        )

    @override_settings(GPU_AVAILABLE_MODELS={"fl2va:int8"})
    def test_job_submission_rejects_untrained_20_second_duration(self):
        preset = RenderPreset.objects.create(
            mode=Mode.TEXT_TO_VIDEO,
            label="Legacy 20 second test",
            megapixels=0.2,
            steps=20,
        )
        duration = RenderDuration.objects.create(
            preset=preset,
            duration_seconds=20,
            estimated_render_seconds=10,
        )
        user = get_user_model().objects.create_user(username="capability-test", password="unused")
        self.client.force_login(user)
        response = self.client.post(
            "/api/jobs/",
            {
                "mode": Mode.TEXT_TO_VIDEO,
                "duration_id": duration.id,
                "aspect_ratio": "16:9",
                "raw_prompt": "test",
                "model_variant": ModelVariant.INT8,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("5-15 seconds", response.json()["error"])

    def test_fp8_and_int8_share_reviewed_duration_cap(self):
        for variant in (ModelVariant.FP8, ModelVariant.INT8):
            capability = capability_for(Mode.TEXT_TO_VIDEO, variant)
            self.assertTrue(is_duration_supported(Mode.TEXT_TO_VIDEO, 15, capability))
            self.assertFalse(is_duration_supported(Mode.TEXT_TO_VIDEO, 16, capability))


class SchedulerLeaseTests(TestCase):
    def test_busy_worker_api_reports_active_model_without_claiming_it_is_confirmed(self):
        user = get_user_model().objects.create_user(username="gpu-active-model-test")
        preset = RenderPreset.objects.create(
            mode=Mode.REFERENCE_TO_VIDEO,
            label="Active model test",
            megapixels=0.2,
            steps=20,
        )
        duration = RenderDuration.objects.create(
            preset=preset,
            duration_seconds=5,
            estimated_render_seconds=10,
        )
        job = GenerationJob.objects.create(
            user=user,
            mode=Mode.REFERENCE_TO_VIDEO,
            preset=preset,
            duration=duration,
            raw_prompt="active model test",
            megapixels=0.2,
            steps=20,
            aspect_ratio="16:9",
            width=608,
            height=352,
            duration_seconds=5,
            estimated_seconds=10,
            model_variant=ModelVariant.INT8,
            status=GenerationJob.Status.PROCESSING,
        )
        GpuWorker.objects.create(
            host="gpu01",
            cuda_index=0,
            gpu_uuid="GPU-active-model",
            port=18100,
            state=GpuWorker.State.BUSY,
            current_job=job,
            loaded_model="",
        )
        self.client.force_login(user)

        response = self.client.get("/api/gpu/workers/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["loaded_model"], "")
        self.assertEqual(response.json()[0]["active_model"], "ref2va:int8")

    def test_prefers_already_running_ready_worker_over_never_started_free_worker(self):
        user = get_user_model().objects.create_user(username="scheduler-lease-test")
        preset = RenderPreset.objects.create(
            mode=Mode.REFERENCE_TO_VIDEO,
            label="Lease test",
            megapixels=0.2,
            steps=20,
        )
        duration = RenderDuration.objects.create(
            preset=preset,
            duration_seconds=5,
            estimated_render_seconds=10,
        )
        job = GenerationJob.objects.create(
            user=user,
            mode=Mode.REFERENCE_TO_VIDEO,
            preset=preset,
            duration=duration,
            raw_prompt="lease test",
            megapixels=0.2,
            steps=20,
            aspect_ratio="16:9",
            width=608,
            height=352,
            duration_seconds=5,
            estimated_seconds=10,
        )
        ready = GpuWorker.objects.create(
            host="gpu01",
            cuda_index=0,
            gpu_uuid="GPU-ready",
            port=18100,
            state=GpuWorker.State.STANDBY,
            managed_pid=1234,
            last_used_at=timezone.now(),
        )
        GpuWorker.objects.create(
            host="gpu01",
            cuda_index=2,
            gpu_uuid="GPU-free",
            port=18102,
            state=GpuWorker.State.FREE,
        )

        leased = gpu_scheduler.lease_worker(job)

        self.assertEqual(leased.id, ready.id)

    def test_failed_release_clears_stale_model_and_never_reports_ready(self):
        worker = GpuWorker.objects.create(
            host="gpu01",
            cuda_index=0,
            gpu_uuid="GPU-failed-release",
            port=18100,
            state=GpuWorker.State.READY,
            loaded_model="ref2va:fp8",
        )

        gpu_scheduler.release_worker(worker, error="prompt failed")

        worker.refresh_from_db()
        self.assertEqual(worker.state, GpuWorker.State.ERROR)
        self.assertEqual(worker.loaded_model, "")

    @mock.patch("generation.gpu_scheduler._remote")
    def test_unload_fully_stops_managed_worker(self, remote):
        remote.return_value = {
            "managed_pid": None,
            "external_processes": [],
            "memory_used_mb": 9,
            "utilization_percent": 0,
        }
        worker = GpuWorker.objects.create(
            host="gpu02",
            cuda_index=1,
            gpu_uuid="GPU-full-unload",
            port=18101,
            state=GpuWorker.State.READY,
            managed_pid=123,
            loaded_model="ref2va:fp8",
            memory_used_mb=40903,
        )

        gpu_scheduler.unload_worker(worker)

        remote.assert_called_once_with("gpu02", "stop", 1, timeout=60)
        worker.refresh_from_db()
        self.assertEqual(worker.state, GpuWorker.State.FREE)
        self.assertIsNone(worker.managed_pid)
        self.assertEqual(worker.loaded_model, "")
        self.assertEqual(worker.memory_used_mb, 9)

    @override_settings(GPU_WORKER_HOSTS=["gpu01"])
    @mock.patch("generation.gpu_scheduler._remote")
    def test_process_pid_change_clears_stale_model(self, remote):
        worker = GpuWorker.objects.create(
            host="gpu01",
            cuda_index=0,
            gpu_uuid="GPU-pid-change",
            port=18100,
            state=GpuWorker.State.READY,
            managed_pid=111,
            loaded_model="ref2va:int8",
        )
        remote.return_value = [
            {
                "index": 0,
                "uuid": worker.gpu_uuid,
                "name": "Test GPU",
                "memory_used_mb": 500,
                "memory_total_mb": 1000,
                "utilization_percent": 0,
                "managed_pid": 222,
                "managed_running": True,
                "external_processes": [],
                "port": 18100,
                "healthy": True,
            }
        ]

        gpu_scheduler.refresh_inventory()

        worker.refresh_from_db()
        self.assertEqual(worker.state, GpuWorker.State.STANDBY)
        self.assertEqual(worker.loaded_model, "")

    @override_settings(GPU_MODEL_IDLE_SECONDS=180)
    @mock.patch("generation.gpu_scheduler.unload_worker")
    def test_reaps_idle_standby_worker_that_still_has_a_managed_process(self, unload_worker):
        worker = GpuWorker.objects.create(
            host="gpu01",
            cuda_index=0,
            gpu_uuid="GPU-idle-standby",
            port=18100,
            state=GpuWorker.State.STANDBY,
            managed_pid=123,
            loaded_model="",
            last_used_at=timezone.now() - timedelta(seconds=181),
        )

        count = gpu_scheduler.reap_idle_models()

        self.assertEqual(count, 1)
        unload_worker.assert_called_once_with(worker)


class QueueWorkerLifetimeTests(SimpleTestCase):
    @mock.patch("generation.tasks._execute_job")
    @mock.patch("generation.tasks._claim_next_job")
    @mock.patch("generation.tasks.gpu_scheduler.refresh_inventory")
    def test_one_q_task_executes_at_most_one_generation_job(
        self, _refresh_inventory, claim_next_job, execute_job
    ):
        first = mock.sentinel.first_job
        second = mock.sentinel.second_job
        claim_next_job.side_effect = [first, second]

        generation_tasks.process_queue()

        execute_job.assert_called_once_with(first)
        claim_next_job.assert_called_once_with()

    def test_qcluster_hard_timeout_exceeds_the_longest_comfyui_wait(self):
        self.assertGreater(
            settings.Q_CLUSTER["timeout"],
            settings.COMFYUI_MAX_RENDER_TIMEOUT,
        )


class ReferenceProtectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="reference-protection")
        self.preset = RenderPreset.objects.create(
            mode=Mode.REFERENCE_TO_VIDEO,
            label="Reference protection",
            megapixels=0.2,
            steps=20,
        )
        self.duration = RenderDuration.objects.create(
            preset=self.preset,
            duration_seconds=5,
            estimated_render_seconds=10,
        )
        self.client.force_login(self.user)

    def _job(self):
        return GenerationJob.objects.create(
            user=self.user,
            mode=Mode.REFERENCE_TO_VIDEO,
            preset=self.preset,
            duration=self.duration,
            raw_prompt="test",
            megapixels=0.2,
            steps=20,
            aspect_ratio="16:9",
            width=608,
            height=352,
            duration_seconds=5,
            estimated_seconds=10,
        )

    def test_reference_token_ranges_include_video_soundtracks(self):
        prompt = "<Picture 1> <Video 1> <Audio 1> <Audio 2>"
        self.assertEqual(
            invalid_reference_tokens(prompt, image_count=1, video_count=1, audio_count=1),
            [],
        )
        self.assertEqual(
            invalid_reference_tokens(
                "<Picture 2> <Video 0> <Audio 3> <Audio x>",
                image_count=1,
                video_count=1,
                audio_count=1,
            ),
            ["<Picture 2>", "<Video 0>", "<Audio 3>", "<Audio x>"],
        )
        self.assertEqual(
            expected_primary_reference_tokens(image_count=1, video_count=1, audio_count=1),
            ["<Picture 1>", "<Video 1>", "<Audio 2>"],
        )

    def test_standalone_audio_label_follows_video_soundtrack_ordinals(self):
        job = self._job()
        ReferenceAsset.objects.create(
            job=job,
            kind=ReferenceAsset.Kind.VIDEO,
            order=0,
            file=SimpleUploadedFile("clip.mp4", b"video", content_type="video/mp4"),
        )
        audio = ReferenceAsset.objects.create(
            job=job,
            kind=ReferenceAsset.Kind.AUDIO,
            order=0,
            file=SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav"),
        )
        self.assertEqual(audio.label, "Audio 2")

    def test_job_api_rejects_out_of_range_reference_token(self):
        response = self.client.post(
            "/api/jobs/",
            {
                "mode": Mode.REFERENCE_TO_VIDEO,
                "duration_id": self.duration.id,
                "aspect_ratio": "16:9",
                "raw_prompt": "use <Picture 2>",
                "model_variant": ModelVariant.FP8,
                "reference_images": SimpleUploadedFile(
                    "face.png", b"png", content_type="image/png"
                ),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("<Picture 2>", response.json()["error"])

    @mock.patch("generation.api.media_post.probe_uploaded_duration", return_value=16.1)
    def test_job_api_rejects_reference_video_longer_than_15_seconds(self, _probe):
        response = self.client.post(
            "/api/jobs/",
            {
                "mode": Mode.REFERENCE_TO_VIDEO,
                "duration_id": self.duration.id,
                "aspect_ratio": "16:9",
                "raw_prompt": "use <Video 1>",
                "model_variant": ModelVariant.INT8,
                "reference_video": SimpleUploadedFile(
                    "long.mp4", b"video", content_type="video/mp4"
                ),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("2-15s", response.json()["error"])


class FfmpegExecutableTests(SimpleTestCase):
    @mock.patch("integrations.media_post.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_prefers_system_ffmpeg(self, _which):
        self.assertEqual(media_post._ffmpeg_executable(), "/usr/bin/ffmpeg")

    @mock.patch("integrations.media_post.shutil.which", return_value=None)
    def test_uses_user_space_imageio_fallback(self, _which):
        module = types.SimpleNamespace(get_ffmpeg_exe=lambda: "/venv/imageio-ffmpeg")
        with mock.patch.dict(sys.modules, {"imageio_ffmpeg": module}):
            self.assertEqual(media_post._ffmpeg_executable(), "/venv/imageio-ffmpeg")

from .api import _prompt_hash
from .models import (
    GenerationJob,
    Mode,
    ReferenceAsset,
    RenderDuration,
    RenderPreset,
)

# The t2v template lives in the repo-root resources/ when running from the
# source tree, but under backend/resources/ inside the Docker image
# (backend/Dockerfile copies backend/ and resources/ into /app/ together,
# so settings.RESOURCES_DIR resolves there — the same path tasks.py's
# render path uses at runtime). Locally RESOURCES_DIR points at a
# nonexistent backend/resources/, so try the image path first, then walk
# up from generation/ to the repo root for the source-tree case.
_WORKFLOW_FILENAME = "video_minimax_h3_t2v.api.json"
_WORKFLOW_ROOTS = (
    settings.RESOURCES_DIR,
    Path(__file__).resolve().parents[2] / "resources",
)

class ApplySpectrumTests(SimpleTestCase):
    """See extras.md#spectrum / integrations/spectrum.py. Exercises the
    graph splice against the real t2v template rather than a hand-built
    fixture, so this actually breaks if that template's shape ever changes
    (e.g. a re-export moves off a single UNETLoader)."""

    def _load_t2v_workflow(self):
        for root in _WORKFLOW_ROOTS:
            path = root / "workflows_api" / _WORKFLOW_FILENAME
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(
            f"{_WORKFLOW_FILENAME} not found under either {_WORKFLOW_ROOTS[0]} or "
            f"{_WORKFLOW_ROOTS[1]}"
        )

    def test_splices_in_after_the_sole_unet_loader(self):
        workflow = self._load_t2v_workflow()
        loader_id = next(nid for nid, node in workflow.items() if node["class_type"] == "UNETLoader")

        result = apply_spectrum(workflow)

        spectrum_ids = [nid for nid, node in result.items() if node["class_type"] == "SpectrumApplyMiniMaxH3"]
        self.assertEqual(len(spectrum_ids), 1)
        spectrum_id = spectrum_ids[0]
        self.assertEqual(result[spectrum_id]["inputs"]["model"], [loader_id, 0])

        # Every existing consumer of the loader's output (BasicGuider,
        # BasicScheduler in the real template) now points at Spectrum instead.
        guider = next(node for node in result.values() if node["class_type"] == "BasicGuider")
        scheduler = next(node for node in result.values() if node["class_type"] == "BasicScheduler")
        self.assertEqual(guider["inputs"]["model"], [spectrum_id, 0])
        self.assertEqual(scheduler["inputs"]["model"], [spectrum_id, 0])

    def test_raises_if_not_exactly_one_unet_loader(self):
        with self.assertRaises(RuntimeError):
            apply_spectrum({})


class RequeueJobTests(TestCase):
    """POST /api/jobs/<id>/requeue/ (see api.requeue_job) -- the "Re-queue"
    item in JobModal's More menu. These need a real DB (and real files in
    MEDIA_ROOT), so they run in Docker, not on the host:
    `docker compose exec backend python manage.py test`.

    The test DB rolls back, but FileField writes go to the real MEDIA_ROOT
    volume and are NOT rolled back -- tearDown deletes every reference file
    the test created (originals and copies) so the media volume doesn't
    accumulate test garbage between runs.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="requeue_tester", password="x")
        self.other_user = get_user_model().objects.create_user(username="requeue_other", password="x")
        self.preset = RenderPreset.objects.create(
            mode=Mode.TEXT_TO_VIDEO, label="Draft", megapixels=0.2, steps=8, is_draft=True
        )
        self.duration = RenderDuration.objects.create(
            preset=self.preset, duration_seconds=3, estimated_render_seconds=60
        )
        self.job = GenerationJob.objects.create(
            user=self.user,
            mode=Mode.TEXT_TO_VIDEO,
            preset=self.preset,
            duration=self.duration,
            raw_prompt="a cat",
            improved_prompt="a cat, cinematic",
            megapixels=0.2,
            steps=8,
            aspect_ratio="16:9",
            width=320,
            height=180,
            duration_seconds=3,
            estimated_seconds=60,
            use_spectrum=False,
            use_turbo=False,
        )
        self.ref = ReferenceAsset.objects.create(
            job=self.job,
            kind=ReferenceAsset.Kind.IMAGE,
            order=0,
            file=SimpleUploadedFile("ref.png", b"original-bytes"),
        )
        # SERVER_NAME="localhost" for the same reason as FUNCTION_CHECK.md
        # section 1.7: the test client's default "testserver" isn't in
        # ALLOWED_HOSTS, so every request would 400 before reaching the view.
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)

    def tearDown(self):
        for ref in ReferenceAsset.objects.filter(job__user=self.user):
            if ref.file:
                Path(ref.file.path).unlink(missing_ok=True)

    def _post(self, data):
        return self.client.post(
            f"/api/jobs/{self.job.id}/requeue/",
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_requeue_default_is_one_identical_copy(self):
        response = self._post({})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(len(body), 1)
        copy = body[0]

        # A fresh job, not the original row.
        self.assertNotEqual(copy["id"], self.job.id)
        self.assertEqual(copy["status"], "queued")
        # Every render-relevant field is copied verbatim...
        for field in (
            "mode",
            "raw_prompt",
            "improved_prompt",
            "megapixels",
            "aspect_ratio",
            "width",
            "height",
            "duration_seconds",
            "estimated_seconds",
            "use_spectrum",
            "use_turbo",
        ):
            self.assertEqual(copy[field], getattr(self.job, field), field)
        # ...and steps too, but _serialize_job doesn't expose steps (the
        # client gets step info from the preset catalog), so assert on the
        # model row instead of the response body.
        copy_obj = GenerationJob.objects.get(id=copy["id"])
        self.assertEqual(copy_obj.steps, self.job.steps)
        # ...so the prompt hash (improved_prompt or raw_prompt, see
        # api._prompt_hash) matches the original's -- the same prompt gets
        # the same queue-list color line.
        self.assertEqual(
            copy["prompt_hash"], _prompt_hash(self.job.improved_prompt or self.job.raw_prompt)
        )
        # Fresh identity fields, not copied (title's model default is the
        # empty string, not None -- blank means the frontend falls back to
        # showing the raw_prompt, see models.GenerationJob.title).
        self.assertEqual(copy["title"], "")
        self.assertFalse(copy["is_favorite"])
        self.assertFalse(copy["is_archived"])
        # The reference file is physically copied: new path, same bytes.
        new_ref = ReferenceAsset.objects.get(job_id=copy["id"])
        self.assertNotEqual(new_ref.file.path, self.ref.file.path)
        with open(new_ref.file.path, "rb") as fh:
            self.assertEqual(fh.read(), b"original-bytes")
        # The original job is untouched -- still one reference, same path.
        self.assertEqual(ReferenceAsset.objects.filter(job=self.job).count(), 1)

    def test_requeue_count_makes_that_many_copies(self):
        response = self._post({"count": 3})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(len(body), 3)
        new_ids = [job["id"] for job in body]
        self.assertEqual(len(set(new_ids)), 3)
        new_paths = set()
        for job_id in new_ids:
            for ref in ReferenceAsset.objects.filter(job_id=job_id):
                self.assertEqual(ref.kind, ReferenceAsset.Kind.IMAGE)
                with open(ref.file.path, "rb") as fh:
                    self.assertEqual(fh.read(), b"original-bytes")
                new_paths.add(ref.file.path)
        # Every copy owns its own distinct file (no shared paths -- a DELETE
        # of any one job must not remove another copy's reference).
        self.assertEqual(len(new_paths), 3)
        self.assertNotIn(self.ref.file.path, new_paths)

    def test_requeue_rejects_bad_count(self):
        for data in ({"count": 0}, {"count": 11}, {"count": "abc"}):
            with self.subTest(data=data):
                response = self._post(data)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_requeue_other_user_404(self):
        self.client.force_login(self.other_user)
        response = self._post({"count": 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_requeue_rejects_inactive_preset_or_duration(self):
        self.preset.is_active = False
        self.preset.save()
        response = self._post({"count": 1})
        self.assertEqual(response.status_code, 400)

        self.preset.is_active = True
        self.preset.save()
        self.duration.is_active = False
        self.duration.save()
        response = self._post({"count": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(GenerationJob.objects.count(), 1)

    def test_requeue_rejects_missing_reference_file(self):
        Path(self.ref.file.path).unlink()
        response = self._post({"count": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(GenerationJob.objects.count(), 1)
@override_settings(RESOURCES_DIR=_REPO_ROOT / "resources")
class SchedulerWorkflowTests(SimpleTestCase):
    def _loader_filename(self, workflow):
        loaders = [node for node in workflow.values() if node["class_type"] == "UNETLoader"]
        self.assertEqual(len(loaders), 1)
        return loaders[0]["inputs"]["unet_name"]

    def test_text_flow_selects_fp8_fl2va(self):
        workflow = build_api_workflow(
            Mode.TEXT_TO_VIDEO,
            width=256,
            height=256,
            duration_seconds=1,
            steps=1,
            prompt_text="test",
            model_variant=ModelVariant.FP8,
        )
        self.assertEqual(
            self._loader_filename(workflow),
            "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
        )

    def test_reference_flow_selects_int8_ref2va(self):
        workflow = build_api_workflow(
            Mode.REFERENCE_TO_VIDEO,
            width=256,
            height=256,
            duration_seconds=1,
            steps=1,
            prompt_text="test",
            model_variant=ModelVariant.INT8,
        )
        self.assertEqual(
            self._loader_filename(workflow),
            "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        )

    def test_model_family_matches_mode_not_gpu_index(self):
        self.assertEqual(gpu_scheduler.model_key(Mode.IMAGE_TO_VIDEO, "fp8"), "fl2va:fp8")
        self.assertEqual(gpu_scheduler.model_key(Mode.REFERENCE_TO_AUDIO, "int8"), "ref2va:int8")

    def test_prewarm_reference_image_is_strictly_valid_png(self):
        data = gpu_scheduler.prewarm_reference_png()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        chunks = []
        while offset < len(data):
            length = struct.unpack(">I", data[offset : offset + 4])[0]
            kind = data[offset + 4 : offset + 8]
            payload = data[offset + 8 : offset + 8 + length]
            checksum = struct.unpack(">I", data[offset + 8 + length : offset + 12 + length])[0]
            self.assertEqual(checksum, crc32(kind + payload) & 0xFFFFFFFF)
            chunks.append((kind, payload))
            offset += 12 + length
        ihdr = next(payload for kind, payload in chunks if kind == b"IHDR")
        width, height, bit_depth, color_type = struct.unpack(">IIBB", ihdr[:10])
        self.assertEqual((width, height, bit_depth, color_type), (64, 64, 8, 2))
        pixels = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
        self.assertEqual(len(pixels), height * (1 + width * 3))


class ComfyUIRoutingTests(SimpleTestCase):
    @override_settings(COMFYUI_BASE_URL="http://default:8000")
    def test_base_url_override_is_scoped(self):
        self.assertEqual(comfyui._base_url(), "http://default:8000")
        with comfyui.use_base_url("http://gpu02:18104/"):
            self.assertEqual(comfyui._base_url(), "http://gpu02:18104")
        self.assertEqual(comfyui._base_url(), "http://default:8000")

import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase

from integrations.spectrum import apply_spectrum

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

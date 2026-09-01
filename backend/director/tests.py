import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from generation.models import GenerationJob, Mode, ModelVariant, RenderDuration, RenderPreset

from . import services
from .models import Clip, JobProjectTag, Project


class PlannedSceneFeasibilityTests(SimpleTestCase):
    def test_warns_when_short_clip_overloads_dialogue_and_exact_generated_text(self):
        prompt = (
            "Show accurate bilingual subtitles. "
            "<d>[English] This line contains far too many spoken words to fit naturally.</d> "
            "<d>[English] A second line makes the timing substantially worse.</d>"
        )

        warnings = services.planned_scene_warning_codes(prompt, 5)

        self.assertIn("dialogue_overload", warnings)
        self.assertIn("exact_generated_text", warnings)

    def test_warns_when_prompt_is_too_dense_for_one_director_clip(self):
        warnings = services.planned_scene_warning_codes("detail " * 321, 8)
        self.assertIn("prompt_too_dense", warnings)

    def test_simple_single_beat_prompt_has_no_warning(self):
        prompt = "A woman pauses at the doorway. <d>[English] Wait.</d>"
        self.assertEqual(services.planned_scene_warning_codes(prompt, 7), [])

    def test_chinese_dialogue_is_counted_in_timing_budget(self):
        prompt = "<d>[中文]这是一句明显无法在五秒内自然说完的中文对白内容</d>"
        self.assertIn("dialogue_overload", services.planned_scene_warning_codes(prompt, 5))

    def test_normalized_scene_includes_computed_warning_codes(self):
        scenes = services.normalize_planned_scenes(
            [
                {
                    "mode": "t2v",
                    "continues_previous": False,
                    "duration_seconds": 5,
                    "prompt": "Display this exact bilingual subtitle " + "word " * 330,
                    "notes": "",
                }
            ]
        )
        self.assertEqual(scenes[0]["warnings"], ["prompt_too_dense", "exact_generated_text"])


class DirectorHistoryCompositionTests(TestCase):
    def setUp(self):
        self.media_tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_tmp.name)
        self.settings_override.enable()
        self.user = get_user_model().objects.create_user(username="director-user", password="test-password")
        self.other_user = get_user_model().objects.create_user(username="other-user", password="test-password")
        self.preset = RenderPreset.objects.create(
            mode=Mode.TEXT_TO_VIDEO,
            label="Native",
            megapixels=1.0,
            steps=20,
        )
        self.duration = RenderDuration.objects.create(
            preset=self.preset,
            duration_seconds=5,
            estimated_render_seconds=60,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def tearDown(self):
        self.settings_override.disable()
        self.media_tmp.cleanup()

    def make_job(
        self,
        number: int,
        *,
        user=None,
        content: bytes | None = None,
        model_variant: str = ModelVariant.FP8,
    ) -> GenerationJob:
        return GenerationJob.objects.create(
            user=user or self.user,
            mode=Mode.TEXT_TO_VIDEO,
            preset=self.preset,
            duration=self.duration,
            raw_prompt=f"clip {number}",
            title=f"Clip {number}",
            megapixels=1.0,
            steps=20,
            aspect_ratio="16:9",
            width=1344,
            height=768,
            duration_seconds=5,
            status=GenerationJob.Status.DONE,
            estimated_seconds=60,
            model_variant=model_variant,
            video_file=SimpleUploadedFile(f"clip-{number}.mp4", content or f"video-{number}".encode()),
        )

    def test_bulk_create_preserves_selection_order_and_reuses_clean_renders(self):
        jobs = [self.make_job(3), self.make_job(1), self.make_job(2)]

        project = services.create_project_from_jobs(jobs, title="My edit")

        clips = list(project.clips.order_by("order"))
        self.assertEqual(project.title, "My edit")
        self.assertEqual([clip.current_job_id for clip in clips], [job.id for job in jobs])
        self.assertEqual([clip.order for clip in clips], [0, 1, 2])
        self.assertTrue(all(not clip.needs_render for clip in clips))
        self.assertTrue(all(not clip.continues_previous for clip in clips))
        self.assertEqual(JobProjectTag.objects.filter(project=project).count(), 3)

    def test_bulk_import_rejects_duplicates_foreign_jobs_and_existing_membership(self):
        job = self.make_job(1)
        project = Project.objects.create(user=self.user, title="Target")

        with self.assertRaisesMessage(services.PlanError, "duplicates"):
            services.append_jobs_to_project(project, [job, job])

        foreign_job = self.make_job(2, user=self.other_user)
        with self.assertRaisesMessage(services.PlanError, "project owner"):
            services.append_jobs_to_project(project, [foreign_job])

        first_project = services.create_project_from_job(job)
        self.assertIsNotNone(first_project.id)
        with self.assertRaisesMessage(services.PlanError, "already belongs"):
            services.append_jobs_to_project(project, [job])

    def test_reordering_independent_history_clips_does_not_require_rerender(self):
        jobs = [self.make_job(1), self.make_job(2), self.make_job(3)]
        project = services.create_project_from_jobs(jobs)
        clips = list(project.clips.order_by("order"))

        response = self.client.post(
            f"/api/director/clips/{clips[2].id}/reorder/",
            {"order": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        reordered = list(project.clips.order_by("order"))
        self.assertEqual([clip.id for clip in reordered], [clips[2].id, clips[0].id, clips[1].id])
        self.assertTrue(all(not clip.needs_render for clip in reordered))

    def test_reordering_dirties_only_continuation_chain_with_changed_predecessor(self):
        jobs = [self.make_job(1), self.make_job(2), self.make_job(3)]
        project = services.create_project_from_jobs(jobs)
        first, continuation, moved = list(project.clips.order_by("order"))
        continuation.continues_previous = True
        continuation.save(update_fields=["continues_previous"])

        response = self.client.post(
            f"/api/director/clips/{moved.id}/reorder/",
            {"order": 1},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        continuation.refresh_from_db()
        moved.refresh_from_db()
        self.assertFalse(first.needs_render)
        self.assertFalse(moved.needs_render)
        self.assertTrue(continuation.needs_render)

    @mock.patch("director.api.assembly.concat_videos")
    def test_assemble_subset_uses_board_order_not_request_order(self, concat_videos):
        jobs = [self.make_job(1), self.make_job(2), self.make_job(3)]
        project = services.create_project_from_jobs(jobs)
        first, _middle, last = list(project.clips.order_by("order"))
        captured = []

        def fake_concat(paths):
            captured.extend(path.read_bytes() for path in paths)
            return b"assembled"

        concat_videos.side_effect = fake_concat
        response = self.client.post(
            f"/api/director/projects/{project.id}/assemble/",
            {"clip_ids": [last.id, first.id]},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [b"video-1", b"video-3"])
        project.refresh_from_db()
        self.assertTrue(project.assembled_video_file)

    def test_bulk_create_api_rejects_an_inaccessible_job_without_leaking_it(self):
        own_job = self.make_job(1)
        foreign_job = self.make_job(2, user=self.other_user)

        response = self.client.post(
            "/api/director/from_jobs/",
            {"job_ids": [own_job.id, foreign_job.id]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("not accessible", response.data["error"])
        self.assertFalse(Clip.objects.filter(current_job=own_job).exists())

    @mock.patch("director.services.async_task")
    def test_project_inherits_model_variant_and_uses_it_for_rerenders(self, async_task):
        source = self.make_job(1, model_variant=ModelVariant.INT8)
        project = services.create_project_from_job(source)
        clip = project.clips.get()

        self.assertEqual(project.model_variant, ModelVariant.INT8)

        clip.needs_render = True
        clip.save(update_fields=["needs_render"])
        rerender = services.render_clip(clip)

        self.assertEqual(rerender.model_variant, ModelVariant.INT8)
        async_task.assert_called_once_with("generation.tasks.process_queue")

    def test_project_model_variant_can_be_changed_and_marks_clips_dirty(self):
        source = self.make_job(1, model_variant=ModelVariant.FP8)
        project = services.create_project_from_job(source)
        clip = project.clips.get()
        self.assertFalse(clip.needs_render)

        response = self.client.patch(
            f"/api/director/projects/{project.id}/",
            {"model_variant": ModelVariant.INT8},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["model_variant"], ModelVariant.INT8)
        clip.refresh_from_db()
        self.assertTrue(clip.needs_render)

    def test_project_model_variant_rejects_unknown_value(self):
        project = Project.objects.create(user=self.user, title="Target")

        response = self.client.patch(
            f"/api/director/projects/{project.id}/",
            {"model_variant": "unknown"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("fp8 or int8", response.data["error"])

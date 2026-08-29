import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from integrations.spectrum import apply_spectrum

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

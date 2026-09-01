import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "comfy_workerctl.py"
SPEC = importlib.util.spec_from_file_location("comfy_workerctl", MODULE_PATH)
assert SPEC and SPEC.loader
comfy_workerctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comfy_workerctl)


class WorkerPidNamespaceTests(unittest.TestCase):
    def test_shared_state_uses_a_distinct_pid_path_for_each_gpu_host(self):
        with mock.patch.dict(os.environ, {"COMFYUI_HOST_ID": "gpu01"}):
            gpu01_path = comfy_workerctl._pid_file(3)
        with mock.patch.dict(os.environ, {"COMFYUI_HOST_ID": "gpu02"}):
            gpu02_path = comfy_workerctl._pid_file(3)

        self.assertNotEqual(gpu01_path, gpu02_path)
        self.assertEqual(gpu01_path.name, "gpu3.pid")
        self.assertEqual(gpu01_path.parent.name, "gpu01")
        self.assertEqual(gpu02_path.parent.name, "gpu02")


if __name__ == "__main__":
    unittest.main()



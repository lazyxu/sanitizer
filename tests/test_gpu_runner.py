"""CPU单元测试仅检查 GPU 报告分类，不代表GPU硬件验收。"""
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_samples.py"
spec = importlib.util.spec_from_file_location("gpu_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ReportClassificationTests(unittest.TestCase):
    def test_cuda_memcheck_real_device_oob(self):
        log = "========= Invalid __global__ write of size 4 bytes\n========= at kernel"
        self.assertEqual(runner.classify("cuda", "detect", "bad", 86, log), "DETECTED_GPU")

    def test_amd_gpu_device_asan(self):
        log = "==123==ERROR: AddressSanitizer: heap-buffer-overflow on amdgpu device 0 at pc 0xabc"
        self.assertEqual(runner.classify("rocm", "detect", "bad", 1, log), "DETECTED_GPU")

    def test_amd_cpu_asan_does_not_count(self):
        log = "==123==ERROR: AddressSanitizer: heap-buffer-overflow on address 0xabc thread T0"
        self.assertEqual(runner.classify("rocm", "detect", "bad", 1, log), "CPU_ASAN_ONLY_NOT_GPU")

    def test_gpu_api_error_is_not_kernel_device_report(self):
        self.assertEqual(runner.classify("cuda", "api", "bad", 0, "GPU_API_ERROR: double-free"),
                         "GPU_API_ERROR_NOT_GPU_KERNEL_SANITIZER")

    def test_unclassified_tool_error_cannot_be_clean(self):
        log = "========= ERROR SUMMARY: 1 errors"
        self.assertNotIn(runner.classify("cuda", "detect", "bad", 86, log),
                         ("CLEAN", "DETECTED_GPU"))

    def test_good_nonzero_exit_is_failure(self):
        self.assertEqual(runner.classify("cuda", "clean", "good", 5, "GPU_RUNTIME_ERROR"),
                         "RUNTIME_ERROR_NO_GPU_REPORT")

    def test_masked_off_invalid_pointer_is_legal(self):
        self.assertEqual(runner.classify("cuda", "clean", "bad", 0, ""), "CLEAN_AS_EXPECTED")

    def test_bad_without_gpu_report_does_not_pass(self):
        self.assertEqual(runner.classify("cuda", "detect", "bad", 0, ""), "NOT_DETECTED")

    def test_timeout_is_failure(self):
        self.assertEqual(runner.classify("cuda", "detect", "bad", 124, "TIMEOUT"), "TIMEOUT")

    def test_rocm_xnack_required(self):
        with patch.object(runner.shutil, "which", return_value="/usr/bin/mock"):
            with patch.dict(runner.os.environ, {"ROCM_ARCH": "gfx90a:xnack-"}):
                self.assertIn("xnack+", runner.preflight("rocm", True))


if __name__ == "__main__":
    unittest.main()

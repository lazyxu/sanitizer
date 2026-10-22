"""CPU-only tests for pinned NVIDIA upstream manifest and log classifiers.

Synthetic examples test parser behavior, NOT GPU execution or NVIDIA tool coverage.
"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_upstream_nvidia", ROOT / "scripts/run_upstream_nvidia.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)


class UpstreamPinAndClassificationTests(unittest.TestCase):
    def test_pinned_nvidia_license_and_sources(self):
        data = upstream.validate_manifest()
        self.assertEqual(len(data["cases"]), 7)
        self.assertEqual(len(data["vendored_files"]), 11)
        self.assertIn("LICENSE", [f["path"] for f in data["vendored_files"]])
        self.assertEqual(len([x for x in data["cases"] if x["source"].endswith("memcheck_demo.cu")]), 2)

    def test_gpu_formatted_memcheck_complete(self):
        log = ("========= Invalid __global__ write of size 4 bytes\n"
               "========= Misaligned address\n========= ERROR SUMMARY: 2 errors\n")
        self.assertEqual(upstream.classify_vendor(86, log,
                         ["Invalid __global__ (?:read|write)", "misaligned"]), "DETECTED_GPU")

    def test_gpu_formatted_memcheck_partial_not_full(self):
        log = "========= Invalid __global__ write of size 4 bytes\n========= ERROR SUMMARY: 1 errors"
        self.assertEqual(upstream.classify_vendor(86, log,
                         ["Invalid __global__ (?:read|write)", "misaligned"]), "PARTIAL_GPU_DIAGNOSTIC")

    def test_leak_is_not_memory_oob(self):
        self.assertEqual(upstream.classify_vendor(86, "========= Leaked 1024 bytes",
                         ["Invalid __global__ write"]), "MISMATCHED_GPU_DIAGNOSTIC")

    def test_generic_summary_not_device_diagnostic(self):
        self.assertEqual(upstream.classify_vendor(86, "========= ERROR SUMMARY: 1 errors",
                         ["Uninitialized __global__"]), "MISMATCHED_GPU_DIAGNOSTIC")

    def test_process_exit_is_not_gpu_diagnostic(self):
        self.assertEqual(upstream.classify_vendor(86, "cudaErrorIllegalAddress",
                         ["Invalid __global__ write"]), "RUNTIME_ERROR_NO_GPU_REPORT")

    def test_memcheck_log_without_nvidia_prefix_does_not_pass(self):
        self.assertEqual(upstream.classify_vendor(0, "Invalid __global__ write",
                         ["Invalid __global__ write"]), "NOT_DETECTED")

    def test_vendor_timeout_and_tool_failure(self):
        self.assertEqual(upstream.classify_vendor(124, "TIMEOUT", ["Uninitialized"]), "TIMEOUT")
        self.assertEqual(upstream.classify_vendor(127, "", ["Uninitialized"]), "TOOL_LAUNCH_FAILED")

    def test_custom_no_exitcode_or_nvidia_fallback(self):
        self.assertEqual(upstream.classify_custom(1, "error", "DEVICE_PROVENANCE_REPORT"),
                         "CUSTOM_TOOL_ERROR_NO_DIAGNOSTIC")
        self.assertEqual(upstream.classify_custom(0, "========= Invalid __global__ write",
                         "DEVICE_PROVENANCE_REPORT"), "NOT_DETECTED")
        self.assertEqual(upstream.classify_custom(1, "DEVICE_PROVENANCE_REPORT: oob",
                         "DEVICE_PROVENANCE_REPORT"), "CUSTOM_DIAGNOSTIC_REPORTED")

    def test_comparison_does_not_count_not_configured_as_pass(self):
        self.assertEqual(upstream.compare("DETECTED_GPU", "NOT_CONFIGURED"), "NOT_COMPARABLE")
        self.assertEqual(upstream.compare("DETECTED_GPU", "NOT_DETECTED"), "VENDOR_ONLY")
        self.assertEqual(upstream.compare("DETECTED_GPU", "CUSTOM_DIAGNOSTIC_REPORTED"),
                         "BOTH_REPORT_DIAGNOSTICS")

    def test_no_shell_placeholders_required(self):
        with self.assertRaises(ValueError):
            upstream.custom_argv("custom-tool --foo", Path("/tmp/exe"), "NV-MEM-01")
        argv = upstream.custom_argv("custom-tool -- {binary}", Path("/tmp/exe"), "NV-MEM-01")
        self.assertEqual(argv, ["custom-tool", "--", "/tmp/exe"])

    def test_reject_path_traversal(self):
        for name in ("../LICENSE", "/etc/passwd", "Synccheck/../LICENSE", r"..\LICENSE"):
            with self.assertRaises(ValueError):
                upstream.safe_upstream_path(name)


if __name__ == "__main__":
    unittest.main()

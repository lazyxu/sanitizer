"""Smoke-test -v/--verbose on all seven entrypoints; CPU-only, no GPU needed."""
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VerboseCliTests(unittest.TestCase):
    def _call(self, *args):
        return subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=25)

    def test_all_python_scripts_accept_verbose_flags(self):
        for script in ("run_samples.py", "run_upstream_nvidia.py",
                       "validate_docs.py", "validate_layout.py"):
            for flag in ("-v", "--verbose"):
                with self.subTest(script=script, flag=flag):
                    result = self._call("python3", "scripts/" + script, flag, "--help")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--verbose", result.stdout)

    def test_all_shell_scripts_accept_verbose_flags(self):
        for script in ("ci-gpu.sh", "run-all.sh", "setup-docker.sh"):
            for flag in ("-v", "--verbose"):
                with self.subTest(script=script, flag=flag):
                    result = self._call("bash", "scripts/" + script, flag, "--help")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--verbose", result.stdout)

    def test_shell_trace_is_opt_in(self):
        verbose = self._call("bash", "scripts/ci-gpu.sh", "cuda", "invalid-suite", "-v")
        quiet = self._call("bash", "scripts/ci-gpu.sh", "cuda", "invalid-suite")
        self.assertEqual(verbose.returncode, 2)
        self.assertEqual(quiet.returncode, 2)
        self.assertIn("+ [ci-gpu]", verbose.stderr)
        self.assertNotIn("+ [ci-gpu]", quiet.stderr)


if __name__ == "__main__":
    unittest.main()

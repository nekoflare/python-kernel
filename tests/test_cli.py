"""Tests for pyck/__main__.py: the `python -m pyck` CLI driver.

These exercise argument handling and error surfaces via subprocess, using
--transpile-only wherever possible so they don't require a C++ compiler.
"""
import tempfile
import unittest
from pathlib import Path

from .helpers import ROOT, run_cli


class TestFileHandling(unittest.TestCase):
    def test_missing_source_file(self):
        result = run_cli(["/no/such/file.py", "--target=hosted"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("file not found", result.stderr)

    def test_target_is_required(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "p.py"
            src.write_text("print(1)\n")
            result = run_cli([str(src)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required", result.stderr)

    def test_invalid_target_choice(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "p.py"
            src.write_text("print(1)\n")
            result = run_cli([str(src), "--target=bogus"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)


class TestTranspileOnly(unittest.TestCase):
    def test_writes_cpp_to_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "prog.py"
            src.write_text("print(1)\n")
            result = run_cli([str(src), "--target=hosted", "--transpile-only"])
            self.assertEqual(result.returncode, 0, result.stderr)
            out = Path(d) / "prog.cpp"
            self.assertTrue(out.exists())
            self.assertIn("#include <Python.h>", out.read_text())

    def test_writes_cpp_to_explicit_output_path(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "prog.py"
            src.write_text("print(1)\n")
            out = Path(d) / "custom_name.cpp"
            result = run_cli([str(src), "--target=hosted", "--transpile-only",
                               "-o", str(out)])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.exists())

    def test_freestanding_transpile_differs_from_hosted(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "prog.py"
            src.write_text("print(1)\n")
            hosted_out = Path(d) / "hosted.cpp"
            free_out = Path(d) / "free.cpp"
            r1 = run_cli([str(src), "--target=hosted", "--transpile-only", "-o", str(hosted_out)])
            r2 = run_cli([str(src), "--target=freestanding", "--transpile-only", "-o", str(free_out)])
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("Python.h", hosted_out.read_text())
            self.assertIn("pyck_runtime.h", free_out.read_text())


class TestErrorSurfacing(unittest.TestCase):
    def test_type_error_prints_to_stderr_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "prog.py"
            src.write_text("def main():\n    return y\nmain()\n")
            result = run_cli([str(src), "--target=hosted", "--transpile-only"])
            self.assertEqual(result.returncode, 1)
            # Caught by pyck/imports.py's per-file scope check (runs before
            # the typechecker even starts) now that every file's visible
            # names are restricted to its own definitions plus whatever it
            # explicitly imports -- "not defined" covers both a genuine
            # typo and a name that exists elsewhere but was never imported.
            self.assertIn("not defined", result.stderr)

    def test_dangerous_ops_require_explicit_flag(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "prog.py"
            src.write_text("def main():\n    x = peek8(1000)\nmain()\n")
            without_flag = run_cli([str(src), "--target=hosted", "--transpile-only"])
            self.assertEqual(without_flag.returncode, 1)
            self.assertIn("enable-dangerous-memory-ops", without_flag.stderr)

            with_flag = run_cli([str(src), "--target=hosted", "--transpile-only",
                                  "--enable-dangerous-memory-ops"])
            self.assertEqual(with_flag.returncode, 0, with_flag.stderr)


if __name__ == "__main__":
    unittest.main()

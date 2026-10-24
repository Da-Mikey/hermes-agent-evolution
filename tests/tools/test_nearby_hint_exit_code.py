"""Regression tests for #282 — no fabricated "File not found" on exit_code 0.

The defect: ``finalize_foreground_result`` called ``_nearby_missing_path_hint``
unconditionally, so a *successful* command whose output merely contained the
substring "not found" (a quoted grep pattern, a nested tool result naming a
directory) had a hard ``error`` string stapled to a result reporting
``exit_code: 0``. Measured on live session data: >=118 such results.

Contract asserted here:
  * exit_code 0  -> ``error`` stays None, whatever the output says;
  * non-zero exit naming a genuinely missing path -> the hint is still produced.
"""

import json
import os
import tempfile
import types
import unittest

from tools.terminal_tool_result import finalize_foreground_result


def _finalize(command: str, output: str, returncode: int) -> dict:
    """Run the real finalizer and return the parsed tool result."""
    raw = finalize_foreground_result(
        command=command,
        result={"output": output, "returncode": returncode},
        env=types.SimpleNamespace(cwd=None),
        env_type="local",
        effective_task_id="test",
        task_id=None,
        session_id=None,
        session_key="test-session",
        workdir=None,
        command_cwd=None,
        approval_note=None,
    )
    return json.loads(raw)


class TestNoFabricatedErrorOnSuccess(unittest.TestCase):
    def test_quoted_phrase_with_path_exit_zero_has_no_error(self):
        """A command that only *quotes* "not found" must not invent an error."""
        res = _finalize(
            "grep -rn 'not found' /home/mike/.hermes/evolution/analysis",
            "notes.md:12: the guidance said not found is a failure mode\n"
            "File not found: /home/mike/.hermes/evolution/analysis",
            0,
        )
        self.assertEqual(res["exit_code"], 0)
        self.assertIsNone(res["error"], f"fabricated error on exit 0: {res['error']!r}")

    def test_nested_tool_result_naming_a_directory_exit_zero_has_no_error(self):
        """The live symptom: a nested tool result mentioning a directory path."""
        res = _finalize(
            "cd ~/.hermes/evolution/analysis && python3 -c 'print(1)'",
            "1\n\nFile not found: ~/.hermes/evolution/analysis\n"
            "Did you mean: ./analysis, ./marketing-analysis-6-ideas.md?",
            0,
        )
        self.assertIsNone(res["error"], f"fabricated error on exit 0: {res['error']!r}")

    def test_bare_not_found_substring_is_not_a_path_report(self):
        """Dropping the bare ``"not found"`` branch: no structured signal, no hint."""
        res = _finalize(
            "systemctl status /etc/nope.service",
            "Service state: nothing was not found in the journal for this unit",
            3,
        )
        # A non-zero exit alone is not proof of a missing path.
        self.assertIsNone(res["error"], f"invented path error: {res['error']!r}")


class TestGenuineMissingPathStillHints(unittest.TestCase):
    def test_missing_file_on_nonzero_exit_still_gets_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "data.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
            missing = os.path.join(tmp, "dat.txt")  # typo of data.txt
            command = f"cat {missing}"
            output = f"cat: {missing}: No such file or directory"
            res = _finalize(command, output, 1)
        self.assertEqual(res["exit_code"], 1)
        self.assertIsNotNone(res["error"], "genuine missing path lost its hint")
        self.assertIn("File not found:", res["error"])
        self.assertIn("Did you mean:", res["error"])

    def test_hint_names_the_producing_tool_not_read_file(self):
        """#282 point 3: the hint must not hard-code ``read_file``."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "data.txt"), "w", encoding="utf-8") as fh:
                fh.write("x")
            missing = os.path.join(tmp, "dat.txt")
            res = _finalize(
                f"cat {missing}", f"cat: {missing}: No such file or directory", 1
            )
        self.assertIn("Re-run terminal", res["error"])
        self.assertNotIn("Re-run read_file", res["error"])


if __name__ == "__main__":
    unittest.main()

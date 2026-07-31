from __future__ import annotations

import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase

from evaluation import cli_clear


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_cli_run(batch_dir: Path, run_id: str) -> Path:
    run_dir = batch_dir / run_id
    _write(run_dir / "_meta.json", json.dumps({"run_id": run_id, "status": "completed"}))
    _write(run_dir / "data" / "input.csv", "a,b\n1,2\n")
    _write(run_dir / "related_work" / "paper.md", "# paper\n")
    _write(run_dir / "INSTRUCTIONS.md", "instructions\n")
    _write(run_dir / "code" / "analysis.py", "print('keep')\n")
    _write(run_dir / "outputs" / "result.json", "{}\n")
    _write(run_dir / "report" / "report.md", "# keep\n")
    _write(run_dir / "_score.json", "{}\n")
    _write(batch_dir / f"{run_id}_trace" / "trace.jsonl", "{}\n")
    return run_dir


class CliClearTests(TestCase):
    def test_dry_run_reports_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / "workspaces"
            batch_dir = workspaces / "cli_runs" / "cli_20260606_000000_deadbeef"
            run_dir = _make_cli_run(batch_dir, "cli_Math_003_20260606_000000_cafebabe")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_clear.main(["--workspaces-dir", str(workspaces)])

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN", output)
            self.assertIn("Runs scanned: 1", output)
            self.assertIn("Runs with duplicated inputs: 1", output)
            self.assertIn("No files were deleted", output)
            self.assertTrue((run_dir / "data").exists())
            self.assertTrue((run_dir / "related_work").exists())
            self.assertTrue((run_dir / "INSTRUCTIONS.md").exists())

    def test_yes_deletes_only_cli_duplicate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / "workspaces"
            batch_dir = workspaces / "cli_runs" / "cli_20260606_000000_deadbeef"
            run_dir = _make_cli_run(batch_dir, "cli_Physics_003_20260606_000000_cafebabe")
            frontend_run = workspaces / "Physics_003_20260606_000000"
            _write(frontend_run / "_meta.json", "{}\n")
            _write(frontend_run / "data" / "must_keep.csv", "keep\n")
            _write(frontend_run / "related_work" / "must_keep.md", "keep\n")
            _write(frontend_run / "INSTRUCTIONS.md", "keep\n")

            stdout = StringIO()
            with redirect_stdout(stdout):
                code = cli_clear.main(["--workspaces-dir", str(workspaces), "--yes"])

            output = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("CLEANED", output)
            self.assertIn("Runs scanned: 1", output)
            self.assertFalse((run_dir / "data").exists())
            self.assertFalse((run_dir / "related_work").exists())
            self.assertTrue((run_dir / "INSTRUCTIONS.md").exists())
            self.assertTrue((run_dir / "code" / "analysis.py").exists())
            self.assertTrue((run_dir / "outputs" / "result.json").exists())
            self.assertTrue((run_dir / "report" / "report.md").exists())
            self.assertTrue((run_dir / "_meta.json").exists())
            self.assertTrue((run_dir / "_score.json").exists())
            self.assertTrue((batch_dir / f"{run_dir.name}_trace" / "trace.jsonl").exists())
            self.assertTrue((frontend_run / "data" / "must_keep.csv").exists())
            self.assertTrue((frontend_run / "related_work" / "must_keep.md").exists())
            self.assertTrue((frontend_run / "INSTRUCTIONS.md").exists())

    def test_batch_filter_limits_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspaces = Path(tmp) / "workspaces"
            first_batch = workspaces / "cli_runs" / "cli_20260606_000000_deadbeef"
            second_batch = workspaces / "cli_runs" / "cli_20260606_000001_deadbeef"
            first_run = _make_cli_run(first_batch, "cli_Earth_000_20260606_000000_aaaaaaaa")
            second_run = _make_cli_run(second_batch, "cli_Earth_001_20260606_000001_bbbbbbbb")

            cli_clear.main([
                "--workspaces-dir",
                str(workspaces),
                "--batch",
                first_batch.name,
                "--yes",
            ])

            self.assertFalse((first_run / "data").exists())
            self.assertTrue((second_run / "data").exists())

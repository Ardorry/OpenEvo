from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openevo_chembench.config import AgentConfig, ExperimentConfig
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
    LocalCommandResult,
)
from openevo_chembench.models import PublicChoice, PublicPrompt
from openevo_chembench.runtime_context import AgentRoundRequest
import openevo_chembench.local_codex_executor as executor_module


_STDERR_SENTINEL = "sensitive-stderr-sentinel target_scores private-uuid must-not-persist"


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        openevo_revision="a" * 40,
        chembench_revision="b" * 40,
        codex_cli_version="0.144.6",
        prompt_version="chembench-public-prompt.v1",
        execution_backend="local_codex_cli",
        agent=AgentConfig(harness="codex_cli"),
    )


def _request() -> AgentRoundRequest:
    return AgentRoundRequest(
        run_id="run_" + "1" * 24,
        episode_id="episode_" + "2" * 24,
        round_index=0,
        public_prompt=PublicPrompt(
            question="Which public choice is appropriate?",
            choices=(
                PublicChoice(label="A", text="public alpha"),
                PublicChoice(label="B", text="public beta"),
            ),
            answer_format="Return one choice label in angle tags.",
        ),
    )


def _transcript() -> str:
    events = (
        {"type": "thread.started", "thread_id": "thread-safe"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-safe",
                "type": "agent_message",
                "text": "<answer>A</answer>",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 20,
                "cached_input_tokens": 5,
                "output_tokens": 4,
                "reasoning_output_tokens": 2,
            },
        },
    )
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


class _StableRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stderr: str = "",
        raised_error: OSError | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.raised_error = raised_error
        self.calls: list[tuple[tuple[str, ...], str | None, Path, dict[str, str], float]] = []

    def __call__(
        self,
        command,
        input_text,
        cwd,
        environment,
        timeout_seconds,
    ) -> LocalCommandResult:
        self.calls.append(
            (
                tuple(command),
                input_text,
                cwd,
                dict(environment),
                timeout_seconds,
            )
        )
        if self.raised_error is not None:
            raise self.raised_error
        return LocalCommandResult(
            returncode=self.returncode,
            stdout=_transcript(),
            stderr=self.stderr,
        )


class LocalCodexStabilityTests(unittest.TestCase):
    def _layout(self, outer: Path) -> tuple[Path, Path, Path]:
        auth_file = outer / "auth.json"
        auth_file.write_text("{}\n", encoding="utf-8")
        auth_file.chmod(0o600)
        isolation_parent = outer / "isolation"
        isolation_parent.mkdir(mode=0o700)
        diagnostic_root = outer / "diagnostics"
        return auth_file, isolation_parent, diagnostic_root

    def _executor(
        self,
        outer: Path,
        runner: _StableRunner,
    ) -> tuple[LocalCodexCLIExecutor, Path, Path]:
        auth_file, isolation_parent, diagnostic_root = self._layout(outer)
        executor = LocalCodexCLIExecutor(
            config=_config(),
            codex_executable=Path("/usr/bin/codex"),
            auth_file=auth_file,
            command_runner=runner,
            verified_codex_version="0.144.6",
            isolation_parent=isolation_parent,
            diagnostic_root=diagnostic_root,
        )
        return executor, isolation_parent, diagnostic_root

    def test_ten_consecutive_calls_are_isolated_and_removed(self) -> None:
        runner = _StableRunner()
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            executor, isolation_parent, _diagnostics = self._executor(
                outer,
                runner,
            )
            for _ in range(10):
                attempt = executor.execute(_request())
                self.assertEqual(attempt.response, "<answer>A</answer>")
            executor.close()

            roots = [Path(call[3]["HOME"]) for call in runner.calls]
            self.assertEqual(len(roots), 10)
            self.assertEqual(len(set(roots)), 10)
            self.assertTrue(all(not root.exists() for root in roots))
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )

        for command, _prompt, cwd, environment, _timeout in runner.calls:
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            for feature in (
                "apps",
                "plugins",
                "plugin_sharing",
                "remote_plugin",
            ):
                indices = [
                    index
                    for index, value in enumerate(command[:-1])
                    if value == "--disable" and command[index + 1] == feature
                ]
                self.assertEqual(len(indices), 1)
            root = Path(environment["HOME"]).parent
            self.assertEqual(Path(environment["HOME"]), root / "home")
            self.assertEqual(Path(environment["CODEX_HOME"]), root / "codex_home")
            self.assertEqual(
                Path(environment["CODEX_SQLITE_HOME"]),
                root / "xdg_state" / "sqlite",
            )
            self.assertEqual(Path(environment["XDG_CACHE_HOME"]), root / "xdg_cache")
            self.assertEqual(Path(environment["XDG_CONFIG_HOME"]), root / "xdg_config")
            self.assertEqual(Path(environment["XDG_STATE_HOME"]), root / "xdg_state")
            for variable in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(Path(environment[variable]), root / "tmp")
            self.assertEqual(cwd, root / "work")

    def test_transient_cleanup_error_is_retried_without_masking_result(self) -> None:
        runner = _StableRunner()
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            executor, isolation_parent, diagnostics = self._executor(outer, runner)
            actual_remove = executor_module._remove_tree_once
            attempts = 0

            def flaky_remove(root: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise OSError(errno.EBUSY, "simulated transient cleanup error")
                actual_remove(root)

            with (
                patch.object(executor_module, "_remove_tree_once", flaky_remove),
                patch.object(
                    executor_module,
                    "_CLEANUP_RETRY_DELAYS_SECONDS",
                    (0.0, 0.0, 0.0),
                ),
            ):
                attempt = executor.execute(_request())
            executor.close()

            self.assertEqual(attempt.response, "<answer>A</answer>")
            self.assertEqual(attempts, 3)
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            receipts = sorted(diagnostics.glob("receipt_*.json"))
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["cleanup_complete"])
            self.assertEqual(payload["cleanup_attempts"], 3)
            self.assertIn("CLEANUP_RETRY", payload["finding_codes"])

    def test_persistent_cleanup_blocks_the_next_invocation(self) -> None:
        runner = _StableRunner()
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            executor, isolation_parent, diagnostics = self._executor(outer, runner)

            def permanently_busy(_root: Path) -> None:
                raise OSError(errno.ENOTEMPTY, "simulated background writer")

            with (
                patch.object(
                    executor_module,
                    "_remove_tree_once",
                    permanently_busy,
                ),
                patch.object(
                    executor_module,
                    "_CLEANUP_RETRY_DELAYS_SECONDS",
                    (0.0,),
                ),
            ):
                with self.assertRaises(LocalCodexExecutionError) as first:
                    executor.execute(_request())
                with self.assertRaises(LocalCodexExecutionError) as second:
                    executor.execute(_request())

            self.assertEqual(
                first.exception.code,
                LocalCodexExecutionErrorCode.DISALLOWED_PLUGIN_ACTIVITY,
            )
            self.assertEqual(
                second.exception.code,
                LocalCodexExecutionErrorCode.CLEANUP_FAILED,
            )
            self.assertEqual(len(runner.calls), 1)
            roots_before_close = list(isolation_parent.glob("openevo-chembench-local-codex-*"))
            self.assertEqual(len(roots_before_close), 1)
            self.assertFalse((roots_before_close[0] / "codex-home" / "auth.json").exists())

            executor.close()
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            receipts = sorted(diagnostics.glob("receipt_*.json"))
            self.assertGreaterEqual(len(receipts), 2)

    def test_command_oserror_cleans_isolation_root(self) -> None:
        runner = _StableRunner(
            raised_error=OSError(errno.EIO, _STDERR_SENTINEL),
        )
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            executor, isolation_parent, diagnostics = self._executor(outer, runner)
            with self.assertRaises(LocalCodexExecutionError) as raised:
                executor.execute(_request())
            executor.close()

            self.assertEqual(
                raised.exception.code,
                LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE,
            )
            self.assertNotIn(_STDERR_SENTINEL, str(raised.exception))
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            receipts = sorted(diagnostics.glob("receipt_*.json"))
            self.assertEqual(len(receipts), 1)
            serialized = receipts[0].read_text(encoding="utf-8")
            self.assertNotIn(_STDERR_SENTINEL, serialized)

    def test_failed_command_persists_only_safe_stderr_metadata(self) -> None:
        runner = _StableRunner(returncode=1, stderr=_STDERR_SENTINEL)
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            executor, isolation_parent, diagnostics = self._executor(outer, runner)
            with self.assertRaises(LocalCodexExecutionError) as raised:
                executor.execute(_request())
            executor.close()

            self.assertEqual(
                raised.exception.code,
                LocalCodexExecutionErrorCode.CLI_FAILED,
            )
            self.assertIsNotNone(raised.exception.diagnostic_receipt)
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            receipts = sorted(diagnostics.glob("receipt_*.json"))
            self.assertEqual(len(receipts), 1)
            serialized = receipts[0].read_text(encoding="utf-8")
            payload = json.loads(serialized)
            encoded = _STDERR_SENTINEL.encode("utf-8")
            self.assertEqual(payload["stderr_bytes"], len(encoded))
            self.assertEqual(
                payload["stderr_sha256"],
                hashlib.sha256(encoded).hexdigest(),
            )
            self.assertIn("STDERR_CAPTURED", payload["finding_codes"])
            self.assertIn("COMMAND_NONZERO", payload["finding_codes"])
            self.assertNotIn(_STDERR_SENTINEL, serialized)
            self.assertNotIn("target_scores", serialized)
            self.assertNotIn("private-uuid", serialized)

    def test_hundred_consecutive_executor_dry_runs_have_no_resource_leak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            auth_file, isolation_parent, diagnostics = self._layout(outer)
            root_log = outer / "isolation-roots.log"
            fake_codex = outer / "dry-run-fake-codex"
            fake_codex.write_text(
                (
                    "#!/usr/bin/env python3\n"
                    "import os\n"
                    "import sys\n"
                    f"root_log = {os.fspath(root_log)!r}\n"
                    "with open(root_log, 'a', encoding='utf-8') as stream:\n"
                    "    stream.write(os.environ['HOME'] + '\\n')\n"
                    f"sys.stdout.write({_transcript()!r})\n"
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            executor = LocalCodexCLIExecutor(
                config=_config(),
                codex_executable=fake_codex,
                auth_file=auth_file,
                verified_codex_version="0.144.6",
                isolation_parent=isolation_parent,
                diagnostic_root=diagnostics,
            )
            descriptor_count_before = _open_descriptor_count()
            for _ in range(100):
                attempt = executor.execute(_request())
                self.assertEqual(attempt.response, "<answer>A</answer>")
            executor.close()
            descriptor_count_after = _open_descriptor_count()

            roots = tuple(
                Path(line) for line in root_log.read_text(encoding="utf-8").splitlines() if line
            )
            self.assertEqual(len(roots), 100)
            self.assertEqual(len(set(roots)), 100)
            self.assertTrue(all(not root.exists() for root in roots))
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            self.assertEqual(list(isolation_parent.rglob("plugins-clone-*")), [])
            self.assertEqual(
                list(isolation_parent.rglob(".remote-plugin-install-staging")),
                [],
            )
            self.assertEqual(executor._pending_cleanup, set())
            self.assertEqual(list(diagnostics.glob("receipt_*.json")), [])
            if descriptor_count_before is not None and descriptor_count_after is not None:
                self.assertLessEqual(
                    descriptor_count_after,
                    descriptor_count_before + 1,
                )

    @unittest.skipUnless(os.name == "posix", "process-group test requires POSIX")
    def test_default_runner_terminates_background_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            auth_file, isolation_parent, diagnostic_root = self._layout(outer)
            pid_file = outer / "background.pid"
            fake_codex = outer / "fake-codex"
            fake_codex.write_text(
                (
                    "#!/usr/bin/env python3\n"
                    "import subprocess\n"
                    "import sys\n"
                    f"pid_file = {os.fspath(pid_file)!r}\n"
                    "child = subprocess.Popen(\n"
                    "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
                    "    stdin=subprocess.DEVNULL,\n"
                    "    stdout=subprocess.DEVNULL,\n"
                    "    stderr=subprocess.DEVNULL,\n"
                    ")\n"
                    "with open(pid_file, 'w', encoding='utf-8') as stream:\n"
                    "    stream.write(str(child.pid))\n"
                    f"sys.stdout.write({_transcript()!r})\n"
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            executor = LocalCodexCLIExecutor(
                config=_config(),
                codex_executable=fake_codex,
                auth_file=auth_file,
                verified_codex_version="0.144.6",
                isolation_parent=isolation_parent,
                diagnostic_root=diagnostic_root,
            )
            attempt = executor.execute(_request())
            executor.close()

            self.assertEqual(attempt.response, "<answer>A</answer>")
            background_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2.0
            while _process_exists(background_pid) and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertFalse(_process_exists(background_pid))
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )

    @unittest.skipUnless(os.name == "posix", "timeout test requires POSIX")
    def test_default_runner_timeout_is_bounded_and_saves_safe_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            auth_file, isolation_parent, diagnostic_root = self._layout(outer)
            fake_codex = outer / "slow-fake-codex"
            fake_codex.write_text(
                (
                    "#!/usr/bin/env python3\n"
                    "import sys\n"
                    "import time\n"
                    f"sys.stderr.write({_STDERR_SENTINEL!r})\n"
                    "sys.stderr.flush()\n"
                    "time.sleep(60)\n"
                ),
                encoding="utf-8",
            )
            fake_codex.chmod(0o700)
            executor = LocalCodexCLIExecutor(
                config=_config(),
                # Leave enough startup budget for a loaded CI host to execute
                # the sentinel write before the bounded timeout fires.
                task_timeout_seconds=0.5,
                codex_executable=fake_codex,
                auth_file=auth_file,
                verified_codex_version="0.144.6",
                isolation_parent=isolation_parent,
                diagnostic_root=diagnostic_root,
            )
            started = time.monotonic()
            with self.assertRaises(LocalCodexExecutionError) as raised:
                executor.execute(_request())
            duration = time.monotonic() - started
            executor.close()

            self.assertEqual(
                raised.exception.code,
                LocalCodexExecutionErrorCode.CLI_TIMEOUT,
            )
            self.assertLess(duration, 5.0)
            self.assertEqual(
                list(isolation_parent.glob("openevo-chembench-local-codex-*")),
                [],
            )
            receipts = sorted(diagnostic_root.glob("receipt_*.json"))
            self.assertEqual(len(receipts), 1)
            serialized = receipts[0].read_text(encoding="utf-8")
            payload = json.loads(serialized)
            self.assertIn("COMMAND_TIMEOUT", payload["finding_codes"])
            self.assertEqual(
                payload["stderr_sha256"],
                hashlib.sha256(_STDERR_SENTINEL.encode("utf-8")).hexdigest(),
            )
            self.assertNotIn(_STDERR_SENTINEL, serialized)


def _open_descriptor_count() -> int | None:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        return None
    return len(list(descriptor_root.iterdir()))


def _process_exists(process_id: int) -> bool:
    return Path(f"/proc/{process_id}").exists()


if __name__ == "__main__":
    unittest.main()

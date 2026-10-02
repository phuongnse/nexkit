"""Public activity, real process I/O and trusted post-session collection."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit.adapters.codex_events import event, prepare_api_observer
from nexkit.agent_observability import begin, finish, summary
from nexkit.common import Blocked, read_regular_json, write_json
from nexkit.observability import Redactor, RunLog, read_trace, run_observed, session_identity
from tests import test_agent_session
from tests.support import project


class ObservedProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX detached descendants")
    def test_detached_output_flood_cannot_bypass_the_drain_deadline(self):
        class SlowLog(RunLog):
            def record(self, value):
                if value.get("kind") == "message":
                    time.sleep(0.004)
                super().record(value)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = SlowLog(root, {}, emit=lambda line: None)
            child = "import json,time; end=time.monotonic()+3\nwhile time.monotonic()<end: print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'flood'}}),flush=True)"
            parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]],start_new_session=True)"
            started = time.monotonic()
            self.assertEqual(
                run_observed([sys.executable, "-c", parent, child], log, event, timeout=0.2), 0
            )
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(
                any(
                    "remaining activity was omitted" in e.get("message", "")
                    for e in read_trace(root / "events.jsonl")
                )
            )

    def test_activity_is_live_drains_both_pipes_and_hides_private_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done = root / "done"
            emitted = []

            def emit(line):
                emitted.append((line, done.exists()))

            log = RunLog(
                root / "trace",
                {"role": "task"},
                redactor=Redactor({"GH_TOKEN": "known-secret-canary"}),
                emit=emit,
            )
            script = """
import json, pathlib, sys, time
def send(value): print(json.dumps(value), flush=True)
send({'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_REASONING_CANARY'}})
send({'type':'future.private','payload':'PRIVATE_PAYLOAD_CANARY'})
send({'type':'item.started','item':{'type':'command_execution','command':'python app.py'}})
sys.stderr.write('known-secret-canary ::error:: diagnostic\\n'); sys.stderr.flush()
sys.stdout.write('x' * 200000 + '\\n'); sys.stdout.flush()
time.sleep(.35)
send({'type':'item.completed','item':{'type':'command_execution','command':'python app.py','exit_code':5,'aggregated_output':'command output é'}})
send({'type':'turn.completed','usage':{'input_tokens':8,'output_tokens':2,'private':'PRIVATE_USAGE_CANARY'}})
pathlib.Path(sys.argv[1]).touch()
sys.exit(5)
"""
            code = run_observed(
                [sys.executable, "-c", script, str(done)], log, event, timeout=5, heartbeat=0.08
            )
            self.assertEqual(code, 5)
            self.assertTrue(any("python app.py" in line and not ended for line, ended in emitted))
            events = read_trace(root / "trace/events.jsonl")
            self.assertTrue(any(item["kind"] == "heartbeat" for item in events))
            command = [item for item in events if item["kind"] == "command"]
            self.assertEqual(command[-1]["exit_code"], 5)
            self.assertIn("é", command[-1]["output"])
            public = "\n".join(p.read_text(encoding="utf-8") for p in (root / "trace").iterdir())
            for secret in (
                "known-secret-canary",
                "PRIVATE_REASONING_CANARY",
                "PRIVATE_PAYLOAD_CANARY",
                "PRIVATE_USAGE_CANARY",
                "::error::",
            ):
                self.assertNotIn(secret, public)
            self.assertIn("[REDACTED]", public)
            state = read_regular_json(root / "trace/run.json")
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["usage"], {"input_tokens": 8, "output_tokens": 2})
            self.assertGreaterEqual(state["omitted_events"], 3)

    def test_timeout_survives_early_pipe_closure_and_keeps_native_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = RunLog(root, {}, emit=lambda line: None)
            started = time.monotonic()
            code = run_observed(
                [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(30)"],
                log,
                event,
                timeout=0.25,
                heartbeat=0.07,
            )
            self.assertEqual(code, 124)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(read_regular_json(root / "run.json")["status"], "timed_out")
            events = read_trace(root / "events.jsonl")
            self.assertEqual(events[-1]["kind"], "session")
            self.assertEqual(events[-1]["status"], "timed_out")

    @unittest.skipUnless(os.name == "posix", "POSIX descendant process groups")
    def test_background_child_cannot_hold_observation_open_after_parent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = RunLog(root, {}, emit=lambda line: None)
            started = time.monotonic()
            script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"
            self.assertEqual(run_observed([sys.executable, "-c", script], log, event, timeout=5), 0)
            self.assertLess(time.monotonic() - started, 4)

    def test_missing_executable_still_records_failed_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = RunLog(root, {}, emit=lambda line: None)
            with self.assertRaises(OSError):
                run_observed([str(root / "absent-cli")], log, event, timeout=1)
            self.assertEqual(read_regular_json(root / "run.json")["exit_code"], 127)

    def test_plain_credentials_and_workflow_commands_are_redacted(self):
        redactor = Redactor({"OPENAI_API_KEY": "private-canary", "PATH": "ordinary-value"})
        text = redactor.text(
            "\x1b[31mprivate-canary\x1b[0m github_pat_"
            + "x" * 25
            + " password='unknown value'\n::set-output name=oops::bad\n##[error]bad"
        )
        for value in ("private-canary", "unknown value", "github_pat_", "::", "##["):
            self.assertNotIn(value, text)
        self.assertIn("[REDACTED]", text)

    def test_diagnostic_metadata_does_not_publish_credentials_in_display_fields(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(os.environ, {"GH_TOKEN": "metadata-secret-canary"}),
        ):
            root = Path(temporary)
            log = RunLog(
                root,
                {"model": "metadata-secret-canary", "capabilities": {"version": "password=hidden"}},
                emit=lambda line: None,
            )
            log.finish(0)
            value = (root / "run.json").read_text()
            self.assertNotIn("metadata-secret-canary", value)
            self.assertNotIn("password=hidden", value)


class DiagnosticCollectionTests(unittest.TestCase):
    def test_request_and_review_artifacts_keep_all_substantive_result_fields(self):
        for role in ("request", "review"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context, home, data = self.fixture(root, role)
                result = {
                    "status": "done",
                    "summary": "Work complete",
                    "skills_used": ["nexkit-" + role],
                    "commands": ["read source"],
                    "limitations": [],
                    "private_protocol": "PRIVATE_PROTOCOL_CANARY",
                }
                if role == "request":
                    result.update(
                        reply="Direct reply é",
                        specification="Approved specification details",
                        questions=["Important question"],
                        ready_for_approval=False,
                    )
                    expected = [
                        "Direct reply é",
                        "Approved specification details",
                        "Important question",
                    ]
                else:
                    result.update(
                        verdict="changes_requested",
                        findings=[{"severity": "blocking", "detail": "Critical finding"}],
                        acceptance=[
                            {
                                "criterion": "Important criterion",
                                "evidence": "Missing evidence",
                                "passed": False,
                            }
                        ],
                    )
                    expected = ["Critical finding", "Important criterion", "Missing evidence"]
                write_json(home / "output/result.json", result)
                with (
                    patch("nexkit.agent_session.require_native", return_value="linux"),
                    patch("nexkit.adapters.codex.CodexAdapter.stop_session"),
                ):
                    identity = finish(context, role, data, "success")
                self.assertTrue(identity["result_available"])
                text = (data / "diagnostics/report.md").read_text(encoding="utf-8")
                for value in expected:
                    self.assertIn(value, text)
                structured = read_regular_json(data / "diagnostics/result.json")
                self.assertEqual(
                    structured,
                    {key: value for key, value in result.items() if key != "private_protocol"},
                )
                self.assertNotIn("PRIVATE_PROTOCOL_CANARY", text)

    def fixture(self, root, role="task"):
        context, home, data = test_agent_session.AcceptedSessionTests.fixture(self, root, role)
        context["issue"] = {"number": 17}
        context["run_key"] = "101.1"
        context["agent_minutes"] = 1
        # Fixture materialization was sealed before these controller fields.
        runtime = read_regular_json(data / "agent-runtime.json")
        from nexkit.common import digest

        runtime["context"] = digest(context)
        write_json(data / "agent-runtime.json", runtime)
        return context, home, data

    def test_full_markdown_report_and_exact_attempt_survive_failed_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, home, data = self.fixture(root)
            text = "## Findings é\n\n" + "complete findings\n" * 8000
            result = {
                "status": "done",
                "summary": text,
                "skills_used": ["nexkit-task"],
                "commands": ["python -m unittest"],
                "limitations": [],
            }
            write_json(home / "output/result.json", result)
            with (
                patch("nexkit.agent_session.require_native", return_value="linux"),
                patch("nexkit.adapters.codex.CodexAdapter.prepare_observation"),
                patch("nexkit.adapters.codex.CodexAdapter.stop_session") as stop,
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_RUN_ID": "101",
                        "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_OUTPUT": "",
                        "GITHUB_STEP_SUMMARY": str(root / "summary"),
                    },
                ),
            ):
                identity = begin(context, "task", data)
                log = RunLog(data / "diagnostics", identity, emit=lambda line: None)
                log.record(
                    {"kind": "command", "command": "python -m unittest", "status": "started"}
                )
                log.finish(124, status="timed_out")
                collected = finish(context, "task", data, "failure")
                stop.assert_called_once()
                self.assertEqual(collected["run_key"], "101.2")
                self.assertEqual(collected["reservation_run_key"], "101.1")
                self.assertEqual(collected["status"], "timed_out")
                self.assertTrue(collected["result_available"])
                self.assertIn(text, (data / "diagnostics/report.md").read_text(encoding="utf-8"))
                summary(data, "https://github.com/owner/project/actions/runs/999/artifacts/8")
                self.assertNotIn("runs/999", (root / "summary").read_text())
                summary(data, "https://github.com/owner/project/actions/runs/101/artifacts/8")
                self.assertIn("/attempts/2", (root / "summary").read_text())
                self.assertIn("runs/101/artifacts/8", (root / "summary").read_text())
                self.assertLess((root / "summary").stat().st_size, 52000)
                if os.name == "posix":
                    self.assertEqual((data / "diagnostics/run.json").stat().st_mode & 0o777, 0o644)

    def test_bootstrap_failure_and_interrupted_session_are_reported_without_valid_result(self):
        for observed in (False, True):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = {"config": project(), "issue": {"number": 17}, "run_key": "101.1"}
                data = root / "data"
                data.mkdir()
                if observed:
                    log = RunLog(
                        data / "diagnostics",
                        session_identity(context, "task"),
                        emit=lambda line: None,
                    )
                    # Simulate an abruptly ended observer without a final event.
                    for handle in (log.trace, log.activity, log.stderr):
                        handle.close()
                with (
                    patch("nexkit.adapters.codex.CodexAdapter.stop_session"),
                    patch.dict(os.environ, {"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "2"}),
                ):
                    # The trusted cleanup step forwards native IDs even when
                    # bootstrap failed before observation preparation.
                    if observed:
                        identity = read_regular_json(data / "diagnostics/run.json")
                        identity["run_key"] = "101.2"
                        write_json(data / "diagnostics/run.json", identity)
                    identity = finish(context, "task", data, "cancelled")
                self.assertFalse(identity["result_available"])
                self.assertEqual(identity["run_key"], "101.2")
                self.assertEqual(identity["status"], "interrupted" if observed else "unavailable")
                self.assertIn(
                    "No valid final agent result", (data / "diagnostics/report.md").read_text()
                )

    def test_changed_session_binding_and_linked_reports_are_not_collected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = {"config": project(), "run_key": "101.1"}
            identity = session_identity(context, "task")
            log = RunLog(root / "diagnostics", identity, emit=lambda line: None)
            log.finish(0)
            identity["context"] = "different"
            write_json(root / "observation.json", identity)
            with (
                patch("nexkit.adapters.codex.CodexAdapter.stop_session"),
                self.assertRaisesRegex(Blocked, "accepted session"),
            ):
                finish(context, "task", root, "success")
            outside = root / "private"
            outside.write_text("PRIVATE_CANARY")
            (root / "diagnostics/report.md").symlink_to(outside)
            with self.assertRaises(Blocked):
                summary(root)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_ISOLATION") == "1",
    "Disposable Linux runner with passwordless sudo and nexkit-agent account",
)
class NativeApiObserverTests(unittest.TestCase):
    def test_real_sudo_identity_prompt_events_and_timeout_are_preserved(self):
        import pwd

        command_user = pwd.getpwnam("nexkit-agent")
        self.assertNotEqual(os.geteuid(), command_user.pw_uid)
        for mode in ("success", "timeout"):
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory(prefix="observer é ") as temporary,
            ):
                root = Path(temporary)
                root.chmod(0o755)
                metadata = root / "observation.json"
                write_json(
                    metadata, {"role": "task", "timeout_seconds": 0.3 if mode == "timeout" else 5}
                )
                with patch.dict(os.environ, {"GITHUB_PATH": ""}):
                    shim = prepare_api_observer(root)
                binary = root / "codex"
                binary.write_text(
                    "#!/usr/bin/python3\nimport json,os,sys,time\n"
                    "assert sys.argv[1] == 'exec' and '--json' in sys.argv\n"
                    "assert sys.stdin.read() == 'accepted prompt é'\n"
                    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':str(os.getuid())}}),flush=True)\n"
                    + ("time.sleep(30)\n" if mode == "timeout" else "")
                )
                binary.chmod(0o755)
                with patch.dict(os.environ, {"GH_TOKEN": "native-token-canary"}):
                    result = subprocess.run(
                        [str(shim / "sudo"), "-u", "nexkit-agent", "--", str(binary), "exec"],
                        input="accepted prompt é",
                        text=True,
                        capture_output=True,
                        timeout=10,
                    )
                self.assertEqual(result.returncode, 124 if mode == "timeout" else 0, result.stderr)
                events = read_trace(root / "diagnostics/events.jsonl")
                self.assertIn(str(command_user.pw_uid), [e.get("message") for e in events])
                self.assertEqual(
                    read_regular_json(root / "diagnostics/run.json")["status"],
                    "timed_out" if mode == "timeout" else "succeeded",
                )
                delegated = subprocess.run([str(shim / "sudo"), "-n", "true"], capture_output=True)
                self.assertEqual(delegated.returncode, 0)
                self.assertNotIn("native-token-canary", result.stdout + result.stderr)

"""Real source snapshots and forced fake-agent timeout, without model access."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.checkpoints import (
    archive_data,
    artifact_prefix,
    binding,
    capture,
    consume,
    discard_proposal,
    discover,
    reconcile_discard,
    record,
    restore,
    select,
    settings,
    validate_payload,
)
from nexkit.ci import collect, materialize
from nexkit.common import Blocked, digest, read_json, run, write_json
from nexkit.policy import now
from nexkit.workspace import remove, snapshot
from runner.job_hook import stop_checkpoint_monitor
from tests.support import FakeGitHub, issue
from tests.test_agent_session import AcceptedSessionTests


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        # Portable snapshot contracts; native OS enforcement has separate coverage.
        self.enterContext(patch("nexkit.agent_session.require_native", return_value="linux"))
        self.temporary = tempfile.TemporaryDirectory(prefix="nexkit-checkpoints-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context, self.home, self.data = AcceptedSessionTests().fixture(self.root, "deliver")
        self.context.update(issue=issue(), run_key="100.1")
        self.runtime = read_json(self.data / "agent-runtime.json")
        self.runtime["context"] = digest(self.context)
        write_json(self.data / "agent-runtime.json", self.runtime)
        self.source, self.work = self.root / "source", self.home / "work"
        self.enterContext(
            patch.dict(os.environ, {"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1"})
        )

    def seal(self, home, data, context=None):
        context = context or self.context
        snapshot(home, data)
        (home / "output").mkdir(exist_ok=True)
        write_json(
            data / "agent-runtime.json",
            {
                **self.runtime,
                "context": digest(context),
                "home": str(home),
                "workspace": str(home / "work"),
                "output": str(home / "output/result.json"),
            },
        )

    def interrupted(self):
        program = "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"print('interrupted-edit')\\n\"); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, "-c", program, str(self.work / "app.py")], check=True, timeout=0.3
            )
        with self.assertRaises(Blocked):
            collect(
                self.source,
                self.work,
                self.context,
                self.home / "output/result.json",
                self.root / "collected.json",
                "deliver",
                self.data / "initial.json",
            )
        return capture(
            self.source, self.work, self.context, "deliver", self.data, 1, capture_point="final"
        )

    def restore_input(self, folder, *, context=None, role="deliver"):
        context = context or self.context
        data = self.root / "fresh-data"
        data.mkdir()
        shutil.copytree(folder, data / "recovery-input")
        fresh = self.root / "fresh-work"
        return materialize(self.source, fresh, context, role, data), fresh

    def test_forced_timeout_without_final_result_restores_exact_source_to_fresh_session(self):
        folder, manifest = self.interrupted()
        self.assertTrue(manifest["untrusted_partial_work"])
        self.assertFalse((self.root / "collected.json").exists())
        remove(self.home)  # Recovery has no dependence on retained agent disk.
        _, fresh = self.restore_input(folder)
        self.assertIn("interrupted-edit", (fresh / "app.py").read_text())
        restored = read_json(self.root / "fresh-data/recovery-restored.json")
        self.assertEqual(restored["restored_paths"], ["app.py"])
        self.assertNotIn("context_snapshot", restored["manifest"])
        self.assertEqual(restored["manifest_sha256"], digest(manifest))
        self.assertNotEqual(restored["manifest_sha256"], digest(restored["manifest"]))
        self.assertIn("Untrusted partial work", (self.root / "fresh-data/prompt.txt").read_text())
        run(["git", "diff", "--check"], cwd=fresh)
        final = self.root / "complete-result.json"
        write_json(
            final,
            {
                "status": "done",
                "summary": "Rechecked recovered work",
                "skills_used": ["nexkit-deliver"],
                "commands": ["git diff --check"],
                "limitations": [],
            },
        )
        report = collect(
            self.source,
            fresh,
            self.context,
            final,
            self.root / "collected.json",
            "deliver",
            self.root / "fresh-data/initial.json",
        )
        self.assertEqual(report["recovered_checkpoint"], digest(manifest))
        state = {
            "agent_calls": 4,
            "recovery": {"deliver/deliver": {"manifest_sha256": digest(manifest)}},
        }
        consume(state, self.context, "deliver", report)
        self.assertFalse(state["recovery"])
        self.assertEqual(state["agent_calls"], 4)

    def test_prior_candidate_and_interrupted_new_file_are_both_restored(self):
        (self.work / "app.py").write_text("print('prior-published-candidate')\n")
        (self.work / "new.py").write_text("print('interrupted-addition')\n")
        folder, _ = capture(self.source, self.work, self.context, "deliver", self.data, 1)
        _, fresh = self.restore_input(folder)
        self.assertIn("prior-published-candidate", (fresh / "app.py").read_text())
        self.assertIn("interrupted-addition", (fresh / "new.py").read_text())

    def test_actual_prior_candidate_reverts_and_deletions_survive_interruption(self):
        for argument in ("user.name=fixture", "user.email=fixture@example.invalid"):
            run(
                ["git", "config", argument.split("=", 1)[0], argument.split("=", 1)[1]],
                cwd=self.source,
            )
        run(["git", "commit", "-m", "base"], cwd=self.source)
        base = run(["git", "rev-parse", "HEAD"], cwd=self.source).stdout.strip()
        (self.source / "app.py").write_text("print('prior candidate')\n")
        (self.source / "prior-only.py").write_text("print('remove this prior addition')\n")
        run(["git", "add", "."], cwd=self.source)
        run(["git", "commit", "-m", "prior published candidate"], cwd=self.source)
        candidate = run(["git", "rev-parse", "HEAD"], cwd=self.source).stdout.strip()
        run(["git", "checkout", "--detach", base], cwd=self.source)
        remove(self.work)
        remove(self.data)
        self.context.update(base=base, source=candidate)
        materialize(self.source, self.work, self.context, "deliver", self.data)
        self.seal(self.home, self.data)
        self.assertIn("prior candidate", (self.work / "app.py").read_text())
        (self.work / "app.py").write_text((self.source / "app.py").read_text())
        (self.work / "prior-only.py").unlink()
        (self.work / "addition.py").write_text("print('interrupted next change')\n")
        folder, _ = capture(self.source, self.work, self.context, "deliver", self.data, 1)
        _, fresh = self.restore_input(folder)
        self.assertEqual((fresh / "app.py").read_text(), (self.source / "app.py").read_text())
        self.assertFalse((fresh / "prior-only.py").exists())
        self.assertIn("interrupted next change", (fresh / "addition.py").read_text())

    def test_a_second_interruption_can_restore_without_requiring_the_first_agent_disk(self):
        folder, _ = self.interrupted()
        home, data = self.root / "second-home", self.root / "second-data"
        home.mkdir()
        shutil.copytree(folder, data / "recovery-input")
        materialize(self.source, home / "work", self.context, "deliver", data)
        self.seal(home, data)
        (home / "work/app.py").write_text("print('second interrupted edit')\n")
        (home / "work/added.py").write_text("print('second addition')\n")
        second, _ = capture(self.source, home / "work", self.context, "deliver", data, 1)
        durable = self.root / "durable-second"
        shutil.copytree(second, durable)
        remove(home)
        remove(data)
        _, fresh = self.restore_input(durable)
        self.assertIn("second interrupted edit", (fresh / "app.py").read_text())
        self.assertIn("second addition", (fresh / "added.py").read_text())

    def test_ordinary_file_directory_transitions_restore_before_any_agent_runs(self):
        (self.work / "app.py").unlink()
        (self.work / "app.py").mkdir()
        (self.work / "app.py/entry.py").write_text("print('module')\n")
        folder, _ = capture(self.source, self.work, self.context, "deliver", self.data, 1)
        _, fresh = self.restore_input(folder)
        self.assertIn("module", (fresh / "app.py/entry.py").read_text())
        # A repeated restore is safe, including the deletion of the former file.
        restore(self.source, fresh, self.context, "deliver", self.root / "fresh-data")

    def test_retry_provenance_does_not_change_inputs_but_changed_task_findings_do(self):
        first = deepcopy(self.context)
        first["previous_outputs"] = [
            {
                "run_key": "99.1",
                "producer": {"run_key": "99.1"},
                "invocation": {"id": "investigate", "run_key": "99.1"},
                "result": {"summary": "same actual findings"},
            }
        ]
        second = deepcopy(first)
        second["run_key"] = "101.1"
        second["previous_outputs"][0].update(run_key="101.1", producer={"run_key": "101.1"})
        second["previous_outputs"][0]["invocation"]["run_key"] = "101.1"
        self.assertEqual(binding(first, "deliver"), binding(second, "deliver"))
        second["previous_outputs"][0]["result"]["summary"] = "changed actual findings"
        self.assertNotEqual(binding(first, "deliver"), binding(second, "deliver"))

    def test_superseded_workspace_cannot_be_captured_by_an_old_monitor(self):
        (self.work / "app.py").write_text("print('pending')\n")
        self.work.rename(self.home / "old-work")
        self.work.mkdir()
        (self.work / "app.py").write_text("print('another job')\n")
        with self.assertRaisesRegex(Blocked, "replaced a workspace"):
            capture(self.source, self.work, self.context, "deliver", self.data, 1)

    def test_native_checkpoint_record_falls_back_and_retires_exact_owned_inputs(self):
        folder, manifest = self.interrupted()
        payload = read_json(folder / "payload.json")
        gh = FakeGitHub()
        gh.state = {
            "run_key": "100.1",
            "agent_calls": 2,
            "checkpoint_context": digest(self.context),
        }
        prefix = artifact_prefix(self.context, "deliver")
        artifacts = [
            {
                "id": index,
                "name": prefix + str(index),
                "workflow_run": {"id": 100, "head_sha": self.context["base"]},
            }
            for index in (1, 2)
        ]
        native = {
            "status": "completed",
            "head_sha": self.context["base"],
            "head_branch": "main",
            "repository": {"full_name": gh.repository},
            "path": ".github/workflows/nexkit-delivery.yml",
            "referenced_workflows": [
                {
                    "sha": "a" * 40,
                    "path": "phuongnse/nexkit/.github/workflows/delivery.yml@" + "a" * 40,
                }
            ],
        }

        def transfer(_gh, artifact):
            if artifact["id"] == 2:
                raise Blocked("Incomplete upload")
            return manifest, payload

        with (
            patch("nexkit.github.run_attempt", return_value=native),
            patch.object(gh, "api", return_value=artifacts),
            patch("nexkit.checkpoints.download", side_effect=transfer),
        ):
            with patch.dict(os.environ, {"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1"}):
                preview, _ = discover(gh, 1, persist=False)
                self.assertEqual(preview["recovery"]["deliver/deliver"]["artifact"], 1)
                self.assertNotIn("recovery", gh.state)
                self.assertEqual(gh.revision, 0)
                discover(gh, 1)
                self.assertEqual(gh.state["recovery"]["deliver/deliver"]["artifact"], 1)
            receipt = record(gh, self.context, "deliver")
            self.assertNotIn("context_snapshot", receipt["manifest"])
            self.assertLess(len(json.dumps(receipt)), 6000)
            self.assertEqual(receipt["artifact"], 1)
            self.assertEqual(receipt["capture_failures"][0]["artifact"], 2)
            revision = gh.revision
            record(gh, self.context, "deliver")
            self.assertEqual(gh.revision, revision)
            with self.assertRaises(Blocked):
                consume(gh.state, self.context, "deliver", {"recovered_checkpoint": "f" * 64})
            consume(
                gh.state,
                self.context,
                "deliver",
                {"recovered_checkpoint": receipt["manifest_sha256"]},
            )
            self.assertFalse(gh.state["recovery"])
            self.assertEqual(gh.state["agent_calls"], 2)
            self.assertEqual(record(gh, self.context, "deliver")["restore_state"], "retired")

    def test_checkpoint_discard_requires_current_unedited_administrator_decision(self):
        _, manifest = self.interrupted()
        scope = {name: value for name, value in manifest.items() if name != "context_snapshot"}
        receipt = {
            "artifact": 1,
            "manifest": scope,
            "manifest_scope_sha256": digest(scope),
            "manifest_sha256": digest(manifest),
        }
        original = {"recovery": {"deliver/deliver": receipt}, "agent_calls": 4}
        gh = FakeGitHub()
        for mutation in ("reader", "edited", "stale", "valid"):
            state = deepcopy(original)
            proposal = discard_proposal(gh.work, self.context["config"], state)
            stamp = now()
            gh.discussion = [
                {
                    "id": 30,
                    "body": proposal["human_comment"],
                    "created_at": stamp,
                    "updated_at": "edited" if mutation == "edited" else stamp,
                    "user": {
                        "type": "User",
                        "login": "reader" if mutation == "reader" else "owner",
                    },
                }
            ]
            if mutation == "stale":
                state["recovery"]["deliver/deliver"]["artifact"] = 2
            with self.subTest(mutation=mutation):
                self.assertEqual(
                    reconcile_discard(gh, gh.work, self.context["config"], state),
                    mutation == "valid",
                )
                self.assertEqual(state["agent_calls"], 4)
                if mutation == "valid":
                    self.assertFalse(state.get("recovery"))
                    self.assertEqual(state["recovery_history"][0]["comment"], 30)
                else:
                    self.assertIsNotNone(select(state, self.context, "deliver"))

    def test_conflicts_and_changed_specification_config_source_or_role_are_rejected(self):
        folder, manifest = self.interrupted()
        payload = read_json(folder / "payload.json")
        for field in ("spec", "config", "source", "base", "role"):
            bad = deepcopy(manifest)
            bad["binding"][field] = "changed"
            with self.subTest(field=field), self.assertRaises(Blocked):
                validate_payload(bad, payload, self.context, "deliver")
        fresh, data = self.root / "conflict", self.root / "conflict-data"
        materialize(self.source, fresh, self.context, "deliver", data)
        (fresh / "app.py").write_text("print('conflicting-source')\n")
        shutil.copytree(folder, data / "recovery-input")
        with self.assertRaisesRegex(Blocked, "conflicts"):
            restore(self.source, fresh, self.context, "deliver", data)
        self.assertIn("conflicting-source", (fresh / "app.py").read_text())

    def test_control_edits_links_and_credentials_cannot_cross_checkpoint_boundary(self):
        for name, content in (
            (".github/workflows/forged.yml", "jobs: {}"),
            (".env", "PRIVATE=canary"),
            ("secret.py", "key='sk-" + "x" * 40 + "'"),
        ):
            with self.subTest(name=name):
                target = self.work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                with self.assertRaises(Blocked):
                    capture(self.source, self.work, self.context, "deliver", self.data, 1)
                target.unlink()
        (self.work / "link.py").symlink_to(self.source / "app.py")
        with self.assertRaises(Blocked):
            capture(self.source, self.work, self.context, "deliver", self.data, 1)

    def test_corrupt_partial_or_unsafe_archive_is_rejected(self):
        folder, _ = self.interrupted()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            for name in ("manifest.json", "payload.json"):
                zipped.writestr(name, (folder / name).read_bytes())
        manifest, payload = archive_data(archive.getvalue())
        self.assertEqual(payload["changes"][0]["path"], "app.py")
        for contents in (
            {"manifest.json": json.dumps(manifest), "payload.json": "{}"},
            {"../manifest.json": "{}", "payload.json": "{}"},
            {"manifest.json": "{}"},
        ):
            invalid = io.BytesIO()
            with zipfile.ZipFile(invalid, "w") as zipped:
                for name, text in contents.items():
                    zipped.writestr(name, text)
            with self.assertRaises(Blocked):
                archive_data(invalid.getvalue())

    def test_readonly_draft_is_restored_as_untrusted_input_without_editing_source(self):
        self.runtime["role"] = "task"
        write_json(self.data / "agent-runtime.json", self.runtime)
        write_json(
            self.home / "output/handover.json",
            {"draft": "Partial task findings", "remaining": "Check the failing path"},
        )
        folder, _ = capture(self.source, self.work, self.context, "task", self.data, 1)
        _, fresh = self.restore_input(folder, role="task")
        self.assertEqual((fresh / "app.py").read_text(), (self.source / "app.py").read_text())
        self.assertIn("Partial task findings", (self.root / "fresh-data/prompt.txt").read_text())

    def test_capture_failure_does_not_replace_previous_complete_snapshot(self):
        folder, _ = self.interrupted()
        initial = (folder / "payload.json").read_bytes()
        with patch("nexkit.ci.file_snapshot", side_effect=[{}, {"different": {}}, {}]):
            with self.assertRaises(Blocked):
                capture(self.source, self.work, self.context, "deliver", self.data, 2)
        self.assertEqual(initial, (folder / "payload.json").read_bytes())
        self.assertFalse((self.data / "checkpoints/2").exists())

    def test_recovery_settings_reserve_artifact_slots_for_final_reports(self):
        for value in (True, 1, 9):
            changed = deepcopy(self.context)
            changed["config"]["recovery"] = {"max_checkpoints": value}
            with self.subTest(value=value), self.assertRaises(Blocked):
                settings(changed)
        self.assertEqual(settings(self.context)["max_checkpoints"], 6)


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_CHECKPOINT_MONITOR") == "1",
    "Actual root monitor and hook cleanup in a disposable Linux container",
)
class NativeCheckpointMonitorTests(unittest.TestCase):
    def test_detached_monitor_uploads_intervals_scrubs_credentials_and_finishes_before_hook_cleanup(
        self,
    ):
        self.assertEqual(os.geteuid(), 0)
        self.assertTrue(Path("/.dockerenv").is_file())
        with tempfile.TemporaryDirectory(prefix="nexkit-monitor-") as temporary:
            root = Path(temporary)
            context, home, data = AcceptedSessionTests().fixture(root, "deliver")
            context.update(issue=issue(), run_key="100.1")
            context["config"]["limits"]["minutes"] = 2
            context["config"]["recovery"] = {"checkpoint_seconds": 10}
            runtime = read_json(data / "agent-runtime.json")
            runtime["context"] = digest(context)
            write_json(data / "agent-runtime.json", runtime)
            write_json(data / "context.json", context)
            (home / "work/app.py").write_text("print('actual monitored source')\n")
            sdk = root / "sdk"
            module = sdk / "node_modules/@actions/artifact/index.js"
            module.parent.mkdir(parents=True)
            module.write_text(
                """const fs = require('node:fs'); const path = require('node:path');
exports.DefaultArtifactClient = class {
  async uploadArtifact(name, files, folder) {
    if (process.env.GH_TOKEN || process.env.OPENAI_API_KEY) throw new Error('Credential crossing');
    const marker = path.resolve(__dirname, '../../../uploaded.json');
    const records = fs.existsSync(marker) ? JSON.parse(fs.readFileSync(marker)) : [];
    const manifest = JSON.parse(fs.readFileSync(path.join(folder, 'manifest.json')));
    records.push({name, point: manifest.capture_point}); fs.writeFileSync(marker, JSON.stringify(records));
    return {id: records.length, size: 1};
  }
};
""",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nexkit.checkpoints",
                    "monitor",
                    "--context",
                    str(data / "context.json"),
                    "--role",
                    "deliver",
                    "--source",
                    str(root / "source"),
                    "--node",
                    shutil.which("node"),
                    "--sdk",
                    str(sdk),
                ],
                input=json.dumps(
                    {
                        "ACTIONS_RUNTIME_TOKEN": "synthetic-artifact-credential",
                        "ACTIONS_RESULTS_URL": "https://fixture.invalid",
                        "GITHUB_RUN_ID": "100",
                        "GITHUB_RUN_ATTEMPT": "1",
                    }
                ),
                env={
                    **os.environ,
                    "GH_TOKEN": "controller-canary",
                    "OPENAI_API_KEY": "model-canary",
                },
                text=True,
                check=True,
                timeout=10,
                capture_output=True,
            )
            metadata = read_json(data / "checkpoint-monitor.json")
            pid = metadata["pid"]
            try:
                import time

                deadline = time.monotonic() + 25
                while not (sdk / "uploaded.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(
                    (sdk / "uploaded.json").exists(),
                    "No interval upload reached the simulated transport",
                )
                stop_checkpoint_monitor(data)
                metadata = read_json(data / "checkpoint-monitor.json")
                self.assertEqual(metadata["status"], "completed")
                records = read_json(sdk / "uploaded.json")
                self.assertEqual(records[0]["point"], "interval")
                self.assertEqual(records[-1]["point"], "final")
                self.assertLessEqual(metadata["upload_attempts"], 6)
                self.assertIsNone(metadata["capture_failure"])
            finally:
                if read_json(data / "checkpoint-monitor.json").get("status") == "running":
                    import signal

                    os.killpg(pid, signal.SIGKILL)

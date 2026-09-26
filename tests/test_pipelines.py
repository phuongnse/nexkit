"""Native workflow configuration and controller boundaries; no live model calls."""

import base64
import hashlib
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.cli import create_request, intake_status, submit
from nexkit.common import Blocked, digest, read_json, write_json
from nexkit.delivery import failed, prepare, publish
from nexkit.pipelines import effective_config, entrypoint, issue_pipeline
from nexkit.policy import candidate_key, config
from nexkit.project import doctor, install, uninstall
from tests.support import FakeGitHub, approve, project
from tests.test_delivery import bundle
from tests.test_intake import IntakeGitHub


def composed():
    legacy = project()
    workflows = {
        ".github/workflows/changes.yml": b"name: Changes\non: workflow_dispatch\njobs: {}\n",
        ".github/workflows/capture.yaml": b"name: Capture\non: workflow_dispatch\njobs: {}\n",
        ".github/workflows/audit.yml": b"name: Audit\non: workflow_dispatch\njobs: {}\n",
    }
    cfg = {
        "schema": 2,
        **{key: legacy[key] for key in ("repository", "default_branch", "kit")},
        "defaults": {
            key: value
            for key, value in legacy.items()
            if key not in ("schema", "repository", "default_branch", "kit")
        },
        "pipelines": {
            "maintenance": {
                "settings": {},
                "entrypoints": {
                    "intake": ".github/workflows/capture.yaml",
                    "delivery": ".github/workflows/changes.yml",
                },
                "agent_workflows": [".github/workflows/changes.yml"],
            },
            "audit": {"settings": {}, "entrypoints": {}, "agent_workflows": []},
        },
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "managed": True}
            for name, content in workflows.items()
        },
    }
    return cfg, workflows


class ComposedGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.cfg, self.files = composed()
        self.work["body"] = (
            "<!-- nexkit:request:maintenance:change -->\n<!-- nexkit:pipeline:maintenance -->\n"
            + self.work["body"]
        )
        self.discussion = [approve(self.work)]

    def content(self, path, ref):
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(self.files[path]).decode(),
        }


class PipelineTests(unittest.TestCase):
    def test_explicit_selection_and_map_inheritance_do_not_mutate_siblings(self):
        cfg, _ = composed()
        cfg["pipelines"]["maintenance"]["settings"] = {
            "models": {"review": "review-model"},
            "knowledge": ["docs/design.md"],
        }
        original = deepcopy(cfg)
        selected = effective_config(cfg, "maintenance")
        self.assertEqual(selected["models"], {"implement": "test-model", "review": "review-model"})
        self.assertEqual(selected["knowledge"], ["docs/design.md"])
        self.assertEqual(effective_config(cfg, "audit")["knowledge"], ["README.md"])
        self.assertEqual(cfg, original)
        self.assertEqual(selected["binding"]["project"], digest(cfg))
        with self.assertRaises(Blocked):
            effective_config(cfg)
        with self.assertRaises(Blocked):
            entrypoint(effective_config(cfg, "audit"), "delivery")

    def test_native_only_pipeline_needs_no_dummy_models_checks_or_release(self):
        cfg, _ = composed()
        cfg["defaults"] = {
            "decisions": ["Run the repository's native audit workflow"],
            "knowledge": ["README.md"],
        }
        cfg["pipelines"].pop("maintenance")
        value = effective_config(cfg, "audit")
        self.assertNotIn("engine", value)
        self.assertNotIn("checks", value)
        self.assertNotIn("release", value)

    def test_clarification_only_pipeline_needs_no_reviewer_or_delivery_budget(self):
        cfg, _ = composed()
        cfg["defaults"] = {
            "decisions": ["Clarify requirements before selecting delivery"],
            "knowledge": [],
            "engine": {"name": "codex", "version": "0.156.1"},
            "models": {"implement": "clarification-model"},
            "reasoning_effort": {"implement": "max"},
            "environment": {"runner": "ubuntu-24.04", "setup": []},
            "clarification": {"agent_minutes": 5},
        }
        cfg["pipelines"] = {
            "conversation": {
                "settings": {},
                "entrypoints": {"clarify": ".github/workflows/changes.yml"},
                "agent_workflows": [],
            }
        }
        self.assertNotIn("review", effective_config(cfg, "conversation")["models"])

    def test_release_only_pipeline_needs_no_agent_call_budget(self):
        cfg, _ = composed()
        for field in ("engine", "models", "merge_method"):
            cfg["defaults"].pop(field)
        cfg["defaults"]["limits"].pop("agent_calls")
        cfg["defaults"]["release"] = {
            "enabled": True,
            "build": ["python3", "build.py"],
            "artifacts": ["dist/package.zip"],
            "tag_prefix": "v",
        }
        cfg["pipelines"] = {
            "shipping": {
                "settings": {},
                "entrypoints": {"release": ".github/workflows/changes.yml"},
                "agent_workflows": [],
            }
        }
        self.assertNotIn("agent_calls", effective_config(cfg, "shipping")["limits"])

    def test_identity_overrides_unknown_fields_and_dynamic_paths_are_rejected(self):
        for settings in (
            {"repository": "someone/else"},
            {"stage_order": ["implement"]},
            {"limits": {"agent_calls": None}},
        ):
            cfg, _ = composed()
            cfg["pipelines"]["maintenance"]["settings"] = settings
            with self.subTest(settings=settings), self.assertRaises(Blocked):
                config(cfg)
        cfg, _ = composed()
        cfg["pipelines"]["maintenance"]["entrypoints"]["delivery"] = ".github/workflows/*.yml"
        with self.assertRaises(Blocked):
            config(cfg)

    def test_explicit_null_agent_runner_is_invalid_in_both_schemas(self):
        legacy = project()
        legacy["environment"]["agent_runner"] = None
        with self.assertRaisesRegex(Blocked, "runner"):
            config(legacy)
        cfg, _ = composed()
        cfg["pipelines"]["maintenance"]["settings"] = {"environment": {"agent_runner": None}}
        with self.assertRaisesRegex(Blocked, "runner"):
            effective_config(cfg, "maintenance")

    def test_pipeline_identity_separates_candidates_and_intake_keys(self):
        cfg, _ = composed()
        a, b = effective_config(cfg, "maintenance"), effective_config(cfg, "audit")
        gh = ComposedGitHub()
        self.assertNotEqual(
            candidate_key(gh.work, a, "b" * 40, "c" * 40),
            candidate_key(gh.work, b, "b" * 40, "c" * 40),
        )
        intake = IntakeGitHub()
        for selected in ("maintenance", "audit"):
            created = create_request(
                intake, "Feature", "Expected behavior", "same-key", pipeline=selected
            )
            self.assertEqual(issue_pipeline(created["issue"]), selected)
            self.assertTrue(
                intake_status(intake, "request", "same-key", pipeline=selected)["found"]
            )
        self.assertEqual(len(intake.issues), 2)
        submit(
            intake,
            a,
            "request",
            {"title": "Feature", "request": "Expected behavior", "key": "same-key"},
        )
        self.assertEqual(intake.dispatched[-1][0], "capture.yaml")
        self.assertEqual(intake.dispatched[-1][2]["pipeline"], "maintenance")

    def env(self, path="changes.yml"):
        return {
            "GITHUB_WORKFLOW_REF": f"owner/project/.github/workflows/{path}@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": "b" * 40,
        }

    def test_retry_dispatches_the_recorded_pipeline_and_protects_all_control_files(self):
        gh = ComposedGitHub()
        with patch.dict(os.environ, self.env()):
            context = prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(context["ready"], context)
        for path in (
            ".github/workflows/audit.yml",
            ".github/workflows/new.yml",
            ".nexkit/controls/new.txt",
        ):
            output = bundle(context)
            output["changes"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(Blocked):
                publish(gh, context, output)
        self.assertEqual(failed(gh, context, "Check failed")["status"], "retry")
        self.assertEqual(
            gh.dispatches[-1], ("changes.yml", "main", {"issue": 1, "pipeline": "maintenance"})
        )

    def test_wrong_pipeline_cannot_disturb_an_active_work_item(self):
        gh = ComposedGitHub()
        with patch.dict(os.environ, self.env()):
            self.assertTrue(prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")["ready"])
            state = deepcopy(gh.state)
            self.assertFalse(prepare(gh, 1, "101.1", "a" * 40, pipeline="audit")["ready"])
            self.assertEqual(gh.state, state)

    def test_actual_workflow_bytes_and_executing_revision_must_match(self):
        for drift in ("bytes", "revision", "caller"):
            gh = ComposedGitHub()
            env = self.env("audit.yml" if drift == "caller" else "changes.yml")
            if drift == "bytes":
                gh.files[".github/workflows/changes.yml"] += b"# drift\n"
            if drift == "revision":
                env["GITHUB_WORKFLOW_SHA"] = "e" * 40
                gh.read_config = lambda ref: (
                    {**deepcopy(gh.cfg), "default_branch": "old"}
                    if ref == "e" * 40
                    else deepcopy(gh.cfg)
                )
            with self.subTest(drift=drift), patch.dict(os.environ, env):
                self.assertFalse(prepare(gh, 1, "100.1", "a" * 40, pipeline="maintenance")["ready"])
            self.assertNotIn("agent_calls", gh.state)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root, self.bundle = Path(self.tmp.name, "consumer"), Path(self.tmp.name, "bundle")
        self.root.mkdir()
        self.cfg, files = composed()
        for name, content in files.items():
            target = self.bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def install(self, apply=True):
        return install(self.root, self.cfg, ["codex"], apply=apply, bundle=self.bundle)

    def test_arbitrary_file_set_preview_reinstall_and_safe_removal(self):
        preview = self.install(False)
        self.assertFalse((self.root / ".github").exists())
        self.assertIn(".github/workflows/audit.yml", {c["path"] for c in preview["changes"]})
        self.install()
        self.assertEqual(read_json(self.root / ".nexkit/project.json"), self.cfg)
        self.assertFalse((self.root / ".github/workflows/nexkit-delivery.yml").exists())
        self.assertEqual(self.install()["changes"], [])
        del self.cfg["files"][".github/workflows/audit.yml"]
        changes = self.install()["changes"]
        self.assertIn(
            {"path": ".github/workflows/audit.yml", "operation": "remove"},
            [{k: c[k] for k in ("path", "operation")} for c in changes],
        )
        self.assertFalse((self.root / ".github/workflows/audit.yml").exists())
        self.assertIn(".github/workflows/changes.yml", uninstall(self.root)["removed"])

    def test_edited_obsolete_workflow_stops_before_other_mutations(self):
        self.install()
        target = self.root / ".github/workflows/audit.yml"
        target.write_text("# Consumer edit\n")
        old = (self.root / ".nexkit/project.json").read_bytes()
        del self.cfg["files"][".github/workflows/audit.yml"]
        with self.assertRaisesRegex(Blocked, "Consumer edits preserved"):
            self.install()
        self.assertEqual(target.read_text(), "# Consumer edit\n")
        self.assertEqual((self.root / ".nexkit/project.json").read_bytes(), old)

    def test_empty_file_addition_and_removal_are_both_visible_in_preview(self):
        name = ".nexkit/controls/empty.py"
        target = self.bundle / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"")
        self.cfg["files"][name] = {"sha256": hashlib.sha256(b"").hexdigest(), "managed": True}
        self.assertIn(
            {"path": name, "operation": "add", "diff": ""}, self.install(False)["changes"]
        )
        self.install()
        del self.cfg["files"][name]
        self.assertIn(
            {"path": name, "operation": "remove", "diff": ""}, self.install(False)["changes"]
        )
        self.install()
        self.assertFalse((self.root / name).exists())

    def test_hash_mismatch_and_symlink_are_rejected_before_writes(self):
        target = self.bundle / ".github/workflows/audit.yml"
        target.write_text("# Wrong bundle\n")
        with self.assertRaisesRegex(Blocked, "hash mismatch"):
            self.install()
        self.assertEqual(list(self.root.iterdir()), [])
        target.unlink()
        target.symlink_to(self.bundle / ".github/workflows/changes.yml")
        with self.assertRaisesRegex(Blocked, "symlink"):
            self.install()

    def test_consumer_ownership_is_not_adopted_or_removed_by_uninstall(self):
        self.install()
        target = ".github/workflows/audit.yml"
        self.cfg["files"][target]["managed"] = False
        changes = self.install()["changes"]
        self.assertIn("preserve-as-consumer", [item["operation"] for item in changes])
        uninstall(self.root)
        self.assertTrue((self.root / target).is_file())

    def test_ledger_cannot_adopt_arbitrary_workflows_or_application_files(self):
        self.install()
        ledger_path = self.root / ".nexkit/installation.json"
        ledger = read_json(ledger_path)
        name = ".github/workflows/customer.yml"
        (self.root / name).write_text("# Consumer-owned\n")
        ledger["bundle_files"].append(name)
        ledger["files"][name] = hashlib.sha256((self.root / name).read_bytes()).hexdigest()
        write_json(ledger_path, ledger)
        self.assertIn(name, uninstall(self.root)["preserved"])

    def test_doctor_reports_native_execution_as_unverified_without_dummy_checks(self):
        self.cfg["defaults"] = {"decisions": ["Use native audit jobs"], "knowledge": []}
        self.cfg["pipelines"].pop("maintenance")
        self.install()
        result = doctor(self.root, self.cfg, checks=True)
        self.assertFalse(result["ready"])
        self.assertIn("native workflow", result["pipelines"]["audit"]["problems"][0])

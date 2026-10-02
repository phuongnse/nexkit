"""Accepted native session identities and safe result collection; no model calls."""

import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.agent_session import configuration, minutes, prepared
from nexkit.ci import materialize
from nexkit.common import Blocked, digest, read_regular_json, write_json
from nexkit.workspace import snapshot
from tests.support import project


class AcceptedSessionTests(unittest.TestCase):
    def setUp(self):
        # File/config contracts are portable. OS enforcement is tested separately.
        patched = patch("nexkit.agent_session.require_native", return_value="linux")
        patched.start()
        self.addCleanup(patched.stop)

    def fixture(self, root, role):
        source, home, data = (root / name for name in ("source", "home", "data"))
        source.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "--quiet", str(source)], check=True)
        (source / "app.py").write_text("print('fixture')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
        cfg = project()
        cfg["environment"]["agent_runner"] = "ubuntu-24.04"
        cfg["models"] = {"implement": "accepted-writer", "review": "accepted-reviewer"}
        cfg["reasoning_effort"] = {"implement": "max", "review": "high"}
        context = {"config": cfg, "source": "b" * 40, "base": "b" * 40}
        work = home / "work"
        materialize(source, work, context, role, data)
        snapshot(home, data)
        (home / "output").mkdir()
        write_json(
            data / "agent-runtime.json",
            {
                "schema": 1,
                "role": role,
                "context": digest(context),
                "native": "linux",
                "home": str(home),
                "workspace": str(work),
                "output": str(home / "output/result.json"),
            },
        )
        return context, home, data

    def test_role_selects_the_accepted_model_effort_and_prepared_paths(self):
        for role in ("request", "deliver", "review", "task"):
            with (
                self.subTest(role=role),
                tempfile.TemporaryDirectory(prefix="session é ") as temporary,
            ):
                context, home, data = self.fixture(Path(temporary), role)
                with patch.dict(os.environ, {"GITHUB_OUTPUT": "", "GITHUB_ENV": ""}):
                    value = configuration(context, role, data)
                self.assertEqual(
                    value["model"], "accepted-reviewer" if role == "review" else "accepted-writer"
                )
                self.assertEqual(value["effort"], "high" if role == "review" else "max")
                self.assertEqual(
                    value["sandbox"], "workspace-write" if role == "deliver" else "read-only"
                )
                self.assertEqual(value["workspace"], str(home / "work"))
                self.assertEqual(value["prompt"], str(data / "prompt.txt"))

    def test_role_context_and_directory_replacement_are_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            context, home, data = self.fixture(Path(temporary), "review")
            with self.assertRaisesRegex(Blocked, "accepted session"):
                prepared(context, "deliver", data)
            changed = deepcopy(context)
            changed["config"]["models"]["review"] = "unaccepted-model"
            with self.assertRaisesRegex(Blocked, "accepted session"):
                prepared(changed, "review", data)
            (home / "work").rename(home / "original")
            (home / "work").mkdir()
            with self.assertRaisesRegex(Blocked, "replaced a workspace directory"):
                prepared(context, "review", data)

    def test_session_reservation_rejects_invalid_values_and_retains_invocation_limit(self):
        cfg = project()
        for value in (0, 61, True, "30"):
            with self.subTest(value=value), self.assertRaises(Blocked):
                minutes({"invocation": {}, "agent_minutes": value}, cfg, "task")
        self.assertEqual(minutes({"invocation": {}, "agent_minutes": 7}, cfg, "task"), 7)

    def test_result_links_directories_and_oversized_data_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.json"
            write_json(outside, {"private_canary": "must not be collected"})
            target = root / "result.json"
            target.symlink_to(outside)
            with self.assertRaises(Blocked):
                read_regular_json(target)
            target.unlink()
            os.link(outside, target)
            with self.assertRaises(Blocked):
                read_regular_json(target)
            target.unlink()
            target.mkdir()
            with self.assertRaises((Blocked, OSError)):
                read_regular_json(target)
            target.rmdir()
            target.write_bytes(b" " * 512001)
            with self.assertRaises(Blocked):
                read_regular_json(target)
            write_json(target, {"summary": "Native result é"})
            self.assertEqual(read_regular_json(target), {"summary": "Native result é"})

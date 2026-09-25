import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.common import Blocked, digest, write_json
from nexkit.release import (
    RELEASE_MARKER,
    build_release,
    failed_release,
    prepare_release,
    publish_release,
    revalidate_release,
)
from tests.support import FakeGitHub, approve


class ReleaseGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.cfg["release"] = {
            "enabled": True,
            "build": ["python3", "build.py"],
            "artifacts": ["dist/library.txt"],
            "tag_prefix": "v",
        }
        self.value = {
            "schema": 1,
            "commit": "b" * 40,
            "version": "1.2.0",
            "notes": "Signed sum fix and HTTP behavior improvements.",
            "config": digest(self.cfg),
            "repository": self.repository,
        }
        self.work["title"] = "Release candidate 1.2.0"
        self.work["body"] = RELEASE_MARKER + "\n```json\n" + json.dumps(self.value) + "\n```\n"
        self.discussion = [approve(self.work, verb="release")]
        self.tag = None
        self.release = None
        self.assets = []
        self.publish_count = 0

    def api(self, path, method="GET", data=None, **kwargs):
        if "/compare/" in path:
            return {"status": "identical"}
        if "/git/ref/tags/" in path:
            if not self.tag:
                raise Blocked("HTTP 404")
            return {"object": {"type": "commit", "sha": self.tag}}
        if path.endswith("/git/refs") and data["ref"].startswith("refs/tags/"):
            if self.tag:
                raise Blocked("HTTP 422")
            self.tag = data["sha"]
            return {}
        if "/releases?" in path:
            return [deepcopy(self.release)] if self.release else []
        if path.endswith("/releases") and method == "POST":
            self.release = {
                **deepcopy(data),
                "id": 50,
                "html_url": "https://example.test/release/1.2.0",
            }
            return deepcopy(self.release)
        if "/assets?" in path:
            return deepcopy(self.assets)
        if path.endswith("/releases/50") and method == "PATCH":
            self.publish_count += 1
            self.release.update(data)
            return deepcopy(self.release)
        return super().api(path, method, data, **kwargs)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.gh = ReleaseGitHub()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return prepare_release(self.gh, 1, "100.1", "a" * 40)

    def assets(self, context):
        path = self.root / "library.txt"
        path.write_bytes(b"release bytes from exact approved source\n")
        manifest = {
            "source": context["release"]["commit"],
            "candidate": digest(context["release"]),
            "run_key": context["run_key"],
            "verification": {"passed": True},
            "artifacts": [
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ],
        }
        write_json(self.root / "nexkit-release-manifest.json", manifest)
        return manifest

    def upload(self, argv, **kwargs):
        self.assertEqual(argv[:3], ["gh", "release", "upload"])
        self.assertNotIn("--clobber", argv)
        path = Path(argv[4])
        self.gh.assets.append(
            {"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}
        )

    def test_only_approved_candidate_published_and_retry_does_not_duplicate(self):
        context = self.prepare()
        self.assertTrue(context["ready"])
        self.assets(context)
        with patch("nexkit.release.run", side_effect=self.upload):
            first = publish_release(self.gh, context, self.root)
            second = publish_release(self.gh, context, self.root)
        self.assertEqual(first["status"], "released")
        self.assertEqual(second["status"], "released")
        self.assertEqual(self.gh.tag, "b" * 40)
        self.assertEqual(self.gh.publish_count, 1)
        self.assertEqual(len(self.gh.assets), 1)

    def test_wrong_approver_and_candidate_drift_block(self):
        self.gh.discussion = [approve(self.gh.work, login="outsider", verb="release")]
        self.assertFalse(self.prepare()["ready"])
        self.gh.discussion = [approve(self.gh.work, verb="release")]
        context = self.prepare()
        self.assets(context)
        self.gh.work["body"] = self.gh.work["body"].replace("1.2.0", "1.3.0")
        with self.assertRaises(Blocked):
            publish_release(self.gh, context, self.root)
        self.assertIsNone(self.gh.tag)

    def test_closed_completed_release_remains_complete_on_duplicate_event(self):
        context = self.prepare()
        self.assets(context)
        with patch("nexkit.release.run", side_effect=self.upload):
            publish_release(self.gh, context, self.root)
        self.gh.work["state"] = "closed"
        result = prepare_release(self.gh, 1, "101.1", "a" * 40)
        self.assertFalse(result["ready"])
        self.assertEqual(self.gh.state["status"], "released")

    def test_cancel_and_persistent_deadline_prevent_release_effects(self):
        context = self.prepare()
        self.assets(context)
        cancel = approve(self.gh.work, number=20)
        cancel["body"] = "/nexkit cancel"
        self.gh.discussion.append(cancel)
        with self.assertRaisesRegex(Blocked, "cancelled"):
            revalidate_release(self.gh, context)
        self.gh.discussion.pop()
        self.gh.state["started_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(Blocked, "time budget"):
            publish_release(self.gh, context, self.root)
        self.assertIsNone(self.gh.tag)
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40)
        self.assertFalse(retry["ready"])
        self.assertIn("time budget", retry["reason"])

    def test_interruption_state_keeps_partial_release_identity_for_resume(self):
        context = self.prepare()
        self.assets(context)
        with patch("nexkit.release.run", side_effect=Blocked("connection interrupted")):
            with self.assertRaises(Blocked):
                publish_release(self.gh, context, self.root)
        failed_release(self.gh, context, "Upload interrupted")
        self.assertEqual(self.gh.state["status"], "blocked")
        self.assertEqual(self.gh.state["release_id"], 50)
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40)
        self.assertTrue(retry["ready"])
        self.assertEqual(self.gh.state["attempts"], 2)
        self.assertTrue(retry["reuse_build"])
        self.assertEqual(retry["build_run_key"], "100.1")
        with patch("nexkit.release.run", side_effect=self.upload):
            result = publish_release(self.gh, retry, self.root)
        self.assertEqual(result["status"], "released")

    def test_uploaded_bytes_survive_new_run_after_lost_upload_response(self):
        context = self.prepare()
        self.assets(context)

        def uploaded_but_response_lost(argv, **kwargs):
            self.upload(argv)
            raise Blocked("Upload response was lost")

        with patch("nexkit.release.run", side_effect=uploaded_but_response_lost):
            with self.assertRaises(Blocked):
                publish_release(self.gh, context, self.root)
        failed_release(self.gh, context, "Lost response")
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40)
        self.assertEqual(retry["build_run_key"], context["run_key"])
        # Workflow must recover this artifact from that run, never rebuild
        # possibly different bytes from a nondeterministic build command.
        with patch("nexkit.release.run", side_effect=AssertionError("Must not reupload")):
            done = publish_release(self.gh, retry, self.root)
        self.assertEqual(done["status"], "released")
        self.assertEqual(self.gh.publish_count, 1)

    def test_wrong_source_or_modified_bytes_never_upload(self):
        context = self.prepare()
        manifest = self.assets(context)
        manifest["source"] = "f" * 40
        write_json(self.root / "nexkit-release-manifest.json", manifest)
        with self.assertRaises(Blocked):
            publish_release(self.gh, context, self.root)
        self.assets(context)
        (self.root / "library.txt").write_text("tampered")
        with self.assertRaises(Blocked):
            publish_release(self.gh, context, self.root)
        self.assertIsNone(self.gh.release)

    def test_partial_upload_failure_recovers_same_draft_without_overwrite(self):
        context = self.prepare()
        self.assets(context)
        with patch("nexkit.release.run", side_effect=Blocked("connection interrupted")):
            with self.assertRaises(Blocked):
                publish_release(self.gh, context, self.root)
        self.assertTrue(self.gh.release["draft"])
        self.assertEqual(self.gh.state["status"], "release-uploading")
        with patch("nexkit.release.run", side_effect=self.upload):
            result = publish_release(self.gh, context, self.root)
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.gh.publish_count, 1)

    def test_existing_tag_or_asset_drift_is_never_overwritten(self):
        context = self.prepare()
        self.assets(context)
        self.gh.tag = "f" * 40
        with self.assertRaises(Blocked):
            publish_release(self.gh, context, self.root)
        self.gh.tag = None
        with patch("nexkit.release.run", side_effect=Blocked("interrupt")):
            with self.assertRaises(Blocked):
                publish_release(self.gh, context, self.root)
        self.gh.assets = [{"name": "library.txt", "digest": "sha256:" + "0" * 64}]
        with self.assertRaises(Blocked):
            publish_release(self.gh, context, self.root)
        self.assertEqual(self.gh.publish_count, 0)

    def test_actual_build_uses_same_environment_as_verification(self):
        cfg = self.gh.cfg
        cfg["environment"]["setup"] = [
            [
                "python3",
                "-c",
                "from pathlib import Path; Path.home().joinpath('build-ready').write_text('library-v1')",
            ]
        ]
        (self.root / "test_real.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass Real(unittest.TestCase):\n"
            " def test_prepared(self):\n  self.assertEqual(Path.home().joinpath('build-ready').read_text(), 'library-v1')\n"
        )
        for c in cfg["checks"]:
            c["argv"] = ["python3", "-m", "unittest", "test_real"]
        (self.root / "build.py").write_text(
            "from pathlib import Path\nPath('dist').mkdir(exist_ok=True)\n"
            "Path('dist/library.txt').write_text(Path.home().joinpath('build-ready').read_text())\n"
        )
        context = {"config": cfg, "release": self.gh.value, "run_key": "100.1"}
        result = build_release(context, self.root, self.root / "package")
        self.assertEqual((self.root / "package/library.txt").read_text(), "library-v1")
        self.assertTrue(result["verification"]["passed"])

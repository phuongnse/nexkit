import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.cli import intake_status, submit
from nexkit.common import Blocked, canonical, digest, write_json
from nexkit.policy import now, spec_hash
from nexkit.release import (
    CANDIDATE_DETAILS,
    CANDIDATE_END,
    RELEASE_MARKER,
    RELEASE_NOTES_END,
    approval_notice,
    build_release,
    candidate,
    candidate_body,
    failed_release,
    parse_candidate,
    post_approval_notice,
    prepare_release,
    publish_release,
    revalidate_release,
)
from tests.support import FakeGitHub, approve, workflow_environment


class ReleaseGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.cfg["release"] = {
            "enabled": True,
            "build": [sys.executable, "build.py"],
            "artifacts": ["dist/library.txt"],
            "tag_prefix": "v",
        }
        self.value = {
            "schema": 1,
            "pipeline": "maintenance",
            "commit": "b" * 40,
            "version": "1.2.0",
            "notes": "Signed sum fix and HTTP behavior improvements.",
            "config": digest(self.cfg),
            "repository": self.repository,
        }
        self.work["title"] = "Release candidate 1.2.0"
        self.work["body"] = candidate_body(self.value)
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
        if path.endswith("/releases/50") and method == "GET":
            return deepcopy(self.release)
        if path.endswith("/releases/50") and method == "PATCH":
            self.publish_count += 1
            self.release.update(data)
            if data.get("draft") is False:
                self.release["published_at"] = now()
            return deepcopy(self.release)
        return super().api(path, method, data, **kwargs)


class CandidateGitHub(ReleaseGitHub):
    def __init__(self):
        super().__init__()
        self.issues = []
        self.discussion = []

    def api(self, path, method="GET", data=None, **kwargs):
        if path.endswith("/issues?state=all&per_page=100"):
            return deepcopy(self.issues)
        if path.endswith("/issues") and method == "POST":
            self.work = {
                **deepcopy(data),
                "number": len(self.issues) + 1,
                "html_url": "https://example.test/issues/1",
                "state": "open",
            }
            self.issues.append(self.work)
            return deepcopy(self.work)
        return super().api(path, method, data, **kwargs)

    def comment(self, number, body):
        super().comment(number, body)
        self.discussion.append({"body": body, "user": {"type": "Bot", "login": "actions[bot]"}})


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, workflow_environment("release")))
        self.gh = CandidateGitHub()

    def create(self):
        value = self.gh.value
        return candidate(self.gh, self.gh.cfg, value["commit"], value["version"], value["notes"])

    def test_readable_candidate_and_approval_comment_without_publishing(self):
        result = self.create()
        body = self.gh.work["body"]
        self.assertIn("## Release notes\n\n" + self.gh.value["notes"], body)
        self.assertIn("## Next step", body)
        self.assertIn("<summary>Candidate metadata</summary>", body)
        self.assertIn("https://github.com/owner/project/commit/" + "b" * 40, body)
        self.assertEqual(parse_candidate(self.gh.work), self.gh.value)
        self.assertEqual(len(self.gh.messages), 1)
        self.assertIn(result["human_comment"], self.gh.messages[0])
        self.assertIn("write, maintain or admin", self.gh.messages[0])
        self.assertIn("After approval, Actions", self.gh.messages[0])
        self.assertIsNone(self.gh.tag)
        self.assertIsNone(self.gh.release)
        self.assertEqual(self.gh.state, {})
        # The bot's instructions contain a command but are never an approval.
        self.assertFalse(
            prepare_release(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")["ready"]
        )

    def test_intake_retry_reuses_issue_and_notice(self):
        first = self.create()
        second = self.create()
        self.assertEqual(first, second)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.messages), 1)

    def test_lost_comment_response_recovers_without_duplicate_notice(self):
        post = self.gh.comment

        def posted_but_lost(number, body):
            post(number, body)
            raise Blocked("Comment response lost")

        with patch.object(self.gh, "comment", side_effect=posted_but_lost):
            with self.assertRaisesRegex(Blocked, "response lost"):
                self.create()
        self.create()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.messages), 1)

    def test_missing_notice_is_repaired_on_retry(self):
        with patch.object(self.gh, "comment", side_effect=Blocked("Temporary failure")):
            with self.assertRaisesRegex(Blocked, "Temporary"):
                self.create()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.messages, [])
        self.create()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(len(self.gh.messages), 1)

    def test_candidate_is_reused_without_changing_approval_identity(self):
        self.gh.issues = [self.gh.work]
        original = deepcopy(self.gh.work)
        result = self.create()
        self.assertEqual(self.gh.work, original)
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(result["human_comment"], "/nexkit release " + spec_hash(original))
        self.assertIn("ready", self.gh.messages[0])
        self.assertNotIn("```json", self.gh.messages[0])

    def test_closed_candidate_is_reused_without_a_ready_notice(self):
        self.gh.work["state"] = "closed"
        self.gh.issues = [self.gh.work]
        self.create()
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.messages, [])

    def test_marker_only_comment_does_not_suppress_actual_instructions(self):
        self.create()
        self.gh.discussion[0]["body"] = self.gh.messages[0].split("\n", 1)[0]
        post_approval_notice(self.gh, self.gh.work)
        self.assertEqual(len(self.gh.messages), 2)

    def test_stable_format_and_pipeline_scoped_intake_lookup(self):
        notes = (
            " \n## Changes\n\n- Health endpoint.\n\n```text\nexample\n```\n"
            + CANDIDATE_DETAILS
            + RELEASE_NOTES_END
            + "\n "
        )
        value = {**self.gh.value, "schema": 1, "pipeline": "maintenance", "notes": notes}
        key = digest({k: value[k] for k in ("commit", "version", "notes")})
        for body in (candidate_body(value),):
            with self.subTest(body=body[:50]):
                self.gh.work["body"] = body
                self.gh.issues = [self.gh.work]
                self.assertEqual(parse_candidate(self.gh.work), value)
                found = intake_status(self.gh, "release", key, pipeline="maintenance")
                self.assertTrue(found["found"])
                self.assertFalse(intake_status(self.gh, "release", key, pipeline="other")["found"])

    def test_lookup_skips_unrelated_invalid_release_candidates(self):
        value = self.gh.value
        key = digest({k: value[k] for k in ("commit", "version", "notes")})
        valid = deepcopy(self.gh.work)
        invalid_bodies = (
            RELEASE_MARKER + "\n```json\n" + json.dumps({**value, "schema": 2}) + "\n```\n",
            RELEASE_MARKER + "\nInvalid candidate metadata",
            candidate_body(value).replace("## Candidate", "## Edited candidate", 1),
        )
        for body in invalid_bodies:
            invalid = {**valid, "number": 2, "body": body}
            for issues in ([invalid, valid], [valid, invalid]):
                with self.subTest(body=body[:60], invalid_first=issues[0] == invalid):
                    self.gh.issues = deepcopy(issues)
                    found = intake_status(self.gh, "release", key, pipeline="maintenance")
                    self.assertEqual(found["issue"], valid)
                    self.assertTrue(found["found"])
                    self.assertEqual(self.gh.issues, issues)
        self.assertEqual(self.gh.messages, [])

    def test_lookup_never_selects_a_malformed_candidate(self):
        value = self.gh.value
        key = digest({k: value[k] for k in ("commit", "version", "notes")})
        self.gh.issues = [
            {**self.gh.work, "body": candidate_body(value).replace("## Candidate", "## Edited")}
        ]
        found = intake_status(self.gh, "release", key, pipeline="maintenance")
        self.assertFalse(found["found"])
        self.assertIsNone(found["issue"])
        with self.assertRaises(Blocked):
            parse_candidate(self.gh.issues[0])

    def test_lookup_still_rejects_duplicate_valid_candidates(self):
        value = self.gh.value
        key = digest({k: value[k] for k in ("commit", "version", "notes")})
        self.gh.issues = [
            {**self.gh.work, "body": RELEASE_MARKER + "\nInvalid metadata"},
            deepcopy(self.gh.work),
            {**self.gh.work, "number": 2},
        ]
        with self.assertRaisesRegex(Blocked, "Multiple issues match"):
            intake_status(self.gh, "release", key, pipeline="maintenance")

    def test_composed_intake_posts_notice_and_approval_prepares_same_candidate(self):
        from nexkit.intake import main as intake_main
        from nexkit.pipelines import effective_config
        from tests.test_pipelines import ComposedGitHub

        composed = ComposedGitHub()
        composed.cfg["defaults"]["release"] = self.gh.cfg["release"]
        composed.cfg["pipelines"]["maintenance"]["entrypoints"]["release"] = (
            ".github/workflows/audit.yml"
        )
        self.gh.cfg = composed.cfg
        self.gh.content = composed.content
        payload = {k: self.gh.value[k] for k in ("commit", "version", "notes")}
        with tempfile.TemporaryDirectory() as directory:
            event, summary = Path(directory) / "event.json", Path(directory) / "summary.md"
            write_json(
                event,
                {"inputs": {"operation": "release", "payload": json.dumps(payload)}},
            )
            env = {
                "GITHUB_REPOSITORY": self.gh.repository,
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_ACTOR": "owner",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_WORKFLOW_SHA": "b" * 40,
                "GITHUB_WORKFLOW_REF": (
                    "owner/project/.github/workflows/capture.yaml@refs/heads/main"
                ),
                "GITHUB_STEP_SUMMARY": str(summary),
                "NEXKIT_PIPELINE": "maintenance",
            }
            with (
                patch.dict(os.environ, env),
                patch("nexkit.intake.GitHub", return_value=self.gh),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(intake_main(), 0)
            result = json.loads(output.getvalue())
            self.assertIn(result["issue"], summary.read_text())
        value = parse_candidate(self.gh.work)
        self.assertEqual(value["schema"], 1)
        self.assertEqual(value["pipeline"], "maintenance")
        self.assertEqual(value["config"], digest(effective_config(self.gh.cfg, "maintenance")))
        self.assertEqual(len(self.gh.messages), 1)
        self.assertIn(result["human_comment"], self.gh.messages[0])
        self.gh.discussion.append(approve(self.gh.work, verb="release"))
        with patch.dict(
            os.environ,
            {
                "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/audit.yml@refs/heads/main",
                "GITHUB_WORKFLOW_SHA": "b" * 40,
            },
        ):
            prepared = prepare_release(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")
        self.assertTrue(prepared["ready"], prepared)
        self.assertEqual(prepared["release"], value)

    def test_visible_summary_must_match_metadata(self):
        self.create()
        original = self.gh.work["body"]
        visible, metadata = original.rsplit(CANDIDATE_DETAILS, 1)
        for changed in (
            visible.replace("1.2.0", "1.3.0"),
            visible.replace("b" * 40, "c" * 40),
            visible.replace(self.gh.value["notes"], "Different release notes."),
        ):
            with self.subTest(changed=changed[:80]):
                self.gh.work["body"] = changed + CANDIDATE_DETAILS + metadata
                with self.assertRaisesRegex(Blocked, "differ"):
                    parse_candidate(self.gh.work)
                with self.assertRaises(Blocked):
                    approval_notice(self.gh.work)

    def test_notes_are_stored_once_and_accepted_payload_fits_the_issue(self):
        for notes in ("x" * 40000, "☀" * 16000, '\\"' * 10000):
            with self.subTest(notes_bytes=len(notes.encode())):
                self.gh = CandidateGitHub()
                self.gh.value["notes"] = notes
                payload = {k: self.gh.value[k] for k in ("commit", "version", "notes")}
                self.assertLess(len(canonical(payload).encode()), 50000)
                self.assertTrue(submit(self.gh, self.gh.cfg, "release", payload)["queued"])
                api = self.gh.api

                def reject_oversized_issue(path, method="GET", data=None, **kwargs):
                    if path.endswith("/issues") and method == "POST":
                        if len(data["body"].encode()) > 65536:
                            raise Blocked("Issue body is too long")
                    return api(path, method, data, **kwargs)

                with patch.object(self.gh, "api", side_effect=reject_oversized_issue):
                    self.create()
                body = self.gh.work["body"]
                self.assertLess(len(body.encode()), 65536)
                self.assertEqual(body.count(notes), 1)
                self.assertEqual(parse_candidate(self.gh.work)["notes"], notes)

    def test_unrelated_deeply_nested_json_does_not_block_intake(self):
        malicious = {
            **self.gh.work,
            "body": RELEASE_MARKER
            + "\n"
            + CANDIDATE_DETAILS
            + "[" * 20000
            + "]" * 20000
            + CANDIDATE_END,
        }
        with self.assertRaisesRegex(Blocked, "Invalid release candidate JSON"):
            parse_candidate(malicious)
        self.gh.issues = [malicious]
        self.create()
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(parse_candidate(self.gh.work), self.gh.value)

    def test_matching_issues_are_detected_as_duplicates(self):
        self.gh.issues = [
            deepcopy(self.gh.work),
            {**self.gh.work, "number": 2, "body": candidate_body(self.gh.value)},
        ]
        with self.assertRaisesRegex(Blocked, "Duplicate release candidate"):
            self.create()
        self.assertEqual(len(self.gh.issues), 2)
        self.assertEqual(self.gh.messages, [])

    def test_invalid_metadata_types_are_blocked(self):
        for value in (
            [],
            None,
            {**self.gh.value, "schema": True},
            {**self.gh.value, "schema": 2},
            {**self.gh.value, "commit": None},
            {**self.gh.value, "version": 1},
            {**self.gh.value, "notes": []},
            {**self.gh.value, "config": "not-a-hash"},
            {**self.gh.value, "repository": "owner/project)fake"},
        ):
            with self.subTest(value=value):
                prefix = candidate_body(self.gh.value).split(CANDIDATE_DETAILS)[0]
                metadata = deepcopy(value)
                if isinstance(metadata, dict):
                    metadata["notes_hash"] = digest(metadata.pop("notes"))
                body = prefix + CANDIDATE_DETAILS + json.dumps(metadata) + CANDIDATE_END
                with self.assertRaises(Blocked):
                    parse_candidate({"body": body})


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, workflow_environment("release")))
        self.gh = ReleaseGitHub()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return prepare_release(self.gh, 1, "100.1", "a" * 40, pipeline="maintenance")

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
            {
                "name": path.name,
                "size": path.stat().st_size,
                "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
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
        self.assertEqual(self.gh.work["state"], "closed")
        self.assertEqual(self.gh.work["state_reason"], "completed")
        self.assertEqual(second["completion"]["status"], "complete")

    def test_release_preparation_clears_a_previous_approval_block_reason(self):
        self.gh.state.update(status="blocked", reason="Awaiting an earlier approval")
        context = self.prepare()
        self.assertTrue(context["ready"])
        self.assertEqual(self.gh.state["status"], "release-building")
        self.assertNotIn("reason", self.gh.state)

    def test_release_completion_opt_out_keeps_candidate_open(self):
        self.gh.cfg["issue_completion"] = {"close_after_release": False}
        self.gh.value["config"] = digest(self.gh.cfg)
        self.gh.work["body"] = candidate_body(self.gh.value)
        self.gh.discussion = [approve(self.gh.work, verb="release")]
        context = self.prepare()
        self.assertTrue(context["ready"], context)
        self.assets(context)
        with patch("nexkit.release.run", side_effect=self.upload):
            state = publish_release(self.gh, context, self.root)
        self.assertEqual(state["status"], "released")
        self.assertEqual(state["completion"]["status"], "disabled")
        self.assertEqual(self.gh.work["state"], "open")

    def test_release_completion_checks_assets_before_closure(self):
        from nexkit.completion import reconcile

        context = self.prepare()
        self.assets(context)
        with (
            patch("nexkit.release.run", side_effect=self.upload),
            patch(
                "nexkit.release.reconcile", side_effect=lambda gh, number: gh.get_state(number)[0]
            ),
        ):
            publish_release(self.gh, context, self.root)
        self.gh.assets[0]["digest"] = "sha256:" + "f" * 64
        state = reconcile(self.gh, 1)
        self.assertEqual(state["status"], "released")
        self.assertEqual(state["completion"]["status"], "pending")
        self.assertIn("assets differ", state["completion"]["error"])
        self.assertEqual(self.gh.work["state"], "open")

    def test_completed_release_clears_an_interrupted_attempt_reason(self):
        context = self.prepare()
        self.assets(context)
        self.gh.state["reason"] = "An earlier attempt was interrupted"
        with patch("nexkit.release.run", side_effect=self.upload):
            result = publish_release(self.gh, context, self.root)
        self.assertEqual(result["status"], "released")
        self.assertNotIn("reason", result)
        self.assertNotIn("reason", self.gh.state)

    def test_readable_candidate_publishes_only_after_its_own_approval(self):
        old_approval = deepcopy(self.gh.discussion)
        self.gh.value["notes"] += " Updated release notes."
        self.gh.work["body"] = candidate_body(self.gh.value)
        self.assertFalse(self.prepare()["ready"])
        self.assertEqual(self.gh.discussion, old_approval)
        self.gh.discussion = [approve(self.gh.work, verb="release")]
        context = self.prepare()
        self.assertTrue(context["ready"])
        self.assets(context)
        with patch("nexkit.release.run", side_effect=self.upload):
            result = publish_release(self.gh, context, self.root)
        self.assertEqual(result["status"], "released")
        self.assertEqual(self.gh.tag, self.gh.value["commit"])

    def test_readable_candidate_changes_revoke_approval_before_publication(self):
        self.gh.work["body"] = candidate_body(self.gh.value)
        self.gh.discussion = [approve(self.gh.work, verb="release")]
        context = self.prepare()
        self.assets(context)
        updated = {**self.gh.value, "notes": "Revised notes."}
        self.gh.work["body"] = candidate_body(updated)
        with self.assertRaisesRegex(Blocked, "approval comment"):
            publish_release(self.gh, context, self.root)
        self.assertIsNone(self.gh.tag)

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
        result = prepare_release(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
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
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
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
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
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
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as artifact:
            for path in self.root.iterdir():
                artifact.writestr(path.name, path.read_bytes())

        def uploaded_but_response_lost(argv, **kwargs):
            self.upload(argv)
            raise Blocked("Upload response was lost")

        with patch("nexkit.release.run", side_effect=uploaded_but_response_lost):
            with self.assertRaises(Blocked):
                publish_release(self.gh, context, self.root)
        failed_release(self.gh, context, "Lost response")
        for path in self.root.iterdir():
            path.unlink()
        self.root.rmdir()
        retry = prepare_release(self.gh, 1, "101.1", "a" * 40, pipeline="maintenance")
        self.assertEqual(retry["build_run_key"], context["run_key"])
        # Simulate artifact transport into a different runner workspace. The
        # original files no longer exist and the build must not run again.
        with tempfile.TemporaryDirectory() as fresh:
            with zipfile.ZipFile(archive) as artifact:
                artifact.extractall(fresh)
            self.assertFalse(self.root.exists())
            with patch("nexkit.release.run", side_effect=AssertionError("Must not reupload")):
                done = publish_release(self.gh, retry, Path(fresh))
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
                sys.executable,
                "-c",
                "from pathlib import Path; Path.home().joinpath('build-ready').write_text('library-v1')",
            ]
        ]
        (self.root / "test_real.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass Real(unittest.TestCase):\n"
            " def test_prepared(self):\n  self.assertEqual(Path.home().joinpath('build-ready').read_text(), 'library-v1')\n"
        )
        for c in cfg["checks"]:
            c["argv"] = [sys.executable, "-m", "unittest", "test_real"]
        (self.root / "build.py").write_text(
            "from pathlib import Path\nPath('dist').mkdir(exist_ok=True)\n"
            "Path('dist/library.txt').write_text(Path.home().joinpath('build-ready').read_text())\n"
        )
        context = {"config": cfg, "release": self.gh.value, "run_key": "100.1"}
        result = build_release(context, self.root, self.root / "package")
        self.assertEqual((self.root / "package/library.txt").read_text(), "library-v1")
        self.assertTrue(result["verification"]["passed"])

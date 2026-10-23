import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from nexkit import evidence
from nexkit.agent import build_prompt, ci_evidence_text
from nexkit.context import gather
from tests.support import FakeGitHub, make_config

TOKEN = "ghp_" + "a" * 36


def zipped(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return data.getvalue()


class EvidenceCase(unittest.TestCase):
    """An issue about a flaky end-to-end test, and two failed runs of the repository."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.cfg = make_config(evidence={"artifacts": ["test-results-e2e"]})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "nexkit-evidence"
        for run_id in (101, 202):
            self.add_run(run_id)

    def add_run(self, run_id, artifact=None, **fields):
        self.gh.runs[run_id] = {
            "id": run_id,
            "html_url": f"https://github.com/acme/app/actions/runs/{run_id}",
            "run_attempt": 1,
            "conclusion": "failure",
            "head_repository": {"full_name": "acme/app"},
            **fields,
        }
        job = run_id * 10
        self.gh.jobs[run_id] = [
            {"id": job, "name": "e2e (chromium)", "conclusion": "failure"},
            {"id": job + 1, "name": "lint", "conclusion": "success"},
        ]
        self.gh.logs[job] = f"step 1\nsecret {TOKEN}\nError: Create stays disabled\n".encode()
        self.gh.annotations[job] = [
            {
                "annotation_level": "failure",
                "path": "e2e/studio.spec.ts",
                "start_line": 42,
                "title": "studio › new resource",
                "message": "expected button to be enabled",
            }
        ]
        artifact = artifact or {"id": run_id + 5, "name": "test-results-e2e", "size_in_bytes": 100}
        self.gh.run_artifact_list[run_id] = [artifact]
        self.gh.zips[artifact["id"]] = zipped(
            {"studio/error-context.md": f"token {TOKEN}\n", "studio/trace.zip": b"PK\0\0binary"}
        )

    def issue(self, body):
        self.gh.add_issue(5, title="Create stays disabled at times", body=body)

    def gather(self, stage="implement"):
        decision = {"action": stage, "issue": 5, "pr": None, "note": ""}
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.folder.parent / "s")}):
            return gather(self.gh, decision, ci=str(self.folder), cfg=self.cfg)


class EvidenceTests(EvidenceCase):
    def test_runs_named_by_marker_and_by_link_are_collected_outside_the_checkout(self):
        self.issue("It failed in https://github.com/acme/app/actions/runs/202/job/2020 too.")
        self.gh.comment(5, "Recorded.\n\n<!-- nexkit:ci-runs 101.1 -->")
        context = self.gather()
        found = context["ci_evidence"]
        self.assertEqual([run["id"] for run in found["runs"]], [202, 101])
        for run_id in (101, 202):
            run = self.folder / f"run-{run_id}"
            log = (run / "attempt-1" / "e2e-chromium.log").read_text()
            self.assertIn("Error: Create stays disabled", log)
            self.assertNotIn(TOKEN, log)  # redacted
            notes = (run / "attempt-1" / "e2e-chromium.annotations.md").read_text()
            self.assertIn("e2e/studio.spec.ts:42 studio › new resource", notes)
            artifact = run / "artifacts" / "test-results-e2e" / "studio"
            self.assertNotIn(TOKEN, (artifact / "error-context.md").read_text())
            self.assertEqual((artifact / "trace.zip").read_bytes(), b"PK\0\0binary")
            self.assertFalse((run / "attempt-1" / "lint.log").exists())  # it passed
        cfg = make_config()
        prompt = build_prompt("implement", {**context, "ci_evidence": ""}, cfg)
        self.assertNotIn("$ci_evidence", prompt)
        section = ci_evidence_text(found)
        prompt = build_prompt("implement", context, cfg, {"ci_evidence": section})
        self.assertIn(f"to\n`{self.folder}`, outside the working tree", prompt)
        self.assertIn("  - `run-202/attempt-1/e2e-chromium.log`", prompt)
        self.assertIn("  - `run-101/artifacts/test-results-e2e/`: an artifact, 2 files", prompt)
        self.assertIn("never ask a person to\ndownload them", prompt)

    def test_runs_of_other_repositories_links_that_are_not_runs_and_expired_artifacts(self):
        expired = {"id": 9, "name": "test-results-e2e", "size_in_bytes": 1, "expired": True}
        self.add_run(303, artifact=expired)
        self.add_run(404, head_repository={"full_name": "mallory/app"})
        self.issue(
            "See https://github.com/acme/app/actions/runs/303, "
            "https://github.com/other/app/actions/runs/1, "
            "https://github.com/acme/app/actions/workflows/e2e.yml and "
            "https://github.com/acme/app/actions/runs/404."
        )
        self.gh.logs.pop(3030)  # the log is gone too
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            found = self.gather()["ci_evidence"]
        log = out.getvalue()
        section = ci_evidence_text(found)
        for text in (log, section):
            self.assertIn("the artifact `test-results-e2e` expired", text)
            self.assertIn("run 1 of other/app: not this repository", text)
            self.assertIn("actions/workflows/e2e.yml: not a run", text)
            self.assertIn("it ran on a fork, mallory/app", text)
        self.assertIn("its log: GitHub API 410", section)
        self.assertEqual([run["id"] for run in found["runs"]], [303, 404])
        self.assertFalse((self.folder / "run-404").exists())

    def test_the_size_limit_leaves_files_out_with_a_note_in_the_run_summary(self):
        self.issue("https://github.com/acme/app/actions/runs/101")
        real = evidence.Budget
        with mock.patch.object(evidence, "Budget", lambda: real(limit=80)):
            self.gather()
        summary = (self.folder.parent / "s").read_text()
        self.assertIn("### NexKit: evidence from CI runs", summary)
        self.assertIn("Left out, past the 50.0 MB a round keeps", summary)

    def test_a_zip_entry_that_leads_out_of_its_folder_is_skipped(self):
        self.issue("https://github.com/acme/app/actions/runs/101")
        self.gh.zips[106] = zipped({"../../escape.txt": "x", "ok/file.txt": "y"})
        self.gather()
        artifact = self.folder / "run-101" / "artifacts" / "test-results-e2e"
        self.assertTrue((artifact / "ok" / "file.txt").exists())
        self.assertFalse((self.folder.parent / "escape.txt").exists())
        self.assertFalse(Path(self.tmp.name, "..", "escape.txt").resolve().exists())

    def test_issues_that_name_no_run_and_plans_collect_nothing(self):
        self.issue("Create stays disabled.")
        self.assertIsNone(self.gather()["ci_evidence"])
        self.issue("https://github.com/acme/app/actions/runs/101")
        self.assertNotIn("ci_evidence", self.gather("plan"))
        self.assertFalse(self.folder.exists())

    def test_the_setting_is_a_list_of_artifact_names(self):
        from nexkit.config import ConfigError

        self.assertEqual(make_config()["evidence"], {"artifacts": []})
        for bad in ("x", [""], ["a", "a"], [1], ["a\nb"]):
            with self.assertRaises(ConfigError):
                make_config(evidence={"artifacts": bad})
        with self.assertRaises(ConfigError):
            make_config(evidence={"names": []})
        json.dumps(make_config(evidence={"artifacts": ["test-results"]}))


if __name__ == "__main__":
    unittest.main()

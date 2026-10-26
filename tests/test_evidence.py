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
            return gather(
                self.gh, decision, ci=str(self.folder), cfg=self.cfg, workflow="nexkit.yml"
            )


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
        self.assertIn("never ask a\nperson to download them", prompt)

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

    def test_the_runs_the_issue_records_count_after_many_nexkit_rounds(self):
        # The issue links its two failed CI runs; a dozen NexKit rounds since link their
        # run logs, and another bot's comment links a run too.
        self.issue(
            "Failed in https://github.com/acme/app/actions/runs/101/job/1010 and "
            "https://github.com/acme/app/actions/runs/202."
        )
        for round in range(1, 13):
            self.add_run(round)
            self.gh.round_comment(5, round, "a" * 40, "failed")
        self.add_run(303)
        bot = self.gh.human_comment(
            5, "https://github.com/acme/app/actions/runs/303", login="ci-bot[bot]"
        )
        bot["user"]["type"] = "Bot"
        found = self.gather()["ci_evidence"]
        self.assertEqual([run["id"] for run in found["runs"]], [101, 202])
        self.assertEqual(found["skipped"], [])
        self.assertTrue((self.folder / "run-101" / "artifacts" / "test-results-e2e").is_dir())

    def test_runs_of_nexkits_own_workflow_never_count_wherever_they_are_linked(self):
        for run_id in range(701, 707):
            self.add_run(run_id, path=".github/workflows/nexkit.yml")
        for run_id in (303, 304, 305):
            self.add_run(run_id, path=".github/workflows/ci.yml")
        self.issue(
            "https://github.com/acme/app/actions/runs/701 broke in "
            + ", ".join(f"https://github.com/acme/app/actions/runs/{r}" for r in (101, 202, 303))
        )
        links = [f"https://github.com/acme/app/actions/runs/{r}" for r in range(702, 707)]
        self.gh.human_comment(5, "Also https://github.com/acme/app/actions/runs/304")
        self.gh.human_comment(
            5, "\n".join([*links[:3], "https://github.com/acme/app/actions/runs/305", *links[3:]])
        )
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            found = self.gather()["ci_evidence"]
        # The five named last that are not NexKit's own fill the five places.
        self.assertEqual([run["id"] for run in found["runs"]], [101, 202, 303, 304, 305])
        own = "a run of NexKit's own workflow, `nexkit.yml`"
        self.assertEqual(found["skipped"], [f"run {r}: {own}" for r in range(702, 707)])
        self.assertIn(f"Not collected: run 706: {own}.", out.getvalue())
        self.assertIn(f"run 702: {own}", ci_evidence_text(found))
        # Outside a NexKit run, nothing names NexKit's workflow.
        runs, _ = evidence.named_runs(self.gh, self.gh.issue(5), self.gh.comments(5))
        chosen, skipped = evidence._latest(self.gh, runs, "")
        self.assertEqual(([r for r, _, _ in chosen], skipped), ([703, 704, 305, 705, 706], []))

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

    def test_a_log_cut_inside_a_character_is_still_redacted_text(self):
        self.issue("https://github.com/acme/app/actions/runs/101")
        self.gh.logs[1010] = ("é" * 10).encode() + f" {TOKEN}\n".encode() + ("x" * 10).encode()
        with mock.patch.object(evidence, "LOG_TAIL", 30 + len(TOKEN)):
            self.gather()
        log = (self.folder / "run-101" / "attempt-1" / "e2e-chromium.log").read_text()
        self.assertNotIn(TOKEN, log)

    def test_an_artifact_with_a_file_and_a_folder_of_the_same_name_keeps_the_rest(self):
        self.issue("https://github.com/acme/app/actions/runs/101")
        self.gh.zips[106] = zipped({"a": "1", "a/b": "2", "c\n::error::x": "3", "d": "4"})
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            context = self.gather()
        artifact = self.folder / "run-101" / "artifacts" / "test-results-e2e"
        self.assertTrue((artifact / "d").exists())
        self.assertNotIn("\n::error::", out.getvalue())
        self.assertIsNotNone(context["ci_evidence"])

    def test_issues_that_name_no_run_collect_nothing(self):
        self.issue("Create stays disabled.")
        self.assertIsNone(self.gather()["ci_evidence"])
        self.assertIsNone(self.gather("plan")["ci_evidence"])
        self.assertNotIn("ci_evidence", self.gather("review"))
        self.assertFalse(self.folder.exists())

    def test_a_plan_gets_the_same_evidence_as_an_implement_round(self):
        self.issue("Recorded.")
        self.gh.comment(5, "Recorded.\n\n<!-- nexkit:ci-runs 101.1 -->")
        found = self.gather("plan")["ci_evidence"]
        self.assertEqual([run["id"] for run in found["runs"]], [101])
        log = self.folder / "run-101" / "attempt-1" / "e2e-chromium.log"
        self.assertIn("Error: Create stays disabled", log.read_text())
        context = {**self.gather("plan"), "ci_evidence": found}
        prompt = build_prompt(
            "plan", context, make_config(), {"ci_evidence": ci_evidence_text(found)}
        )
        self.assertIn("## Evidence from the CI runs this issue names", prompt)
        self.assertIn("  - `run-101/attempt-1/e2e-chromium.log`", prompt)
        self.assertLess(prompt.index("e2e-chromium.log"), prompt.index("## What to return"))
        # Without evidence the plan prompt has no such section and no placeholder.
        prompt = build_prompt("plan", context, make_config(), {"ci_evidence": ""})
        self.assertNotIn("$ci_evidence", prompt)
        self.assertNotIn("## Evidence from the CI runs", prompt)

    def test_a_failed_download_still_lets_the_plan_run_and_says_what_is_missing(self):
        self.issue("https://github.com/acme/app/actions/runs/101")
        self.gh.logs.pop(1010)
        self.gh.run_artifact_list[101] = []
        found = self.gather("plan")["ci_evidence"]
        section = ci_evidence_text(found)
        self.assertIn("e2e-chromium.log`: missing, its log: GitHub API 410", section)
        self.assertIn("Missing: no artifact named `test-results-e2e`.", section)

    def test_each_run_says_whether_a_newer_run_of_its_workflow_passed_on_the_default_branch(self):
        self.add_run(101, path=".github/workflows/ci.yml")
        self.add_run(202, path=".github/workflows/ci.yml")
        base = {"head_branch": "main", "status": "completed", "event": "push"}
        mine = {"head_repository": {"full_name": "acme/app"}}
        self.gh.workflow_history["ci.yml"] = [
            {"id": 101, **base, **mine, "conclusion": "failure"},
            {
                "id": 150,
                **base,
                **mine,
                "conclusion": "success",
                "head_sha": "c" * 40,
                "html_url": "https://github.com/acme/app/actions/runs/150",
            },
            # A pull request's run and a fork's run on a branch named main never count.
            {"id": 250, **base, **mine, "conclusion": "success", "event": "pull_request"},
            {
                "id": 260,
                **base,
                "conclusion": "success",
                "head_repository": {"full_name": "mallory/app"},
            },
            {"id": 270, **base, **mine, "conclusion": "failure"},
        ]
        self.issue(
            "https://github.com/acme/app/actions/runs/101 and "
            "https://github.com/acme/app/actions/runs/202"
        )
        found = self.gather("plan")["ci_evidence"]
        since = {run["id"]: run.get("passed_since") for run in found["runs"]}
        self.assertEqual(since[101]["id"], 150)
        self.assertIsNone(since[202])
        section = ci_evidence_text(found)
        self.assertIn(
            "  - Since: run 150 of `ci.yml` passed on `main` since at ccccccc, "
            "https://github.com/acme/app/actions/runs/150.",
            section,
        )
        self.assertIn("  - Since: no newer run of `ci.yml` has passed on `main` since.", section)
        summary = (self.folder.parent / "s").read_text()
        self.assertIn("Run 101: run 150 of `ci.yml` passed on `main` since", summary)

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

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nexkit.gitutil import BOT_NAME
from nexkit.publish import PublishError, publish, render_plan
from nexkit.state import latest_plan, plan_text
from tests.support import FakeGitHub, GitRepos, git, make_config

CONTEXT = {"title": "Fix add", "issue": 5}


def decision(action, **kw):
    base = {
        "action": action,
        "issue": 5,
        "pr": None,
        "target": 5,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": None,
        "note": "",
        "auto": False,
    }
    return {**base, **kw}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.cfg = make_config()

    def tearDown(self):
        self.tmp.cleanup()
        self.repos.cleanup()

    def make_patch(self, files, ref="main"):
        work = self.repos.clone(f"agent-{len(list(self.repos.root.iterdir()))}")
        git(work, "checkout", "-q", ref)
        start = git(work, "rev-parse", "HEAD")
        for name, content in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(work, "add", "-A")
        patch = subprocess.run(
            ["git", "diff", "--cached", "--binary", "HEAD"], cwd=work, capture_output=True
        ).stdout
        (self.out / "changes.patch").write_bytes(patch)
        return start

    def result(self, start, **kw):
        return {"status": "done", "summary": "Fixed add.", "start_head": start, **kw}

    def test_implement_pushes_branch_and_opens_pull_request(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        repo = self.repos.clone("publisher")
        # The workflow passes relative paths; git runs inside the repository directory.
        cwd = os.getcwd()
        os.chdir(self.out.parent)
        try:
            outcome = publish(
                self.gh,
                decision("implement"),
                self.result(start),
                CONTEXT,
                self.cfg,
                os.path.relpath(repo),
                os.path.relpath(self.out),
            )
        finally:
            os.chdir(cwd)
        self.assertTrue(outcome["published"])
        remote_head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(outcome["head"], remote_head)
        self.assertEqual(git(self.repos.origin, "log", "-1", "--format=%an", remote_head), BOT_NAME)
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%s", remote_head), "Fix add (#5)"
        )
        pull = self.gh.pulls[outcome["pr"]]
        self.assertTrue(pull["body"].startswith("Closes #5"))
        self.assertEqual(pull["head"]["ref"], "nexkit/issue-5")

    def test_pull_request_author_can_differ_from_bot(self):
        start = self.make_patch({"calc.py": "x\n"})
        author = FakeGitHub()
        author.add_issue(5)
        outcome = publish(
            self.gh,
            decision("implement"),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone("publisher"),
            self.out,
            author=author,
        )
        self.assertIn(outcome["pr"], author.pulls)
        self.assertFalse(self.gh.pulls)

    def test_protected_paths_are_rejected(self):
        start = self.make_patch({".github/workflows/ci.yml": "on: push\n", "calc.py": "x\n"})
        with self.assertRaisesRegex(PublishError, r"\.github/workflows/ci\.yml"):
            publish(
                self.gh,
                decision("implement"),
                self.result(start),
                CONTEXT,
                self.cfg,
                self.repos.clone("publisher"),
                self.out,
            )
        branches = git(self.repos.origin, "branch", "--list", "nexkit/*")
        self.assertEqual(branches, "")

    def test_fix_commits_on_top_of_the_pull_request(self):
        first = self.make_patch({"calc.py": "v1\n"})
        publish(
            self.gh,
            decision("implement"),
            self.result(first),
            CONTEXT,
            self.cfg,
            self.repos.clone("p1"),
            self.out,
        )
        start = self.make_patch({"calc.py": "v2\n"}, ref="nexkit/issue-5")
        outcome = publish(
            self.gh,
            decision("fix", pr=6, target=6),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone("p2"),
            self.out,
        )
        self.assertEqual(outcome["pr"], 6)
        log = git(self.repos.origin, "log", "--format=%s", "nexkit/issue-5")
        self.assertEqual(log.splitlines()[:2], ["Address feedback on #6", "Fix add (#5)"])

    def test_fix_refuses_when_branch_moved(self):
        first = self.make_patch({"calc.py": "v1\n"})
        publish(
            self.gh,
            decision("implement"),
            self.result(first),
            CONTEXT,
            self.cfg,
            self.repos.clone("p1"),
            self.out,
        )
        self.make_patch({"calc.py": "v2\n"}, ref="nexkit/issue-5")
        with self.assertRaisesRegex(PublishError, "branch moved"):
            publish(
                self.gh,
                decision("fix", pr=6, target=6),
                self.result("0" * 40),
                CONTEXT,
                self.cfg,
                self.repos.clone("p2"),
                self.out,
            )

    def test_unfinished_results_are_not_published(self):
        outcome = publish(
            self.gh, decision("implement"), {"status": "blocked"}, CONTEXT, self.cfg, ".", self.out
        )
        self.assertEqual(outcome, {"published": False})

    def test_plan_comment(self):
        output = {
            "summary": "Fix add.",
            "approach": "- Edit calc.py",
            "acceptance_criteria": ["add(2, 3) == 5"],
            "questions": [],
            "too_large": False,
            "split": [],
        }
        outcome = publish(
            self.gh,
            decision("plan"),
            {"status": "done", "output": output},
            CONTEXT,
            self.cfg,
            ".",
            self.out,
        )
        self.assertTrue(outcome["published"])
        plan = latest_plan(self.gh.comments(5))
        self.assertIn("- [ ] add(2, 3) == 5", plan_text(plan))
        self.assertIn("/nexkit go", plan["body"])

    def test_plan_rendering_variants(self):
        base = {"summary": "S", "approach": "", "acceptance_criteria": [], "split": []}
        questions = render_plan({**base, "questions": ["Which API?"], "too_large": False})
        self.assertIn("1. Which API?", questions)
        self.assertIn("Answer the questions", questions)
        large = render_plan(
            {**base, "questions": [], "too_large": True, "split": [{"title": "A", "body": "a"}]}
        )
        self.assertIn("1. **A**: a", large)
        self.assertIn("too large", large)
        json.dumps(large)


if __name__ == "__main__":
    unittest.main()

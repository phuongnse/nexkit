import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from nexkit import agent, gitutil
from nexkit.context import gather
from nexkit.github import GitHubError
from nexkit.gitutil import GitError, merge_tree
from nexkit.publish import PublishError, identity, publish, pull_body, render_plan
from nexkit.state import latest_plan, plan_data, plan_profile, plan_text
from tests.support import (
    CALC,
    MUL,
    SUB,
    FakeClaude,
    FakeGitHub,
    GitRepos,
    git,
    make_config,
    profile_config,
)

CONTEXT = {"title": "Fix add", "issue": 5}
PLAN = {
    "summary": "add() returns the sum instead of the difference.",
    "changes": ["add(a, b) returns a + b.", "Negative numbers add up too."],
    "decisions": [{"decision": "add() keeps its name.", "reason": "Callers use it today."}],
    "risks": ["Floats are out of scope."],
    "acceptance_criteria": ["add(2, 3) returns 5."],
    "implementation_notes": ["Edit `calc.py`: `return a + b`.", "Add `test_add_negative`."],
    "questions": [],
    "too_large": False,
    "split": [],
}


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
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%an <%ae>", remote_head),
            "nexkit-acme[bot] <4242+nexkit-acme[bot]@users.noreply.github.com>",
        )
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%s", remote_head), "Fix add (#5)"
        )
        pull = self.gh.pulls[outcome["pr"]]
        self.assertTrue(pull["body"].startswith("Closes #5"))
        self.assertEqual(pull["head"]["ref"], "nexkit/issue-5")

    def test_a_changed_round_with_a_no_change_note_opens_its_pull_request(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        note = "Nothing skipped. Every planned change was made."
        output = {"summary": "Fixed add.", "no_change": "", "reviewer_notes": [note]}
        result = self.result(start, output=output, no_change_note=note)
        outcome = publish(
            self.gh,
            decision("implement"),
            result,
            CONTEXT,
            self.cfg,
            self.repos.clone("publisher"),
            self.out,
        )
        self.assertTrue(outcome["published"])
        self.assertNotIn("no_change", outcome)
        body = self.gh.pulls[outcome["pr"]]["body"]
        self.assertIn(f"## Notes for the reviewer\n\n- {note}", body)

    def failing_push(self, *errors):
        """Patch git so that its next pushes fail with `errors`, each git's error output,
        and later ones run. Returns the patch and the sleep patch to enter."""
        real, left = gitutil._run, list(errors)

        def run(repo, args, binary=False, input=None):
            if args and args[0] == "push" and left:
                return subprocess.CompletedProcess(["git", *args], 1, "", left.pop(0))
            return real(repo, args, binary, input)

        return mock.patch.object(gitutil, "_run", run), mock.patch.object(gitutil.time, "sleep")

    def test_a_push_that_fails_on_a_server_error_is_tried_again(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        error = (
            "error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
            "fatal: the remote end hung up unexpectedly\n"
        )
        run, sleep = self.failing_push(error)
        with run, sleep as slept, redirect_stderr(io.StringIO()) as log:
            outcome = self.publish_implement(start)
        self.assertTrue(outcome["published"])
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["head"], git(self.repos.origin, "rev-parse", "nexkit/issue-5"))
        self.assertEqual([c.args[0] for c in slept.call_args_list], [10])
        self.assertIn("Trying again in 10 seconds", log.getvalue())

    def test_a_refused_push_fails_at_once(self):
        start = self.make_patch({"calc.py": "x\n"})
        refused = (
            "remote: error: GH006: Protected branch update failed for refs/heads/nexkit/issue-5.\n"
            " ! [remote rejected] HEAD -> nexkit/issue-5 (protected branch hook declined)\n"
        )
        run, sleep = self.failing_push(refused)
        with run, sleep as slept, self.assertRaises(GitError) as caught:
            self.publish_implement(start)
        self.assertFalse(caught.exception.transient)
        slept.assert_not_called()
        self.assertFalse(self.gh.pulls)

    def test_git_errors_another_try_may_pass(self):
        for stderr, transient in (
            ("error: RPC failed; HTTP 502 curl 22\nfatal: the remote end hung up", True),
            ("fatal: unable to access 'x': Could not resolve host: github.com", True),
            (" ! [remote rejected] HEAD -> b (Internal Server Error)", True),
            (" ! [remote rejected] HEAD -> b (protected branch hook declined)", False),
            (" ! [rejected] HEAD -> b (non-fast-forward)", False),
            ("error: RPC failed; HTTP 413 curl 22\nfatal: the remote end hung up", False),
            ("remote: Permission to acme/app.git denied to x.\nHTTP 403", False),
            ("error: pathspec 'x' did not match", False),
        ):
            self.assertEqual(gitutil.transient_failure(stderr), transient, stderr)

    def test_a_push_that_fails_on_a_server_error_every_time_is_transient(self):
        start = self.make_patch({"calc.py": "x\n"})
        error = (
            "fatal: unable to access 'https://github.com/acme/app/': "
            "The requested URL returned error: 503\n"
        )
        run, sleep = self.failing_push(*[error] * 4)
        with run, sleep as slept, redirect_stderr(io.StringIO()):
            with self.assertRaises(GitError) as caught:
                self.publish_implement(start)
        self.assertTrue(caught.exception.transient)
        self.assertEqual([c.args[0] for c in slept.call_args_list], [10, 30, 60])
        self.assertFalse(self.gh.pulls)

    def test_a_pull_request_that_github_opened_on_a_try_it_answered_with_an_error(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        self.gh.add_pull(9, 5, branch="nexkit/issue-5")
        error = GitHubError(422, "A pull request already exists for acme:nexkit/issue-5.")
        with mock.patch.object(self.gh, "create_pull", side_effect=error):
            outcome = self.publish_implement(start)
        self.assertEqual((outcome["published"], outcome["pr"]), (True, 9))
        # Without one, the refusal stands.
        self.gh.pulls[9]["state"] = "closed"
        with (
            mock.patch.object(self.gh, "create_pull", side_effect=error),
            self.assertRaises(GitHubError),
        ):
            self.publish_implement(start, name="again")

    def test_an_issue_closed_while_the_round_ran_gets_nothing_published(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        self.gh.close_issue(5, "not_planned")
        for action, result in (("implement", self.result(start)), ("plan", {"output": PLAN})):
            outcome = publish(
                self.gh,
                decision(action),
                {"status": "done", **result},
                CONTEXT,
                self.cfg,
                self.repos.clone(f"publisher-{action}"),
                self.out,
            )
            self.assertEqual(outcome, {"published": False, "issue_closed": True})
        self.assertNotIn("nexkit/issue-5", git(self.repos.origin, "branch", "--list"))
        self.assertEqual((self.gh.pulls, self.gh.comments(5)), ({}, []))
        self.assertEqual(self.gh.issues[5]["state_reason"], "not_planned")

    def test_a_fix_round_that_checks_again_pushes_nothing_and_keeps_the_head(self):
        before = git(self.repos.origin, "rev-parse", "main")
        fix = decision("fix", pr=6, target=6, head="a" * 40)
        outcome = publish(
            self.gh,
            fix,
            self.result(before, recheck=True),
            CONTEXT,
            self.cfg,
            self.repos.clone("publisher"),
            self.out,
        )
        self.assertEqual(outcome, {"published": False, "recheck": True, "pr": 6, "head": "a" * 40})
        self.assertEqual(git(self.repos.origin, "rev-parse", "main"), before)

    def test_implement_starts_where_the_agent_started_when_the_base_moved(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        # The base branch changes the same line while the agent works.
        later = self.repos.push_commit(
            "dev", "main", {"calc.py": "def add(a, b):\n    return b - a\n"}, "Swap"
        )
        outcome = publish(
            self.gh,
            decision("implement"),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone("publisher"),
            self.out,
        )
        self.assertTrue(outcome["published"])
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(git(self.repos.origin, "rev-parse", f"{head}^"), start)
        self.assertEqual(
            git(self.repos.origin, "show", f"{head}:calc.py"), "def add(a, b):\n    return a + b"
        )
        self.assertEqual(git(self.repos.origin, "rev-parse", "main"), later)
        self.assertIn(outcome["pr"], self.gh.pulls)

    def parents(self, sha):
        return git(self.repos.origin, "log", "-1", "--format=%P", sha).split()

    def test_implement_merges_a_moved_base_when_its_rules_want_branches_up_to_date(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        later = self.repos.push_commit("dev", "main", {"later.py": "x = 1\n"}, "Later")
        self.gh.strict = True
        outcome = self.publish_implement(start)
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual((outcome["head"], outcome["updated"]), (head, later))
        # The round's commit on the commit the agent started from, then the merge of the tip.
        commit, merged = self.parents(head)
        self.assertEqual((self.parents(commit), merged), ([start], later))
        self.assertEqual(git(self.repos.origin, "log", "-1", "--format=%s", commit), "Fix add (#5)")
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%s", head),
            "Merge main into nexkit/issue-5",
        )
        self.assertEqual(git(self.repos.origin, "show", f"{head}:later.py"), "x = 1")
        self.assertNotIn("conflicts", outcome)

    def test_implement_keeps_its_start_when_the_rules_do_not_want_branches_up_to_date(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        self.repos.push_commit("dev", "main", {"later.py": "x = 1\n"}, "Later")
        self.cfg = make_config(conflicts={"auto_resolve": True})
        outcome = self.publish_implement(start)
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(self.parents(head), [start])
        self.assertEqual(set(outcome), {"published", "pr", "head"})
        # Nor when the base branch has not moved, whatever the rules.
        self.gh.strict = True
        start = self.make_patch({"calc.py": "def add(a, b):\n    return b + a\n"})
        outcome = self.publish_implement(start, name="publisher-2")
        self.assertEqual(self.parents(outcome["head"]), [start])
        self.assertEqual(set(outcome), {"published", "pr", "head"})

    def test_a_rule_that_cannot_be_read_counts_as_off(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        self.repos.push_commit("dev", "main", {"later.py": "x = 1\n"}, "Later")
        self.gh.strict = GitHubError(500, "down")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            outcome = self.publish_implement(start)
        self.assertEqual(self.parents(outcome["head"]), [start])
        self.assertIn("could not read the rules of `main`", stderr.getvalue())

    def test_a_round_whose_commit_conflicts_with_the_base_tip_says_so(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        # The base branch changes the same line while the agent works.
        self.repos.push_commit(
            "dev", "main", {"calc.py": "def add(a, b):\n    return b - a\n"}, "S"
        )
        self.cfg = make_config(conflicts={"auto_resolve": True})
        self.gh.strict = True
        outcome = self.publish_implement(start)
        # The round's commit is pushed as it is, and no merge is made.
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual((outcome["head"], outcome["conflicts"]), (head, ["calc.py"]))
        self.assertEqual(self.parents(head), [start])
        self.assertNotIn("updated", outcome)
        # Without conflicts.auto_resolve, no conflict round follows: the round goes on.
        self.cfg = make_config()
        outcome = self.publish_implement(start, name="publisher-2")
        self.assertNotIn("conflicts", outcome)

    def fix_on_a_moved_base(self, base_files, **options):
        """Publish a fix round whose base branch moved with `base_files` while its agent
        worked. Returns (outcome, the head the round started from)."""
        first = self.make_patch({"calc.py": "v1\n"})
        self.publish_implement(first, name="p1")
        start = self.make_patch({"calc.py": "v2\n"}, ref="nexkit/issue-5")
        self.repos.push_commit("dev", "main", base_files, "Later")
        outcome = publish(
            self.gh,
            decision("fix", pr=6, target=6),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone("p2"),
            self.out,
            **options,
        )
        return outcome, start

    def test_a_fix_round_merges_a_moved_base_when_its_rules_want_branches_up_to_date(self):
        self.gh.strict = True
        outcome, start = self.fix_on_a_moved_base({"later.py": "x = 1\n"})
        later = git(self.repos.origin, "rev-parse", "main")
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual((outcome["head"], outcome["updated"]), (head, later))
        commit, merged = self.parents(head)
        self.assertEqual((self.parents(commit), merged), ([start], later))
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%s", commit), "Address feedback on #6"
        )
        self.assertEqual(git(self.repos.origin, "show", f"{head}:calc.py"), "v2")

    def test_a_fix_round_without_the_rule_publishes_its_commit_alone(self):
        outcome, start = self.fix_on_a_moved_base({"later.py": "x = 1\n"})
        self.assertEqual(self.parents(outcome["head"]), [start])
        self.assertEqual(set(outcome), {"published", "pr", "head"})

    def test_workflow_changes_merge_like_any_other_with_the_app(self):
        self.gh.strict = True
        files = {".github/workflows/ci.yml": "on: push\n"}
        outcome, start = self.fix_on_a_moved_base(files)
        self.assertEqual(len(self.parents(outcome["head"])), 2)
        self.assertIn("updated", outcome)

    def test_a_commit_without_the_bots_id_still_names_the_app(self):
        self.gh.user = mock.Mock(side_effect=GitHubError(404, "Not Found"))
        self.assertEqual(
            identity(self.gh, self.cfg),
            (
                "-c",
                "user.name=nexkit-acme[bot]",
                "-c",
                "user.email=nexkit-acme[bot]@users.noreply.github.com",
            ),
        )

    def publish_implement(self, start, name="publisher", **options):
        return publish(
            self.gh,
            decision("implement"),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone(name),
            self.out,
            **options,
        )

    def test_implement_starts_where_the_agent_started_when_workflows_moved(self):
        start = self.make_patch({"calc.py": "def add(a, b):\n    return a + b\n"})
        self.repos.push_commit("dev", "main", {".github/workflows/ci.yml": "on: push\n"}, "Cache")
        self.publish_implement(start)
        head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(git(self.repos.origin, "rev-parse", f"{head}^"), start)

    def test_implement_start_must_be_on_the_base_branch(self):
        self.make_patch({"calc.py": "x\n"})
        other = self.repos.push_commit("dev", "other", {"x.py": "1\n"}, "Elsewhere")
        for start in (other, "a" * 40, "main", None):
            with self.subTest(start=start):
                with self.assertRaisesRegex(PublishError, "no longer on `main`"):
                    publish(
                        self.gh,
                        decision("implement"),
                        self.result(start),
                        CONTEXT,
                        self.cfg,
                        self.repos.clone(f"publisher-{start}"),
                        self.out,
                    )
        self.assertEqual(git(self.repos.origin, "branch", "--list", "nexkit/*"), "")
        self.assertFalse(self.gh.pulls)

    def test_pull_request_gets_the_labels_and_milestone_of_its_issue(self):
        self.gh.add_issue(5, milestone=3, labels=("bug", "nexkit"))
        outcome = self.publish_implement(self.make_patch({"calc.py": "x\n"}))
        self.assertEqual(self.gh.updates, [(outcome["pr"], 3, ["bug", "nexkit"])])
        pull = self.gh.issue(outcome["pr"])
        self.assertEqual(pull["milestone"], {"number": 3})
        self.assertEqual([label["name"] for label in pull["labels"]], ["bug", "nexkit"])

    def test_pull_request_of_an_issue_without_labels_or_milestone_is_left_alone(self):
        self.publish_implement(self.make_patch({"calc.py": "x\n"}))
        self.assertEqual(self.gh.updates, [])

    def test_labels_that_cannot_be_copied_only_warn(self):
        self.gh.add_issue(5, labels=("bug",))
        self.gh.update_errors[6] = GitHubError(422, "Validation Failed")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            outcome = self.publish_implement(self.make_patch({"calc.py": "x\n"}))
        self.assertTrue(outcome["published"])
        self.assertEqual(outcome["pr"], 6)
        self.assertIn("could not copy the labels and milestone of #5", stderr.getvalue())

    def test_fix_round_leaves_labels_and_milestone_to_people(self):
        self.gh.add_issue(5, milestone=3, labels=("bug",))
        self.publish_implement(self.make_patch({"calc.py": "x\n"}))
        self.gh.updates.clear()
        start = self.make_patch({"calc.py": "y\n"}, ref="nexkit/issue-5")
        outcome = publish(
            self.gh,
            decision("fix", pr=6, head=git(self.repos.origin, "rev-parse", "nexkit/issue-5")),
            self.result(start),
            CONTEXT,
            self.cfg,
            self.repos.clone("fixer"),
            self.out,
        )
        self.assertTrue(outcome["published"])
        self.assertEqual(self.gh.updates, [])

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
        cause = "add() subtracted: calc.py:2 returned a - b."
        outcome = publish(
            self.gh,
            decision("fix", pr=6, target=6),
            self.result(start, output={"cause": cause}),
            CONTEXT,
            self.cfg,
            self.repos.clone("p2"),
            self.out,
        )
        self.assertEqual(outcome["pr"], 6)
        log = git(self.repos.origin, "log", "--format=%s", "nexkit/issue-5")
        self.assertEqual(log.splitlines()[:2], ["Address feedback on #6", "Fix add (#5)"])
        body = git(self.repos.origin, "log", "-1", "--format=%b", "nexkit/issue-5")
        self.assertEqual(body, f"Fixed add.\n\nCause: {cause}")

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
        outcome = publish(
            self.gh,
            decision("plan"),
            {"status": "done", "output": PLAN},
            CONTEXT,
            self.cfg,
            ".",
            self.out,
        )
        self.assertTrue(outcome["published"])
        plan = latest_plan(self.gh.comments(5))
        self.assertIn("- [ ] add(2, 3) returns 5.", plan_text(plan))
        self.assertIn("/nexkit go", plan["body"])

    def publish_plan(self, triage, profile=None):
        result = {"status": "done", "output": PLAN, "triage": triage}
        result["profile"] = profile or triage["profile"]
        publish(self.gh, decision("plan"), result, CONTEXT, profile_config(), ".", self.out)
        return latest_plan(self.gh.comments(5))

    def test_plan_comment_shows_the_profile(self):
        plan = self.publish_plan(
            {"profile": "hard", "chosen_by": "triage", "reason": "Touches the login code."}
        )
        self.assertTrue(plan["body"].startswith("<!-- nexkit:plan -->\n<!-- nexkit:profile "))
        self.assertIn(
            "## Plan\n\nadd() returns the sum instead of the difference.\n\n"
            "<!-- nexkit:profile-text -->\n\n**Profile: hard**",
            plan["body"],
        )
        self.assertIn(
            "**Profile: hard**: plan `claude` (`fable`) · implement `claude` (`opus`) · "
            "review `claude` (`fable`). "
            "Touches the login code.\n\nTo use another profile, say which in a comment and "
            "comment `/nexkit plan`, or comment `/nexkit plan <request>`.",
            plan["body"],
        )
        self.assertEqual(plan_profile(plan), {"profile": "hard", "chosen_by": "triage"})
        self.assertNotIn("Profile", plan_text(plan))
        self.assertIn("### What changes", plan_text(plan))

    def test_plan_comment_names_the_agent_of_every_stage(self):
        cfg = profile_config(
            agents={
                "backup": {
                    "runtime": "claude-code",
                    "provider": "anthropic",
                    "api_key_secret": "ANTHROPIC_API_KEY",
                }
            }
        )
        cfg["profiles"]["hard"]["stages"]["implement"].update(agent="backup")
        result = {"status": "done", "output": PLAN, "profile": "hard"}
        result["triage"] = {"profile": "hard", "chosen_by": "triage", "reason": "Auth."}
        publish(self.gh, decision("plan"), result, CONTEXT, cfg, ".", self.out)
        self.assertIn(
            "plan `claude` (`fable`) · implement `backup` (`opus`) · review `claude` (`fable`)",
            latest_plan(self.gh.comments(5))["body"],
        )

    def test_plan_comment_says_when_triage_failed(self):
        error = "Claude did not finish within 5 minutes.\nMore detail."
        plan = self.publish_plan({"profile": "standard", "chosen_by": "default", "error": error})
        self.assertIn(
            "**Profile: standard**: plan `claude` (`opus`) · implement `claude` (`sonnet`) · "
            "review `claude` (`sonnet`). "
            "Triage failed (Claude did not finish within 5 minutes.), so this plan uses the "
            "default profile.",
            plan["body"],
        )
        plan = self.publish_plan({"profile": "hard", "chosen_by": "previous", "error": "x"})
        self.assertIn("so this plan uses the previous plan's profile.", plan["body"])
        self.assertEqual(plan_profile(plan)["chosen_by"], "previous")

    def test_plan_with_an_unknown_profile_is_not_published(self):
        with self.assertRaisesRegex(PublishError, "no configured profile: 'expert'"):
            self.publish_plan({"profile": "standard", "chosen_by": "triage"}, profile="expert")
        self.assertIsNone(latest_plan(self.gh.comments(5)))

    def test_full_plan_layout(self):
        body = render_plan(PLAN)
        order = [
            "## Plan\n\nadd() returns the sum instead of the difference.",
            "### What changes\n\n- add(a, b) returns a + b.\n- Negative numbers add up too.",
            "### Decisions to check\n\n- **add() keeps its name.** Callers use it today.",
            "### Risks and limits\n\n- Floats are out of scope.",
            "### Acceptance criteria\n\n- [ ] add(2, 3) returns 5.",
            "<details><summary>Implementation notes</summary>\n\n1. Edit `calc.py`: `return a + b`."
            "\n2. Add `test_add_negative`.\n\n</details>",
            "---\nComment `/nexkit go`",
        ]
        positions = [body.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        # The notes appear only inside <details>.
        self.assertEqual(body.count("calc.py"), 1)

    def test_the_plan_says_how_it_meets_each_checklist_item(self):
        entry = {"number": 1, "how": "It fixes the section.", "item": "Docs change with the code"}
        body = render_plan({**PLAN, "checklist": [entry]})
        self.assertIn("### Checklist\n\n- **Docs change with the code** It fixes", body)
        self.assertNotIn("Checklist", render_plan(PLAN))

    def test_a_plan_in_protected_paths_says_a_person_makes_the_change(self):
        body = render_plan({**PLAN, "protected_paths": [".github/workflows/ci.yml"]})
        self.assertIn("### Needs a hand-made change\n\nThe change modifies protected paths", body)
        self.assertIn("docs/troubleshooting.md#the-plans-change-is-in-protected-paths", body)
        self.assertIn("A person makes the change in protected paths by hand", body)
        self.assertEqual(
            plan_data(self.posted(body))["protected_paths"], [".github/workflows/ci.yml"]
        )

    def test_a_plan_that_finds_no_change_is_needed_shows_the_run_that_passed_since(self):
        passed = [
            {
                "id": 61,
                "url": "https://x/61",
                "sha": "c" * 40,
                "workflow": "ci.yml",
                "branch": "main",
            }
        ]
        body = render_plan({**PLAN, "no_change": "The host answered 409."}, passed=passed)
        self.assertIn(
            "### No change needed\n\nThe host answered 409.\n\n"
            "Passed on `main` since: [run 61 of `ci.yml`](https://x/61) at ccccccc.",
            body,
        )
        self.assertIn("This plan finds that no change is needed.", body)
        data = plan_data(self.posted(body))
        self.assertIn("Passed on `main` since", data["no_change"])
        self.assertNotIn("protected_paths", plan_data(self.posted(render_plan(PLAN))))

    def test_a_plan_whose_no_change_is_only_quotes_finds_nothing(self):
        for empty in ('""', "''", '" "', "  "):
            body = render_plan({**PLAN, "no_change": empty})
            self.assertNotIn("No change needed", body)
            self.assertNotIn("no_change", plan_data(self.posted(body)))
        body = render_plan({**PLAN, "no_change": '"The host answered 409."'})
        self.assertIn('### No change needed\n\n"The host answered 409."', body)

    def posted(self, body):
        return self.gh.comment(5, body)

    def test_replan_says_what_changed_since_the_last_plan(self):
        changed = ["The name limit is 2,000 characters again.", "`label` is accepted."]
        revised = {**PLAN, "revision": {"started_over": False, "changes": changed}}
        body = render_plan(revised, replan=True)
        self.assertIn(
            "### Since the last plan\n\nRevised the last plan.\n\n"
            "- The name limit is 2,000 characters again.\n- `label` is accepted.\n\n"
            "### What changes",
            body,
        )
        same = render_plan({**PLAN, "revision": {"started_over": False, "changes": []}}, None, True)
        self.assertIn("Revised the last plan.\n\nNo decision, acceptance criterion", same)
        fresh = {**PLAN, "revision": {"started_over": True, "changes": []}}
        self.assertIn(
            "### Since the last plan\n\nPlanned again from scratch, as asked.\n\n### What",
            render_plan(fresh, replan=True),
        )
        self.assertNotIn("Since the last plan", render_plan(revised))  # a first plan

    def test_replan_comment(self):
        result = {
            "status": "done",
            "output": {**PLAN, "revision": {"started_over": False, "changes": []}},
        }
        context = {**CONTEXT, "since_plan": "No discussion."}
        publish(self.gh, decision("plan"), result, context, self.cfg, ".", self.out)
        self.assertIn("Revised the last plan.", latest_plan(self.gh.comments(5))["body"])
        publish(self.gh, decision("plan"), result, CONTEXT, self.cfg, ".", self.out)
        self.assertNotIn("Since the last plan", latest_plan(self.gh.comments(5))["body"])

    def test_implementation_note_can_hold_a_code_block(self):
        step = (
            "Add the migration:\n\n```sh\ndotnet ef migrations add Orders\n"
            "dotnet ef database update\n```"
        )
        body = render_plan({**PLAN, "implementation_notes": [step, "Run the tests."]})
        self.assertIn(
            "1. Add the migration:\n\n   ```sh\n   dotnet ef migrations add Orders\n"
            "   dotnet ef database update\n   ```\n2. Run the tests.",
            body,
        )

    def test_plan_without_decisions_or_risks(self):
        body = render_plan({**PLAN, "decisions": [], "risks": [], "implementation_notes": []})
        for heading in ("Decisions to check", "Risks and limits", "<details>", "Questions"):
            self.assertNotIn(heading, body)
        self.assertIn("### What changes", body)
        self.assertIn("### Acceptance criteria", body)

    def test_implementing_agent_receives_the_notes(self):
        self.gh.comment(5, render_plan(PLAN))
        context = gather(self.gh, decision("implement"))
        self.assertIn("Add `test_add_negative`.", context["plan"])
        self.assertIn("Implementation notes", context["plan"])

    def test_plan_rendering_variants(self):
        base = {**PLAN, "decisions": [], "risks": [], "implementation_notes": []}
        questions = render_plan({**base, "questions": ["Which API?"], "too_large": False})
        self.assertIn("1. Which API?", questions)
        self.assertIn("Answer the questions", questions)
        large = render_plan(
            {**base, "questions": [], "too_large": True, "split": [{"title": "A", "body": "a"}]}
        )
        self.assertIn("1. **A**: a", large)
        self.assertIn("too large", large)
        json.dumps(large)

    def test_pull_request_description_order(self):
        output = {
            "summary": "add() now returns the sum.",
            "changes": ["add(a, b) returns a + b."],
            "testing": "test_add_negative covers negative numbers.",
            "outside_plan": ["Fixed a typo in the docstring."],
            "reviewer_notes": ["Floats still round down."],
        }
        body = pull_body({"issue": 5}, {"summary": output["summary"], "output": output})
        order = [
            "Closes #5",
            "## Summary\n\nadd() now returns the sum.",
            "## What changed\n\n- add(a, b) returns a + b.",
            "## How it is tested\n\ntest_add_negative covers negative numbers.",
            "## Outside the plan\n\n- Fixed a typo in the docstring.",
            "## Notes for the reviewer\n\n- Floats still round down.",
            "---\nImplemented by NexKit",
        ]
        positions = [body.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        plain = pull_body(
            {"issue": 5},
            {
                "summary": "S.",
                "output": {**output, "outside_plan": [], "reviewer_notes": [], "no_change": ""},
            },
        )
        self.assertNotIn("Outside the plan", plain)
        self.assertNotIn("Notes for the reviewer", plain)

    def test_pull_request_footer_follows_automatic_merge(self):
        result = {"summary": "S.", "output": {}}
        manual = pull_body({"issue": 5}, result)
        self.assertTrue(manual.endswith("A person decides whether to merge."))
        auto = pull_body({"issue": 5}, result, auto_merge=True)
        self.assertNotIn("A person decides", auto)
        self.assertIn("NexKit merges it once the checks pass and the review approves", auto)


class MergePublishTests(unittest.TestCase):
    """A fix round that merged the base branch, from the agent's checkout to the push."""

    def setUp(self):
        self.repos = GitRepos()
        self.gh = FakeGitHub()
        self.gh.add_issue(5)
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.claude = FakeClaude(self.tmp.name)
        self.cfg = make_config()

    def tearDown(self):
        self.claude.restore()
        self.tmp.cleanup()
        self.repos.cleanup()

    def run_fix(self, write, **context):
        """Run the fix stage in a fresh checkout with a fake agent that writes `write`."""
        self.rounds = getattr(self, "rounds", 0) + 1
        work = self.repos.clone(f"agent-{self.rounds}")
        git(work, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5")
        done = {
            "status": "done",
            "summary": "Kept sub() and mul().",
            "blocker": "",
            "questions": [],
        }
        result = {
            **done,
            "checks_run": [],
            "protected_paths": [],
            "cause": "",
            "cause_outside": False,
            "open_conflicts": [],
        }
        self.claude.configure(result=result, write=write)
        context = {
            **CONTEXT,
            "pr": 6,
            "body": "",
            "discussion": "",
            "plan": "",
            "note": "",
            **context,
        }
        result = agent.run_stage(
            "fix", context, self.cfg, work, self.out, program=str(self.claude.path), base="main"
        )
        self.assertEqual(result["status"], "done", result.get("error"))
        return result

    def publish_fix(self, result, **options):
        return publish(
            self.gh,
            decision("fix", pr=6, target=6),
            result,
            CONTEXT,
            self.cfg,
            self.repos.clone(f"publisher-{self.rounds}"),
            self.out,
            **options,
        )

    def fix_round(self, write, **options):
        return self.publish_fix(self.run_fix(write), **options)

    def test_fix_round_publishes_a_merge_commit(self):
        head, base = self.repos.diverge({".nexkit/config.json": "{}\n"})
        result = self.run_fix({"calc.py": CALC + SUB + MUL})
        # The base branch moves on while the agent works.
        later = self.repos.push_commit("dev", "main", {"later.py": "x = 1\n"}, "Later")
        outcome = self.publish_fix(result)
        self.assertTrue(outcome["published"])
        new_head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(outcome["head"], new_head)
        # Parents: the branch head and the exact base commit merged, not the moved base.
        parents = git(self.repos.origin, "log", "-1", "--format=%P", new_head).split()
        self.assertEqual(parents, [head, base])
        self.assertEqual(git(self.repos.origin, "rev-parse", "main"), later)
        self.assertEqual(
            git(self.repos.origin, "log", "-1", "--format=%s", new_head),
            "Merge main and address feedback on #6",
        )
        calc = git(self.repos.origin, "show", f"{new_head}:calc.py")
        self.assertIn("def sub(a, b):", calc)
        self.assertIn("def mul(a, b):", calc)
        # The base's change to a protected path came with the merge and was not rejected.
        self.assertEqual(git(self.repos.origin, "show", f"{new_head}:.nexkit/config.json"), "{}")
        # The pull request merges cleanly into the base branch again.
        self.assertEqual(merge_tree(self.repos.origin, "main", new_head)[1], [])
        self.assertEqual(result["conflicts"], ["calc.py"])

    def test_merge_that_keeps_the_branch_side_has_an_empty_patch(self):
        head, base = self.repos.diverge()
        result = self.run_fix({"calc.py": CALC + SUB})
        self.assertEqual((self.out / "changes.patch").read_bytes(), b"")
        outcome = self.publish_fix(result)
        new_head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(outcome["head"], new_head)
        parents = git(self.repos.origin, "log", "-1", "--format=%P", new_head).split()
        self.assertEqual(parents, [head, base])
        self.assertEqual(
            git(self.repos.origin, "show", f"{new_head}:calc.py"), (CALC + SUB).strip()
        )

    def test_a_moved_base_merged_after_failing_checks_is_published_alone(self):
        head = self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Sub")
        base = self.repos.push_commit("dev", "main", {"flaky.py": "fixed = True\n"}, "Flaky")
        result = self.run_fix({}, merge_moved_base=True)
        self.assertEqual((result["start_base"], result["conflicts"]), (base, []))
        outcome = self.publish_fix(result)
        new_head = git(self.repos.origin, "rev-parse", "nexkit/issue-5")
        self.assertEqual(outcome["head"], new_head)
        parents = git(self.repos.origin, "log", "-1", "--format=%P", new_head).split()
        self.assertEqual(parents, [head, base])
        self.assertEqual(git(self.repos.origin, "show", f"{new_head}:flaky.py"), "fixed = True")
        self.assertEqual(git(self.repos.origin, "diff", base, new_head, "--name-only"), "calc.py")

    def test_agent_edit_to_a_protected_path_is_still_rejected(self):
        self.repos.diverge({".nexkit/config.json": "{}\n"})
        with self.assertRaisesRegex(PublishError, r"protected paths.*`\.nexkit/config\.json`"):
            self.fix_round({"calc.py": CALC + SUB + MUL, ".nexkit/config.json": "[]\n"})
        self.assertNotIn("Merge", git(self.repos.origin, "log", "--format=%s", "nexkit/issue-5"))

    def test_merged_commit_must_be_on_the_base_branch(self):
        self.repos.diverge()
        other = self.repos.push_commit("dev", "other", {"x.py": "1\n"}, "Elsewhere")
        work = self.repos.clone("agent")
        git(work, "checkout", "-q", "nexkit/issue-5")
        start = git(work, "rev-parse", "HEAD")
        (self.out).mkdir(parents=True, exist_ok=True)
        (work / "calc.py").write_text(CALC + SUB + MUL)
        git(work, "add", "-A")
        patch = subprocess.run(
            ["git", "diff", "--cached", "--binary", "HEAD"], cwd=work, capture_output=True
        ).stdout
        (self.out / "changes.patch").write_bytes(patch)
        result = {"status": "done", "summary": "S.", "start_head": start, "start_base": other}
        with self.assertRaisesRegex(PublishError, "is not on `main`"):
            publish(
                self.gh,
                decision("fix", pr=6, target=6),
                result,
                CONTEXT,
                self.cfg,
                self.repos.clone("publisher"),
                self.out,
            )


if __name__ == "__main__":
    unittest.main()

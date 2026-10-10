import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, runlog
from nexkit.redact import Redactor
from tests.support import (
    CALC,
    MUL,
    SUB,
    FakeClaude,
    GitRepos,
    git,
    make_config,
    profile_config,
)

CONTEXT = {
    "issue": 5,
    "pr": "",
    "title": "Fix add",
    "body": "add() subtracts. Use $HOME and ${{ secrets.X }} literally.",
    "discussion": "No discussion.",
    "outside_plan": "None.",
    "plan": "Change add to return a + b.",
    "note": "None.",
    "feedback": "",
}
DONE = {"status": "done", "summary": "Fixed add.", "blocker": "", "checks_run": ["make test"]}
FIX_DONE = {**DONE, "open_conflicts": []}


class PromptTests(unittest.TestCase):
    def test_prompt_keeps_untrusted_text_literal(self):
        prompt = agent.build_prompt("implement", CONTEXT, make_config())
        self.assertIn("Use $HOME and ${{ secrets.X }} literally.", prompt)
        self.assertIn("`test`: `true`", prompt)
        self.assertIn("`.github/`", prompt)
        self.assertNotIn("$plan", prompt)

    def test_first_plan_has_no_latest_plan(self):
        extra = {"previous": agent.previous_plan(CONTEXT)}
        prompt = agent.build_prompt("plan", CONTEXT, make_config(), extra)
        self.assertNotIn("Latest plan", prompt)
        self.assertIn("</untrusted>\n\n## Note from the person", prompt)
        self.assertNotRegex(prompt, r"\$(previous|since_plan)\b")

    def test_replan_revises_the_latest_plan(self):
        context = {**CONTEXT, "since_plan": "@olivia: Also accept `label`."}
        extra = {"previous": agent.previous_plan(context)}
        prompt = agent.build_prompt("plan", context, make_config(), extra)
        order = [
            "## Discussion\n",
            "## Latest plan\n",
            "Change add to return a + b.",
            "## Discussion since the latest plan\n",
            "Also accept `label`.",
            "## How to use the latest plan",
            "## Note from the person",
            "`revision`",
        ]
        positions = [prompt.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("`revision.started_over`", prompt)

    def test_first_review_has_no_previous_round(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        prompt = agent.build_prompt("review", CONTEXT, make_config(), extra)
        self.assertNotIn("Previous round", prompt)
        self.assertIn("</untrusted>\n\n## How to review", prompt)

    def test_review_sees_the_discussion_and_why_the_code_leaves_the_plan(self):
        context = {
            **CONTEXT,
            "discussion": "@olivia: Planned for M6.",
            "outside_plan": "- Wrote M6: the owner asked for it.",
        }
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        prompt = agent.build_prompt("review", context, make_config(), extra)
        self.assertIn("## Discussion\n", prompt)
        self.assertIn("@olivia: Planned for M6.", prompt)
        self.assertIn("## Outside the plan\n", prompt)
        self.assertIn("- Wrote M6: the owner asked for it.", prompt)
        self.assertIn("against its reason under *Outside the plan*", prompt)
        self.assertNotIn("$discussion", prompt)
        self.assertNotIn("$outside_plan", prompt)

    def test_every_prompt_has_the_writing_guide(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("plan", "implement", "fix", "review"):
            prompt = agent.build_prompt(stage, CONTEXT, make_config(), extra)
            self.assertIn("## How to write", prompt, stage)
            self.assertIn("Write for a person who knows the project", prompt, stage)
            self.assertNotIn("$writing", prompt, stage)
            guide = prompt.index("## How to write")
            self.assertLess(guide, prompt.index("## What to return"), stage)

    def test_plan_schema_separates_the_person_from_the_agent(self):
        schema = agent.SCHEMAS["plan"]
        for field in ("changes", "decisions", "risks", "implementation_notes"):
            self.assertIn(field, schema["required"])
            self.assertIn(f"`{field}`", agent._template("plan"))
        decision = schema["properties"]["decisions"]["items"]
        self.assertEqual(decision["required"], ["decision", "reason"])
        self.assertIn("revision", schema["required"])
        revision = schema["properties"]["revision"]
        self.assertEqual(revision["required"], ["started_over", "changes"])
        self.assertNotIn("approach", schema["properties"])

    def test_implement_schema_has_the_pull_request_sections(self):
        implement, fix = agent.SCHEMAS["implement"], agent.SCHEMAS["fix"]
        for field in ("summary", "changes", "testing", "outside_plan", "reviewer_notes"):
            self.assertIn(field, implement["required"])
            self.assertIn(f"`{field}`", agent._template("implement"))
        self.assertNotIn("changes", fix["required"])
        self.assertIn("status", fix["required"])

    def test_review_schema_keeps_evidence_apart_from_the_criterion(self):
        item = agent.SCHEMAS["review"]["properties"]["criteria"]["items"]
        self.assertEqual(item["required"], ["criterion", "met", "test", "evidence"])

    def test_review_schema_requires_previous_findings(self):
        schema = agent.SCHEMAS["review"]
        self.assertIn("previous_findings", schema["required"])
        item = schema["properties"]["previous_findings"]["items"]
        self.assertEqual(
            item["properties"]["resolution"]["enum"],
            ["resolved", "unresolved", "rejection_accepted"],
        )
        self.assertIn("severity", item["required"])
        self.assertEqual(item["properties"]["severity"]["enum"], ["blocking", "suggestion"])
        self.assertIn("`severity` (the severity that review gave it)", agent._template("review"))

    def test_every_stage_template_is_complete(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("plan", "implement", "fix", "review"):
            prompt = agent.build_prompt(stage, CONTEXT, make_config(), extra)
            for name in ("$issue", "$title", "$plan", "$checks", "$protected", "$diff"):
                self.assertNotIn(name, prompt, f"{stage} leaves {name}")

    def test_command_restricts_read_only_stages(self):
        cfg = make_config(
            effort="high", stages={"implement": {"max_budget_usd": 5, "model": "opus"}}
        )
        plan = agent.claude_command("plan", cfg)
        self.assertIn("Read,Grep,Glob", plan)
        self.assertNotIn("bypassPermissions", plan)
        fix = agent.claude_command("fix", cfg)
        self.assertIn("bypassPermissions", fix)
        self.assertEqual(fix[fix.index("--model") + 1], "opus")
        self.assertEqual(fix[fix.index("--max-budget-usd") + 1], "5")
        self.assertEqual(fix[fix.index("--effort") + 1], "high")
        schema = json.loads(fix[fix.index("--json-schema") + 1])
        self.assertIn("status", schema["required"])

    def test_triage_command_has_no_tools_and_names_the_profiles(self):
        cfg = profile_config(stages={"triage": {"model": "fable", "max_budget_usd": 1}})
        cmd = agent.claude_command("triage", cfg)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "fable")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1")
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["profile"]["enum"], ["standard", "hard"])
        self.assertNotIn("enum", agent.SCHEMAS["triage"]["properties"]["profile"])


class InterpretTests(unittest.TestCase):
    def run_result(self, event=None, **kw):
        return {
            "event": event,
            "returncode": 0,
            "timed_out": False,
            "stderr": "",
            "seconds": 1.0,
            **kw,
        }

    def test_outcomes(self):
        ok = {"subtype": "success", "structured_output": DONE, "total_cost_usd": 1.5}
        self.assertEqual(agent.interpret("implement", self.run_result(ok), 5)["status"], "done")
        blocked = {**ok, "structured_output": {**DONE, "status": "blocked", "blocker": "No spec"}}
        result = agent.interpret("implement", self.run_result(blocked), 5)
        self.assertEqual((result["status"], result["error"]), ("blocked", "No spec"))
        timeout = agent.interpret("plan", self.run_result(ok, timed_out=True), 7)
        self.assertIn("7 minutes", timeout["error"])
        missing = agent.interpret("plan", self.run_result(None, stderr="auth failed"), 5)
        self.assertIn("auth failed", missing["error"])
        budget = agent.interpret(
            "plan", self.run_result({"subtype": "error_max_budget_usd", "is_error": True}), 5
        )
        self.assertIn("error_max_budget_usd", budget["error"])
        unstructured = agent.interpret("review", self.run_result({"subtype": "success"}), 5)
        self.assertIn("structured result", unstructured["error"])


class StageCase(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.repo = self.repos.clone("work")
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.claude = FakeClaude(self.tmp.name)

    def tearDown(self):
        self.claude.restore()
        self.tmp.cleanup()
        self.repos.cleanup()

    def run_stage(self, stage, cfg=None, **kw):
        return agent.run_stage(
            stage,
            CONTEXT,
            cfg or make_config(),
            self.repo,
            self.out,
            claude=str(self.claude.path),
            **kw,
        )

    def run_quietly(self, stage, cfg=None, **kw):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.run_stage(stage, cfg, **kw)
        return result, stdout.getvalue()


class RunStageTests(StageCase):
    def test_implement_collects_only_agent_changes(self):
        # Setup side effects (tracked or not) must not appear in the patch.
        (self.repo / "setup-output.txt").write_text("generated by npm install\n")
        start = git(self.repo, "rev-parse", "HEAD")
        self.claude.configure(
            result=DONE,
            write={
                "calc.py": "def add(a, b):\n    return a + b\n",
                "ignored/x": "1",
                "__pycache__/calc.cpython-312.pyc": "x",
                "web/node_modules/pkg/index.js": "x",
            },
        )
        result = self.run_stage("implement")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertEqual(result["start_head"], start)
        self.assertEqual(result["cost"], 0.5)
        patch = (self.out / "changes.patch").read_text()
        self.assertIn("+    return a + b", patch)
        self.assertNotIn("setup-output", patch)
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["status"], "done")
        self.assertTrue((self.out / "transcript.jsonl").read_text())
        call = self.claude.call()
        self.assertIsNone(call["base_sha"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("Change add to return a + b.", call["prompt"])

    def triage(self, previous=None, **spec):
        self.claude.configure(**spec)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return agent.run_triage(
                {**CONTEXT, "requests": "@olivia: Use the hard profile."},
                profile_config(),
                previous,
                self.out,
                claude=str(self.claude.path),
            )

    def test_triage_chooses_the_profile_from_the_issue_text(self):
        result = self.triage(result={"profile": "hard", "reason": "Touches the login code."})
        self.assertEqual(
            {k: result[k] for k in ("status", "profile", "chosen_by", "reason", "cost")},
            {
                "status": "done",
                "profile": "hard",
                "chosen_by": "triage",
                "reason": "Touches the login code.",
                "cost": 0.5,
            },
        )
        call = self.claude.call()
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        prompt = call["prompt"]
        self.assertIn("- `hard`: Touches security or storage.", prompt)
        self.assertIn("None. This is the first plan", prompt)
        self.assertIn("@olivia: Use the hard profile.", prompt)
        self.assertIn("Use $HOME and ${{ secrets.X }} literally.", prompt)
        self.assertIn("## How to write", prompt)
        self.assertNotRegex(prompt.replace("$HOME", ""), r"\$[a-z_]+")
        self.assertEqual(prompt, (self.out / "triage/prompt.md").read_text())
        self.assertTrue((self.out / "triage/transcript.md").exists())

    def test_triage_failure_falls_back_to_the_kept_or_default_profile(self):
        unknown = self.triage(result={"profile": "expert", "reason": "x"})
        self.assertEqual((unknown["status"], unknown["profile"]), ("error", "standard"))
        self.assertEqual(unknown["chosen_by"], "default")
        self.assertIn("unknown profile: 'expert'", unknown["error"])
        crashed = self.triage("hard", exit=1)
        self.assertEqual((crashed["profile"], crashed["chosen_by"]), ("hard", "previous"))
        self.assertIn("without a result", crashed["error"])
        prompt = self.claude.call()["prompt"]
        self.assertIn("The latest plan of this issue used the profile `hard`.", prompt)

    def test_plan_keeps_the_triage_result_and_its_cost(self):
        triage = self.triage(result={"profile": "hard", "reason": "Auth."})
        cfg = agent.configuration.with_profile(profile_config(), "hard")
        self.claude.configure(result={"summary": "Plan."})
        result, _ = self.run_quietly("plan", cfg, triage=triage)
        self.assertEqual(result["profile"], "hard")
        self.assertEqual(result["triage"]["reason"], "Auth.")
        self.assertEqual(result["cost"], 1.0)
        args = self.claude.call()["args"]
        self.assertEqual(args[args.index("--model") + 1], "fable")
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["triage"]["chosen_by"], "triage")

    def test_no_change_is_an_error(self):
        self.claude.configure(result=DONE)
        result = self.run_stage("implement")
        self.assertEqual(result["status"], "error")
        self.assertIn("without changing", result["error"])

    def test_blocked_agent_publishes_nothing(self):
        self.claude.configure(
            result={**DONE, "status": "blocked", "blocker": "Needs a decision"},
            write={"calc.py": "partial"},
        )
        result = self.run_stage("implement")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse((self.out / "changes.patch").exists())

    def test_review_includes_diff_and_checks(self):
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "fix")
        verdict = {"verdict": "approve", "summary": "Good", "criteria": [], "findings": []}
        self.claude.configure(result=verdict)
        checks = [{"name": "test", "run": "true", "exit_code": 0, "passed": True, "output": ""}]
        result = self.run_stage("review", checks=checks, base="main")
        self.assertEqual(result["status"], "done")
        prompt = self.claude.call()["prompt"]
        self.assertIn("+    return a + b", prompt)
        self.assertIn("`test` (`true`): passed", prompt)

    def test_review_after_a_fix_gets_the_previous_round(self):
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        reviewed = self.repos.commit(self.repo, "fix")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return float(a) + b\n")
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "floats")
        context = {
            **CONTEXT,
            "previous_head": reviewed,
            "previous_round": "1. blocking, `calc.py:2`: Use $floats",
        }
        verdict = {
            "verdict": "approve",
            "summary": "Good",
            "criteria": [],
            "findings": [],
            "previous_findings": [],
        }
        self.claude.configure(result=verdict)
        agent.run_stage(
            "review",
            context,
            make_config(),
            self.repo,
            self.out,
            claude=str(self.claude.path),
            checks=[],
            base="main",
        )
        prompt = self.claude.call()["prompt"]
        previous = prompt.split("## Previous round", 1)[1].split("## How to review", 1)[0]
        self.assertIn(f"commit {reviewed[:7]}", previous)
        self.assertIn("Use $floats", previous)
        full = prompt.split("## Diff against main", 1)[1].split("## Previous round", 1)[0]
        self.assertIn("-    return a - b", full)
        self.assertNotIn("-    return a + b", full)
        self.assertIn("-    return a + b\n+    return float(a) + b", previous)
        self.assertNotIn("-    return a - b", previous)

    def test_previous_round_survives_a_missing_commit(self):
        context = {**CONTEXT, "previous_head": "f" * 40, "previous_round": "x"}
        self.assertIn("not in this checkout", agent.previous_round(self.repo, context, "main"))
        context["previous_head"] = "--output=/tmp/x"
        self.assertIn("unknown", agent.previous_round(self.repo, context, "main"))
        self.assertEqual(agent.previous_round(self.repo, CONTEXT, "main"), "")

    def test_missing_credentials(self):
        self.claude.configure(result=DONE)
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_API_KEY": "", "GITHUB_ACTIONS": "true"}
        with mock.patch.dict(os.environ, env):
            result = self.run_stage("plan")
        self.assertEqual(result["status"], "error")
        self.assertIn("No Claude credential", result["error"])

    def test_timeout_kills_claude(self):
        self.claude.configure(result=DONE, sleep=30)
        run = agent.run_claude(
            "x",
            [str(self.claude.path)],
            cwd=self.repo,
            timeout_seconds=1,
            log=runlog.RunLog(Redactor(), transcript_dir=self.tmp.name),
        )
        self.assertTrue(run["timed_out"])
        self.assertLess(run["seconds"], 15)


class ConflictTests(StageCase):
    """A fix round on a pull request that conflicts with its base branch."""

    def checkout_branch(self):
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5")

    def run_fix(self, cfg=None, **spec):
        self.claude.configure(**{"result": FIX_DONE, **spec})
        return self.run_stage("fix", cfg, base="main")

    def test_fix_merges_a_conflicting_base_branch(self):
        head, base = self.repos.diverge({".nexkit/config.json": '{"model": "opus"}\n'})
        self.checkout_branch()
        result = self.run_fix(write={"calc.py": CALC + SUB + MUL})
        self.assertEqual(result["status"], "done")
        self.assertEqual((result["start_head"], result["start_base"]), (head, base))
        self.assertEqual(result["conflicts"], ["calc.py"])
        self.assertEqual(sorted(result["changed_files"]), [".nexkit/config.json", "calc.py"])
        prompt = self.claude.call()["prompt"]
        section = prompt.split("## Conflicts with `main`", 1)[1].split("## Approved plan")[0]
        self.assertIn(f"`main` (commit {base[:7]})", section)
        self.assertIn("- `calc.py`\n", section)
        self.assertIn("`open_conflicts`", section)
        patch = (self.out / "changes.patch").read_text()
        self.assertIn("+def mul(a, b):", patch)
        self.assertNotIn("<<<<<<<", patch)

    def test_claude_sees_the_base_of_the_merge(self):
        fork = git(self.repo, "rev-parse", "HEAD")
        _, base = self.repos.diverge()
        self.checkout_branch()
        self.claude.configure(result=FIX_DONE, write={"calc.py": CALC + SUB + MUL})
        with mock.patch.dict(os.environ, {"NEXKIT_BASE_SHA": "stale"}):
            _, log = self.run_quietly("fix", base="main", base_sha=fork)
        # Once the base branch is merged in, the merged commit is the merge base.
        self.assertEqual(self.claude.call()["base_sha"], base)
        self.assertIn(f"NEXKIT_BASE_SHA={base}", log)

    def test_branch_that_merges_cleanly_is_not_merged(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"other.py": "x = 1\n"}, "Other")
        fork = git(self.repo, "rev-parse", "HEAD")
        self.checkout_branch()
        self.claude.configure(result=FIX_DONE, write={"calc.py": CALC + SUB + "# fixed\n"})
        with mock.patch.dict(os.environ, {"NEXKIT_BASE_SHA": "stale"}):
            result = self.run_stage("fix", base="main", base_sha=fork)
        self.assertEqual(self.claude.call()["base_sha"], fork)
        self.assertEqual(result["status"], "done")
        self.assertIsNone(result["start_base"])
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertNotIn("## Conflicts with", self.claude.call()["prompt"])
        self.assertIn("\n</untrusted>\n\n## Approved plan", self.claude.call()["prompt"])

    def test_leftover_conflict_markers_are_an_error(self):
        self.repos.diverge()
        self.checkout_branch()
        result = self.run_fix(write={"README.md": "notes\n"})
        self.assertEqual(result["status"], "error")
        self.assertIn("conflict markers in `calc.py`", result["error"])

    def test_blocked_on_a_conflict_publishes_nothing(self):
        self.repos.diverge()
        self.checkout_branch()
        question = {
            "file": "calc.py",
            "base_change": "Adds mul().",
            "pr_change": "Adds sub().",
            "question": "Which one?",
        }
        blocked = {**FIX_DONE, "status": "blocked", "blocker": "Needs a decision"}
        result = self.run_fix(result={**blocked, "open_conflicts": [question]})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["output"]["open_conflicts"], [question])
        self.assertEqual(result["conflicts"], ["calc.py"])
        self.assertFalse((self.out / "changes.patch").exists())

    def test_conflicts_in_protected_paths_stop_before_claude(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {".nexkit/config.json": "{}\n"}, "PR")
        self.repos.push_commit("dev", "main", {".nexkit/config.json": "[]\n"}, "Base")
        self.checkout_branch()
        result = self.run_fix()
        self.assertEqual(result["status"], "error")
        self.assertIn("conflicts with `main` in protected paths", result["error"])
        self.assertIn("`.nexkit/config.json`", result["error"])
        self.assertFalse(self.claude.log.exists())
        self.assertTrue((self.out / "result.json").exists())

    def test_fix_schema_lists_open_conflicts(self):
        schema = agent.SCHEMAS["fix"]
        self.assertIn("open_conflicts", schema["required"])
        item = schema["properties"]["open_conflicts"]["items"]
        self.assertEqual(item["required"], ["file", "base_change", "pr_change", "question"])
        self.assertNotIn("open_conflicts", agent.SCHEMAS["implement"]["required"])
        for field in item["required"]:
            self.assertIn(f"`{field}`", agent._template("fix"))

    def test_fix_prompt_asks_for_a_decision_instead_of_a_guess(self):
        section = agent.conflicts_text("main", "b" * 40, ["Program.cs"])
        self.assertIn("whether it has one right answer or needs a choice", section)
        self.assertIn("do not pick a side and do not guess", section)
        self.assertIn("return `status: blocked` and list every\n  such conflict", section)
        self.assertIn("A note from the person who requested this round settles the choice", section)
        self.assertIn("only resolve the conflicts", section)
        self.assertEqual(agent.conflicts_text("main", None, []), "")

    def review_after_merge(self):
        """Review a fix round that merged main, after an earlier review of the branch."""
        head, base = self.repos.diverge()
        self.checkout_branch()
        # The merge stops on the conflict in calc.py; the fix round resolves it.
        merge = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-commit"]
        merge.append("origin/main")
        self.assertEqual(subprocess.run(merge, cwd=self.repo, capture_output=True).returncode, 1)
        (self.repo / "calc.py").write_text(CALC + SUB + MUL)
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "Merge main")
        context = {
            **CONTEXT,
            "previous_head": head,
            "previous_round": "1. suggestion, `calc.py:4`: Document sub().",
            "merged": {"head": head, "base": base, "conflicts": ["calc.py"]},
        }
        verdict = {
            "verdict": "approve",
            "summary": "Good",
            "criteria": [],
            "findings": [],
            "previous_findings": [],
        }
        self.claude.configure(result=verdict)
        agent.run_stage(
            "review",
            context,
            make_config(),
            self.repo,
            self.out,
            claude=str(self.claude.path),
            checks=[],
            base="main",
        )
        return self.claude.call()["prompt"], base

    def test_review_checks_both_sides_of_a_merge(self):
        prompt, base = self.review_after_merge()
        section = prompt.split("## Merge with `main` in this round", 1)[1]
        section = section.split("## Previous round", 1)[0]
        self.assertIn(f"merged `main` (commit\n{base[:7]})", section)
        self.assertIn("- `calc.py`", section)
        main_side, pr_side = section.split("This pull request:", 1)
        self.assertIn("+def mul(a, b):", main_side)
        self.assertIn("+def sub(a, b):", pr_side)
        self.assertNotIn("+def mul", pr_side)
        self.assertIn("check that the current code keeps both sides", section)

    def test_changes_since_the_last_review_leave_out_the_base(self):
        prompt, _ = self.review_after_merge()
        since = prompt.split("## Changes since commit", 1)[1].split("## How to use", 1)[0]
        self.assertIn("Changes that came from `main` through a merge are left out.", since)
        # The resolution shows; the clean part of main's change does not.
        self.assertIn("def mul(a, b):", since)
        self.assertNotIn("+def add", since)
        self.assertIn(">>>>>>>", since)
        full = prompt.split("## Diff against main", 1)[1].split("## Merge with", 1)[0]
        self.assertNotIn("+def mul", full)
        self.assertIn("+def sub(a, b):", full)


class AgentLogTests(StageCase):
    """What the agent job prints, stores and summarises."""

    SECRET = "fake-api-key-0123456789"

    def configure(self, **spec):
        events = [
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "text", "text": f"The key is {self.SECRET}"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "env"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": f"ANTHROPIC_API_KEY={self.SECRET}\n::add-mask::x",
                        }
                    ]
                },
            },
        ]
        self.claude.configure(events=events, **spec)
        self.enterContext(mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": self.SECRET}))

    def test_secrets_from_the_environment_are_redacted(self):
        self.configure(result={**DONE, "summary": f"Used {self.SECRET}"}, write={"calc.py": "1"})
        summary = Path(self.tmp.name) / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            result, stdout = self.run_quietly("implement")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["summary"], "Used ***")
        stored = [self.out / name for name in ("transcript.jsonl", "transcript.md", "result.json")]
        for text in [stdout, summary.read_text(), *(path.read_text() for path in stored)]:
            self.assertNotIn(self.SECRET, text)
        self.assertIn("ANTHROPIC_API_KEY=***", stdout)
        self.assertIn("::group::[00:00 #1] ▸ Bash env", stdout)
        self.assertIn("### NexKit implement: done", summary.read_text())
        self.assertIn("✓ env", summary.read_text())

    def test_transcript_switch(self):
        self.configure(result=DONE, write={"calc.py": "1"})
        result, _ = self.run_quietly("implement", make_config(transcript=False))
        self.assertEqual(result["status"], "done")
        self.assertFalse((self.out / "transcript.jsonl").exists())
        self.assertFalse((self.out / "transcript.md").exists())
        self.assertTrue((self.out / "result.json").exists())
        self.assertTrue((self.out / "changes.patch").exists())

    def test_tool_output_none_keeps_the_one_line_log(self):
        self.claude.configure(result=DONE, write={"calc.py": "1"})
        _, stdout = self.run_quietly("implement", make_config(log={"tool_output": "none"}))
        self.assertEqual(stdout, "· Working on it\n▸ Bash make test\n")


if __name__ == "__main__":
    unittest.main()

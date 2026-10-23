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
from nexkit.runtimes import RUNTIMES
from nexkit.runtimes.claude_code import ClaudeReader
from tests.support import (
    CALC,
    MUL,
    SUB,
    FakeClaude,
    GitRepos,
    git,
    interpret_claude,
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
DONE = {
    "status": "done",
    "summary": "Fixed add.",
    "blocker": "",
    "questions": [],
    "checks_run": ["make test"],
    "changes": ["add() adds."],
    "testing": "make test",
    "outside_plan": [],
    "reviewer_notes": [],
}
FIX_DONE = {**DONE, "cause": "", "open_conflicts": []}
PLAN = {
    "summary": "Plan.",
    "changes": ["Fix add()."],
    "decisions": [],
    "risks": [],
    "acceptance_criteria": ["add(2, 3) is 5."],
    "implementation_notes": [],
    "questions": [],
    "too_large": False,
    "split": [],
    "revision": {"started_over": True, "changes": []},
}
VERDICT = {
    "verdict": "approve",
    "summary": "Good",
    "criteria": [],
    "findings": [],
    "previous_findings": [],
}


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

    def test_plan_criteria_are_what_the_review_can_verify(self):
        prompt = " ".join(agent.build_prompt("plan", CONTEXT, make_config()).split())
        self.assertIn(
            "each verifiable by the review from what it sees: a test in the change, or the "
            "result of a project check above, run once on the candidate commit.",
            prompt,
        )
        self.assertIn(
            "a step only the implementing agent can do, such as repeated runs, putting the "
            "cause back or a manual probe, is not a criterion; put it in `implementation_notes`",
            prompt,
        )
        # A flaky failure gets a test that reproduces its cause, not repeated passes.
        self.assertIn(
            "For a failure that happens only at times, such as a flaky test, make the "
            "criterion a test that reproduces the cause on every run",
            prompt,
        )
        self.assertIn("cannot be shown by a test or a check, say so in `risks`", prompt)

    def test_review_judges_each_criterion_and_unmet_ones_request_changes(self):
        prompt = " ".join(agent.build_prompt("review", CONTEXT, make_config()).split())
        self.assertIn("no criterion is `unmet` and every check passed", prompt)
        self.assertIn("`unverifiable` when they hold no evidence either way", prompt)
        self.assertIn("A criterion that a collaborator dropped or replaced", prompt)

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

    def test_writing_stages_run_commands_in_the_foreground(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("implement", "fix"):
            prompt = agent.build_prompt(stage, CONTEXT, make_config(), extra)
            self.assertIn("Run every command in the foreground", prompt, stage)

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
        self.assertEqual(item["required"], ["criterion", "status", "test", "evidence"])
        self.assertEqual(item["properties"]["status"]["enum"], ["met", "unmet", "unverifiable"])

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
            self.assertNotIn("$round_checks", prompt, stage)

    def test_without_slow_checks_the_prompts_list_every_check_as_before(self):
        checks = [{"name": "unit", "run": "make test"}, {"name": "lint", "run": "make lint"}]
        cfg = make_config(checks=checks)
        listed = "  - `unit`: `make test`\n  - `lint`: `make lint`\n"
        for stage in ("plan", "implement", "fix"):
            prompt = agent.build_prompt(stage, CONTEXT, cfg)
            self.assertIn(listed, prompt, stage)
            self.assertNotIn("slow", prompt, stage)
        rule = "- Before finishing, run the project checks and fix any failures you caused:\n"
        self.assertIn(rule + listed, agent.build_prompt("implement", CONTEXT, cfg))
        rule = "- Before finishing, run the project checks:\n"
        self.assertIn(rule + listed, agent.build_prompt("fix", CONTEXT, cfg))

    def test_slow_checks_are_listed_apart_and_not_run_in_full(self):
        checks = [
            {"name": "unit", "run": "make test"},
            {"name": "e2e", "run": "scripts/e2e.sh", "slow": True},
            {"name": "lint", "run": "make lint"},
        ]
        cfg = make_config(checks=checks)
        for stage in ("implement", "fix"):
            prompt = agent.build_prompt(stage, CONTEXT, cfg)
            order = [
                "  - `unit`: `make test`\n  - `lint`: `make lint`\n",
                "  - NexKit runs these slow checks after your round. Do not run them in full.",
                "only the tests in them that cover your change or that failed",
                "    - `e2e`: `scripts/e2e.sh`\n- Run every command in the foreground",
            ]
            positions = [prompt.index(text) for text in order]
            self.assertEqual(positions, sorted(positions), stage)
            self.assertEqual(prompt.count("`e2e`"), 1, stage)
        # The plan lists every check NexKit runs, slow or not.
        plan = agent.build_prompt("plan", CONTEXT, cfg)
        self.assertIn("  - `unit`: `make test`\n  - `e2e`: `scripts/e2e.sh`\n", plan)
        self.assertNotIn("slow", plan)

    def argv(self, stage, cfg):
        spec = agent.stage_spec(stage, cfg, "/nonexistent")
        return RUNTIMES["claude-code"].launch(cfg["agents"]["claude"], spec, {}).argv

    def test_command_restricts_read_only_stages(self):
        cfg = make_config(
            effort="high", stages={"implement": {"max_budget_usd": 5, "model": "opus"}}
        )
        plan = self.argv("plan", cfg)
        self.assertIn("Read,Grep,Glob", plan)
        self.assertNotIn("bypassPermissions", plan)
        fix = self.argv("fix", cfg)
        self.assertIn("bypassPermissions", fix)
        self.assertEqual(fix[fix.index("--model") + 1], "opus")
        self.assertEqual(fix[fix.index("--max-budget-usd") + 1], "5")
        self.assertEqual(fix[fix.index("--effort") + 1], "high")
        schema = json.loads(fix[fix.index("--json-schema") + 1])
        self.assertIn("status", schema["required"])

    def test_triage_command_has_no_tools_and_names_the_profiles(self):
        cfg = profile_config(stages={"triage": {"model": "fable", "max_budget_usd": 1}})
        cmd = self.argv("triage", cfg)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "fable")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "1")
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["profile"]["enum"], ["standard", "hard"])
        self.assertNotIn("enum", agent.SCHEMAS["triage"]["properties"]["profile"])


class InterpretTests(unittest.TestCase):
    def test_outcomes(self):
        ok = {"subtype": "success", "structured_output": DONE, "total_cost_usd": 1.5}
        self.assertEqual(interpret_claude("implement", ok)["status"], "done")
        blocked = {**ok, "structured_output": {**DONE, "status": "blocked", "blocker": "No spec"}}
        result = interpret_claude("implement", blocked)
        self.assertEqual((result["status"], result["error"]), ("blocked", "No spec"))
        timeout = interpret_claude("plan", ok, minutes=7, timed_out=True)
        self.assertEqual(timeout["error"], "Claude did not finish within 7 minutes.")
        missing = interpret_claude("plan", None, stderr="auth failed", returncode=1)
        self.assertEqual(
            missing["error"], "Claude exited with code 1 without a result. auth failed"
        )
        budget = interpret_claude("plan", {"subtype": "error_max_budget_usd", "is_error": True})
        self.assertIn("Claude stopped (error_max_budget_usd).", budget["error"])
        unstructured = interpret_claude("review", {"subtype": "success"})
        self.assertIn("structured result", unstructured["error"])

    def test_the_result_must_fit_the_schema(self):
        wrong = {**DONE, "checks_run": "make test"}
        del wrong["testing"]
        event = {"subtype": "success", "structured_output": wrong}
        result = interpret_claude("implement", event, schema=agent.SCHEMAS["implement"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "Claude returned a result that does not fit the implement schema: "
            "result.testing is missing; result.checks_run must be an array",
        )
        self.assertIsNone(result["output"])


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

    def run_stage(self, stage, cfg=None, context=CONTEXT, **kw):
        return agent.run_stage(
            stage,
            context,
            cfg or make_config(),
            self.repo,
            self.out,
            program=str(self.claude.path),
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
        self.assertEqual(result["tokens"], {"input": 1000, "cached": 850, "output": 120})
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
                program=str(self.claude.path),
            )

    def test_triage_chooses_the_profile_from_the_issue_text(self):
        result = self.triage(result={"profile": "hard", "reason": "Touches the login code."})
        self.assertEqual(
            {k: result[k] for k in ("status", "profile", "chosen_by", "reason", "tokens")},
            {
                "status": "done",
                "profile": "hard",
                "chosen_by": "triage",
                "reason": "Touches the login code.",
                "tokens": {"input": 1000, "cached": 850, "output": 120},
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

    def test_plan_keeps_the_triage_result(self):
        triage = self.triage(result={"profile": "hard", "reason": "Auth."})
        cfg = agent.configuration.with_profile(profile_config(), "hard")
        self.claude.configure(result=PLAN)
        result, _ = self.run_quietly("plan", cfg, triage=triage)
        self.assertEqual(result["profile"], "hard")
        self.assertEqual(result["triage"]["reason"], "Auth.")
        # Triage has its own run summary; the plan counts only its own tokens.
        self.assertEqual(result["tokens"], {"input": 1000, "cached": 850, "output": 120})
        args = self.claude.call()["args"]
        self.assertEqual(args[args.index("--model") + 1], "fable")
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["triage"]["chosen_by"], "triage")

    def test_the_patch_size_limit_is_configured(self):
        self.claude.configure(result=DONE, write={"calc.py": "x"})
        with mock.patch.object(agent, "collect_changes", return_value=(["calc.py"], 1_200_000)):
            result = self.run_stage("implement", make_config(limits={"max_patch_mb": 1}))
        self.assertEqual(result["status"], "error")
        self.assertIn("too large", result["error"])

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
        self.claude.configure(result=VERDICT)
        checks = [{"name": "test", "run": "true", "exit_code": 0, "passed": True, "output": ""}]
        result = self.run_stage("review", checks=checks, base="main")
        self.assertEqual(result["status"], "done")
        prompt = self.claude.call()["prompt"]
        self.assertIn("+    return a + b", prompt)
        self.assertIn("`test` (`true`): passed", prompt)

    def commit_legacy_subtitles(self):
        """A commit on the NexKit branch that adds a cp1250 file, as git diffs it as text."""
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5")
        (self.repo / "legacy.srt").write_bytes("1\nPříliš žluťoučký kůň\n".encode("cp1250"))
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "subtitles")

    def test_review_diff_survives_files_that_are_not_utf8(self):
        self.commit_legacy_subtitles()
        diff = agent.review_diff(self.repo, "main")
        self.assertIn("+++ b/legacy.srt", diff)
        self.assertIn("+P\ufffd\ufffdli\ufffd \ufffdlu\ufffdou\ufffdk\ufffd k\ufffd\ufffd", diff)

    def test_git_text_output_replaces_bytes_that_are_not_utf8(self):
        self.commit_legacy_subtitles()
        self.assertEqual(
            agent.git(self.repo, "show", "HEAD:legacy.srt"),
            "1\nP\ufffd\ufffdli\ufffd \ufffdlu\ufffdou\ufffdk\ufffd k\ufffd\ufffd\n",
        )
        raw = agent.git(self.repo, "show", "HEAD:legacy.srt", binary=True)
        self.assertEqual(raw.decode("cp1250"), "1\nPříliš žluťoučký kůň\n")

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
        self.claude.configure(result=VERDICT)
        agent.run_stage(
            "review",
            context,
            make_config(),
            self.repo,
            self.out,
            program=str(self.claude.path),
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
        self.assertEqual(
            result["error"],
            "No secret for the `claude` agent: set the CLAUDE_CODE_OAUTH_TOKEN repository secret "
            "and pass it in the `secrets:` of the NexKit workflow.",
        )
        self.assertFalse(self.claude.log.exists())
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            triage = agent.run_triage(CONTEXT, profile_config(), None, self.out)
        self.assertEqual((triage["status"], triage["chosen_by"]), ("error", "default"))
        self.assertIn("No secret for the `claude` agent", triage["error"])

    def test_timeout_kills_claude(self):
        self.claude.configure(result=DONE, sleep=30)
        run = agent.run_process(
            "x",
            [str(self.claude.path)],
            env=dict(os.environ),
            cwd=self.repo,
            timeout_seconds=1,
            log=runlog.RunLog(Redactor(), reader=ClaudeReader(), transcript_dir=self.tmp.name),
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
        commits = {"NEXKIT_BASE_SHA": fork, "NEXKIT_VERIFIED_SHA": fork}
        with mock.patch.dict(os.environ, {"NEXKIT_BASE_SHA": "stale"}):
            _, log = self.run_quietly("fix", base="main", commits=commits)
        # Once the base branch is merged in, the merged commit is the merge base.
        self.assertEqual(self.claude.call()["base_sha"], base)
        self.assertIn(f"NEXKIT_BASE_SHA={base}", log)
        # The verified commit stays: the diff from it includes the merged base changes.
        self.assertEqual(self.claude.call()["verified_sha"], fork)

    def test_branch_that_merges_cleanly_is_not_merged(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"other.py": "x = 1\n"}, "Other")
        fork = git(self.repo, "rev-parse", "HEAD")
        self.checkout_branch()
        self.claude.configure(result=FIX_DONE, write={"calc.py": CALC + SUB + "# fixed\n"})
        with mock.patch.dict(os.environ, {"NEXKIT_BASE_SHA": "stale"}):
            result = self.run_stage("fix", base="main", commits={"NEXKIT_BASE_SHA": fork})
        self.assertEqual(self.claude.call()["base_sha"], fork)
        self.assertEqual(result["status"], "done")
        self.assertIsNone(result["start_base"])
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertNotIn("## Conflicts with", self.claude.call()["prompt"])
        self.assertIn("\n</untrusted>\n\n## Approved plan", self.claude.call()["prompt"])

    def behind_base(self):
        """A branch that merges cleanly into a base branch that has moved since."""
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"other.py": "x = 1\n"}, "Fix a flaky test")
        self.checkout_branch()
        return git(self.repo, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "origin/main")

    def run_after_failed_checks(self, **spec):
        self.claude.configure(**{"result": FIX_DONE, **spec})
        context = {**CONTEXT, "merge_moved_base": True}
        return self.run_stage("fix", context=context, base="main")

    def test_a_round_after_failed_checks_merges_a_moved_base_branch(self):
        head, base = self.behind_base()
        result = self.run_after_failed_checks()
        self.assertEqual(result["status"], "done")
        self.assertEqual((result["start_head"], result["start_base"]), (head, base))
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["changed_files"], ["other.py"])  # the merge alone
        prompt = self.claude.call()["prompt"]
        section = prompt.split("## Merge of `main`", 1)[1].split("## Approved plan")[0]
        self.assertIn(f"merged `main` (commit {base[:7]})", section)
        self.assertIn("fix only what still fails", section)
        self.assertNotIn("## Conflicts with", prompt)
        self.assertIn("+x = 1", (self.out / "changes.patch").read_text())

    def test_a_round_after_failed_checks_on_a_current_branch_merges_nothing(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.checkout_branch()
        result = self.run_after_failed_checks()
        self.assertIsNone(result["start_base"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "The agent finished without changing any files.")
        self.assertNotIn("## Merge of", self.claude.call()["prompt"])

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
            "options": ["Keep both"],
            "recommended": 0,
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
        self.assertEqual(
            item["required"],
            ["file", "base_change", "pr_change", "question", "options", "recommended"],
        )
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
        self.claude.configure(result=VERDICT)
        agent.run_stage(
            "review",
            context,
            make_config(),
            self.repo,
            self.out,
            program=str(self.claude.path),
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


REPEATED = [{"name": "e2e", "signature": "✘ [chromium] › desk.spec.ts:: › picks an option"}]


class DiagnosisTests(StageCase):
    """An automatic fix round after a check failed the same way twice looks for the cause
    first, and may change code only once it names one."""

    def run_fix(self, context=None, **spec):
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5")
        self.claude.configure(**{"result": FIX_DONE, **spec})
        context = {**CONTEXT, "diagnose": REPEATED, **(context or {})}
        return self.run_stage("fix", context=context, base="main")

    def test_the_prompt_asks_for_the_cause_before_a_change(self):
        cause = "pickOption clicks before the popup renders: trace.zip shows the click at 0 ms."
        result = self.run_fix(result={**FIX_DONE, "cause": cause}, write={"calc.py": "# fixed\n"})
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["output"]["cause"], cause)
        prompt = self.claude.call()["prompt"]
        section = prompt.split("## Diagnosis round", 1)[1].split("## Note from", 1)[0]
        self.assertIn("- `e2e`: `✘ [chromium] › desk.spec.ts:: › picks an option`", section)
        self.assertIn("finds the cause before it changes any code", section)
        self.assertIn("Write the cause in `cause`", section)
        self.assertIn("leave `cause` empty and return\n   `status: blocked`", section)

    def test_other_rounds_have_no_diagnosis_section(self):
        result = self.run_fix({"diagnose": []}, write={"calc.py": "# fixed\n"})
        self.assertEqual(result["status"], "done")  # without a cause
        prompt = self.claude.call()["prompt"]
        self.assertNotIn("## Diagnosis round", prompt)
        self.assertNotIn("$diagnosis", prompt)
        self.assertIn("`cause`: the cause of the failures you addressed", prompt)
        self.assertIn("None. This is the first fix round of this pull request.", prompt)

    def test_a_change_without_a_cause_is_rejected(self):
        result = self.run_fix(write={"calc.py": "# another try\n"})
        self.assertEqual(result["status"], "error")
        self.assertIn("changed code without naming the cause", result["error"])
        self.assertIn("`calc.py`", result["error"])

    def test_no_cause_and_no_change_ends_blocked_with_a_question(self):
        shown = "The trace shows the option list opens, then closes before the click."
        result = self.run_fix(result={**FIX_DONE, "summary": shown})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("found no cause for the repeated failure of `e2e`", result["error"])
        self.assertIn(f"What the evidence shows: {shown}", result["error"])
        question = result["output"]["questions"][0]
        self.assertIn("`e2e` failed the same way after two fix rounds", question["question"])
        self.assertEqual(question["recommended"], 0)
        self.assertFalse((self.out / "changes.patch").read_text())

    def test_a_blocked_result_keeps_the_agents_own_questions(self):
        asked = {"question": "Is the popup meant to close?", "options": ["Yes", "No"]}
        blocked = {
            **FIX_DONE,
            "status": "blocked",
            "blocker": "The trace ends before the click.",
            "questions": [{**asked, "recommended": 0}],
        }
        result = self.run_fix(result=blocked)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "The trace ends before the click.")
        self.assertEqual(result["output"]["questions"][0]["question"], asked["question"])

    def test_changes_that_a_merge_brought_are_not_the_agents(self):
        self.repos.push_commit("dev", "main", {"other.py": "x = 1\n"}, "Fix a flaky test")
        git(self.repo, "fetch", "-q", "origin")
        self.claude.configure(result=FIX_DONE)
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5", "HEAD")
        context = {**CONTEXT, "diagnose": REPEATED, "merge_moved_base": True}
        result = self.run_stage("fix", context=context, base="main")
        # Only the merge changed files, so the round is not rejected: the merge is
        # published alone, and the next checks show whether the base branch fixed it.
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["changed_files"], ["other.py"])


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

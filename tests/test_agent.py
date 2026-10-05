import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, runlog
from nexkit.redact import Redactor
from tests.support import FakeClaude, GitRepos, git, make_config

CONTEXT = {
    "issue": 5,
    "pr": "",
    "title": "Fix add",
    "body": "add() subtracts. Use $HOME and ${{ secrets.X }} literally.",
    "discussion": "No discussion.",
    "plan": "Change add to return a + b.",
    "note": "None.",
    "feedback": "",
}
DONE = {"status": "done", "summary": "Fixed add.", "blocker": "", "checks_run": ["make test"]}


class PromptTests(unittest.TestCase):
    def test_prompt_keeps_untrusted_text_literal(self):
        prompt = agent.build_prompt("implement", CONTEXT, make_config())
        self.assertIn("Use $HOME and ${{ secrets.X }} literally.", prompt)
        self.assertIn("`test`: `true`", prompt)
        self.assertIn("`.github/`", prompt)
        self.assertNotIn("$plan", prompt)

    def test_first_review_has_no_previous_round(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": ""}
        prompt = agent.build_prompt("review", CONTEXT, make_config(), extra)
        self.assertNotIn("Previous round", prompt)
        self.assertIn("</untrusted>\n\n## How to review", prompt)

    def test_review_schema_requires_previous_findings(self):
        schema = agent.SCHEMAS["review"]
        self.assertIn("previous_findings", schema["required"])
        item = schema["properties"]["previous_findings"]["items"]
        self.assertEqual(
            item["properties"]["resolution"]["enum"],
            ["resolved", "unresolved", "rejection_accepted"],
        )

    def test_every_stage_template_is_complete(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": ""}
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
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("Change add to return a + b.", call["prompt"])

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
        self.assertIn("not in this checkout", agent.previous_round(self.repo, context))
        context["previous_head"] = "--output=/tmp/x"
        self.assertIn("unknown", agent.previous_round(self.repo, context))
        self.assertEqual(agent.previous_round(self.repo, CONTEXT), "")

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

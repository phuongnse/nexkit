"""Agents that fall back to other agents at a usage limit, and how runs show who ran."""

import os
import unittest
from unittest import mock

from nexkit import config, report, runlog, state
from tests.support import AZURE, CALC, MUL, SUB, git, make_config, with_claude
from tests.test_agent import DONE, FIX_DONE, StageCase
from tests.test_report import ReportCase, review_result

LIMIT = "You've hit your session limit · resets 4:40am (UTC)"
# A reset in 2030, later than any 4:40 UTC from now.
LATER = "Claude AI usage limit reached|1893456000"
AGENTS = {
    "claude": {
        "runtime": "claude-code",
        "provider": "anthropic",
        "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN",
        "fallback": ["spare", "api"],
    },
    "spare": {
        "runtime": "claude-code",
        "provider": "anthropic",
        "oauth_token_secret": "CLAUDE_SPARE_TOKEN",
    },
    "api": {"runtime": "claude-code", "provider": "anthropic", "api_key_secret": "CLAUDE_API_KEY"},
}
SECRETS = {"CLAUDE_SPARE_TOKEN": "spare-token", "CLAUDE_API_KEY": "api-key"}


def limited(text=LIMIT, **spec):
    """A Claude Code run that stops at a usage limit."""
    return {"result": None, "is_error": True, "text": text, "exit": 1, **spec}


def fallback_config(**overrides):
    return make_config(agents=AGENTS, **overrides)


class ConfigTests(unittest.TestCase):
    def check(self, agents, message):
        with self.assertRaises(config.ConfigError) as caught:
            config.validate(with_claude({"agents": agents}))
        self.assertIn(message, str(caught.exception))

    def test_fallbacks_must_be_other_agents_of_the_same_runtime(self):
        claude = AGENTS["claude"]
        cases = [
            ([], "must be a non-empty list of agent names"),
            ("spare", "must be a non-empty list of agent names"),
            (["claude"], "cannot name the agent itself"),
            (["nobody"], "'nobody' is not an agent"),
            (["spare", "spare"], "names an agent twice"),
            (["azure"], "'azure' must use the claude-code runtime too"),
        ]
        for fallback, message in cases:
            with self.subTest(fallback=fallback):
                agents = {**AGENTS, "azure": AZURE, "claude": {**claude, "fallback": fallback}}
                self.check(agents, message)

    def test_a_runtime_without_usage_limits_has_no_fallback(self):
        agents = {"azure": {**AZURE, "fallback": ["other"]}, "other": AZURE}
        self.check(agents, "Codex does not report usage limits, so a fallback would never run")

    def test_fallback_secrets_count_for_the_jobs_that_may_run_them(self):
        cfg = fallback_config()
        self.assertEqual(config.chain(cfg, "claude"), ["claude", "spare", "api"])
        # A fallback's own fallbacks are not followed.
        self.assertEqual(config.chain(cfg, "spare"), ["spare"])
        self.assertEqual(
            config.secret_names([cfg], ["implement"]),
            ["CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_SPARE_TOKEN"],
        )
        self.assertEqual(config.agents_used(cfg), ["claude", "spare", "api"])
        self.assertEqual([name for name, _ in config.credentials(cfg)], ["claude", "spare", "api"])

    def test_a_stage_may_take_its_timeout_for_each_agent(self):
        cfg = fallback_config(stages={"implement": {"timeout_minutes": 45}})
        self.assertEqual(config.stage_minutes(cfg, "implement"), 135)
        cfg = fallback_config(stages={"implement": {"timeout_minutes": 200}})
        self.assertEqual(config.stage_minutes(cfg, "implement"), config.MAX_STAGE_MINUTES)
        self.assertEqual(config.stage_minutes(make_config(), "implement"), 45)


class FallbackStageTests(StageCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, SECRETS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, stage="implement", **by_token):
        self.claude.configure(by_token=by_token)
        return self.run_quietly(stage, fallback_config(), base="main")

    def test_a_limit_moves_the_stage_to_the_next_agent(self):
        done = {"result": DONE, "write": {"calc.py": "def add(a, b):\n    return a + b\n"}}
        result, log = self.run_with(
            **{"test-token": limited(write={"half.py": "x = 1\n"}), "spare-token": done}
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["agent"], "spare")
        # The first agent's unfinished change is gone before the next one starts.
        self.assertEqual(result["changed_files"], ["calc.py"])
        first, second = self.claude.calls()
        self.assertEqual((first["token"], second["token"]), ("test-token", "spare-token"))
        self.assertNotIn("half.py", second["files"])
        self.assertIn("! The `claude` agent stopped at its usage limit; running the implement", log)
        self.assertEqual(
            [(a["agent"], a["status"]) for a in result["attempts"]],
            [("claude", "paused"), ("spare", "done")],
        )
        self.assertEqual(result["attempts"][0]["limit"], LIMIT)
        # The stage counts the tokens of every agent that ran.
        self.assertEqual(result["tokens"], {"input": 2000, "cached": 1700, "output": 240})
        self.assertTrue((self.out / "transcript.md").exists())
        self.assertTrue((self.out / "agent-spare" / "transcript.md").exists())

    def test_every_run_starts_on_the_agent_itself(self):
        result, _ = self.run_with(**{"test-token": {"result": DONE, "write": {"a.py": "1\n"}}})
        self.assertEqual(result["agent"], "claude")
        self.assertEqual([call["token"] for call in self.claude.calls()], ["test-token"])
        self.assertEqual(len(result["attempts"]), 1)

    def test_errors_and_blocked_results_stay_on_the_agent(self):
        blocked = {"result": {**DONE, "status": "blocked", "blocker": "No spec"}}
        failed = {"result": None, "is_error": True, "text": "API Error: 500", "exit": 1}
        for spec in (blocked, failed):
            with self.subTest(spec=spec["result"] and "blocked"):
                self.claude.log.with_name(self.claude.log.name + "l").unlink(missing_ok=True)
                result, _ = self.run_with(**{"test-token": spec, "spare-token": DONE})
                self.assertIn(result["status"], ("blocked", "error"))
                self.assertEqual(len(self.claude.calls()), 1)

    def test_every_agent_at_its_limit_pauses_until_the_earliest_reset(self):
        result, _ = self.run_with(
            stage="plan",
            **{
                "test-token": limited(LATER),
                "spare-token": limited(),
                "api-key": limited(LATER),
            },
        )
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["agent"], "api")
        self.assertTrue(result["reset_known"])
        self.assertTrue(result["resume_at"].endswith("T04:40:00Z"))
        self.assertTrue(result["error"].startswith("Every agent stopped at its usage limit: "))
        self.assertIn(f"`spare`: {LIMIT}", result["limit"])
        message, resume = report.pause(result, make_config(), "plan", 5)
        self.assertTrue(message.startswith("⏸️ Every agent stopped at its usage limit: `claude`"))
        self.assertEqual(resume["at"], result["resume_at"])

    def test_a_fallback_without_its_secret_is_skipped(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SPARE_TOKEN": ""}):
            result, _ = self.run_with(**{"test-token": limited(), "api-key": {"result": DONE}})
        self.assertEqual(
            [(a["agent"], a["status"]) for a in result["attempts"]],
            [("claude", "paused"), ("spare", "skipped"), ("api", "done")],
        )
        self.assertIn("No secret for the `spare` agent", result["attempts"][1]["error"])
        self.assertEqual(result["agent"], "api")

    def test_a_fix_round_starts_again_from_the_merge_of_the_base_branch(self):
        self.repos.diverge()
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5")
        fixed = {"result": FIX_DONE, "write": {"calc.py": CALC + SUB + MUL}}
        result, _ = self.run_with(
            stage="fix",
            **{"test-token": limited(write={"calc.py": "broken\n"}), "spare-token": fixed},
        )
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["conflicts"], ["calc.py"])
        self.assertTrue(self.claude.calls()[1]["merging"])
        self.assertIn("+def mul(a, b):", (self.out / "changes.patch").read_text())


class ShowTests(unittest.TestCase):
    ATTEMPTS = [
        {
            "agent": "claude",
            "runtime": "claude-code",
            "provider": "anthropic",
            "model": "opus",
            "status": "paused",
            "seconds": 3,
            "tokens": {"input": 900, "cached": 0, "output": 15},
            "limit": LIMIT,
        },
        {
            "agent": "spare",
            "runtime": "claude-code",
            "provider": "anthropic",
            "model": "opus",
            "status": "skipped",
            "error": "No secret for the `spare` agent.",
        },
        {
            "agent": "api",
            "runtime": "claude-code",
            "provider": "anthropic",
            "model": "opus",
            "status": "done",
            "seconds": 302,
            "tokens": {"input": 1_240_000, "cached": 1_100_000, "output": 15_200},
        },
    ]

    def test_the_run_summary_names_the_agent_and_each_run(self):
        result = {"stage": "implement", "status": "done", "agent": "api", "attempts": self.ATTEMPTS}
        text = runlog.summary(result, [])
        self.assertIn("**Agent:** implement `api` (`opus`), claude-code on anthropic", text)
        self.assertIn("| `claude` | `opus` | usage limit | 3s | 900 in, 15 out |", text)
        skipped = "skipped: No secret for the `spare` agent."
        self.assertIn(f"| `spare` | `opus` | {skipped} | N/A | N/A |", text)
        self.assertIn(
            "| `api` | `opus` | done | 5m 02s | 1.24M in (1.1M cached), 15.2k out |", text
        )
        one = {**result, "attempts": self.ATTEMPTS[2:]}
        self.assertNotIn("Agents that ran in turn", runlog.summary(one, []))

    def test_the_run_comment_names_the_agent_of_each_stage(self):
        triage = {"stage": "triage", "status": "done", "attempts": self.ATTEMPTS[2:]}
        plan = {
            "stage": "plan",
            "status": "done",
            "attempts": self.ATTEMPTS,
            "triage": triage,
        }
        review = {"stage": "review", "status": "paused", "attempts": self.ATTEMPTS[:1]}
        entries = report.agents_ran(plan, None, review)
        self.assertEqual(
            entries,
            [
                {"stage": "triage", "agents": ["api"], "models": ["opus"], "paused": False},
                {
                    "stage": "plan",
                    "agents": ["claude", "api"],
                    "models": ["opus", "opus"],
                    "paused": False,
                },
                {"stage": "review", "agents": ["claude"], "models": ["opus"], "paused": True},
            ],
        )
        run = {"url": "u", "command": "plan", "status": "success", "agents": entries}
        body = state.render_run(run)
        self.assertIn(
            "**Agents:** triage `api` (`opus`) · plan `api` (`opus`) after `claude` (`opus`) "
            "hit its usage limit · review: `claude` (`opus`) hit its usage limit",
            body,
        )
        self.assertEqual(state.agents_text([]), "")
        both = [
            {"stage": "fix", "agents": ["claude", "spare"], "models": ["a", "b"], "paused": True}
        ]
        self.assertEqual(
            state.agents_text(both),
            "**Agents:** fix: `claude` (`a`) and `spare` (`b`) hit their usage limits",
        )

    def test_runs_recorded_without_models_show_names(self):
        old = [
            {"stage": "triage", "agents": ["claude"], "paused": False},
            {"stage": "plan", "agents": ["claude", "api"], "paused": False},
        ]
        self.assertEqual(
            state.agents_text(old),
            "**Agents:** triage `claude` · plan `api` after `claude` hit its usage limit",
        )

    def test_every_stage_names_its_agent_and_model(self):
        entries = [
            {"stage": "plan", "agents": ["claude"], "models": ["claude-opus-5-5"]},
            {"stage": "implement", "agents": ["sol"], "models": ["gpt-6.1-sol"]},
            {"stage": "review", "agents": ["claude"], "models": ["claude-fable-5-1"]},
        ]
        self.assertEqual(
            state.agents_text(entries),
            "**Agents:** plan `claude` (`claude-opus-5-5`) · implement `sol` (`gpt-6.1-sol`) · "
            "review `claude` (`claude-fable-5-1`)",
        )


class ReportTests(ReportCase):
    def test_the_round_and_the_issue_name_the_agents(self):
        implement = {
            "stage": "implement",
            "status": "done",
            "agent": "api",
            "attempts": ShowTests.ATTEMPTS,
        }
        self.candidate()
        self.write("nexkit-agent", "result.json", implement)
        review = {**review_result(), "stage": "review", "attempts": ShowTests.ATTEMPTS[:1]}
        review["attempts"] = [{**review["attempts"][0], "status": "done"}]
        self.write("nexkit-review", "result.json", review)
        self.run_report()
        line = (
            "**Agents:** implement `api` (`opus`) after `claude` (`opus`) hit its usage limit · "
            "review `claude` (`opus`)"
        )
        self.assertTrue(self.gh.comments_matching(6, line))
        self.assertTrue(self.gh.comments_matching(5, line))


if __name__ == "__main__":
    unittest.main()

"""The runner that takes a `runAgent` action and saves the protocol's result as `run.json`."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, cli, protocol
from nexkit.config import validate
from tests.support import (
    CALC,
    CLAUDE,
    MUL,
    SUB,
    FakeClaude,
    GitRepos,
    git,
    interpret_claude,
    make_config,
    profile_config,
)
from tests.test_agent import CONTEXT, DONE, FIX_DONE, PLAN, VERDICT, StageCase
from tests.test_fallback import LATER, limited

RESULT = "actions/runAgent.result.schema.json"
OUTPUTS = {
    "plan": PLAN,
    "implement": DONE,
    "fix": FIX_DONE,
    "review": VERDICT,
    "triage": {"profile": "hard", "reason": "Touches the login code."},
}


class RunActionTests(StageCase):
    def run_action(self, stage, cfg=None, **kw):
        cfg = cfg or make_config()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            return agent.run_action(
                agent.action_for(stage, cfg),
                CONTEXT,
                cfg,
                self.repo,
                self.out,
                program=str(self.claude.path),
                **kw,
            )

    def saved(self, path):
        return json.loads((self.out / path).read_text())

    def test_each_stage_saves_a_result_that_fits_the_protocol(self):
        for stage, output in OUTPUTS.items():
            with self.subTest(stage=stage):
                spec = {"result": output}
                if stage == "implement":
                    spec["write"] = {"calc.py": CALC + SUB}
                if stage == "fix":
                    spec["write"] = {"calc.py": CALC + SUB + MUL}
                    self.repos.diverge()
                    git(self.repo, "fetch", "-q", "origin")
                    git(
                        self.repo, "checkout", "-q", "-B", "nexkit/issue-5", "origin/nexkit/issue-5"
                    )
                self.claude.configure(**spec)
                cfg = profile_config() if stage == "triage" else make_config()
                kw = {"base": "main"} if stage in ("fix", "review") else {}
                result, ran = self.run_action(stage, cfg, **kw)
                path = "triage/run.json" if stage == "triage" else "run.json"
                self.assertEqual(self.saved(path), ran)
                self.assertEqual(protocol.check(ran, RESULT), [])
                self.assertIsNotNone(ran["output"])
                self.assertEqual(protocol.check(ran["output"], f"results/{stage}.schema.json"), [])
                self.assertEqual(ran["tokens"], result["tokens"])
                self.assertIsNone(ran["error"])
                self.assertEqual((ran["session"], ran["changeVersion"]), (None, None))
                self.assertNotIn("usd", ran)

    def test_triage_has_an_output_only_when_it_chose_the_profile(self):
        self.claude.configure(result={"profile": "expert", "reason": "x"})
        result, ran = self.run_action("triage", profile_config())
        self.assertEqual(result["chosen_by"], "default")
        self.assertIsNone(ran["output"])
        self.assertEqual(ran["error"], result["error"])
        self.assertEqual(protocol.check(ran, RESULT), [])

    def test_the_action_comes_from_the_configuration(self):
        cfg = make_config(stages={"plan": {"model": "opus", "timeout_minutes": 7}})
        action = agent.action_for("plan", cfg, "abc")
        self.assertEqual(protocol.check(action, "actions/runAgent.schema.json"), [])
        self.assertEqual((action["model"], action["changeVersion"]), ("opus", "abc"))
        self.assertEqual(action["budget"]["seconds"], 7 * 60)
        self.assertEqual(action["access"], "read")

    def test_a_paused_run_has_a_limit_with_its_reset(self):
        self.claude.configure(**limited(LATER))
        result, ran = self.run_action("plan")
        self.assertEqual(result["status"], "paused")
        self.assertEqual(ran["limit"], {"message": result["limit"], "reset": result["resume_at"]})
        self.assertEqual(ran["limit"]["reset"], "2030-01-01T00:00:00Z")
        self.assertEqual(protocol.check(ran, RESULT), [])

    def test_a_limit_without_a_reset_has_none(self):
        found = {"status": "paused", "limit": "Limit", "resume_at": "2030-01-01T00:00:00Z"}
        ran = agent.finished({**found, "reset_known": False}, "plan")
        self.assertEqual(ran["limit"], {"message": "Limit", "reset": None})
        self.assertEqual(protocol.check(ran, RESULT), [])

    def test_a_blocked_run_has_no_error(self):
        self.claude.configure(result={**DONE, "status": "blocked", "blocker": "x"})
        result, ran = self.run_action("implement", base="main")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(ran["error"])
        self.assertIsNone(ran["limit"])
        self.assertEqual(protocol.check(ran, RESULT), [])

    def test_a_stop_is_told_apart(self):
        ok = {"subtype": "success", "structured_output": DONE}
        timed = interpret_claude("plan", ok, minutes=7, timed_out=True)
        ran = agent.finished(timed, "plan")
        self.assertEqual((ran["timedOut"], ran["noResult"], ran["transient"]), (True, False, False))
        self.assertEqual(ran["error"], "Claude did not finish within 7 minutes.")
        unfit = interpret_claude("plan", {"subtype": "success"})
        ran = agent.finished(unfit, "plan")
        self.assertEqual((ran["timedOut"], ran["noResult"], ran["transient"]), (False, True, False))
        self.assertTrue(ran["error"])
        transient = interpret_claude("plan", None, stderr="Connection reset", returncode=1)
        ran = agent.finished({**transient, "transient": True}, "plan")
        self.assertEqual((ran["timedOut"], ran["transient"]), (False, True))
        self.assertEqual(ran["seconds"], 1)
        for found in (timed, unfit, transient):
            self.assertEqual(protocol.check(agent.finished(found, "plan"), RESULT), [])

    def test_an_action_that_does_not_fit_is_refused_before_the_agent_runs(self):
        action = agent.action_for("plan", make_config())
        del action["kind"]
        with self.assertRaisesRegex(ValueError, "kind"):
            agent.run_action(action, CONTEXT, make_config(), self.repo, self.out)
        self.assertFalse(self.claude.log.exists())

    def test_an_action_for_another_agent_is_refused_before_the_agent_runs(self):
        cfg = make_config()
        for key, value in (
            ("agent", "other"),
            ("model", "other"),
            ("effort", "other"),
            ("access", "write"),
        ):
            with self.subTest(key=key):
                action = {**agent.action_for("plan", cfg), key: value}
                with self.assertRaisesRegex(ValueError, key):
                    agent.run_action(action, CONTEXT, cfg, self.repo, self.out)
                self.assertFalse(self.claude.log.exists())
        self.assertFalse((self.out / "run.json").exists())


class AgentCommandTests(unittest.TestCase):
    def setUp(self):
        self.repos = GitRepos()
        self.addCleanup(self.repos.cleanup)
        self.repo = self.repos.clone("work")
        self.root = Path(self.repos.root)
        self.claude = FakeClaude(str(self.root))
        self.addCleanup(self.claude.restore)

    def run_agent(self, stage, result):
        out = self.root / f"out-{stage}"
        out.mkdir()
        (out / "context.json").write_text(json.dumps(CONTEXT))
        self.claude.configure(result=result)
        env = {
            "NEXKIT_DECISION": json.dumps({"action": stage, "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(validate({"agents": CLAUDE})),
            "PATH": f"{self.claude.dir}{os.pathsep}{os.environ['PATH']}",
        }
        ran = []

        def wrapped(*args, **kw):
            ran.append(real(*args, **kw))
            return ran[-1]

        real = agent.run_stage
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("nexkit.agent.run_stage", side_effect=wrapped),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = cli.main(["agent", "--repo", str(self.repo), "--out", str(out)])
        self.assertEqual(code, 0)
        return out, ran[0]

    def test_an_agent_stage_saves_its_run_beside_the_result(self):
        out, result = self.run_agent("plan", PLAN)
        self.assertEqual(json.loads((out / "result.json").read_text()), result)
        run = json.loads((out / "run.json").read_text())
        self.assertEqual(run, agent.finished(result, "plan"))
        self.assertEqual(protocol.check(run, RESULT), [])
        self.assertEqual(run["output"], result["output"])

    def test_a_review_saves_its_run_beside_the_result(self):
        out, result = self.run_agent("review", VERDICT)
        self.assertEqual(json.loads((out / "result.json").read_text()), result)
        run = json.loads((out / "run.json").read_text())
        self.assertEqual(protocol.check(run, RESULT), [])
        self.assertEqual(protocol.check(run["output"], "results/review.schema.json"), [])
        self.assertEqual(run["tokens"], result["tokens"])

    def test_a_run_that_stops_before_an_agent_saves_no_run(self):
        out = self.root / "out"
        out.mkdir()
        (out / "context.json").write_text(json.dumps({**CONTEXT, "untrusted": "@bob edited it"}))
        env = {
            "NEXKIT_DECISION": json.dumps({"action": "plan", "base": "main"}),
            "NEXKIT_CONFIG": json.dumps(validate({"agents": CLAUDE})),
        }
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "--repo", str(self.repo), "--out", str(out)])
        self.assertTrue((out / "result.json").exists())
        self.assertFalse((out / "run.json").exists())


if __name__ == "__main__":
    unittest.main()

"""Who is mentioned when a run needs a person (`notify`)."""

import unittest

from nexkit.config import ConfigError, validate
from nexkit.report import needs_person, report
from tests.support import make_config
from tests.test_report import HEAD, LIMIT, ReportCase, decision, review_result


class RuleTests(unittest.TestCase):
    """One case per row of the table in docs/how-it-works.md."""

    def setUp(self):
        self.cfg = make_config()

    def test_planned_with_questions_or_too_large(self):
        self.assertEqual(
            needs_person("planned", self.cfg, plan={"questions": ["Which?"]}),
            "the plan has questions",
        )
        self.assertIn("too large", needs_person("planned", self.cfg, plan={"too_large": True}))

    def test_planned_without_either(self):
        self.assertIsNone(needs_person("planned", self.cfg, plan={"questions": []}))

    def test_agent_blocked(self):
        self.assertIn("blocked", needs_person("agent_blocked", self.cfg))

    def test_agent_and_publish_and_review_failures(self):
        for outcome in ("agent_error", "agent_failed", "publish_failed", "review_failed"):
            self.assertTrue(needs_person(outcome, self.cfg), outcome)

    def test_needs_human(self):
        self.assertIn("no automatic fix rounds remain", needs_person("needs_human", self.cfg))

    def test_ready_only_without_auto_merge(self):
        self.assertIn("ready", needs_person("ready", self.cfg))
        self.assertIsNone(needs_person("ready", make_config(merge={"auto": True})))

    def test_merge_refused(self):
        self.assertIn("refused", needs_person("merge_refused", self.cfg))

    def test_merged_and_automatic_rounds(self):
        for outcome in ("merged", "auto_fix", "conflict_fix", "up_to_date"):
            self.assertIsNone(needs_person(outcome, self.cfg), outcome)

    def test_paused_and_resuming(self):
        self.assertIsNone(needs_person("paused", self.cfg, resume={"at": "x"}))

    def test_paused_without_resume(self):
        self.assertIn("usage limit", needs_person("paused", self.cfg))


class ConfigTests(unittest.TestCase):
    def test_notify(self):
        self.assertEqual(validate({})["notify"], [])
        self.assertEqual(validate({"notify": ["octo-cat"]})["notify"], ["octo-cat"])
        for bad in (["@owner"], "owner", ["a", "A"], [""], ["-x"]):
            with self.assertRaises(ConfigError, msg=bad):
                validate({"notify": bad})


class MentionTests(ReportCase):
    def mentions(self, number):
        return [c["body"] for c in self.gh.comments(number) if c["body"].startswith("@")]

    def test_a_ready_pull_request_mentions_the_listed_people_once(self):
        self.candidate()
        self.run_report(cfg=make_config(notify=["owner", "lead"]))
        self.assertEqual(
            self.mentions(6),
            ["@owner @lead a person is needed: the pull request is ready for a human decision."],
        )
        self.assertEqual(self.mentions(5), [])
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["attention"], "the pull request is ready for a human decision")

    def test_nobody_is_mentioned_without_notify(self):
        self.candidate()
        self.run_report()
        self.assertEqual(self.mentions(6), [])
        # The reason is still recorded for `nexkit status`.
        self.assertIn("attention", self.gh.run_comments(6)[0]["run"])

    def test_a_plan_with_questions_mentions_on_the_issue(self):
        self.write(
            "nexkit-agent", "result.json", {"status": "done", "output": {"questions": ["?"]}}
        )
        d = decision("plan")
        needs = {"publish": {"outputs": {"result": '{"published": true, "comment": "u"}'}}}
        report(
            self.gh,
            d,
            make_config(notify=["owner"]),
            needs,
            self.artifacts,
            workflow_ref="x/nexkit.yml@main",
            default_branch="main",
        )
        self.assertEqual(self.mentions(5), ["@owner a person is needed: the plan has questions."])
        [run] = self.gh.run_comments(5)
        self.assertEqual(run["run"]["attention"], "the plan has questions")

    def test_automatic_rounds_and_resumed_pauses_mention_nobody(self):
        finding = {"severity": "blocking", "file": "a", "line": 1, "body": "x"}
        self.candidate(checks_pass=False, review=review_result("request_changes", [finding]))
        self.run_report(cfg=make_config(notify=["owner"]))
        self.write("nexkit-agent", "result.json", LIMIT)
        d = decision("fix", pr=6, target=6, head=HEAD)
        self.run_report(d=d, cfg=make_config(notify=["owner"]), published=False)
        self.assertEqual(self.mentions(6), [])

    def test_a_blocked_fix_round_mentions_on_the_pull_request(self):
        self.write("nexkit-agent", "result.json", {"status": "blocked", "error": "Which way?"})
        d = decision("fix", pr=6, target=6, head=HEAD)
        self.run_report(d=d, cfg=make_config(notify=["owner"]), published=False)
        self.assertEqual(
            self.mentions(6),
            ["@owner a person is needed: the agent is blocked and needs a decision."],
        )


if __name__ == "__main__":
    unittest.main()

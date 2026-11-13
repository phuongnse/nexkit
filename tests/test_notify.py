"""Who is mentioned when a run needs a person (`notify`)."""

import json
import os
import unittest

from nexkit import report as report_module
from nexkit.autopilot import mention_text, person
from nexkit.config import ConfigError, validate
from nexkit.report import NEEDS_PERSON, READY, needs_person, report
from nexkit.status import collect, render, to_json
from tests.support import CLAUDE, make_config
from tests.test_report import HEAD, LIMIT, ReportCase, decision, review_result

WITH_CLAUDE = {"agents": CLAUDE}


class RuleTests(unittest.TestCase):
    """One case per row of the table in docs/how-it-works.md. Each reason says what a
    person does, then why NexKit does not (`autopilot.person`)."""

    def setUp(self):
        self.cfg = make_config()

    def assertReason(self, reason, do, why):
        self.assertTrue(reason.startswith(do), reason)
        self.assertIn(why, reason)

    def test_planned_with_questions_or_too_large(self):
        self.assertEqual(
            needs_person("planned", self.cfg, plan={"questions": ["Which?"]}),
            "answer the plan's questions with `/nexkit plan <answers>`. The plan asks for "
            "decisions that are the owner's to make:\n\n1. `Which?`",
        )
        self.assertReason(
            needs_person("planned", self.cfg, plan={"too_large": True}),
            "split the issue by hand",
            "The plan says the issue is too large for one session",
        )

    def test_planned_without_either(self):
        self.assertIsNone(needs_person("planned", self.cfg, plan={"questions": []}))

    def test_agent_blocked_names_the_decision_and_the_command(self):
        found = {"questions": [{"question": "Keep @alice's API?"}, {"question": "Rename it?"}]}
        self.assertEqual(
            needs_person("agent_blocked", self.cfg, found=found),
            "answer the questions in the run's comment with `/nexkit plan <answers>`, or "
            "`/nexkit go <answers>` when they leave the plan as it is. The agent stopped at a "
            "decision that is the owner's to make:\n\n1. `Keep @alice's API?`\n2. `Rename it?`",
        )
        self.assertTrue(
            needs_person("agent_blocked", self.cfg, found={"pull": True}).startswith(
                "answer the questions in the round's comment with `/nexkit fix <answers>`."
            )
        )

    def test_every_outcome_that_needs_a_person_says_what_to_do_and_why(self):
        for outcome, (do, why) in NEEDS_PERSON.items():
            with self.subTest(outcome):
                reason = needs_person(outcome, self.cfg)
                self.assertEqual(reason, person(do, why))
                text = mention_text(make_config(notify=["owner"]), reason)
                self.assertTrue(text.startswith(f"@owner a person is needed: {do}. "), text)
                self.assertTrue(text.endswith(f"{why[1:]}."), text)

    def test_errors_are_named_in_code_spans(self):
        for outcome in ("agent_error", "publish_failed", "close_failed", "retry_failed"):
            reason = needs_person(outcome, self.cfg, found={"error": "boom\n@bob <!-- x -->"})
            self.assertTrue(reason.endswith(": `boom @bob ‹!-- x --›`"), reason)

    def test_an_agent_that_ran_out_of_time(self):
        self.assertReason(
            needs_person("agent_error", self.cfg, found={"timed_out": True}),
            "comment `/nexkit plan` to split the issue",
            "The agent ran out of time before it finished",
        )

    def test_a_review_that_cannot_sign_in_says_another_try_stops_the_same_way(self):
        self.assertReason(
            needs_person("review_failed", self.cfg, found={"error": "401 Unauthorized"}),
            "fix the review agent's secret, then comment `/nexkit review`",
            "another try stops the same way: `401 Unauthorized`",
        )
        self.assertReason(
            needs_person("review_failed", self.cfg, found={"error": "no verdict"}),
            "comment `/nexkit review` to run the review again",
            "after an error of the service, the network or the runner: `no verdict`",
        )

    def test_needs_human_names_what_still_fails(self):
        finding = {"severity": "blocking", "file": "app.py", "line": 3, "body": "x"}
        found = {"failing": ["unit", "lint"], "findings": [finding, finding]}
        self.assertEqual(
            needs_person("needs_human", self.cfg, found=found),
            "comment `/nexkit fix <instructions>` for what still fails, push a commit "
            "yourself, or close the pull request. Checks `unit`, `lint` fail and the review "
            "has 2 blocking findings, the first at `app.py:3`, and no automatic fix rounds "
            "remain",
        )

    def test_ready_only_without_auto_merge(self):
        self.assertReason(
            needs_person("ready", self.cfg),
            "review the pull request, then merge it or close it",
            "with `merge.auto` off a person decides the merge",
        )
        self.assertIsNone(needs_person("ready", make_config(merge={"auto": True})))

    def test_ready_with_criteria_to_confirm(self):
        for cfg, then in (
            (self.cfg, "then merge the pull request."),
            (make_config(merge={"auto": True}), "then approve the pull request, which NexKit"),
        ):
            reason = needs_person("ready", cfg, confirm=["passes five runs", "is fast"])
            self.assertReason(
                reason,
                "confirm from the diff and the checks the acceptance criteria",
                "The review could not verify these acceptance criteria:\n\n"
                "- `passes five runs`\n- `is fast`",
            )
            self.assertIn(then, reason)

    def test_ready_with_items_to_decide_lists_each_whole_with_its_evidence(self):
        decide = ["a" * 250 + " end", "b" * 250 + " end"]
        evidence = {decide[0]: "found x " * 40, decide[1]: "found y " * 40}
        reason = needs_person(
            "ready", self.cfg, confirm=["is fast"], decide=decide, evidence=evidence
        )
        self.assertTrue(reason.startswith("decide the checklist items in the round's comment"))
        self.assertTrue(
            reason.endswith(
                "Only a person decides these checklist items, which the review found not met:"
                f"\n\n- `{decide[0]}`: `{evidence[decide[0]].strip()}`"
                f"\n- `{decide[1]}`: `{evidence[decide[1]].strip()}`"
                "\n\nThe review could not verify these acceptance criteria:\n\n- `is fast`"
            ),
            reason,
        )
        self.assertNotIn("more", reason)
        self.assertNotIn("…", reason)

    def test_questions_are_listed_whole_with_their_options(self):
        asked = [
            {
                "question": "Which store? " * 20,
                "options": ["Postgres", "SQLite"],
                "recommended": "Postgres",
            },
            {"question": "Close it?", "options": ["Keep it", "Close"], "closing": "Close"},
        ]
        reason = needs_person("agent_blocked", self.cfg, found={"questions": asked})
        self.assertTrue(
            reason.endswith(
                "The agent stopped at a decision that is the owner's to make:\n\n"
                f"1. `{asked[0]['question'].strip()}`\n"
                "   - `Postgres` (recommended)\n   - `SQLite`\n"
                "2. `Close it?`\n   - `Keep it`\n"
                "   - `Close` (NexKit closes the issue as not planned)"
            ),
            reason,
        )

    def test_a_list_past_its_room_says_it_was_cut_and_ends_at_a_word(self):
        lines = ["- `one two three`", "- `four five six seven eight`", "- `nine`"]
        text = report_module._listed("these items", lines, "the round's comment", room=30)
        self.assertEqual(
            text,
            "these items (NexKit cut this list to fit GitHub's limit on a comment; the "
            "round's comment has it whole):\n\n- `one two three`\n- `four…`",
        )
        whole = report_module._listed("these items", lines, "the round's comment")
        self.assertEqual(whole, "these items:\n\n" + "\n".join(lines))

    def test_a_long_list_stays_within_its_limit(self):
        decide = [f"item {n} " + "word " * 58 for n in range(20)]
        evidence = {item: "evidence " * 200 for item in decide}
        reason = needs_person("ready", self.cfg, decide=decide, evidence=evidence)
        listed = reason.split("whole):\n\n", 1)[1]
        self.assertLessEqual(len(listed), report_module.LISTED_LIMIT)
        self.assertIn("NexKit cut this list to fit GitHub's limit on a comment", reason)
        self.assertTrue(listed.endswith("…`"), listed[-40:])
        self.assertEqual(listed.count("`") % 2, 0)

    def test_a_mention_that_ends_with_a_list_adds_no_period(self):
        cfg = make_config(notify=["owner"])
        self.assertEqual(
            mention_text(cfg, "do it. Because:\n\n- `a`"),
            "@owner a person is needed: do it. Because:\n\n- `a`",
        )
        self.assertEqual(
            mention_text(cfg, "do it. Because:\n\n- `a`\n\nThe agent found: `b`"),
            "@owner a person is needed: do it. Because:\n\n- `a`\n\nThe agent found: `b`.",
        )

    def test_merge_refused_says_what_unblocks_it(self):
        found = {"refused": ("approve it", "GitHub wants a review")}
        self.assertEqual(
            needs_person("merge_refused", self.cfg, found=found),
            "approve it. GitHub wants a review",
        )

    def test_merged_and_automatic_rounds(self):
        for outcome in ("merged", "auto_fix", "conflict_fix", "up_to_date"):
            self.assertIsNone(needs_person(outcome, self.cfg), outcome)

    def test_paused_and_resuming(self):
        self.assertIsNone(needs_person("paused", self.cfg, resume={"at": "x"}))

    def test_paused_without_resume(self):
        self.assertIn("usage limit", needs_person("paused", self.cfg))


class ConfigTests(unittest.TestCase):
    def test_notify(self):
        self.assertEqual(
            validate(
                {
                    **WITH_CLAUDE,
                }
            )["notify"],
            [],
        )
        self.assertEqual(validate({**WITH_CLAUDE, "notify": ["octo-cat"]})["notify"], ["octo-cat"])
        for bad in (["@owner"], "owner", ["a", "A"], [""], ["-x"]):
            with self.assertRaises(ConfigError, msg=bad):
                validate({**WITH_CLAUDE, "notify": bad})


class MentionTests(ReportCase):
    def mentions(self, number):
        return [c["body"] for c in self.gh.comments(number) if c["body"].startswith("@")]

    def test_a_ready_pull_request_mentions_the_listed_people_once(self):
        self.candidate()
        self.run_report(cfg=make_config(notify=["owner", "lead"]))
        self.assertEqual(self.mentions(6), [f"@owner @lead a person is needed: {READY}."])
        self.assertEqual(self.mentions(5), [])
        [row] = self.gh.run_comments(6)
        self.assertEqual(row["run"]["attention"], READY)

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
        reason = (
            "answer the plan's questions with `/nexkit plan <answers>`. The plan asks for "
            "decisions that are the owner's to make:\n\n1. `?`"
        )
        self.assertEqual(self.mentions(5), [f"@owner a person is needed: {reason}"])
        [run] = self.gh.run_comments(5)
        self.assertEqual(run["run"]["attention"], reason)

    def person_review(self, *items):
        """A review that approves, with checklist items not met that a person decides, each
        over 200 characters, and what it found for each."""
        review = review_result()
        review["output"]["checklist"] = [
            {
                "item": f"{item} " + "and the rule holds everywhere it applies " * 5,
                "answer": "not_met",
                "person": True,
                "evidence": f"{item.lower()}.py:8-12 does it another way, " + "with a reason " * 15,
            }
            for item in items
        ]
        return review

    def test_a_ready_round_lists_each_item_to_decide_with_its_evidence(self):
        review = self.person_review("Rule A", "Rule B")
        self.candidate(review=review)
        cfg = make_config(notify=["owner"])
        self.run_report(cfg=cfg)
        [text] = self.mentions(6)
        for c in review["output"]["checklist"]:
            self.assertGreater(len(c["item"]), 200)
            self.assertIn(f"- `{c['item'].strip()}`: `{c['evidence'].strip()}`", text)
        self.assertNotIn("more", text)
        self.assertNotIn("…", text)
        # `nexkit status --json` gives the reason the mention gives.
        [row] = self.gh.run_comments(6)
        reason = row["run"]["attention"]
        self.assertEqual(text, f"@owner a person is needed: {reason}")
        data = json.loads(to_json(collect(self.gh, cfg, workers=1)))
        self.assertIn({"number": 6, "kind": "pull_request", "reason": reason}, data["needs_person"])
        # Its text form puts each item on a line of its own.
        shown = render(collect(self.gh, cfg, workers=1))
        self.assertIn(f"\n         - `{review['output']['checklist'][1]['item'].strip()}`", shown)

    def test_a_carried_approval_keeps_the_evidence_of_each_item(self):
        review = self.person_review("Rule A")
        self.candidate(review=review)
        self.run_report()
        os.environ["GITHUB_RUN_ID"] = "carried"
        self.candidate(review={"stage": "review", "status": "carried", "carried": HEAD})
        self.write("nexkit-review", "carry.json", {"from": HEAD, "carried": True, "why": ""})
        d = decision("review", pr=6, target=6, head="d" * 40, carry_from=HEAD)
        outcome = self.run_report(d=d, cfg=make_config(notify=["owner"]), published=False)
        self.assertEqual(outcome["outcome"], "ready")
        [c] = review["output"]["checklist"]
        self.assertIn(f"- `{c['item'].strip()}`: `{c['evidence'].strip()}`", self.mentions(6)[0])
        self.assertIn(f"- {c['item'].strip()}: {c['evidence'].strip()}", outcome["summary"])

    def test_a_ready_round_lists_each_criterion_to_confirm(self):
        review = review_result()
        criteria = [f"criterion {n} " + "holds under load " * 15 for n in range(3)]
        review["output"]["criteria"] = [
            {"criterion": c, "status": "unverifiable", "test": "", "evidence": "no trace"}
            for c in criteria
        ]
        self.candidate(review=review)
        self.run_report(cfg=make_config(notify=["owner"]))
        [text] = self.mentions(6)
        self.assertIn(
            "The review could not verify these acceptance criteria:\n\n"
            + "\n".join(f"- `{c.strip()}`" for c in criteria),
            text,
        )

    def test_a_plan_with_three_questions_lists_each_whole(self):
        asked = [
            {
                "question": f"Question {n}: " + "which way should it go " * 10,
                "options": ["This way", "That way"],
                "recommended": "This way",
            }
            for n in range(3)
        ]
        self.write(
            "nexkit-agent", "result.json", {"status": "done", "output": {"questions": asked}}
        )
        needs = {"publish": {"outputs": {"result": '{"published": true, "comment": "u"}'}}}
        report(
            self.gh,
            decision("plan"),
            make_config(notify=["owner"]),
            needs,
            self.artifacts,
            workflow_ref="x/nexkit.yml@main",
            default_branch="main",
        )
        [text] = self.mentions(5)
        for n, item in enumerate(asked, 1):
            self.assertIn(
                f"{n}. `{item['question'].strip()}`\n   - `This way` (recommended)\n"
                "   - `That way`",
                text,
            )

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
            [
                "@owner a person is needed: answer the questions in the round's comment with "
                "`/nexkit fix <answers>`. The agent stopped at a decision that is the owner's "
                "to make."
            ],
        )


if __name__ == "__main__":
    unittest.main()

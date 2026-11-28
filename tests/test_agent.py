import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import agent, first_state, program, protocol, runlog
from nexkit.checks import kept_output
from nexkit.context import ci_input
from nexkit.engine import prompt as engine_prompt
from nexkit.evidence import since_text
from nexkit.redact import Redactor
from nexkit.report import apply_criteria
from nexkit.runtimes import RUNTIMES
from nexkit.runtimes.claude_code import ClaudeReader
from nexkit.textutil import TEXT_LIMIT
from tests import engine_here
from tests.support import (
    CALC,
    MUL,
    SUB,
    FakeClaude,
    GitRepos,
    build_prompt,
    git,
    interpret_claude,
    make_config,
    previous_round,
    profile_config,
)

# The texts of a prompt that come from GitHub, as the engine writes them (`prompt.texts`), for
# the checks of how a prompt is built.
TEXTS = {
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
# What the context step hands the agent step: what it fetched from GitHub, and the fields later
# steps read (`context.record`). A round writes its prompt from it.
CONTEXT = {
    "inputs": {
        "item": {
            "id": 5,
            "title": "Fix add",
            "body": TEXTS["body"],
            "change": None,
            "person": False,
        },
        "notes": "",
        "discussion": {"item": [], "change": []},
        "plan": "Change add to return a + b.",
    },
    "state": None,
    "title": "Fix add",
    "person": False,
    "replan": False,
}


def context_with(inputs=None, **fields):
    """`CONTEXT` with `inputs` added to what it fetched, and `fields` of the record set."""
    return {**CONTEXT, **fields, "inputs": {**CONTEXT["inputs"], **(inputs or {})}}


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
    "no_change": "",
    "protected_paths": [],
}
FIX_DONE = {**DONE, "cause": "", "cause_outside": False, "open_conflicts": []}
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
    "protected_paths": [],
    "no_change": "",
}
VERDICT = {
    "verdict": "approve",
    "summary": "Good",
    "criteria": [],
    "findings": [],
    "previous_findings": [],
}


CHECKLIST = ["Docs change with the code", "A setting has a safe default"]


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(review={"checklist": CHECKLIST})

    def result(self, stage, output):
        return {"stage": stage, "status": "done", "output": output, "error": ""}

    def test_the_items_are_in_the_plan_and_review_prompts(self):
        for stage in ("plan", "review"):
            prompt = build_prompt(stage, TEXTS, self.cfg)
            self.assertIn("1. Docs change with the code\n2. A setting has a safe default", prompt)
            self.assertIn("`checklist`:", prompt)
            self.assertNotIn("$items", prompt)
            self.assertNotIn("$checklist", prompt)
        review = build_prompt("review", TEXTS, self.cfg)
        self.assertIn("`not_met`", review)
        self.assertIn("`not_applicable`", review)

    def test_with_the_setting_empty_the_prompts_and_results_are_as_before(self):
        cfg = make_config()
        for stage in ("plan", "review"):
            prompt = build_prompt(stage, TEXTS, cfg)
            self.assertNotIn("checklist", prompt)
            self.assertNotIn("$checklist", prompt)
            self.assertIs(agent.stage_schema(stage, cfg), agent.SCHEMAS[stage])
        result = self.result("review", dict(VERDICT))
        agent.answer_checklist(result, "review", cfg)
        self.assertEqual(result, self.result("review", dict(VERDICT)))

    def test_the_other_stages_have_no_checklist(self):
        for stage in ("implement", "fix"):
            self.assertNotIn("checklist", build_prompt(stage, TEXTS, self.cfg))
            self.assertIs(agent.stage_schema(stage, self.cfg), agent.SCHEMAS[stage])

    def test_the_schema_asks_for_an_answer_to_each_item(self):
        schema = agent.stage_schema("review", self.cfg)
        self.assertIn("checklist", schema["required"])
        entry = schema["properties"]["checklist"]["items"]
        self.assertEqual(
            entry["properties"]["answer"]["enum"], ["met", "not_met", "not_applicable"]
        )
        self.assertEqual(entry["required"], ["number", "answer", "evidence"])
        self.assertNotIn("checklist", agent.SCHEMAS["review"]["properties"])

    def test_a_review_that_lacks_an_answer_is_incomplete(self):
        answers = [{"number": 2, "answer": "met", "evidence": "x"}]
        result = self.result("review", {**VERDICT, "checklist": answers})
        agent.answer_checklist(result, "review", self.cfg)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["output"])
        self.assertEqual(
            result["error"], "The review gave no answer for item 1 of `review.checklist`."
        )

    def test_each_answer_gets_the_text_of_its_item(self):
        answers = [
            {"number": 2, "answer": "not_met", "evidence": "y"},
            {"number": 1, "answer": "met", "evidence": "x"},
            {"number": 9, "answer": "met", "evidence": "no such item"},
        ]
        result = self.result("review", {**VERDICT, "checklist": answers})
        agent.answer_checklist(result, "review", self.cfg)
        self.assertEqual(result["status"], "done")
        self.assertEqual(
            result["output"]["checklist"],
            [
                {"number": 1, "answer": "met", "evidence": "x", "item": CHECKLIST[0]},
                {"number": 2, "answer": "not_met", "evidence": "y", "item": CHECKLIST[1]},
            ],
        )

    def test_a_plan_that_leaves_an_item_out_is_shown_as_it_is(self):
        result = self.result("plan", {"checklist": [{"number": 1, "how": "It edits the docs."}]})
        agent.answer_checklist(result, "plan", self.cfg)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["output"]["checklist"][0]["item"], CHECKLIST[0])

    def test_an_item_a_person_decides_is_marked_in_prompts_and_answers(self):
        person = {"text": "No check loosens", "person": True}
        cfg = make_config(review={"checklist": [*CHECKLIST, person]})
        for stage in ("plan", "review"):
            prompt = build_prompt(stage, TEXTS, cfg)
            self.assertIn("3. No check loosens (a person decides)", prompt)
            self.assertIn("Only a person decides the items marked (a person decides)", prompt)
            self.assertNotIn("$mark", prompt)
        self.assertNotIn("a person decides", build_prompt("review", TEXTS, self.cfg))
        answers = [
            {"number": n, "answer": "met" if n < 3 else "not_met", "evidence": "x"}
            for n in (1, 2, 3)
        ]
        result = self.result("review", {**VERDICT, "checklist": answers})
        agent.answer_checklist(result, "review", cfg)
        self.assertEqual(
            [(e["item"], e.get("person", False)) for e in result["output"]["checklist"]],
            [(CHECKLIST[0], False), (CHECKLIST[1], False), ("No check loosens", True)],
        )

    def test_only_the_configuration_says_which_items_a_person_decides(self):
        answers = [
            {"number": 1, "answer": "not_met", "evidence": "x", "person": True},
            {"number": 2, "answer": "met", "evidence": "y", "extra": "z"},
        ]
        result = self.result("review", {**VERDICT, "checklist": answers})
        agent.answer_checklist(result, "review", self.cfg)
        self.assertEqual(
            result["output"]["checklist"],
            [
                {"number": 1, "answer": "not_met", "evidence": "x", "item": CHECKLIST[0]},
                {"number": 2, "answer": "met", "evidence": "y", "item": CHECKLIST[1]},
            ],
        )

    def test_a_stopped_review_stays_as_it_stopped(self):
        result = {"stage": "review", "status": "error", "output": None, "error": "It failed."}
        agent.answer_checklist(result, "review", self.cfg)
        self.assertEqual(result["error"], "It failed.")


def github_template(stage):
    """The template of `stage` with GitHub's words in it, on one line."""
    words = agent.prompt_words(5, 6)
    return " ".join(engine_prompt.fill(engine_prompt.template(stage), words).split())


class PromptTests(unittest.TestCase):
    def test_prompt_keeps_untrusted_text_literal(self):
        prompt = build_prompt("implement", TEXTS, make_config())
        self.assertIn("Use $HOME and ${{ secrets.X }} literally.", prompt)
        self.assertIn("`test`: `true`", prompt)
        self.assertIn("`.github/`", prompt)
        self.assertNotIn("$plan", prompt)

    def test_first_plan_has_no_latest_plan(self):
        extra = {"previous": engine_prompt.previous_plan(TEXTS)}
        prompt = build_prompt("plan", TEXTS, make_config(), extra)
        self.assertNotIn("Latest plan", prompt)
        self.assertIn("</untrusted>\n\n## Note from the person", prompt)
        self.assertNotRegex(prompt, r"\$(previous|since_plan)\b")

    def test_a_split_asks_that_a_parts_criteria_prove_what_later_parts_rely_on(self):
        prompt = " ".join(build_prompt("plan", TEXTS, make_config()).split())
        self.assertIn(
            "when later parts depend on a part, its acceptance criteria must prove end to "
            "end the behaviour they rely on",
            prompt,
        )

    def test_a_parts_plan_names_and_checks_what_it_assumes_earlier_parts_do(self):
        earlier = {
            "parent": 3,
            "parts": [
                {
                    "number": 6,
                    "title": "Run an eval stack",
                    "state": "closed",
                    "body": "Serve $MCP over http.",
                    "plan": "Start the stack.",
                },
                {"number": 7, "title": "Add a case client", "state": "open", "body": "B."},
            ],
        }
        context = {**TEXTS, "earlier": earlier}
        prompt = build_prompt("plan", context, make_config())
        flat = " ".join(prompt.split())
        self.assertIn("This issue is a part of #3", flat)
        self.assertIn("### #6: Run an eval stack (closed)", prompt)
        self.assertIn("Serve $MCP over http.", prompt)
        self.assertIn("Its latest plan:\n\nStart the stack.", prompt)
        self.assertIn("### #7: Add a case client (still open)", prompt)
        self.assertIn(
            "name what this part assumes it does, and check each assumption in the code", flat
        )
        self.assertIn("When the code does not do what this part needs", flat)
        self.assertIn("Say so in `risks`", flat)
        order = [
            "## Discussion\n",
            "## Earlier parts this part relies on",
            "## Note from the person",
        ]
        positions = [prompt.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        # An issue that is not a part, or a part that needs no other, has no such section.
        for other in (TEXTS, {**TEXTS, "earlier": {"parent": 3, "parts": []}}):
            prompt = build_prompt("plan", other, make_config(), {"previous": ""})
            self.assertNotIn("Earlier parts", prompt)
            self.assertNotIn("$earlier_parts", prompt)
            self.assertIn("</untrusted>\n\n## Note from the person", prompt)

    def test_the_plan_prompt_states_how_many_parts_a_split_may_have(self):
        from nexkit.state import MAX_PARTS

        prompt = " ".join(build_prompt("plan", TEXTS, make_config()).split())
        self.assertIn(f"in order, at most {MAX_PARTS} of them,", prompt)
        self.assertIn("NexKit files no part of a split with more parts than that", prompt)
        self.assertNotIn("$max_parts", prompt)

    def test_plan_criteria_are_what_the_review_can_verify(self):
        prompt = " ".join(build_prompt("plan", TEXTS, make_config()).split())
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

    def test_a_persons_pull_request_is_reviewed_in_its_protected_paths_too(self):
        context = {**TEXTS, "person": True}
        prompt = " ".join(build_prompt("review", context, make_config()).split())
        self.assertIn("## A person's pull request", prompt)
        self.assertIn("a person makes any change", prompt)
        self.assertIn("return no `criteria`", prompt)
        self.assertIn(
            "Review a change to CI configuration, workflows or the protected paths "
            "`.github/`, `.nexkit/` like any other code",
            prompt,
        )
        self.assertNotIn("cannot change: the pull request description", prompt)
        self.assertNotIn("$person", prompt)
        self.assertNotIn("$limits", prompt)
        self.assertNotIn("$protected", prompt)

    def test_the_review_of_a_nexkit_pull_request_leaves_protected_paths_out(self):
        prompt = " ".join(build_prompt("review", TEXTS, make_config()).split())
        self.assertNotIn("A person's pull request", prompt)
        self.assertNotIn("$person", prompt)
        self.assertIn(
            "things the implementation agent cannot change: the pull request description, "
            "approvals, CI configuration or the protected paths `.github/`, `.nexkit/`.",
            prompt,
        )

    def test_review_judges_each_criterion_and_unmet_ones_request_changes(self):
        prompt = " ".join(build_prompt("review", TEXTS, make_config()).split())
        self.assertIn("no criterion is `unmet` and every check passed", prompt)
        self.assertIn("`unverifiable` when they hold no evidence either way", prompt)
        self.assertIn("A criterion that a collaborator dropped or replaced", prompt)

    def test_replan_revises_the_latest_plan(self):
        context = {**TEXTS, "since_plan": "@olivia: Also accept `label`."}
        extra = {"previous": engine_prompt.previous_plan(context)}
        prompt = build_prompt("plan", context, make_config(), extra)
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
        prompt = build_prompt("review", TEXTS, make_config(), extra)
        self.assertNotIn("Previous round", prompt)
        self.assertIn("</untrusted>\n\n## How to review", prompt)

    def test_review_sees_the_discussion_and_why_the_code_leaves_the_plan(self):
        context = {
            **TEXTS,
            "discussion": "@olivia: Planned for the next release.",
            "outside_plan": "- Wrote the migration: the owner asked for it.",
        }
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        prompt = build_prompt("review", context, make_config(), extra)
        self.assertIn("## Discussion\n", prompt)
        self.assertIn("@olivia: Planned for the next release.", prompt)
        self.assertIn("## Outside the plan\n", prompt)
        self.assertIn("- Wrote the migration: the owner asked for it.", prompt)
        self.assertIn("against its reason under *Outside the plan*", prompt)
        self.assertNotIn("$discussion", prompt)
        self.assertNotIn("$outside_plan", prompt)

    def test_writing_stages_run_commands_in_the_foreground(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("implement", "fix"):
            prompt = build_prompt(stage, TEXTS, make_config(), extra)
            self.assertIn("Run every command in the foreground", prompt, stage)

    def test_every_prompt_has_the_writing_guide(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("plan", "implement", "fix", "review"):
            prompt = build_prompt(stage, TEXTS, make_config(), extra)
            self.assertIn("## How to write", prompt, stage)
            self.assertIn("Write for a person who knows the project", prompt, stage)
            self.assertNotIn("$writing", prompt, stage)
            guide = prompt.index("## How to write")
            self.assertLess(guide, prompt.index("## What to return"), stage)

    def test_plan_schema_separates_the_person_from_the_agent(self):
        schema = agent.SCHEMAS["plan"]
        for field in ("changes", "decisions", "risks", "implementation_notes"):
            self.assertIn(field, schema["required"])
            self.assertIn(f"`{field}`", engine_prompt.template("plan"))
        decision = schema["properties"]["decisions"]["items"]
        self.assertEqual(decision["required"], ["decision", "reason"])
        self.assertIn("revision", schema["required"])
        revision = schema["properties"]["revision"]
        self.assertEqual(revision["required"], ["started_over", "changes"])
        self.assertNotIn("approach", schema["properties"])

    def test_implement_schema_has_the_pull_request_sections(self):
        implement, fix = agent.SCHEMAS["implement"], agent.SCHEMAS["fix"]
        for field in ("summary", "changes", "testing", "outside_plan", "reviewer_notes"):
            self.assertIn(field, implement["properties"])
            self.assertIn(f"`{field}`", engine_prompt.template("implement"))
        # The notes are optional: an empty list means the same as none.
        self.assertEqual(agent.OPTIONAL["implement"]["outside_plan"], [])
        self.assertIn("testing", implement["required"])
        self.assertNotIn("changes", fix["required"])
        self.assertIn("status", fix["required"])

    def test_the_implement_prompt_calls_each_array_of_its_result_a_list(self):
        # A field described as prose, such as "one bullet each", led models to write it as
        # text or Markdown bullets, which the structured result cannot parse.
        template = engine_prompt.template("implement")
        properties = agent.SCHEMAS["implement"]["properties"]
        arrays = [name for name, sub in properties.items() if sub.get("type") == "array"]
        self.assertIn("outside_plan", arrays)
        for name in arrays:
            entry = template.split(f"\n- `{name}`:", 1)[1].split("\n- `", 1)[0]
            self.assertIn("list", entry, name)
        self.assertNotIn("bullet", template)

    def test_a_closing_option_is_kept_only_where_it_names_an_option(self):
        from nexkit import state

        options = ["Keep the issue open until someone attaches the logs", "Close the issue"]
        output = {
            "questions": [
                {"question": "A?", "options": options, "recommended": 0, "closes": 2},
                {"question": "B?", "options": options, "recommended": 0, "closes": 3},
                {"question": "C?", "options": options, "recommended": 0, "closes": True},
            ]
        }
        agent.check_closing(output)
        self.assertEqual([q["closes"] for q in output["questions"]], [2, 0, 0])
        recorded = state.questions(output["questions"])
        self.assertEqual([q.get("closing") for q in recorded], ["Close the issue", None, None])
        # Only a recommended closing option is one autopilot carries out by itself.
        self.assertNotIn("closes", recorded[0])
        # The comments mark the option that NexKit carries out.
        self.assertEqual(state.option_marks(recorded[0], "Close the issue"), state.CLOSING_MARK)
        self.assertEqual(state.questions(recorded), recorded)

    def test_options_that_need_a_new_plan_are_kept_only_where_they_name_options(self):
        from nexkit import state

        options = ["Split the work into its own issue", "Widen it", "Close the issue"]
        output = {
            "questions": [
                {"question": "A?", "options": options, "recommended": 1, "replans": [1, 2, 1]},
                {"question": "B?", "options": options, "recommended": 1, "replans": [0, 4, True]},
                {
                    "question": "C?",
                    "options": options,
                    "recommended": 3,
                    "replans": [3],
                    "closes": 3,
                },
                {"question": "D?", "options": options, "recommended": 2, "replans": 2},
            ]
        }
        agent.check_replans(output)
        self.assertEqual([q["replans"] for q in output["questions"]], [[1, 2], [], [], []])
        recorded = state.questions(output["questions"])
        self.assertEqual(recorded[0]["replanning"], options[:2])
        self.assertTrue(recorded[0]["replans"])
        self.assertTrue(all("replanning" not in q and "replans" not in q for q in recorded[1:]))
        # The comments mark each option that a new plan carries out.
        marks = state.option_marks(recorded[0], options[1])
        self.assertEqual(marks, state.REPLAN_MARK)
        self.assertEqual(state.questions(recorded), recorded)

    def test_an_implement_or_fix_question_names_the_options_that_need_a_new_plan(self):
        for stage in ("implement", "fix"):
            item = agent.SCHEMAS[stage]["properties"]["questions"]["items"]
            self.assertEqual(item["properties"]["replans"]["items"], {"type": "integer"})
            self.assertIn("replans", item["required"])
            template = github_template(stage)
            self.assertIn("`replans`: the numbers of the options that change the issue's", template)
            self.assertIn(
                "when no issue holds that work yet, the option changes the scope", template
            )
        self.assertNotIn(
            "replans", agent.SCHEMAS["plan"]["properties"]["questions"]["items"]["properties"]
        )
        template = github_template("plan")
        self.assertIn(
            "the note answers a blocked round with an option that splits the issue", template
        )

    def test_a_plan_or_implement_question_names_the_option_that_closes_the_issue(self):
        for stage in ("plan", "implement"):
            item = agent.SCHEMAS[stage]["properties"]["questions"]["items"]
            closing = ["question", "options", "recommended", "closes"]
            replans = ["replans"] if stage == "implement" else []
            self.assertEqual(item["required"], [*closing, *replans], stage)
            template = github_template(stage)
            self.assertIn(
                "`closes`: the number of the option that only closes the issue, with no "
                "change, or 0 when no option does. NexKit closes the issue when a person "
                "picks that option",
                template,
            )
            self.assertIn("Never offer closing together with another step", template)
        item = agent.SCHEMAS["fix"]["properties"]["questions"]["items"]
        self.assertNotIn("closes", item["properties"])
        self.assertIn("Never offer to close the issue", github_template("fix"))
        template = github_template("implement")
        self.assertIn("and set its `closes` to that option's number", template)
        self.assertIn(
            "when you are blocked for any other reason, such as work that is not merged yet, "
            "leave it empty, and never use it to say why there is no finding",
            template,
        )

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
        self.assertIn(
            "`severity` (the severity that review gave it)", engine_prompt.template("review")
        )

    def test_every_stage_template_is_complete(self):
        extra = {"diff": "+x", "check_results": "ok", "previous": "", "merge": ""}
        for stage in ("plan", "implement", "fix", "review"):
            prompt = build_prompt(stage, TEXTS, make_config(), extra)
            for name in ("$issue", "$title", "$plan", "$checks", "$protected", "$diff"):
                self.assertNotIn(name, prompt, f"{stage} leaves {name}")
            self.assertNotIn("$round_checks", prompt, stage)

    def test_without_slow_checks_the_prompts_list_every_check_as_before(self):
        checks = [{"name": "unit", "run": "make test"}, {"name": "lint", "run": "make lint"}]
        cfg = make_config(checks=checks)
        listed = "  - `unit`: `make test`\n  - `lint`: `make lint`\n"
        for stage in ("plan", "implement", "fix"):
            prompt = build_prompt(stage, TEXTS, cfg)
            self.assertIn(listed, prompt, stage)
            self.assertNotIn("slow", prompt, stage)
        rule = "- Before finishing, run the project checks and fix any failures you caused:\n"
        self.assertIn(rule + listed, build_prompt("implement", TEXTS, cfg))
        rule = "- Before finishing, run the project checks:\n"
        self.assertIn(rule + listed, build_prompt("fix", TEXTS, cfg))

    def test_slow_checks_are_listed_apart_and_not_run_in_full(self):
        checks = [
            {"name": "unit", "run": "make test"},
            {"name": "e2e", "run": "scripts/e2e.sh", "slow": True},
            {"name": "lint", "run": "make lint"},
        ]
        cfg = make_config(checks=checks)
        for stage in ("implement", "fix"):
            prompt = build_prompt(stage, TEXTS, cfg)
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
        plan = build_prompt("plan", TEXTS, cfg)
        self.assertIn("  - `unit`: `make test`\n  - `e2e`: `scripts/e2e.sh`\n", plan)
        self.assertNotIn("slow", plan)

    def argv(self, stage, cfg):
        spec = agent.stage_spec(stage, cfg, "/nonexistent")
        return RUNTIMES["claude-code"].launch(cfg["agents"]["claude"], spec, {}).argv

    def test_command_restricts_read_only_stages(self):
        cfg = make_config(effort="high", stages={"implement": {"model": "opus"}})
        plan = self.argv("plan", cfg)
        self.assertIn("Read,Grep,Glob", plan)
        self.assertNotIn("bypassPermissions", plan)
        fix = self.argv("fix", cfg)
        self.assertIn("bypassPermissions", fix)
        self.assertEqual(fix[fix.index("--model") + 1], "opus")
        for stage in ("implement", "fix", "plan", "review"):
            self.assertNotIn("--max-budget-usd", self.argv(stage, cfg), stage)
        self.assertEqual(fix[fix.index("--effort") + 1], "high")
        schema = json.loads(fix[fix.index("--json-schema") + 1])
        self.assertIn("status", schema["required"])

    def test_a_read_only_stage_reads_the_folders_it_is_given(self):
        cfg = make_config()
        spec = agent.stage_spec("plan", cfg, "/tmp/home", ["/tmp/nexkit-evidence"])
        cmd = RUNTIMES["claude-code"].launch(cfg["agents"]["claude"], spec, {}).argv
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/tmp/nexkit-evidence")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("--add-dir", self.argv("plan", cfg))

    def test_triage_command_has_no_tools_and_names_the_profiles(self):
        cfg = profile_config(stages={"triage": {"model": "fable"}})
        cmd = self.argv("triage", cfg)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "fable")
        self.assertNotIn("--max-budget-usd", cmd)
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        self.assertEqual(schema["properties"]["profile"]["enum"], ["standard", "hard"])
        self.assertNotIn("enum", agent.SCHEMAS["triage"]["properties"]["profile"])


class InterpretTests(unittest.TestCase):
    def test_outcomes(self):
        ok = {"subtype": "success", "structured_output": DONE}
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
        budget = interpret_claude("plan", {"subtype": "error_max_turns", "is_error": True})
        self.assertIn("Claude stopped (error_max_turns).", budget["error"])
        unstructured = interpret_claude("review", {"subtype": "success"})
        self.assertIn("structured result", unstructured["error"])

    def test_a_blocked_result_must_say_why_or_ask(self):
        blocked = {**FIX_DONE, "status": "blocked"}
        conflict = {
            "file": "a.py",
            "base_change": "x",
            "pr_change": "y",
            "question": "Which?",
            "options": ["A", "B"],
            "recommended": 0,
        }
        question = {
            "question": "Which?",
            "options": ["A", "B"],
            "recommended": 0,
            "closes": 0,
            "replans": [],
        }
        fits = [
            ("implement", {**blocked, "blocker": "No spec"}),
            ("implement", {**blocked, "questions": [question]}),
            ("fix", {**blocked, "open_conflicts": [conflict]}),
            ("fix", {**blocked, "blocker": '"Why"'}),
            ("implement", DONE),
            ("fix", {**FIX_DONE, "blocker": ""}),
        ]
        for stage, output in fits:
            with self.subTest(stage=stage, output=output):
                self.assertEqual(agent.contract_problems(output, stage), [])
                event = {"subtype": "success", "structured_output": output}
                result = interpret_claude(stage, event, schema=agent.SCHEMAS[stage])
                self.assertIn(result["status"], ("done", "blocked"), result["error"])
        for stage in ("implement", "fix"):
            for empty in ("", '""', " '' "):
                with self.subTest(stage=stage, blocker=empty):
                    output = {**blocked, "blocker": empty, "questions": []}
                    [problem] = agent.contract_problems(output, stage)
                    self.assertIn("result.blocker must say why you stopped", problem)
                    event = {"subtype": "success", "structured_output": output}
                    result = interpret_claude(stage, event, schema=agent.SCHEMAS[stage])
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["unfit"], problem)
                    self.assertNotIn("transient", result)
        # Other stages never get the rule.
        self.assertEqual(agent.contract_problems({"status": "blocked"}, "plan"), [])

    def test_an_optional_field_left_out_takes_its_empty_value(self):
        full = {"plan": PLAN, "implement": DONE, "fix": FIX_DONE}
        for stage, fields in agent.OPTIONAL.items():
            for field, empty in fields.items():
                given = {k: v for k, v in full[stage].items() if k != field}
                event = {"subtype": "success", "structured_output": given}
                result = interpret_claude(stage, event, schema=agent.SCHEMAS[stage])
                self.assertEqual(result["status"], "done", (stage, field, result["error"]))
                self.assertEqual(result["output"][field], empty, (stage, field))
                self.assertNotIn(field, agent.SCHEMAS[stage]["required"])
        # Every other field is required, and the defaults cover only fields that exist.
        for stage, fields in agent.OPTIONAL.items():
            schema = agent.SCHEMAS[stage]
            self.assertEqual(
                set(schema["required"]) | set(fields), set(schema["properties"]), stage
            )
            self.assertFalse(set(schema["required"]) & set(fields), stage)

    def test_a_review_finding_of_a_file_and_a_line_is_read_at_its_location(self):
        findings = [
            {"severity": "blocking", "file": "a.py", "line": 3, "body": "x"},
            {"severity": "suggestion", "file": "b.py", "line": 0, "body": "y"},
            {"severity": "suggestion", "file": "", "line": 0, "body": "z"},
            {"severity": "suggestion", "location": "form Customer", "body": "w"},
        ]
        given = {**VERDICT, "verdict": "request_changes", "findings": findings}
        event = {"subtype": "success", "structured_output": given}
        result = interpret_claude("review", event, schema=agent.SCHEMAS["review"])
        self.assertEqual(result["status"], "done", result["error"])
        self.assertEqual(
            [item["location"] for item in result["output"]["findings"]],
            ["a.py:3", "b.py", "", "form Customer"],
        )
        self.assertEqual(findings[0]["file"], "a.py")  # the result given is left as it was
        # A file that is not a text, or a line that is not a number, is left for the schema.
        odd = [{"severity": "blocking", "file": 4, "line": "x", "body": "x"}]
        event = {"subtype": "success", "structured_output": {**VERDICT, "findings": odd}}
        result = interpret_claude("review", event, schema=agent.SCHEMAS["review"])
        self.assertEqual(result["status"], "error")
        self.assertIn("location is missing", result["error"])

    def test_a_cli_that_ran_out_of_result_tries_is_unfit_with_what_it_said(self):
        call = {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {}}
        said = "Output does not match required schema: root: must have property 'testing'"
        refused = {"type": "tool_result", "tool_use_id": "s1", "is_error": True, "content": said}
        stream = [
            {"type": "assistant", "message": {"id": "m1", "content": [call]}},
            {"type": "user", "message": {"content": [refused]}},
        ]
        event = {"subtype": "error_max_structured_output_retries", "is_error": True}
        result = interpret_claude("implement", event, stream=stream)
        self.assertEqual(result["status"], "error")
        self.assertIn("error_max_structured_output_retries", result["error"])
        self.assertEqual(result["unfit"], said)
        self.assertNotIn("transient", result)
        # A result that does not fit NexKit's own check is unfit too, with its problems.
        wrong = {k: v for k, v in DONE.items() if k != "testing"}
        event = {"subtype": "success", "structured_output": wrong}
        result = interpret_claude("implement", event, schema=agent.SCHEMAS["implement"])
        self.assertEqual(result["unfit"], "result.testing is missing")
        # Other errors are not.
        budget = {"subtype": "error_max_turns", "is_error": True}
        self.assertNotIn("unfit", interpret_claude("implement", budget))

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

    def reviewed(self, reviewed, head, findings, merged=None):
        """The record of a review of the pull request, after the review of `reviewed` that
        found `findings`."""
        last = {"round": 1, "head": reviewed, "verdict": "request_changes", "findings": findings}
        state = first_state.for_prompt(5, {"id": "6", "version": head}, {"last_review": last})
        item = {**CONTEXT["inputs"]["item"], "change": 6}
        inputs = {"item": item, **({"diff": {"merged": merged}} if merged else {})}
        found = {"previous_head": reviewed, **({"merged": merged} if merged else {})}
        return context_with(inputs, state=state, **found)

    def run_quietly(self, stage, cfg=None, **kw):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.run_stage(stage, cfg, **kw)
        return result, stdout.getvalue()


class PromptCallTests(StageCase):
    """The agent step writes each prompt with one `prompt` request to the engine."""

    def test_the_request_holds_what_was_fetched_and_what_the_round_adds(self):
        cfg = make_config()
        added = {"diff": {"base": "main"}, "workspace": agent.workspace(cfg)}
        request = agent.prompt_request("implement", CONTEXT, added, cfg, self.out / "texts")
        self.assertEqual(
            sorted(request["inputs"]), ["discussion", "item", "notes", "plan", "workspace"]
        )
        self.assertEqual(request["inputs"]["item"]["id"], 5)
        self.assertEqual((request["call"], request["stage"]), ("prompt", "implement"))
        self.assertEqual(request["texts"], str(self.out / "texts"))
        self.assertEqual(protocol.check(request, "calls/prompt.schema.json", "request"), [])

    def test_the_request_holds_githubs_words_built_from_the_numbers_of_the_item(self):
        cfg = make_config()
        added = {"diff": {"base": "main"}, "workspace": agent.workspace(cfg)}
        item = {**CONTEXT["inputs"]["item"], "id": 5, "change": 6}
        context = context_with({"item": item})
        request = agent.prompt_request("fix", context, added, cfg, self.out / "texts")
        words = request["words"]
        self.assertEqual(words["item"], "GitHub issue #5")
        self.assertEqual(words["itemHeading"], "Issue #5")
        self.assertEqual(words["change"], "pull request #6")
        self.assertEqual(
            (words["itemKind"], words["itemKinds"], words["changeKind"]),
            ("issue", "issues", "pull request"),
        )
        self.assertEqual((words["source"], words["readers"]), ("on GitHub", "on GitHub"))
        self.assertEqual(
            (words["approve"], words["fixRound"], words["commands"]),
            ("`/nexkit go`", "`/nexkit fix`", "`/nexkit` commands"),
        )
        self.assertEqual(protocol.check(request, "calls/prompt.schema.json", "request"), [])
        # Without a pull request the item has no change.
        item = {**item, "change": None}
        context = context_with({"item": item})
        request = agent.prompt_request("implement", context, added, cfg, self.out / "texts")
        self.assertEqual(request["words"]["change"], "")
        self.assertEqual(request["words"]["item"], "GitHub issue #5")

    def test_the_request_holds_githubs_workspace_texts(self):
        cfg = make_config()
        added = {"diff": {"base": "main"}, "workspace": agent.workspace(cfg)}
        request = agent.prompt_request("implement", CONTEXT, added, cfg, self.out / "texts")
        text = request["inputs"]["workspace"]["text"]
        self.assertEqual(text, agent.camel(agent.WORKSPACE_TEXT))
        self.assertEqual(text["location"], "working in a checkout of the repository")
        self.assertEqual(text["protectedExample"], "`.github/workflows/ci.yml`")
        self.assertEqual(text["diffHeading"], "Diff against")
        self.assertEqual(protocol.check(request, "calls/prompt.schema.json", "request"), [])

    def test_the_diff_the_context_step_fetched_keeps_what_the_agent_step_adds_to_it(self):
        merged = {"head": "a" * 40, "base": "b" * 40, "conflicts": ["calc.py"]}
        context = context_with({"diff": {"merged": merged}})
        added = {"diff": {"base": "main", "text": "d"}}
        request = agent.prompt_request("review", context, added, make_config(), self.out)
        self.assertEqual(
            request["inputs"]["diff"],
            {"merged": merged, "base": "main", "text": "d"},
        )

    def test_a_request_the_engine_refuses_ends_the_round_with_an_error_and_no_agent(self):
        self.claude.configure(result=DONE)
        broken = {**CONTEXT, "inputs": {**CONTEXT["inputs"], "item": {"id": "five"}}}
        result = self.run_stage("implement", context=broken)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["error"].startswith("The engine wrote no prompt: request"))
        self.assertNotIn("transient", result)
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["status"], "error")
        self.assertFalse(self.claude.log.exists())
        self.assertFalse((self.out / "prompt.md").exists())

    def test_the_prompt_is_written_from_one_request(self):
        self.claude.configure(result=DONE, write={"calc.py": "x\n"})
        with mock.patch.object(agent, "prompt_call", wraps=agent.prompt_call) as call:
            self.run_stage("implement")
        call.assert_called_once()
        text = (self.out / "prompt.md").read_text()
        self.assertEqual(text, self.claude.call()["prompt"])
        self.assertIn("Change add to return a + b.", text)


FAILS = (sys.executable, "-c", "import sys; sys.exit(1)")


class EngineProgramTests(StageCase):
    """The agent step gets each prompt from the real `nexkit-engine` program."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(program, "call", engine_here.REAL)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_prompt_comes_from_a_process_that_holds_no_secret(self):
        secrets = {
            "ANTHROPIC_API_KEY": "sk-secret-1",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-2",
            "GITHUB_TOKEN": "ghs-secret-3",
            "NEXKIT_AGENT_SECRET_1": "agent-secret-4",
        }
        self.claude.configure(result=DONE, write={"calc.py": "x\n"})
        with (
            mock.patch.dict(os.environ, secrets),
            mock.patch("subprocess.run", wraps=subprocess.run) as run,
        ):
            self.run_stage("implement")
        tail = [*program.COMMAND, "prompt"]
        calls = [c for c in run.call_args_list if list(c.args[0])[-len(tail) :] == tail]
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(calls[0].args[0][1:3]), ["-P", "-m"])
        env = calls[0].kwargs["env"]
        self.assertLessEqual(set(env), {"PATH", "PYTHONPATH", "LANG"})
        for value in secrets.values():
            self.assertNotIn(value, env.values())
        text = (self.out / "prompt.md").read_text()
        self.assertEqual(text, self.claude.call()["prompt"])
        self.assertIn("Change add to return a + b.", text)

    def test_a_program_that_exits_with_no_answer_ends_the_round_with_an_error_and_no_agent(self):
        self.claude.configure(result=DONE)
        with mock.patch.object(program, "COMMAND", FAILS):
            result = self.run_stage("implement")
        self.assertEqual(result["status"], "error")
        self.assertIn("The engine wrote no prompt", result["error"])
        self.assertIn("The engine program failed", result["error"])
        self.assertNotIn("transient", result)
        self.assertEqual(json.loads((self.out / "result.json").read_text())["status"], "error")
        self.assertFalse((self.out / "prompt.md").exists())
        self.assertFalse(self.claude.log.exists())

    def test_triage_falls_back_when_the_program_fails(self):
        self.claude.configure(result={"profile": "hard", "reason": "x"})
        with mock.patch.object(program, "COMMAND", FAILS):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = agent.run_triage(
                    CONTEXT, profile_config(), "hard", self.out, program=str(self.claude.path)
                )
        self.assertEqual(result["status"], "error")
        self.assertIn("The engine wrote no prompt", result["error"])
        self.assertEqual((result["profile"], result["chosen_by"]), ("hard", "previous"))
        with mock.patch.object(program, "COMMAND", FAILS):
            with contextlib.redirect_stdout(io.StringIO()):
                result = agent.run_triage(
                    CONTEXT, profile_config(), None, self.out, program=str(self.claude.path)
                )
        self.assertEqual((result["profile"], result["chosen_by"]), ("standard", "default"))
        self.assertFalse(self.claude.log.exists())


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
                context_with(
                    {
                        "discussion": {
                            "item": [
                                {
                                    "id": 9,
                                    "at": "2026-11-01T00:00:00Z",
                                    "author": "olivia",
                                    "text": "Use the hard profile.",
                                    "kind": "comment",
                                }
                            ],
                            "change": [],
                        }
                    }
                ),
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
        self.assertIn("@olivia (2026-11-01T00:00:00Z):\nUse the hard profile.", prompt)
        self.assertIn("Use $HOME and ${{ secrets.X }} literally.", prompt)
        self.assertIn("## How to write", prompt)
        self.assertNotRegex(prompt.replace("$HOME", ""), r"\$[a-z_]+")
        self.assertEqual(prompt, (self.out / "triage/prompt.md").read_text())
        self.assertTrue((self.out / "triage/transcript.md").exists())

    NO_RESULT = {"result": None, "subtype": "error_max_structured_output_retries"}

    def test_a_session_without_a_result_that_fits_is_asked_for_it_alone(self):
        refused = {
            "type": "tool_result",
            "tool_use_id": "s1",
            "is_error": True,
            "content": "Output does not match required schema: must have property 'testing'",
        }
        call = {"type": "tool_use", "id": "s1", "name": "StructuredOutput", "input": {}}
        events = [
            {"type": "assistant", "message": {"id": "m1", "content": [call]}},
            {"type": "user", "message": {"content": [refused]}},
        ]
        self.claude.configure(
            **self.NO_RESULT,
            is_error=True,
            events=events,
            write={"calc.py": "def add(a, b):\n    return a + b\n"},
            resumed={"result": DONE, "events": []},
        )
        result, log = self.run_quietly("implement")
        self.assertEqual(result["status"], "done")
        self.assertTrue(result["asked_again"])
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertIn("+    return a + b", (self.out / "changes.patch").read_text())
        self.assertEqual(result["tokens"], {"input": 2000, "cached": 1700, "output": 240})
        first, second = self.claude.calls()
        session = first["args"][first["args"].index("--session-id") + 1]
        self.assertEqual(second["args"][second["args"].index("--resume") + 1], session)
        self.assertNotIn("--no-session-persistence", first["args"] + second["args"])
        self.assertIn("must have property 'testing'", second["prompt"])
        self.assertIn("Do not change, run or read anything more", second["prompt"])
        self.assertEqual(second["prompt"], (self.out / "result-again/prompt.md").read_text())
        self.assertTrue((self.out / "result-again/transcript.md").exists())
        self.assertIn("asking the same session for its result alone", log)

    def test_a_blocked_implement_result_without_a_reason_is_asked_for_again(self):
        silent = {**DONE, "status": "blocked", "blocker": "", "questions": []}
        reason = "The plan forbids a known.py entry."
        self.claude.configure(
            result=silent,
            write={"calc.py": "x\n"},
            resumed={"result": {**silent, "blocker": reason}, "events": []},
        )
        result, log = self.run_quietly("implement")
        self.assertEqual((result["status"], result["error"]), ("blocked", reason))
        self.assertTrue(result["asked_again"])
        first, second = self.claude.calls()
        self.assertIn("result.blocker must say why you stopped", second["prompt"])
        self.assertIn("asking the same session for its result alone", log)

    def test_a_blocked_fix_result_without_a_reason_twice_stops_as_transient(self):
        silent = {**FIX_DONE, "status": "blocked", "blocker": "", "questions": []}
        self.claude.configure(
            result=silent, write={"calc.py": "x\n"}, resumed={"result": silent, "events": []}
        )
        result, _ = self.run_quietly("fix", base="main")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["transient"])
        self.assertTrue(result["asked_again"])
        self.assertIn("result.blocker must say why you stopped", result["error"])
        self.assertEqual(len(self.claude.calls()), 2)

    def test_a_session_that_gives_no_result_again_stops_as_transient(self):
        self.claude.configure(
            **self.NO_RESULT,
            is_error=True,
            write={"calc.py": "x\n"},
            resumed={**self.NO_RESULT, "is_error": True, "events": []},
        )
        result, _ = self.run_quietly("implement")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["transient"])
        self.assertTrue(result["asked_again"])
        self.assertTrue(result["error"].endswith(agent.NO_RESULT_AGAIN))
        self.assertEqual(len(self.claude.calls()), 2)

    def test_a_request_for_the_result_that_times_out_is_still_no_result(self):
        self.claude.configure(
            **self.NO_RESULT,
            is_error=True,
            write={"calc.py": "x\n"},
            resumed={"result": DONE, "events": [], "sleep": 10},
        )
        real = agent.run_process

        def run_process(prompt, argv, **kw):
            if "--resume" in argv:
                kw["timeout_seconds"] = 0.5  # the request runs out of its time
            return real(prompt, argv, **kw)

        with mock.patch.object(agent, "run_process", run_process):
            result, _ = self.run_quietly("implement")
        self.assertEqual((result["status"], result["transient"]), ("error", True))
        self.assertNotIn("timed_out", result)
        self.assertTrue(result["asked_again"])
        self.assertIn("error_max_structured_output_retries", result["error"])
        self.assertTrue(result["error"].endswith(agent.NO_RESULT_AGAIN))

    def test_a_runtime_that_cannot_resume_stops_as_transient_at_once(self):
        self.claude.configure(**self.NO_RESULT, is_error=True, write={"calc.py": "x\n"})
        with mock.patch.object(type(RUNTIMES["claude-code"]), "resumes", False):
            result, _ = self.run_quietly("implement")
        self.assertEqual((result["status"], result["transient"]), ("error", True))
        self.assertNotIn("asked_again", result)
        self.assertNotIn(agent.NO_RESULT_AGAIN, result["error"])
        [call] = self.claude.calls()
        self.assertIn("--no-session-persistence", call["args"])

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

    def test_no_change_needed_closes_the_issue_and_a_change_with_it_is_published(self):
        finding = "8b9cb87 already fixed it: test_add passes on main."
        self.claude.configure(result={**DONE, "no_change": finding})
        result = self.run_stage("implement")
        self.assertEqual((result["status"], result.get("no_change")), ("done", True))
        self.assertEqual(result["output"]["no_change"], finding)
        self.assertIn("`no_change`", self.claude.call()["prompt"])
        # A round that changed files is a change: the text is a note for the reviewer.
        note = "Nothing skipped. Every planned change was made."
        output = {**DONE, "no_change": note, "reviewer_notes": ["Check the rounding."]}
        self.claude.configure(result=output, write={"calc.py": "x\n"})
        result = self.run_stage("implement")
        self.assertEqual((result["status"], result.get("no_change")), ("done", None))
        self.assertEqual(result["changed_files"], ["calc.py"])
        self.assertEqual(result["no_change_note"], note)
        self.assertEqual(result["output"]["no_change"], "")
        self.assertEqual(result["output"]["reviewer_notes"], ["Check the rounding.", note])
        self.assertNotIn("passed_since", result)

    def test_a_no_change_of_only_quotes_is_no_finding(self):
        self.claude.configure(result={**DONE, "no_change": '""'})
        result = self.run_stage("implement")
        self.assertEqual(result["status"], "error")
        self.assertIn("without changing", result["error"])
        self.assertNotIn("no_change", result)
        self.claude.configure(result={**DONE, "no_change": "' '"}, write={"calc.py": "x\n"})
        result = self.run_stage("implement")
        self.assertEqual((result["status"], result.get("no_change")), ("done", None))

    def test_a_plan_reads_the_evidence_of_the_ci_runs_outside_the_checkout(self):
        folder = Path(self.tmp.name) / "nexkit-evidence"
        (folder / "run-50" / "attempt-1").mkdir(parents=True)
        (folder / "run-50" / "attempt-1" / "build.log").write_text("HTTP 409\n")
        found = {
            "folder": str(folder),
            "runs": [
                {
                    "id": 50,
                    "url": "https://github.com/acme/app/actions/runs/50",
                    "files": [{"path": "run-50/attempt-1/build.log"}],
                    "missing": [],
                    "workflow": "ci.yml",
                    "branch": "main",
                    "passed_since": {
                        "id": 61,
                        "url": "https://github.com/acme/app/actions/runs/61",
                        "sha": "c" * 40,
                        "workflow": "ci.yml",
                        "branch": "main",
                    },
                }
            ],
            "skipped": [],
            "left_out": [],
        }
        finding = "The download host answered 409; run 61 passed since."
        self.claude.configure(result={**PLAN, "no_change": finding})
        context = context_with({"evidence": {"ci": ci_input(found)}}, ci_evidence=found)
        result = self.run_stage("plan", context=context)
        call = self.claude.call()
        self.assertEqual(call["args"][call["args"].index("--add-dir") + 1], str(folder))
        self.assertIn("  - `run-50/attempt-1/build.log`", call["prompt"])
        self.assertIn(
            "  - Since: run 61 of `ci.yml` passed on `main` since at ccccccc", call["prompt"]
        )
        self.assertIn("NexKit's agents may not change these paths: `.github/`", call["prompt"])
        self.assertEqual(result["passed_since"][0]["id"], 61)

    def test_a_github_plan_request_sends_the_since_text_and_githubs_findings(self):
        found = {
            "folder": "/out/ci",
            "runs": [
                {
                    "id": 50,
                    "url": "",
                    "files": [{"path": "run-50/attempt-1/build.log"}],
                    "missing": [],
                    "workflow": "ci.yml",
                    "branch": "main",
                    "passed_since": {"id": 61, "sha": "c" * 40, "url": ""},
                }
            ],
            "skipped": [],
            "left_out": [],
        }
        cfg = make_config()
        context = context_with({"evidence": {"ci": ci_input(found)}}, ci_evidence=found)
        added = {"workspace": agent.workspace(cfg)}
        request = agent.prompt_request("plan", context, added, cfg, self.out / "texts")
        run = request["inputs"]["evidence"]["ci"]["runs"][0]
        self.assertEqual(run["since"], since_text(found["runs"][0]))
        for gone in ("workflow", "branch", "passedSince", "passed_since"):
            self.assertNotIn(gone, run)
        self.assertEqual(request["policies"]["noChange"], agent.NO_CHANGE["plan"])
        implement = agent.prompt_request("implement", context, added, cfg, self.out / "texts")
        self.assertEqual(implement["policies"]["noChange"], agent.NO_CHANGE["implement"])
        review = agent.prompt_request("review", context, added, cfg, "")
        self.assertNotIn("noChange", review["policies"])
        self.assertEqual(protocol.check(request, "calls/prompt.schema.json", "request"), [])
        # The record keeps the raw run, so the round still records the run that passed.
        self.assertEqual(found["runs"][0]["passed_since"]["id"], 61)

    def test_an_agent_reads_the_texts_its_prompt_shortened_outside_the_checkout(self):
        folder = Path(self.tmp.name) / "out" / "texts"
        body = "x" * (TEXT_LIMIT + 4_000)
        item = {**CONTEXT["inputs"]["item"], "body": body}
        self.claude.configure(result={**DONE, "status": "blocked", "blocker": "Which file?"})
        result = self.run_stage("implement", context=context_with({"item": item}))
        call = self.claude.call()
        self.assertEqual(call["args"][call["args"].index("--add-dir") + 1], str(folder.resolve()))
        self.assertEqual((folder / "issue-5.md").read_text(), body)
        self.assertIn("## Texts this prompt shortened", call["prompt"])
        self.assertIn(f"the whole text in `{folder.resolve() / 'issue-5.md'}`", call["prompt"])
        # The round's comment names them.
        self.assertEqual(
            result["shortened"],
            [{"name": "the body of #5", "kept": TEXT_LIMIT, "length": TEXT_LIMIT + 4_000}],
        )

    def test_an_agent_asks_rather_than_guess_about_a_link_nexkit_did_not_read(self):
        linked = {
            "texts": [
                {
                    "kind": "comment",
                    "id": 9,
                    "on": 2,
                    "author": "olivia",
                    "at": "2026-11-01T00:00:00Z",
                    "text": "Rule: cents.",
                }
            ],
            "chain": [],
            "not_read": ["comment 77 of other/repo: not this repository"],
        }
        self.claude.configure(result={**DONE, "status": "blocked", "blocker": "Which rule?"})
        context = context_with({"linked": linked}, linked={"read": ["comment 9 on #2"]})
        result = self.run_stage("implement", context=context)
        prompt = self.claude.call()["prompt"]
        self.assertIn("Rule: cents.", prompt)
        self.assertIn("- comment 77 of other/repo: not this repository", prompt)
        self.assertIn("do not guess: stop and return `status: blocked`", prompt)
        # The round's comment says what the agent read and did not.
        self.assertEqual(
            result["linked"],
            {"read": ["comment 9 on #2"], "skipped": linked["not_read"]},
        )

    def test_only_protected_paths_count_as_needing_a_person(self):
        listed = [".github/workflows/ci.yml", "src/app.py", "/.nexkit/config.json", "src/app.py"]
        self.claude.configure(
            result={**DONE, "status": "blocked", "blocker": "CI", "protected_paths": listed}
        )
        result = self.run_stage("implement")
        self.assertEqual(
            result["output"]["protected_paths"], [".github/workflows/ci.yml", ".nexkit/config.json"]
        )

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

    def test_review_shows_a_failing_checks_failure_lines_end_and_file(self):
        self.claude.configure(result=VERDICT)
        output = "FAIL suite > picks an option: expected 2, got 3\n" + "teardown ok\n" * 1_700
        output += "1 failed"
        failing = {"name": "e2e", "run": "make e2e", "exit_code": 1, "passed": False}
        checks = [
            {"name": "unit", "run": "true", "exit_code": 0, "passed": True, "output": ""},
            {**failing, **kept_output(output)},
        ]
        self.run_stage("review", checks=checks, base="main")
        call = self.claude.call()
        prompt = call["prompt"]
        self.assertIn("`e2e` (`make e2e`): FAILED (exit 1)", prompt)
        self.assertIn("FAIL suite > picks an option: expected 2, got 3", prompt)
        self.assertIn("teardown ok\n1 failed\n```", prompt)
        folder = (self.out / "texts").resolve()
        path = folder / "check-2-e2e.txt"
        self.assertIn("FAIL suite > picks an option", path.read_text())
        self.assertTrue(path.read_text().endswith(output[-12_000:]))
        self.assertIn(f"failure) is in `{path}`", prompt)
        self.assertEqual(call["args"][call["args"].index("--add-dir") + 1], str(folder))
        # No text a person wrote was shortened.
        self.assertNotIn("## Texts this prompt shortened", prompt)

    def test_a_persons_pull_request_review_drops_the_criteria_it_made_up(self):
        git(self.repo, "checkout", "-q", "-b", "fix-add")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "fix")
        made_up = {
            "criterion": "add(2, 3) returns 5",
            "status": "unmet",
            "test": "",
            "evidence": "No test asserts it.",
        }
        self.claude.configure(result={**VERDICT, "criteria": [made_up]})
        checks = [{"name": "test", "run": "true", "exit_code": 0, "passed": True, "output": ""}]
        item = {**CONTEXT["inputs"]["item"], "person": True}
        person = context_with({"item": item}, person=True)
        result, log = self.run_quietly("review", context=person, checks=checks, base="main")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["output"]["criteria"], [])
        self.assertIn("Dropped the 1 acceptance criteria the review gave", log)
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["output"]["criteria"], [])
        # So the review approves: nothing else blocks.
        self.assertEqual(apply_criteria(result["output"])["verdict"], "approve")
        # A NexKit pull request's review keeps its criteria.
        result = self.run_stage("review", checks=checks, base="main")
        self.assertEqual(result["output"]["criteria"], [made_up])
        self.assertEqual(apply_criteria(result["output"])["verdict"], "request_changes")

    def test_a_review_that_skips_a_checklist_item_gives_no_verdict(self):
        cfg = make_config(review={"checklist": CHECKLIST})
        git(self.repo, "checkout", "-q", "-b", "nexkit/issue-5")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        self.repos.commit(self.repo, "fix")
        checks = [{"name": "test", "run": "true", "exit_code": 0, "passed": True, "output": ""}]
        answer = {"number": 1, "answer": "met", "evidence": "docs/x.md:3"}
        self.claude.configure(result={**VERDICT, "checklist": [answer]})
        result = self.run_stage("review", cfg, checks=checks, base="main")
        self.assertEqual(result["status"], "error")
        self.assertIn("no answer for item 2", result["error"])
        both = [answer, {**answer, "number": 2, "answer": "not_applicable"}]
        self.claude.configure(result={**VERDICT, "checklist": both})
        result = self.run_stage("review", cfg, checks=checks, base="main")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["output"]["checklist"][1]["item"], CHECKLIST[1])

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
        head = git(self.repo, "rev-parse", "HEAD")
        finding = {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Use $floats"}
        context = self.reviewed(reviewed, head, [finding])
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
        context = {**TEXTS, "previous_head": "f" * 40, "previous_round": "x"}
        self.assertIn("not in this checkout", previous_round(self.repo, context, "main"))
        context["previous_head"] = "--output=/tmp/x"
        self.assertIn("unknown", previous_round(self.repo, context, "main"))
        self.assertEqual(previous_round(self.repo, TEXTS, "main"), "")

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

    def test_an_agent_merge_of_main_is_the_rounds_merge(self):
        head, base = self.behind_base()
        merge = ["git", "merge", "--no-commit", "--no-ff", "-q", "origin/main"]
        result = self.run_fix(run=[merge], write={"calc.py": CALC + SUB + "# fixed\n"})
        self.assertEqual(result["status"], "done")
        self.assertEqual((result["start_head"], result["start_base"]), (head, base))
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(sorted(result["changed_files"]), ["calc.py", "other.py"])
        self.assertIn("- Do not merge the base branch", self.claude.call()["prompt"])

    def test_an_agent_merge_in_place_of_nexkits_publishes_nothing(self):
        _, base = self.repos.diverge()
        other = self.repos.push_commit("dev", "other", {"x.py": "1\n"}, "Elsewhere")
        self.checkout_branch()
        run = [
            ["git", "merge", "--abort"],
            ["git", "merge", "--no-commit", "--no-ff", "-q", "origin/other"],
        ]
        result = self.run_fix(run=run)
        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            f"The agent merged {other[:7]} in place of NexKit's merge of `main` "
            f"({base[:7]}), so NexKit publishes nothing.",
        )

    def behind_base(self):
        """A branch that merges cleanly into a base branch that has moved since."""
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", {"other.py": "x = 1\n"}, "Fix a flaky test")
        self.checkout_branch()
        return git(self.repo, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "origin/main")

    def run_after_failed_checks(self, **spec):
        self.claude.configure(**{"result": FIX_DONE, **spec})
        context = context_with(merge_moved_base=True)
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
        self.assertEqual(
            result["error"],
            "The agent finished without changing any files. What it found: Fixed add.",
        )
        self.assertTrue(result["unchanged"])
        self.assertNotIn("## Merge of", self.claude.call()["prompt"])

    def test_a_round_after_failed_checks_with_a_cause_outside_the_code_checks_again(self):
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.checkout_branch()
        cause = "The registry returned 500 Internal Server Error: e2e log line 812."
        outside = {**FIX_DONE, "cause": cause, "cause_outside": True}
        result = self.run_after_failed_checks(result=outside)
        self.assertEqual((result["status"], result.get("recheck")), ("done", True))
        self.assertIn("`cause_outside: true`", self.claude.call()["prompt"])
        # Without a cause, or in a round a person started, it is still an error.
        for spec, context in (
            ({"result": {**outside, "cause": ""}}, {"merge_moved_base": True}),
            ({"result": outside}, {}),
        ):
            self.checkout_branch()
            self.claude.configure(**spec)
            result = self.run_stage("fix", context=context_with(**context), base="main")
            self.assertEqual(result["status"], "error")
            self.assertNotIn("recheck", result)
        self.assertIn(f"What it found: {cause}", result["error"])

    def test_a_moved_base_that_changed_workflows_merges_like_any_other(self):
        workflow = {".github/workflows/ci.yml": "on: push\n"}
        self.repos.push_commit("dev", "nexkit/issue-5", {"calc.py": CALC + SUB}, "Add sub")
        self.repos.push_commit("dev", "main", workflow, "Change CI")
        self.checkout_branch()
        self.claude.configure(result=FIX_DONE)
        context = context_with(merge_moved_base=True)
        result = self.run_stage("fix", make_config(), context=context, base="main")
        self.assertEqual(result["changed_files"], [".github/workflows/ci.yml"])

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
        self.assertEqual(agent.OPTIONAL["fix"]["open_conflicts"], [])
        item = schema["properties"]["open_conflicts"]["items"]
        self.assertEqual(
            item["required"],
            ["file", "base_change", "pr_change", "question", "options", "recommended"],
        )
        self.assertNotIn("open_conflicts", agent.SCHEMAS["implement"]["required"])
        for field in item["required"]:
            self.assertIn(f"`{field}`", engine_prompt.template("fix"))

    def test_fix_prompt_asks_for_a_decision_instead_of_a_guess(self):
        words, text = agent.prompt_words(5, 6), agent.WORKSPACE_TEXT
        section = engine_prompt.conflicts_text("main", "b" * 40, ["Program.cs"], words, text)
        self.assertIn("whether it has one right answer or needs a choice", section)
        self.assertIn("do not pick a side and do not guess", section)
        self.assertIn("return `status: blocked` and list every\n  such conflict", section)
        self.assertIn("A note from the person who requested this round settles the choice", section)
        self.assertIn("only resolve the conflicts", section)
        self.assertEqual(engine_prompt.conflicts_text("main", None, []), "")

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
        finding = {
            "severity": "suggestion",
            "file": "calc.py",
            "line": 4,
            "body": "Document sub().",
        }
        merged = {"head": head, "base": base, "conflicts": ["calc.py"]}
        context = self.reviewed(head, git(self.repo, "rev-parse", "HEAD"), [finding], merged)
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
        context = {"diagnose": REPEATED, **(context or {})}
        found = context_with({"checks": {"diagnose": context["diagnose"]}}, **context)
        return self.run_stage("fix", context=found, base="main")

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
            "questions": [{**asked, "recommended": 0, "replans": []}],
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
        context = context_with(
            {"checks": {"diagnose": REPEATED}}, diagnose=REPEATED, merge_moved_base=True
        )
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

    def test_a_secret_in_the_blocker_is_stored_and_shown_as_stars(self):
        blocked = {**DONE, "status": "blocked", "blocker": f"Cannot reach it with {self.SECRET}"}
        self.configure(result=blocked, write={"calc.py": "1"})
        result, stdout = self.run_quietly("implement")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"], "Cannot reach it with ***")
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["error"], "Cannot reach it with ***")
        self.assertEqual(saved["output"]["blocker"], "Cannot reach it with ***")
        self.assertNotIn(self.SECRET, (self.out / "result.json").read_text())
        self.assertNotIn(self.SECRET, stdout)

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


class CarriedReviewTests(unittest.TestCase):
    """After NexKit merged the base branch into an approved pull request, the approval
    holds only for a clean merge of a base commit that leaves the change as it was."""

    def setUp(self):
        self.repos = GitRepos()
        self.addCleanup(self.repos.cleanup)
        files = {"calc.py": CALC + SUB, "logo.bin": "\0png"}
        self.approved = self.repos.push_commit("dev", "nexkit/issue-5", files, "Add sub").strip()

    def update(self, base_files, edit=None):
        """Move `main`, then merge it into the branch as GitHub's update does, with `edit`
        made in the merge commit when given. Returns the checkout at the new head."""
        self.repos.push_commit("base", "main", base_files, "Move main")
        repo = self.repos.clone(f"verify-{len(list(self.repos.root.iterdir()))}")
        git(repo, "checkout", "-q", "nexkit/issue-5")
        git(
            repo,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "merge",
            "-q",
            "--no-commit",
            "origin/main",
        )
        for path, content in (edit or {}).items():
            (repo / path).write_text(content)
        git(repo, "add", "-A")
        GitRepos.commit(repo, "Merge main")
        return repo

    def test_a_clean_merge_that_leaves_the_change_as_it_was_carries_it_over(self):
        repo = self.update({"other.py": "x = 1\n"})
        self.assertEqual(agent.carried_review(repo, "main", self.approved), (True, ""))

    def test_a_merge_that_changes_the_change_runs_the_review_again(self):
        repo = self.update({"other.py": "x = 1\n"}, edit={"calc.py": CALC + SUB + "# more\n"})
        carried, why = agent.carried_review(repo, "main", self.approved)
        self.assertFalse(carried)
        self.assertIn("differs from the one the review approved", why)
        # Binary content counts too.
        repo = self.update({"other.py": "x = 2\n"}, edit={"logo.bin": "\0gif"})
        self.assertFalse(agent.carried_review(repo, "main", self.approved)[0])

    def test_only_a_merge_of_the_base_branch_into_the_approved_commit_counts(self):
        repo = self.update({"other.py": "x = 1\n"})
        (repo / "calc.py").write_text(CALC + SUB + "# pushed\n")
        git(repo, "add", "-A")
        GitRepos.commit(repo, "A person's commit")
        carried, why = agent.carried_review(repo, "main", self.approved)
        self.assertFalse(carried)
        self.assertIn("not a merge of `main` into", why)
        self.assertFalse(agent.carried_review(repo, "main", "f" * 40)[0])


if __name__ == "__main__":
    unittest.main()

"""scripts/docs_audit.py: the docs audit's pages, its reading with read-only tools, and the
issues it plans, as .claude/skills/nexkit-docs-audit/SKILL.md says."""

import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nexkit.config
from tests.support import FakeClaude

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("docs_audit", ROOT / "scripts" / "docs_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

REPO = "https://github.com/acme/nexkit"


def finding(page="docs/a.md", kind="wrong", statement="It says X.", line=3, **extra):
    evidence = [{"file": "nexkit/a.py", "line": 7, "note": "does Y"}]
    return {
        "page": page,
        "line": line,
        "kind": kind,
        "statement": statement,
        "evidence": evidence,
        **extra,
    }


class PagesTests(unittest.TestCase):
    def test_every_audited_page_is_read_once_in_four_runs(self):
        files = audit.tracked()
        audited = [f for f in files if audit.AUDITED.match(f)]
        for page in (
            "AGENTS.md",
            "README.md",
            "docs/engine.md",
            "skills/setup/SKILL.md",
            audit.SKILL,
        ):
            self.assertIn(page, audited)
        for page in ("CHANGELOG.md", "tests/differential/README.md", "docs/README.txt"):
            self.assertFalse(audit.AUDITED.match(page), page)
        runs = [audit.pages_for(files, run) for run in range(audit.SHARES)]
        self.assertEqual(sorted(p for pages in runs for p in pages), sorted(audited))
        # The same pages again SHARES runs later.
        self.assertEqual(audit.pages_for(files, 7), runs[3])


class PlanTests(unittest.TestCase):
    def test_code_and_test_issues_come_first_then_one_issue_per_page(self):
        findings = [
            finding("docs/a.md", "wrong"),
            finding("docs/b.md", "missing"),
            finding("docs/a.md", "built", "Marked planned, built."),
            finding("docs/c.md", "untested-list", "Lists the statuses."),
            finding("docs/c.md", "unbuilt", "Says the sweep waits."),
        ]
        issues, left = audit.plan(findings, [], "99", REPO)
        self.assertEqual(
            [i["title"] for i in issues],
            [
                "The code follows the rule at docs/c.md:3",
                "A test compares the list at docs/c.md:3 with the code",
                "The docs of docs/a.md match the code",
                "The docs of docs/b.md match the code",
            ],
        )
        self.assertEqual(left, [])
        self.assertEqual(issues[0]["labels"], ["bug", "documentation", "docs-audit"])
        for issue in issues[1:]:
            self.assertEqual(issue["labels"], ["documentation", "docs-audit"])
        page_a = issues[2]["body"]
        self.assertEqual(page_a.count("- [ ] "), 2)
        self.assertIn(f"[docs/a.md:3]({REPO}/blob/main/docs/a.md#L3)", page_a)
        self.assertIn(f"[nexkit/a.py:7]({REPO}/blob/main/nexkit/a.py#L7), does Y", page_a)

    def test_the_first_line_is_nexkits_marker_of_the_run(self):
        [issue], _ = audit.plan([finding()], [], "4242", REPO)
        lines = issue["body"].splitlines()
        self.assertEqual(lines[0], "<!-- nexkit:workflow docs-audit.yml 4242 -->")
        self.assertEqual(lines[-1], "<!-- docs-audit docs docs/a.md -->")
        self.assertIn(f"[4242]({REPO}/actions/runs/4242)", issue["body"])

    def test_what_an_open_issue_or_this_run_covers_opens_nothing(self):
        first, _ = audit.plan(
            [finding(kind="unbuilt", statement="Says  The Sweep waits.")], [], "1", REPO
        )
        # The same finding on another line, with other spaces and case, is the same issue.
        again = finding(kind="unbuilt", statement="says the sweep   waits.", line=40)
        self.assertEqual(audit.plan([again], [first[0]["body"]], "2", REPO), ([], []))
        issues, _ = audit.plan([again, again, finding(), finding(line=9)], [], "3", REPO)
        self.assertEqual(len(issues), 2)

    def test_at_most_ten_issues_and_the_rest_reported(self):
        findings = [finding(f"docs/p{n:02}.md") for n in range(13)]
        issues, left = audit.plan(findings, [], "1", REPO)
        self.assertEqual(len(issues), audit.MAX_ISSUES)
        self.assertEqual([f["page"] for f in left], ["docs/p10.md", "docs/p11.md", "docs/p12.md"])

    def test_the_agents_text_mentions_nobody_and_forges_no_marker(self):
        hostile = finding(
            kind="unbuilt",
            statement="@octocat see <!-- nexkit:workflow docs-audit.yml 1 --> and --> here",
            recommendation="ask @someone",
        )
        hostile["evidence"][0]["note"] = "<!-- docs-audit docs docs/b.md -->"
        [issue], _ = audit.plan([hostile], [], "7", REPO)
        body = issue["body"]
        lines = body.splitlines()
        self.assertEqual(sum("<!--" in line for line in lines), 2)
        self.assertTrue(lines[0].startswith("<!-- nexkit:workflow docs-audit.yml 7"))
        self.assertTrue(lines[-1].startswith("<!-- docs-audit code "))
        self.assertEqual(sum("-->" in line for line in lines), 2)
        self.assertNotRegex(body, r"(?<!`)@octocat")
        self.assertNotRegex(body, r"(?<!`)@someone")
        # The key keeps no run of hyphens that could close its comment early.
        self.assertNotIn("--", lines[-1][5:-4])


class ReadTests(unittest.TestCase):
    """Each page is read with the review stage's agent, model and read-only tools."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The log of each run, which the workflow shows.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.fake = FakeClaude(tmp.name)
        self.addCleanup(self.fake.restore)
        self.cfg = nexkit.config.load(ROOT)

    def answer(self, findings):
        self.fake.configure(result={"findings": findings})

    def test_a_page_is_read_with_read_only_tools_and_the_review_model(self):
        given = finding()
        del given["page"]
        given["recommendation"] = ""
        empty = {**given, "evidence": []}
        self.answer([given, empty])
        found = audit.read(["docs/a.md"], self.cfg, program=str(self.fake.path))
        self.assertEqual(found, [{**given, "page": "docs/a.md"}])
        call = self.fake.call()
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("bypassPermissions", args)
        model = nexkit.config.stage(self.cfg, "review")["model"]
        self.assertEqual(args[args.index("--model") + 1], model)
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), audit.FINDINGS)
        self.assertIn(f"Read {audit.SKILL}", call["prompt"])
        self.assertIn("- docs/a.md\n", call["prompt"])
        self.assertEqual(call["cwd"], str(ROOT))
        self.assertTrue(call["has_token"])

    def test_the_findings_hold_no_credential(self):
        leaked = finding(statement="The token is test-token.")
        del leaked["page"]
        leaked["recommendation"] = ""
        self.answer([leaked])
        [found] = audit.read(["docs/a.md"], self.cfg, program=str(self.fake.path))
        self.assertNotIn("test-token", json.dumps(found))

    def test_every_page_is_read_before_a_failure_raises(self):
        self.fake.configure(result=None, is_error=True, subtype="error_during_execution", exit=1)
        with self.assertRaises(RuntimeError) as raised:
            audit.read(["docs/a.md", "docs/b.md"], self.cfg, program=str(self.fake.path))
        self.assertEqual(len(self.fake.calls()), 2)
        self.assertRegex(str(raised.exception), r"(?s)docs/a\.md: .*\ndocs/b\.md: ")

    def test_an_answer_that_does_not_fit_fails_the_page(self):
        self.answer([{"line": 1, "kind": "rumour"}])
        with self.assertRaisesRegex(RuntimeError, "docs/a.md: .*does not fit the review schema"):
            audit.read(["docs/a.md"], self.cfg, program=str(self.fake.path))

    def test_no_secret_fails_before_the_agent_runs_on_actions(self):
        # On a person's machine the agent may run on that person's own login instead.
        self.answer([])
        with mock.patch.dict(
            "os.environ", {"CLAUDE_CODE_OAUTH_TOKEN": "", "GITHUB_ACTIONS": "true"}
        ):
            with self.assertRaisesRegex(
                RuntimeError, "docs/a.md: No secret for the `claude` agent"
            ):
                audit.read(["docs/a.md"], self.cfg, program=str(self.fake.path))
        self.assertEqual(self.fake.calls(), [])


class MainTests(unittest.TestCase):
    def test_plan_refuses_lost_findings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as empty:
            with self.assertRaisesRegex(SystemExit, "holds no findings"):
                audit.main(["docs_audit.py", "plan", empty.name, "1"])

    def test_the_schema_holds_only_what_an_agent_receives(self):
        keywords = {"type", "enum", "properties", "required", "items", "description"}

        def walk(schema):
            self.assertLessEqual(set(schema), keywords)
            for sub in schema.get("properties", {}).values():
                walk(sub)
            if "items" in schema:
                walk(schema["items"])

        walk(audit.FINDINGS)
        self.assertEqual(
            set(audit.FINDINGS["properties"]["findings"]["items"]["properties"]["kind"]["enum"]),
            set(audit.KINDS),
        )
        self.assertTrue(re.match(r"^\.claude/skills/", audit.SKILL))
        self.assertTrue((ROOT / audit.SKILL).exists())


if __name__ == "__main__":
    unittest.main()

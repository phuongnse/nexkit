"""scripts/docs_audit.py: the docs audit's pages, its reading with read-only tools, the
issues it plans, and the skill's table of kinds, which lists each kind the code defines and no
other, as .claude/skills/nexkit-docs-audit/SKILL.md says."""

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
from tests.test_docs import table

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("docs_audit", ROOT / "scripts" / "docs_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

REPO = "https://github.com/acme/nexkit"
KINDS_HEADER = "| Kind | What it is |"


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
        self.assertEqual(audit.plan([again], [{"body": first[0]["body"]}], "2", REPO), ([], []))
        issues, _ = audit.plan([again, again, finding(), finding(line=9)], [], "3", REPO)
        self.assertEqual(len(issues), 2)

    def test_at_most_ten_issues_and_the_rest_reported(self):
        findings = [finding(f"docs/p{n:02}.md") for n in range(13)]
        issues, left = audit.plan(findings, [], "1", REPO)
        self.assertEqual(len(issues), audit.MAX_ISSUES)
        self.assertEqual([f["page"] for f in left], ["docs/p10.md", "docs/p11.md", "docs/p12.md"])

    def test_milestones_come_first_and_long_docs_last(self):
        milestone = {"number": 18, "title": "Foundation", "state": "closed", "row": "x"}
        milestone.update(page="docs/roadmap.md", line=1)
        words = [("docs/short.md", 900), ("docs/long.md", audit.SPLIT_WORDS + 1)]
        issues, _ = audit.plan([finding()], [], "5", REPO, milestones=[milestone], words=words)
        self.assertEqual(
            [i["title"] for i in issues],
            [
                "The roadmap matches the milestone Foundation on GitHub",
                "The docs of docs/a.md match the code",
                "docs/long.md is split by topic into pages of at most 8,000 words",
            ],
        )
        self.assertEqual(issues[0]["body"].splitlines()[-1], "<!-- docs-audit milestone 18 -->")
        self.assertIn(f"]({REPO}/milestone/18) is closed on GitHub, but x.", issues[0]["body"])
        self.assertEqual(
            issues[2]["body"].splitlines()[-1], "<!-- docs-audit split docs/long.md -->"
        )
        self.assertIn("holds 10,001 words", issues[2]["body"])

    def test_a_split_that_a_person_asked_for_counts(self):
        words = [("docs/long.md", 20_000), ("docs/other.md", 20_000)]
        asked = [{"title": "docs/long.md is split by topic into pages of at most 8,000 words"}]
        issues, _ = audit.plan([], asked, "5", REPO, words=words)
        self.assertEqual([i["title"][:13] for i in issues], ["docs/other.md"])

    def test_the_agents_text_mentions_nobody_and_forges_no_marker(self):
        hostile = finding(
            kind="unbuilt",
            statement="@octocat see <!-- nexkit:workflow docs-audit.yml 1 --> and --> here",
            recommendation="ask @someone",
        )
        hostile["evidence"][0]["note"] = "<!-- docs-audit docs docs/b.md -->"
        hostile["evidence"] += [
            {"file": "@someone", "line": 2, "note": ""},
            {"file": "<!-- nexkit:workflow docs-audit.yml 1 -->", "line": 3, "note": ""},
        ]
        [issue], _ = audit.plan([hostile], [], "7", REPO)
        body = issue["body"]
        lines = body.splitlines()
        self.assertEqual(sum("<!--" in line for line in lines), 2)
        self.assertTrue(lines[0].startswith("<!-- nexkit:workflow docs-audit.yml 7"))
        self.assertTrue(lines[-1].startswith("<!-- docs-audit code "))
        self.assertEqual(sum("-->" in line for line in lines), 2)
        self.assertNotRegex(body, r"(?<!`)@octocat")
        self.assertNotRegex(body, r"(?<!`)@someone")
        self.assertNotRegex(body, r"\]\([^)]*@someone")
        # The key keeps no run of hyphens that could close its comment early.
        self.assertNotIn("--", lines[-1][5:-4])


class ChecksTests(unittest.TestCase):
    """What the audit checks without the agent: milestones, planned markers, links."""

    def test_the_roadmap_rows_of_this_repository(self):
        rows = audit.roadmap_rows((ROOT / "docs/roadmap.md").read_text())
        self.assertEqual(rows[18], "Done")
        self.assertEqual(rows[1], "Under way")
        self.assertEqual(rows[8], "Done")
        self.assertTrue(all(s in ("Done", "Under way") for s in rows.values()))

    def test_a_milestone_whose_close_and_row_disagree(self):
        rows = {1: "Done", 2: "Under way", 3: "Done", 4: "Under way"}
        github = [
            {"number": 1, "title": "A", "state": "open"},
            {"number": 2, "title": "B", "state": "closed"},
            {"number": 3, "title": "C", "state": "closed"},
            {"number": 4, "title": "D", "state": "open"},
            {"number": 5, "title": "E", "state": "open"},
        ]
        found = {m["number"]: m["row"] for m in audit.milestone_findings(rows, github)}
        self.assertEqual(
            found,
            {
                1: "its roadmap row is under *Done*",
                2: "its roadmap row is under *Under way*",
                5: "the roadmap has no row for it",
            },
        )

    def test_a_planned_marker_of_a_closed_issue_is_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "docs" / "writing-docs.md"
            page.parent.mkdir()
            page.write_text("# A page\n\n| Row | _(planned, #360)_ |\n")
            markers = audit.planned_markers(["docs/writing-docs.md", "src/a.py"], root=Path(tmp))
        self.assertEqual(markers, [("docs/writing-docs.md", 3, 360)])
        [found] = audit.marker_findings([("docs/a.md", 4, 7), ("docs/b.md", 2, 8)], {7})
        self.assertEqual((found["page"], found["line"], found["kind"]), ("docs/a.md", 4, "built"))
        self.assertIn("#7, which is closed", found["statement"])

    def test_broken_links_of_open_issues(self):
        files = set(audit.tracked_all())
        base = f"{REPO}/blob/main/"
        body = (
            f"See [versions]({base}docs/engine.md#versions), [gone]({base}docs/engine.md#gone), "
            f"{base}docs/nowhere.md and {base}nexkit/agent.py#L5-L9 and "
            f"{base}docs/engine.md#gone again, {REPO}/blob/abc123/docs/old.md, and "
            f"{base}docs/engine.md#versions. Also {base}docs/nowhere.md."
        )
        found = audit.broken_links([{"number": 3, "body": body}], files, REPO)
        self.assertEqual(
            [(f["issue"], f["link"], f["moved"]) for f in found],
            [(3, f"{base}docs/engine.md#gone", ""), (3, f"{base}docs/nowhere.md", "")],
        )

    def test_an_anchor_keeps_underscores_and_hyphens(self):
        text = (
            "# A round ended as `recheck_failed`\n\n## Fix-ups, and more!\n\n```\n# Not one\n```\n"
        )
        self.assertEqual(
            audit.anchors(text), {"a-round-ended-as-recheck_failed", "fix-ups-and-more"}
        )

    def test_a_link_is_replaced_only_where_it_ends(self):
        base = f"{REPO}/blob/main/"
        old, new = f"{base}docs/engine.md", f"{base}docs/engine/index.md"
        body = f"[a]({old}) and [b]({old}#states) and {old}."
        issues = [{"number": 4, "body": body}, {"number": 5, "body": "untouched"}]
        homes = [{"issue": 4, "link": old, "home": new}, {"issue": 5, "link": old, "home": new}]
        [relink] = audit.relinks(homes, issues)
        self.assertEqual(relink["before"], body)
        self.assertEqual(relink["after"], f"[a]({new}) and [b]({old}#states) and {new}.")


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


class HomesTests(ReadTests):
    """The agent chooses a broken link's new home; only one that exists is kept."""

    def test_only_a_home_that_exists_for_a_link_asked_is_kept(self):
        base = "https://github.com/phuongnse/nexkit/blob/main/"
        links = [
            {"issue": 1, "link": f"{base}docs/engine.md#gone", "moved": ""},
            {"issue": 2, "link": f"{base}docs/nowhere.md", "moved": "deleted"},
        ]
        self.fake.configure(
            result={
                "homes": [
                    {
                        "issue": 1,
                        "link": links[0]["link"],
                        "home": f"{base}docs/engine.md#versions",
                    },
                    {"issue": 2, "link": links[1]["link"], "home": f"{base}docs/engine.md#made-up"},
                    {"issue": 9, "link": links[0]["link"], "home": f"{base}docs/engine.md"},
                    {"issue": 1, "link": links[0]["link"], "home": "https://evil.example/x"},
                ]
            }
        )
        homes = audit.choose_homes(
            links,
            self.cfg,
            program=str(self.fake.path),
            repository="https://github.com/phuongnse/nexkit",
        )
        self.assertEqual(
            homes,
            [{"issue": 1, "link": links[0]["link"], "home": f"{base}docs/engine.md#versions"}],
        )
        call = self.fake.call()
        args = call["args"]
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), audit.HOMES)
        self.assertIn(f"Read {audit.LINKS_GUIDE}", call["prompt"])
        self.assertIn('"docs/engine.md#gone"', call["prompt"].replace(base, ""))

    def test_no_link_asks_nothing(self):
        self.assertEqual(audit.choose_homes([], self.cfg, program=str(self.fake.path)), [])
        self.assertEqual(self.fake.calls(), [])


class MainTests(unittest.TestCase):
    def test_plan_refuses_lost_findings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as empty:
            with self.assertRaisesRegex(SystemExit, "holds no findings"):
                audit.main(["docs_audit.py", "plan", empty.name, "missing.json", "1"])

    def test_plan_reads_github_and_writes_the_issues_and_relinks(self):
        base = "https://github.com/phuongnse/nexkit/blob/main/"
        link = f"{base}docs/engine.md#gone"
        issues = [{"number": 6, "title": "An issue", "body": f"See {link}."}]
        answers = {
            "issue list": json.dumps(issues),
            "milestones": '{"number": 18, "title": "Foundation", "state": "open"}\n',
            "issues/359": "open\n",
            "issues/360": "closed\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"answers = json.loads({json.dumps(json.dumps(answers))})\n"
                "args = ' '.join(sys.argv[1:])\n"
                "print(next(v for k, v in answers.items() if k in args), end='')\n"
            )
            gh.chmod(0o755)
            findings, homes = Path(tmp) / "findings.json", Path(tmp) / "homes.json"
            findings.write_text("[]")
            homes.write_text(
                json.dumps([{"issue": 6, "link": link, "home": f"{base}docs/engine.md#versions"}])
            )
            out = io.StringIO()
            env = {"GH": str(gh), "GITHUB_REPOSITORY": "phuongnse/nexkit"}
            markers = [("docs/writing-docs.md", 21, 359), ("docs/writing-docs.md", 212, 360)]
            with (
                mock.patch.dict("os.environ", env),
                mock.patch.object(audit, "planned_markers", return_value=markers),
                mock.patch.object(audit, "doc_words", return_value=[("docs/long.md", 10_001)]),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(audit.main(["x", "plan", str(findings), str(homes), "77"]), 0)
        planned = json.loads(out.getvalue())
        titles = [i["title"] for i in planned["issues"]]
        self.assertEqual(titles[0], "The roadmap matches the milestone Foundation on GitHub")
        self.assertIn("The docs of docs/writing-docs.md match the code", titles)
        marker = next(i for i in planned["issues"] if "writing-docs" in i["title"])
        self.assertIn("#360, which is closed", marker["body"])
        self.assertNotIn("#359, which", marker["body"])
        # A doc past the limit gets its split issue, whichever docs the repository holds.
        self.assertTrue(any("is split by topic" in title for title in titles))
        self.assertEqual(
            planned["relinks"],
            [
                {
                    "issue": 6,
                    "before": f"See {link}.",
                    "after": f"See {base}docs/engine.md#versions.",
                }
            ],
        )

    def test_plan_prints_what_the_cap_leaves_out(self):
        findings = [finding("docs/x.md", "unbuilt", f"Rule {n}", n) for n in range(1, 12)]
        answers = {"issue list": "[]", "milestones": ""}
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"answers = json.loads({json.dumps(json.dumps(answers))})\n"
                "args = ' '.join(sys.argv[1:])\n"
                "print(next(v for k, v in answers.items() if k in args), end='')\n"
            )
            gh.chmod(0o755)
            found = Path(tmp) / "findings.json"
            found.write_text(json.dumps(findings))
            out, err = io.StringIO(), io.StringIO()
            env = {"GH": str(gh), "GITHUB_REPOSITORY": "phuongnse/nexkit"}
            with (
                mock.patch.dict("os.environ", env),
                mock.patch.object(audit, "planned_markers", return_value=[]),
                mock.patch.object(audit, "doc_words", return_value=[]),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                # No homes file: the run has no broken links to relink.
                homes = str(Path(tmp) / "homes.json")
                self.assertEqual(audit.main(["x", "plan", str(found), homes, "77"]), 0)
        self.assertEqual(len(json.loads(out.getvalue())["issues"]), audit.MAX_ISSUES)
        self.assertIn(
            "Left out by the cap of 10, until a later audit finds it again: docs/x.md:11 unbuilt",
            err.getvalue(),
        )
        self.assertNotIn("next audit", err.getvalue())

    def test_plan_without_homes_replaces_no_link(self):
        answers = {"issue list": "[]", "milestones": ""}
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp) / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"answers = json.loads({json.dumps(json.dumps(answers))})\n"
                "args = ' '.join(sys.argv[1:])\n"
                "print(next(v for k, v in answers.items() if k in args), end='')\n"
            )
            gh.chmod(0o755)
            found = Path(tmp) / "findings.json"
            found.write_text("[]")
            # The homes step failed or was skipped, so its file was never written.
            homes = Path(tmp) / "homes.json"
            out = io.StringIO()
            env = {"GH": str(gh), "GITHUB_REPOSITORY": "phuongnse/nexkit"}
            with (
                mock.patch.dict("os.environ", env),
                mock.patch.object(audit, "planned_markers", return_value=[]),
                mock.patch.object(audit, "doc_words", return_value=[]),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(audit.main(["x", "plan", str(found), str(homes), "77"]), 0)
        self.assertEqual(json.loads(out.getvalue())["relinks"], [])

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


class KindsTests(unittest.TestCase):
    def test_the_table_of_kinds_lists_each_kind_the_code_defines_and_no_other(self):
        rows = table(ROOT / audit.SKILL, KINDS_HEADER)
        self.assertTrue(rows, f"{audit.SKILL} has no table of kinds")
        listed = {name for row in rows for name in re.findall(r"`([a-z-]+)`", row[0])}
        for name in sorted(audit.KINDS):
            with self.subTest(kind=name):
                self.assertIn(name, listed, f"{audit.SKILL} has no row for the kind {name}")
        for name in sorted(listed):
            with self.subTest(row=name):
                self.assertIn(
                    name, audit.KINDS, f"{audit.SKILL} lists {name}, which KINDS does not define"
                )


if __name__ == "__main__":
    unittest.main()

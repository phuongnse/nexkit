"""The docs' map lists every doc, with the decisions listed through their index, and its list of the
plugin's skills matches skills/. The links of every page under docs/, of the README, of AGENTS.md,
of the plugin's skills and of the maintenance skills resolve, every link to how it works and the
pages split from it resolves, each task of the nexkit-maintain skill links a section of the delivery
page, AGENTS.md keeps within its word limits, the architecture page's job and module tables match
the workflows and nexkit/, the how-it-works page's job table matches the jobs of pipeline.yml, the
permissions its rows name and the number in its heading, each test the security page names exists,
and the outcome table of how it works lists each outcome the code defines and no other. The mention
table of the troubleshooting page lists each outcome that needs a person and no other, with the
code's text for what to do and why. The command table of the README lists each comment command the
code accepts, where it accepts it, and no other. The key tables and the complete configuration of
the configuration reference list each key the code defines and no other, with the defaults and
ranges the code has. The list of a check's fields on the checks page lists each key the code
defines and no other. The runtime table of that page lists each runtime the code defines and no
other, with its providers, effort levels and budget support, and which runtimes report usage limits.
The trigger table of its workflow file section lists each trigger of the workflow template and no
other. The table of the App's permissions matches the permissions and levels of the App manifest.
The refusals of `unaffected_by` that the checks page lists name each refusal of the code and no
other, and the code refuses each one. The plan and review layouts of stages match the sections
the code writes, and the values the pages repeat match their constants. The README's table of
supported versions is compared with the pins in nexkit/__init__.py, and each ubuntu-NN.NN in the
README with the pinned runner. The local commands
table of the README lists each command the CLI defines for people and no other, and each option a
row names exists. The configuration example of the README is what `nexkit init` writes for the
README's own setup command. No doc, the README, AGENTS.md or a skill repeats a pinned version:
each pin that D9 governs, or a lint tool's version from scripts/lint.sh. The decisions index
matches the decision files, and each decision is linked, as
docs/writing-docs.md#how-the-docs-stay-correct says."""

import argparse
import ast
import contextlib
import copy
import inspect
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nexkit
from nexkit import (
    app,
    autopilot,
    checks,
    cli,
    config,
    context,
    github,
    publish,
    report,
    route,
    runtimes,
    scaffold,
    state,
)
from nexkit.engine import prompt, textutil
from tests.support import make_config, with_claude
from tests.test_cli import jobs_of, permissions_of

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+?)\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
AGENTS = ROOT / "AGENTS.md"
README = ROOT / "README.md"
VERSIONS_HEADER = "| Dependency | Supported | Tested |"
# A runner such as ubuntu-24.04, or a version such as 3.12, 2.1.323 or 3.12 or newer.
VERSION = re.compile(r"ubuntu-\d+\.\d+|\d+\.\d+(?:\.\d+)?(?: or newer)?")
ARCH = DOCS / "architecture.md"
HOW_IT_WORKS_PAGE = DOCS / "how-it-works.md"
TROUBLESHOOTING_PAGE = DOCS / "troubleshooting.md"
MENTIONS_HEADER = "| Outcome | `@owner a person is needed: …` | It also names |"
STAGES = DOCS / "stages.md"
SECURITY = DOCS / "security.md"
OUTCOMES_HEADER = "| Outcome | Meaning |"
COMMANDS_HEADER = "| Where | Comment | What happens |"
# The README's *Where* cells, by the place where route.decide reads the command.
PLACES = {"Issue": "issue", "NexKit PR": "pull request", "Your PR": "pull request"}
LOCAL_COMMANDS_HEADER = "| Command | What it does |"
COMMAND = re.compile(r"`nexkit ([\w-]+)`")
CONFIG_PAGE = DOCS / "configuration.md"
CHECKS_PAGE = DOCS / "checks.md"
# A check's field, as the bullets under "## Checks" of the checks page name it.
CHECK_FIELD = re.compile(r"^- `([a-z_]+)`:", re.M)
PERMISSIONS_HEADER = "| Permission | Level | For |"
# GitHub's names on an App's settings page, by the manifest's keys in nexkit/app.py.
PERMISSION_NAMES = {
    "Actions": "actions",
    "Checks": "checks",
    "Contents": "contents",
    "Issues": "issues",
    "Pull requests": "pull_requests",
    "Commit statuses": "statuses",
    "Workflows": "workflows",
    "Dependabot alerts": "vulnerability_alerts",
}
KEYS_HEADER = "| Key | Default | Meaning |"
TRIGGERS_HEADER = "| Trigger | Starts NexKit for |"
WORKFLOW_TEMPLATE = ROOT / "nexkit/templates/workflow.yml"
RUNTIMES_HEADER = "| Runtime | Providers | `effort` |"
LIMITS = re.compile(r"((?:`[\w-]+`(?:, | and )?)+) reports? usage limits")
DECISIONS = DOCS / "decisions"
DECISIONS_INDEX = DOCS / "decisions.md"
HOW_IT_WORKS = (
    "how-it-works.md",
    "stages.md",
    "work-without-a-command.md",
    "agents-and-profiles.md",
    "failure-handling.md",
)
TEST_FILE = re.compile(r"tests/[\w/]+\.py")
TEST_NAME = re.compile(r"`(test_\w+)`")
WORKFLOWS = ("pipeline.yml", "pipeline-agent.yml", "pipeline-review.yml")
JOBS_HEADER = "| Workflow | Job | Job token | App token | Secrets |"
MODULES_HEADER = "| Module | Purpose |"
RUN_JOBS_HEADER = "| Job | Runs for | Does |"
RUN_HEADING = re.compile(r"^## One run, (\w+) jobs$", re.M)
NUMBERS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)
DECISIONS_HEADER = "| Decision | Title | Status |"
WORD = re.compile(r"\S*\w\S*")
RULES = re.compile(r"^## Rules\n(.*?)(?=^## |\Z)", re.M | re.S)
DECISION_FILE = re.compile(r"d(\d+)-[a-z0-9-]+\.md")
DECISION_TITLE = re.compile(r"# D(\d+)\. (.+)$", re.M)
DECISION_STATUS = re.compile(r"^Status: \*\*(Agreed|Proposed)\*\*$", re.M)
DECISION_LINK = re.compile(r"\[D(\d+)\]\(decisions/([^)]+)\)")
SKILL_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.M)


def anchors(path):
    """The anchors GitHub gives the headings of `path`, outside code blocks."""
    found = set()
    for title in HEADING.findall(FENCE.sub("", path.read_text())):
        slug = re.sub(r"[^\w\- ]", "", title.lower().replace("`", "")).replace(" ", "-")
        found.add(slug)
    return found


def links(path):
    """The relative links of `path`, outside code blocks, as (target file, anchor or None)."""
    out = []
    for target in LINK.findall(FENCE.sub("", path.read_text())):
        if re.match(r"[a-z]+:", target):
            continue
        file, _, anchor = target.partition("#")
        out.append(((path.parent / file).resolve() if file else path, anchor or None))
    return out


def words(text):
    """The words of `text`: whitespace-separated tokens that hold a letter or a digit."""
    return len(WORD.findall(text))


def rules(path):
    """Each top-level bullet under `## Rules` of `path`, with its continuation lines."""
    section = RULES.search(path.read_text())
    lines = section.group(1).splitlines() if section else []
    bullets = []
    for line in lines:
        if line.startswith("- "):
            bullets.append(line)
        elif line.startswith("  ") and bullets:
            bullets[-1] += "\n" + line
    return bullets


class DocsMapTests(unittest.TestCase):
    def test_the_map_lists_every_doc(self):
        listed = {file for file, _ in links(DOCS / "README.md")}
        indexed = {file for file, _ in links(DECISIONS_INDEX)}
        for doc in sorted(DOCS.rglob("*.md")):
            if doc.name == "README.md":
                continue
            if doc.parent == DECISIONS:
                self.assertIn(
                    doc.resolve(), indexed, f"docs/decisions.md has no row for {doc.name}"
                )
            else:
                self.assertIn(doc.resolve(), listed, f"docs/README.md has no row for {doc.name}")

    def test_the_map_names_each_skill_of_the_plugin(self):
        paragraphs = re.split(r"\n\s*\n", (DOCS / "README.md").read_text())
        found = next((p for p in paragraphs if p.startswith("The plugin's skills")), None)
        self.assertTrue(found, "docs/README.md names no skills of the plugin")
        listed = set(re.findall(r"`([\w-]+)`", found))  # `skills/` has a slash, so it is left out
        defined = set()
        for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
            match = SKILL_NAME.search(skill.read_text())
            self.assertIsNotNone(match, f"{skill.relative_to(ROOT)} has no name: field")
            defined.add(match.group(1))
        for name in sorted(defined):
            self.assertIn(name, listed, f"docs/README.md does not list the skill {name}")
        for name in sorted(listed):
            self.assertIn(
                name,
                defined,
                f"docs/README.md lists the skill {name}, which skills/ does not define",
            )

    def test_the_links_of_the_docs_the_readme_agents_md_and_the_skills_resolve(self):
        pages = [
            *sorted(DOCS.rglob("*.md")),
            ROOT / "README.md",
            AGENTS,
            *sorted((ROOT / "skills").rglob("*.md")),
        ]
        for page in pages:
            where = page.relative_to(ROOT)
            for file, anchor in links(page):
                self.assertTrue(file.exists(), f"{where} links {file}, which does not exist")
                if anchor and file.suffix == ".md":
                    self.assertIn(anchor, anchors(file), f"{where} links #{anchor} of {file.name}")

    def test_each_link_to_how_it_works_and_its_pages_resolves(self):
        pages = {(DOCS / name).resolve() for name in HOW_IT_WORKS}
        sources = [
            *DOCS.rglob("*.md"),
            *(ROOT / "skills").rglob("*.md"),
            *(ROOT / ".claude" / "skills").rglob("*.md"),
            ROOT / "README.md",
            AGENTS,
        ]
        for source in sources:
            for file, anchor in links(source):
                if file not in pages:
                    continue
                self.assertTrue(file.exists(), f"{source.name} links {file}, which does not exist")
                if anchor:
                    self.assertIn(
                        anchor, anchors(file), f"{source.name} links #{anchor} of {file.name}"
                    )


class SecurityTests(unittest.TestCase):
    def test_each_test_the_page_names_exists_in_the_file_it_names(self):
        named = 0
        for line in FENCE.sub("", SECURITY.read_text()).splitlines():
            names = TEST_NAME.findall(line)
            if not names:
                continue
            named += len(names)
            files = TEST_FILE.findall(line)
            self.assertTrue(files, f"docs/security.md names a test and no file: {line[:80]!r}")
            sources = []
            for file in files:
                self.assertTrue((ROOT / file).is_file(), f"docs/security.md names {file}")
                sources.append((ROOT / file).read_text())
            for name in names:
                self.assertTrue(
                    any(f"def {name}(" in source for source in sources),
                    f"docs/security.md names {name}, which {', '.join(files)} does not define",
                )
        self.assertTrue(named, "docs/security.md names no test")

    def test_each_untrusted_input_names_a_rule_and_a_test(self):
        rows = table(SECURITY, "| Input |")
        self.assertTrue(rows, "docs/security.md has no table of untrusted inputs")
        found = anchors(SECURITY)
        for input, _reached, rule, tests in rows:
            self.assertTrue(
                any(a in found for a in re.findall(r"\]\(#([^)]+)\)", rule)),
                f"the rule of {input} links no section of docs/security.md",
            )
            self.assertTrue(TEST_NAME.search(tests), f"{input} names no test")


class AgentsTests(unittest.TestCase):
    def test_agents_md_holds_at_most_800_words(self):
        count = words(AGENTS.read_text())
        self.assertLessEqual(count, 800, f"AGENTS.md has {count} words; the limit is 800")

    def test_each_rule_holds_at_most_60_words(self):
        found = rules(AGENTS)
        self.assertTrue(found, "AGENTS.md has no rules under ## Rules")
        for rule in found:
            count = words(rule)
            self.assertLessEqual(
                count, 60, f"a rule of AGENTS.md has {count} words; the limit is 60: {rule[:60]!r}"
            )

    def test_each_rule_links_a_doc_section(self):
        for rule in rules(AGENTS):
            targets = LINK.findall(rule)
            self.assertTrue(
                any(target.startswith("docs/") and "#" in target for target in targets),
                f"a rule of AGENTS.md links no doc section: {rule[:60]!r}",
            )


class ReadmeVersionsTests(unittest.TestCase):
    def test_the_supported_versions_table_gives_the_pins_of_the_code(self):
        python = ".".join(map(str, nexkit.PYTHON))
        # The versions each row's Supported and Tested cells name; Claude Code links its pin.
        pins = {
            "Platform": ([], []),
            "Runner": ([nexkit.RUNNER], [nexkit.RUNNER]),
            "Claude Code": ([], []),
            "Python for NexKit in the pipeline": ([python], [python]),
            "Python for the `nexkit` CLI on your machine": ([f"{python} or newer"], [python]),
        }
        rows = {cells[0]: cells for cells in table(README, VERSIONS_HEADER)}
        self.assertTrue(rows, "README.md has no table of supported versions")
        for name in sorted(set(rows) | set(pins)):
            with self.subTest(row=name):
                self.assertIn(
                    name, pins, f"README.md has a row for {name}, which the test does not know"
                )
                self.assertIn(name, rows, f"README.md has no row for {name}")
                found = tuple(VERSION.findall(cell) for cell in rows[name][1:3])
                self.assertEqual(
                    found, pins[name], f"the versions of {name} in README.md differ from the pins"
                )
        claude = " ".join(rows.get("Claude Code", []))
        self.assertIn(
            "(nexkit/__init__.py)", claude, "the Claude Code row of README.md does not link its pin"
        )
        self.assertNotIn(
            nexkit.CLAUDE_CODE,
            claude,
            "the Claude Code row of README.md repeats the pinned version",
        )

    def test_every_runner_the_readme_names_is_the_pinned_runner(self):
        names = set(re.findall(r"ubuntu-\d+\.\d+", README.read_text()))
        self.assertEqual(
            names, {nexkit.RUNNER}, "README.md names a runner other than nexkit.RUNNER"
        )


class PinSourcesTests(unittest.TestCase):
    def test_no_doc_repeats_a_pinned_version(self):
        text = (ROOT / "scripts" / "lint.sh").read_text()
        lint = dict(re.findall(r"^(RUFF|ACTIONLINT)_VERSION=(\S+)$", text, re.M))
        self.assertEqual(
            set(lint),
            {"RUFF", "ACTIONLINT"},
            "scripts/lint.sh does not set each tool's version once",
        )
        pins = {runtime.version for runtime in runtimes.RUNTIMES.values()}
        pins |= {nexkit.PYTHON_IMAGE.split("@")[1], *lint.values()}
        self.assertNotIn("", pins)
        files = [AGENTS, README, *DOCS.rglob("*.md"), *(ROOT / "skills").rglob("*.md")]
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))):
                body = path.read_text()
                for pin in sorted(pins):
                    self.assertNotIn(
                        pin, body, f"{path.relative_to(ROOT)} repeats the pinned version {pin}"
                    )


def table(path, header):
    """The rows under the table line that starts with `header`, as lists of stripped cells."""
    lines = path.read_text().splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith(header)), len(lines))
    return rows_from(lines, start)


def tables(path, header):
    """The rows of every table whose header line is `header`, as lists of stripped cells."""
    lines = path.read_text().splitlines()
    rows = []
    for i, line in enumerate(lines):
        if line == header:
            rows += rows_from(lines, i + 1)
    return rows


def rows_from(lines, start):
    """The table rows from `lines[start]` on, as lists of stripped cells."""
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        if not re.fullmatch(r"[|\s:-]+", line):  # the row of dashes under the header
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def people_commands():
    """The subcommands of nexkit/cli.py whose help is not marked (pipeline), with their options."""
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    helps = {action.dest: action.help or "" for action in sub._choices_actions}
    return {
        name: {option for action in parser._actions for option in action.option_strings}
        for name, parser in sub.choices.items()
        if not helps[name].startswith("(pipeline)")
    }


def readme_configuration():
    """The configuration example of the README: its first ```json block after `## Configuration`,
    parsed."""
    text = README.read_text()
    start = text.index("```json\n", text.index("## Configuration")) + len("```json\n")
    return json.loads(text[start : text.index("\n```", start)])


class ReadmeTests(unittest.TestCase):
    def test_the_configuration_example_is_what_nexkit_init_writes(self):
        block = readme_configuration()
        settings = app.settings("nexkit-acme")
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(scaffold, "github_app", return_value=(settings, [])),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = cli.main(
                    [
                        "init",
                        "--repo",
                        tmp,
                        "--check",
                        "test=npm test",
                        "--check",
                        "lint=npm run lint",
                        "--setup",
                        "npm ci",
                    ]
                )
            written = json.loads(Path(tmp, config.CONFIG_PATH).read_text())
        self.assertEqual(status, 0)
        self.assertEqual(
            written,
            block,
            "README.md's configuration example differs from what nexkit init writes",
        )

    def test_the_local_commands_table_lists_each_command_for_people_and_no_other(self):
        rows = table(README, LOCAL_COMMANDS_HEADER)
        self.assertTrue(rows, "README.md has no table of local commands")
        commands = people_commands()
        listed = {}
        for row in rows:
            match = COMMAND.fullmatch(row[0])
            self.assertIsNotNone(
                match,
                f"README.md has the cell {row[0]}; each cell names only `nexkit NAME`, "
                "and `nexkit COMMAND --help` lists the options",
            )
            listed[match.group(1)] = row
        for name in sorted(commands):
            with self.subTest(command=name):
                self.assertIn(name, listed, f"README.md has no row for nexkit {name}")
        for name in sorted(listed):
            with self.subTest(row=name):
                self.assertIn(
                    name,
                    commands,
                    f"README.md lists nexkit {name}, which the CLI does not define for people",
                )
        for name, row in sorted(listed.items()):
            if name not in commands:
                continue
            for option in sorted(set(re.findall(r"--[\w-]+", " ".join(row)))):
                with self.subTest(command=name, option=option):
                    self.assertIn(
                        option,
                        commands[name],
                        f"README.md's row of nexkit {name} names {option}, which it lacks",
                    )


class OutcomesTests(unittest.TestCase):
    def test_the_outcome_table_lists_each_outcome_the_code_defines_and_no_other(self):
        code = (
            set(report.COMPLETED)
            | set(report.NEEDS_PERSON)
            | state.UNCOUNTED
            | autopilot.CHAINED
            | autopilot.UNCHANGED
            | {state.PAUSED}
        )
        rows = table(HOW_IT_WORKS_PAGE, OUTCOMES_HEADER)
        self.assertTrue(rows, "docs/how-it-works.md has no table of outcomes")
        listed = {name for row in rows for name in re.findall(r"`([a-z_]+)`", row[0])}
        for name in sorted(code):
            with self.subTest(outcome=name):
                self.assertIn(name, listed, f"docs/how-it-works.md has no row for outcome {name}")
        for name in sorted(listed):
            with self.subTest(row=name):
                self.assertIn(
                    name,
                    code,
                    f"docs/how-it-works.md lists {name}, which the code does not define",
                )


def mention_texts():
    """What the mention table's row for each outcome must say, from the code: the row's exact
    outcome cell, and the fragments its second cell must contain, which are the code's what to
    do and why. The reasons that hold values filled in at run time, such as a retry's count,
    are compared by their outcome name only."""
    cfg = make_config()
    texts = {
        f"`{outcome}`": (do, why[:1].upper() + why[1:])
        for outcome, (do, why) in report.NEEDS_PERSON.items()
    }
    texts |= {
        "plan with questions": (
            report.needs_person("planned", cfg, plan={"questions": ["Which?"]}).split(":\n\n")[0],
        ),
        "plan too large": (report.TOO_LARGE,),
        "plan finds no change": (report.NO_CHANGE_PLAN,),
        "a recommended close": (autopilot.CLOSE_UNFOUNDED,),
        "`ready`, with `merge.auto` off": (report.READY,),
        "`paused`, not resumed": (report.PAUSED_REASON,),
        "`pull_closed`, reopened": (autopilot.person(*autopilot.REOPENED),),
        "`agent_error`, out of time": (
            report.needs_person("agent_error", cfg, found={"timed_out": True}),
        ),
        "`review_failed`, cannot sign in": (
            report.needs_person("review_failed", cfg, found={"error": "401"}).rsplit(": `", 1)[0],
        ),
    }
    return texts


class MentionsTests(unittest.TestCase):
    def test_the_mention_table_lists_each_outcome_that_needs_a_person_and_no_other(self):
        rows = table(TROUBLESHOOTING_PAGE, MENTIONS_HEADER)
        self.assertTrue(rows, "docs/troubleshooting.md has no mention table")
        listed = {name for row in rows for name in re.findall(r"`([a-z_]+)`", row[0])}
        # `pull_closed` and `retry_waiting` are reasons that `nexkit status` and autopilot give,
        # which the table lists beside the outcomes `ready` and `paused`.
        code = set(report.NEEDS_PERSON) | {
            "ready",
            "paused",
            autopilot.PULL_CLOSED,
            autopilot.RETRY_WAITING,
        }
        for name in sorted(code):
            with self.subTest(outcome=name):
                self.assertIn(name, listed, f"docs/troubleshooting.md has no row for {name}")
        for name in sorted(listed):
            with self.subTest(row=name):
                self.assertIn(
                    name,
                    code,
                    f"docs/troubleshooting.md lists {name}, which needs no person",
                )

    def test_each_row_says_what_the_code_writes_in_the_mention(self):
        cells = {row[0]: row[1] for row in table(TROUBLESHOOTING_PAGE, MENTIONS_HEADER)}
        for label, fragments in mention_texts().items():
            with self.subTest(row=label):
                self.assertIn(label, cells, f"docs/troubleshooting.md has no mention row {label}")
                for fragment in fragments:
                    with self.subTest(row=label, text=fragment):
                        self.assertIn(
                            fragment,
                            cells[label],
                            f"the row {label} of docs/troubleshooting.md lacks the code's text",
                        )


class CommandsTests(unittest.TestCase):
    def test_the_readme_lists_each_command_the_code_accepts_and_no_other(self):
        code = {("issue", command) for command in (*route.ISSUE_COMMANDS, route.TRUST)}
        code |= {("pull request", command) for command in route.PR_COMMANDS}
        rows = table(README, COMMANDS_HEADER)
        self.assertTrue(rows, "README.md has no table of the comment commands")
        listed = set()
        for row in rows:
            commands = re.findall(r"`/nexkit ([a-z]+)", row[1])
            if not commands:
                continue
            with self.subTest(row=row[0]):
                self.assertIn(
                    row[0],
                    PLACES,
                    f"README.md has a command row under {row[0]}; add it to PLACES in "
                    "tests/test_docs.py",
                )
                listed |= {(PLACES[row[0]], command) for command in commands}
        for place, command in sorted(code):
            with self.subTest(place=place, command=command):
                self.assertIn(
                    (place, command),
                    listed,
                    f"README.md has no row for /nexkit {command} on the {place}",
                )
        for place, command in sorted(listed):
            with self.subTest(place=place, command=command):
                self.assertIn(
                    (place, command),
                    code,
                    f"README.md lists /nexkit {command} on the {place}, which "
                    "nexkit/route.py does not accept",
                )


class AppPermissionsTests(unittest.TestCase):
    def test_the_permissions_table_lists_each_permission_the_app_asks_for_at_its_level(self):
        rows = table(CONFIG_PAGE, PERMISSIONS_HEADER)
        self.assertTrue(rows, "docs/configuration.md has no table of the App's permissions")
        listed = {}
        for row in rows:
            with self.subTest(row=row[0]):
                self.assertIn(
                    row[0],
                    PERMISSION_NAMES,
                    f"docs/configuration.md lists {row[0]}; add it to PERMISSION_NAMES in "
                    "tests/test_docs.py",
                )
                key = PERMISSION_NAMES[row[0]]
                listed[key] = row[1]
                self.assertEqual(
                    "autopilot.dependabot" in row[2],
                    key in app.ALERTS_PERMISSION,
                    f"docs/configuration.md and nexkit/app.py disagree on autopilot.dependabot "
                    f"for {key}",
                )
        code = {**app.PERMISSIONS, **app.ALERTS_PERMISSION}
        for key in sorted(code):
            with self.subTest(permission=key):
                self.assertIn(key, listed, f"docs/configuration.md has no row for permission {key}")
                self.assertEqual(
                    listed[key],
                    code[key],
                    f"docs/configuration.md gives {key} as {listed[key]}, "
                    f"but nexkit/app.py asks for {code[key]}",
                )
        for key in sorted(listed):
            with self.subTest(row=key):
                self.assertIn(
                    key,
                    code,
                    f"docs/configuration.md lists {key}, which nexkit/app.py does not ask for",
                )


def complete_configuration():
    """The complete configuration of the configuration reference: its first ```json block
    after 'A complete configuration'."""
    text = CONFIG_PAGE.read_text()
    start = text.index("```json\n", text.index("A complete configuration")) + len("```json\n")
    return json.loads(text[start : text.index("\n```", start)])


def key_rows():
    """The rows of the three key tables of the configuration reference, by their key."""
    return {cells[0].strip("`"): cells for cells in tables(CONFIG_PAGE, KEYS_HEADER)}


def is_key(key):
    """Whether config.DEFAULTS defines `key`, top-level or `group.sub` of a group."""
    head, _, sub = key.partition(".")
    if head not in config.DEFAULTS:
        return False
    return not sub or (head in config.GROUPS and sub in config.DEFAULTS[head])


def recorded_ranges():
    """The (minimum, maximum) pairs that validate() checks, by the setting each one is for.

    validate() runs on the complete configuration, with a value in each list that may hold
    several. Checks have limits of their own, outside the tables, so the sample has none."""
    seen = {}
    real = config._positive_int

    def record(value, where, maximum, minimum=1):
        key = re.sub(r"\[\d+\].*", "", where.removeprefix("each wait in "))
        seen.setdefault(key, set()).add((minimum, maximum))
        return real(value, where, maximum, minimum)

    sample = complete_configuration()
    sample["checks"] = []
    sample["autopilot"]["max_parallel"] = 1
    sample["merge"]["after_workflows"] = [{"workflow": "ci.yml", "every": 1}]
    with mock.patch.object(config, "_positive_int", record):
        config.validate(sample)
    return seen


def workflow_triggers():
    """The triggers under `on:` of the workflow template, commented ones included: each key
    two spaces in, with or without a `#` before it."""
    lines = WORKFLOW_TEMPLATE.read_text().splitlines()
    start = lines.index("on:")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i][:1] not in ("", " ")), len(lines)
    )
    block = "\n".join(lines[start:end])
    return set(re.findall(r"^  (?:# )?([a-z_]+):\s*$", block, re.M))


class ConfigurationTests(unittest.TestCase):
    def test_the_runtime_table_lists_each_runtime_the_code_defines_and_no_other(self):
        rows = {cells[0].strip("`"): cells for cells in table(CONFIG_PAGE, RUNTIMES_HEADER)}
        self.assertTrue(rows, "docs/configuration.md has no runtime table")
        for name in sorted(set(rows) | set(runtimes.RUNTIMES)):
            with self.subTest(runtime=name):
                self.assertIn(
                    name,
                    runtimes.RUNTIMES,
                    f"docs/configuration.md has a row for {name}, which the code does not define",
                )
                self.assertIn(name, rows, f"docs/configuration.md has no row for runtime {name}")
                runtime, cells = runtimes.RUNTIMES[name], rows[name]
                self.assertEqual(
                    set(re.findall(r"`([^`]+)`", cells[1])),
                    set(runtime.providers),
                    f"the providers of {name} in docs/configuration.md differ from the code",
                )
                self.assertEqual(
                    re.findall(r"`([^`]+)`", cells[2]),
                    list(runtime.efforts),
                    f"the effort levels of {name} in docs/configuration.md differ from the code",
                )

    def test_the_page_names_each_runtime_that_reports_usage_limits(self):
        text = " ".join(CONFIG_PAGE.read_text().split())  # the sentence wraps across lines
        match = LIMITS.search(text)
        self.assertIsNotNone(
            match, "docs/configuration.md does not name the runtimes that report usage limits"
        )
        named = set(re.findall(r"`([\w-]+)`", match.group(1)))
        reporting = {name for name, runtime in runtimes.RUNTIMES.items() if runtime.reports_limits}
        self.assertEqual(
            named,
            reporting,
            "the runtimes that docs/configuration.md says report usage limits differ from the code",
        )

    def test_the_key_tables_list_each_key_the_code_defines_and_no_other(self):
        rows = key_rows()
        self.assertTrue(rows, "docs/configuration.md has no key tables")
        for key in sorted(rows):
            with self.subTest(row=key):
                self.assertTrue(
                    is_key(key),
                    f"docs/configuration.md has a row for {key}, which the code does not define",
                )
        listed = set(rows)
        for key, value in config.DEFAULTS.items():
            if key in listed:  # a table may list an object whole, as it lists `log`
                continue
            names = [f"{key}.{sub}" for sub in value] if key in config.GROUPS else [key]
            for name in names:
                with self.subTest(key=name):
                    self.assertIn(name, listed, f"docs/configuration.md has no row for {name}")

    def test_each_key_row_gives_the_code_default_and_range(self):
        seen = recorded_ranges()
        rows = key_rows()
        for key, cells in sorted(rows.items()):
            if not is_key(key):  # the test above names it
                continue
            with self.subTest(row=key):
                code = config.setting(config.DEFAULTS, key)
                span = re.fullmatch(r"`([^`]+)`", cells[1])
                if span:
                    self.assertEqual(
                        json.loads(span.group(1)),
                        code,
                        f"the default of {key} in docs/configuration.md differs from the code",
                    )
                else:  # `agents`, `stages` and `github_app` have no single default
                    self.assertIsInstance(code, dict, f"the default of {key} is not an object")
                found = {(int(a), int(b)) for a, b in re.findall(r"(\d+) to (\d+)", cells[2])}
                self.assertEqual(
                    found,
                    seen.get(key, set()),
                    f"the range of {key} in docs/configuration.md differs from validate()",
                )
        retry = rows["retry.wait_minutes"][2]
        self.assertIn(
            f"at most {config.MAX_RETRIES} waits",
            retry,
            "the retry.wait_minutes row differs from validate()",
        )

    def test_the_complete_configuration_lists_each_key_the_code_defines_and_no_other(self):
        block = complete_configuration()
        sections = [("", config.DEFAULTS, block)]
        sections += [(f"{g}.", config.DEFAULTS[g], block.get(g, {})) for g in config.GROUPS]
        for where, defined, listed in sections:
            for key in sorted(set(defined) | set(listed)):
                with self.subTest(key=where + key):
                    if key in defined:
                        self.assertIn(
                            key, listed, f"the complete configuration has no {where}{key}"
                        )
                    else:
                        self.fail(
                            f"the complete configuration lists {where}{key}, "
                            "which the code does not define"
                        )

    def test_the_complete_configuration_holds_each_default_it_says_it_does(self):
        block = complete_configuration()
        full = config.validate(copy.deepcopy(block))
        # `agents` and `github_app` are the block's own, as `nexkit init` writes them.
        base = config.validate({"agents": block["agents"], "github_app": block["github_app"]})
        for key in config.DEFAULTS:
            if key in ("setup", "checks"):  # `nexkit init` writes these too
                continue
            with self.subTest(key=key):
                self.assertEqual(
                    full[key],
                    base[key],
                    f"the complete configuration sets {key} to something but its default",
                )

    def test_the_workflow_file_table_lists_each_trigger_of_the_template(self):
        listed = {cells[0].strip("`") for cells in table(CONFIG_PAGE, TRIGGERS_HEADER)}
        self.assertTrue(listed, "docs/configuration.md has no trigger table under Workflow file")
        for trigger in sorted(listed - workflow_triggers()):
            with self.subTest(trigger=trigger):
                self.fail(
                    f"the trigger table of docs/configuration.md lists {trigger}, "
                    "which nexkit/templates/workflow.yml lacks"
                )
        for trigger in sorted(workflow_triggers() - listed):
            with self.subTest(trigger=trigger):
                self.fail(f"the trigger table of docs/configuration.md has no row for {trigger}")


REFUSALS_LEAD = "- NexKit refuses, and `nexkit doctor` says why:"
# Each refusal of a check's `unaffected_by` as docs/checks.md words it, a fragment of the code's
# message, and a list of patterns that the code refuses with that message.
REFUSALS = [
    ("a pattern that is empty", "is empty", [""]),
    ("a pattern that is only `!`", "is only `!`", ["!"]),
    ("a pattern that starts with `/`", "starts with `/`", ["/docs/**"]),
    ("a pattern that ends with `/`", "ends with `/`", ["docs/"]),
    ("`**` inside a segment", "inside a segment", ["docs/a**"]),
    ("an empty, `.` or `..` segment", "empty, `.` or `..` segment", ["docs//a"]),
    ("a `[` without its `]`", "without its `]`", ["a[bc"]),
    ("a `]` without its `[`", "without its `[`", ["a]"]),
    ("an empty `[]`", "empty `[]`", ["a[]"]),
    ("a range that runs backwards, such as `[z-a]`", "is not a valid glob", ["[z-a]"]),
    ("a pattern longer than 200 characters", "at most 200 characters", ["a" * 201]),
    ("a list whose first pattern starts with `!`", "starts with `!` but is first", ["!docs/**"]),
    ("a list that names a pattern twice", "lists a pattern twice", ["docs/**", "docs/**"]),
    (
        "a list of more than 50 patterns",
        "must be a list of at most",
        [f"d{i}/**" for i in range(config.MAX_UNAFFECTED + 1)],
    ),
]


def refusal_entries():
    """The entries under the refusal lead-in of docs/checks.md, one nested line each."""
    lines = CHECKS_PAGE.read_text().splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line == REFUSALS_LEAD), len(lines))
    entries = []
    for line in lines[start:]:
        if not line.startswith("  - "):
            break
        entries.append(line[4:])
    return entries


def fixed_text(node):
    """The text of a message argument: a string, or an f-string with `{}` for each value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def refusal_messages():
    """The messages of the refusals the code makes: the first argument of each GlobError in
    nexkit/globs.py and each _fail in config._unaffected_by. A re-raise such as "'{}' {}" holds
    no text of its own, so it is left out."""
    sources = [
        (ast.parse((ROOT / "nexkit/globs.py").read_text()), "GlobError"),
        (ast.parse(inspect.getsource(config._unaffected_by)), "_fail"),
    ]
    found = []
    for tree, name in sources:
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
                and node.args
            ):
                continue
            text = fixed_text(node.args[0])
            if text is None:  # not a string the test can read: it names the expression
                found.append(ast.unparse(node.args[0]))
            elif text.replace("{}", "").strip(" '\":"):
                found.append(text)
    return found


class UnaffectedByTests(unittest.TestCase):
    def test_the_page_lists_each_refusal_of_the_code_and_no_other(self):
        entries = refusal_entries()
        self.assertTrue(entries, f"docs/checks.md has no list under {REFUSALS_LEAD!r}")
        tabled = [entry for entry, _, _ in REFUSALS]
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertIn(
                    entry, tabled, f"docs/checks.md lists {entry}, which the code does not refuse"
                )
        for entry in tabled:
            with self.subTest(entry=entry):
                self.assertIn(entry, entries, f"docs/checks.md has no entry for {entry}")
        for message in refusal_messages():
            with self.subTest(message=message):
                self.assertTrue(
                    any(fragment in message for _, fragment, _ in REFUSALS),
                    f'the code refuses "{message}", which docs/checks.md does not list',
                )

    def test_the_code_refuses_each_listed_pattern(self):
        for entry, fragment, patterns in REFUSALS:
            with self.subTest(entry=entry):
                raw = {"checks": [{"name": "e2e", "run": "x", "unaffected_by": patterns}]}
                with self.assertRaisesRegex(config.ConfigError, re.escape(fragment)):
                    config.validate(with_claude(raw))


class ChecksPageTests(unittest.TestCase):
    def test_the_field_list_names_each_check_key_the_code_defines_and_no_other(self):
        section = CHECKS_PAGE.read_text().split("\n## Checks\n", 1)[1].split("\n### ", 1)[0]
        listed = set(CHECK_FIELD.findall(section))
        self.assertTrue(listed, "docs/checks.md has no list of a check's fields")
        for key in sorted(config.CHECK_KEYS - listed):
            with self.subTest(key=key):
                self.fail(f"docs/checks.md does not list the check field {key}")
        for key in sorted(listed - config.CHECK_KEYS):
            with self.subTest(key=key):
                self.fail(f"docs/checks.md lists {key}, which CHECK_KEYS does not define")


class ArchitectureTests(unittest.TestCase):
    def test_the_page_has_a_section_for_each_part(self):
        for part in (
            "the-package",
            "the-workflows-and-their-jobs",
            "tokens-and-secrets",
            "state",
            "the-engine-and-its-hosts",
            "the-plugin",
        ):
            self.assertIn(part, anchors(ARCH), f"docs/architecture.md has no section {part}")

    def test_each_job_has_a_row_with_its_tokens_and_secrets(self):
        rows = {(row[0], row[1]): row for row in table(ARCH, JOBS_HEADER)}
        self.assertTrue(rows, "docs/architecture.md has no job table")
        jobs = set()
        for file in WORKFLOWS:
            text = (ROOT / ".github/workflows" / file).read_text()
            for name, job in jobs_of(text).items():
                key = (f"`{file}`", f"`{name}`")
                jobs.add(key)
                self.assertIn(key, rows, f"docs/architecture.md has no row for {file} job {name}")
                row = rows[key]
                self.assertEqual(
                    dict(re.findall(r"([\w-]+): (read|write)", row[2])),
                    permissions_of(job),
                    f"the job token of {file} job {name} differs from its workflow",
                )
                self.assertEqual(
                    dict(re.findall(r"([\w-]+): (read|write)", row[3])),
                    dict(re.findall(r"^\s+permission-([\w-]+): (\w+)", job, re.M)),
                    f"the App token of {file} job {name} differs from its workflow",
                )
                self.assertEqual(
                    row[4] == "none",
                    re.search(r"secrets[.\[]", job) is None,
                    f"the secrets of {file} job {name} differ from its workflow",
                )
        for key in rows:
            self.assertIn(
                key, jobs, f"docs/architecture.md has a row for {key[0]} {key[1]}, no job"
            )

    def test_each_module_has_a_row(self):
        firsts = [row[0] for row in table(ARCH, MODULES_HEADER)]
        self.assertTrue(firsts, "docs/architecture.md has no module table")
        for p in sorted((ROOT / "nexkit").iterdir()):
            if p.is_dir() and p.name != "__pycache__":
                name = f"{p.name}/"
            elif p.suffix == ".py":
                name = p.name
            else:
                continue
            self.assertTrue(
                any(f"`{name}`" in cell for cell in firsts),
                f"docs/architecture.md has no row for nexkit/{name}",
            )


class HowItWorksTests(unittest.TestCase):
    def test_the_run_table_has_a_row_for_each_job_of_the_pipeline(self):
        rows = {row[0]: row for row in table(HOW_IT_WORKS_PAGE, RUN_JOBS_HEADER)}
        self.assertTrue(rows, "docs/how-it-works.md has no job table")
        jobs = jobs_of((ROOT / ".github/workflows/pipeline.yml").read_text())
        for name, job in sorted(jobs.items()):
            self.assertIn(
                f"`{name}`", rows, f"docs/how-it-works.md has no row for pipeline.yml job {name}"
            )
            named = dict(re.findall(r"`([\w-]+): (read|write)`", rows[f"`{name}`"][2]))
            for permission, level in named.items():
                self.assertEqual(
                    level,
                    permissions_of(job).get(permission),
                    f"the row of {name} in docs/how-it-works.md differs from its workflow "
                    f"on {permission}",
                )
        for key in rows:
            self.assertIn(
                key.strip("`"),
                jobs,
                f"docs/how-it-works.md has a row for {key}, which pipeline.yml does not define",
            )
        match = RUN_HEADING.search(HOW_IT_WORKS_PAGE.read_text())
        self.assertIsNotNone(match, "docs/how-it-works.md has no heading 'One run, <number> jobs'")
        self.assertEqual(
            match.group(1),
            NUMBERS[len(jobs)],
            f"the heading of docs/how-it-works.md says {match.group(1)} jobs; "
            f"pipeline.yml has {len(jobs)}",
        )


def decision(path):
    """The number, title and status of the decision file `path`, or None if a line is missing."""
    text = path.read_text()
    title = DECISION_TITLE.match(text)
    status = DECISION_STATUS.search(text)
    if not title or not status:
        return None
    return title.group(1), title.group(2), status.group(1)


class DecisionsTests(unittest.TestCase):
    def test_each_decision_file_follows_the_template(self):
        files = sorted(DECISIONS.glob("*.md"))
        self.assertTrue(files, "docs/decisions/ has no decision")
        for file in files:
            match = DECISION_FILE.fullmatch(file.name)
            self.assertIsNotNone(match, f"docs/decisions/{file.name} is not d<n>-<slug>.md")
            text = file.read_text()
            title = DECISION_TITLE.match(text)
            self.assertIsNotNone(title, f"{file.name} has no title '# D<n>. <title>'")
            self.assertEqual(title.group(1), match.group(1), f"{file.name} has another number")
            self.assertRegex(text, DECISION_STATUS, f"{file.name} has no status Agreed or Proposed")
            for heading in ("Context", "Decision", "Consequences"):
                section = re.compile(rf"^## {heading}$", re.M)
                self.assertRegex(text, section, f"{file.name} has no {heading} section")

    def test_the_index_has_one_row_for_each_decision_file(self):
        rows = table(DECISIONS_INDEX, DECISIONS_HEADER)
        self.assertTrue(rows, "docs/decisions.md has no table of decisions")
        listed = set()
        for cells in rows:
            link = DECISION_LINK.fullmatch(cells[0]) if len(cells) == 3 else None
            self.assertIsNotNone(link, f"a row of docs/decisions.md links no decision: {cells}")
            number, name = link.groups()
            listed.add(name)
            if cells[2].startswith("Superseded by"):
                continue
            file = DECISIONS / name
            self.assertTrue(file.exists(), f"docs/decisions.md links {name}, which does not exist")
            self.assertEqual(
                decision(file),
                (number, cells[1], cells[2]),
                f"the row of docs/decisions.md for {name} differs from the file",
            )
        for file in sorted(DECISIONS.glob("*.md")):
            self.assertIn(file.name, listed, f"docs/decisions.md has no row for {file.name}")

    def test_each_decision_is_linked_from_a_rule_or_a_page(self):
        linked = set()
        for page in (AGENTS, *DOCS.rglob("*.md")):
            if page != DECISIONS_INDEX:
                linked.update(file for file, _ in links(page))
        for file in sorted(DECISIONS.glob("*.md")):
            self.assertIn(
                file.resolve(), linked, f"no rule of AGENTS.md and no page links {file.name}"
            )


class MaintenanceSkillsTests(unittest.TestCase):
    SKILLS = ROOT / ".claude" / "skills"
    MAINTAIN = SKILLS / "nexkit-maintain"

    def test_the_links_of_the_maintenance_skills_resolve(self):
        pages = sorted(self.SKILLS.rglob("*.md"))
        self.assertTrue(pages, "there is no maintenance skill under .claude/skills/")
        for page in pages:
            for file, anchor in links(page):
                where = page.relative_to(ROOT)
                self.assertTrue(file.exists(), f"{where} links {file}, which does not exist")
                if anchor and file.suffix == ".md":
                    self.assertIn(anchor, anchors(file), f"{where} links #{anchor} of {file.name}")

    def test_each_task_of_nexkit_maintain_links_a_delivery_section(self):
        tasks = sorted(
            file
            for file, _ in links(self.MAINTAIN / "SKILL.md")
            if file.suffix == ".md" and file.parent == self.MAINTAIN.resolve()
        )
        self.assertEqual(len(tasks), 6, "SKILL.md must link the six task files beside it")
        delivery = anchors(DOCS / "delivery.md")
        for task in tasks:
            sections = [
                anchor
                for file, anchor in links(task)
                if file == (DOCS / "delivery.md").resolve() and anchor in delivery
            ]
            self.assertTrue(sections, f"{task.name} links no section of docs/delivery.md")


def block_after(path, marker):
    """The first ```markdown block of `path` after the text `marker`, without its fences."""
    text = path.read_text()
    opening = text.index("```markdown\n", text.index(marker)) + len("```markdown\n")
    return text[opening : text.index("\n```", opening)]


def parts(text):
    """The bold parts that start a line of `text`, except **Checks**, which a review writes last."""
    return [part for part in re.findall(r"^\*\*([^*]+)\*\*", text, re.M) if part != "Checks"]


def restart_delays():
    """The restart delays of a command that did not run, as the pages word them: 30, 60 and 120
    minutes."""
    minutes = [int(delay.total_seconds() // 60) for delay in autopilot.AGAIN[1:]]
    return ", ".join(str(m) for m in minutes[:-1]) + f" and {minutes[-1]} minutes"


def full_plan():
    """A plan that fills every field its layout shows, as a re-plan that split its issue."""
    return {
        "summary": "add() returns the sum of its arguments.",
        "changes": ["add(a, b) returns a + b."],
        "decisions": [{"decision": "add() keeps its name.", "reason": "Callers use it today."}],
        "risks": ["Floats are out of scope."],
        "acceptance_criteria": ["add(2, 3) returns 5."],
        "checklist": [
            {"number": 1, "item": "Docs change with the code", "how": "It updates the page."}
        ],
        "questions": [
            {"question": "Which name?", "options": ["Keep add.", "Rename it."], "recommended": 1}
        ],
        "no_change": "The host answered 409, and a retry went through.",
        "protected_paths": [".github/workflows/ci.yml"],
        "too_large": True,
        "split": [{"title": "Add the sum", "body": "Changes add().", "depends_on": []}],
        "implementation_notes": ["Edit `calc.py`: `return a + b`."],
        "revision": {"started_over": False, "changes": ["Added a question."]},
    }


def full_review():
    """A review that fills every part its layout shows."""
    return {
        "status": "done",
        "output": {
            "verdict": "request_changes",
            "summary": "One finding is blocking.",
            "criteria": [
                {"criterion": "add works", "status": "met", "test": "test_add", "evidence": "e"},
                {"criterion": "floats add", "status": "unmet", "test": "", "evidence": "No test."},
            ],
            "findings": [
                {"severity": "blocking", "file": "calc.py", "line": 2, "body": "Floats are cut."}
            ],
            "previous_findings": [
                {"finding": "Add a docstring.", "resolution": "resolved", "evidence": "calc.py:1"},
                {"finding": "Keep the name.", "resolution": "unresolved", "severity": "blocking"},
            ],
            "checklist": [
                {"item": "Docs change", "answer": "met", "evidence": "docs/calc.md"},
                {"item": "No dependency", "answer": "not_met", "person": True, "evidence": "e"},
            ],
        },
    }


VALUES = (
    ("agents-and-profiles.md", f"latest {prompt.MAX_COMMENTS} comments"),
    ("agents-and-profiles.md", f"{prompt.BLOCK_LIMIT:,} characters"),
    ("agents-and-profiles.md", f"first {prompt.SHORT:,} characters"),
    (
        "agents-and-profiles.md",
        f"at most {context.MAX_LINKED} linked comments and "
        f"{context.MAX_LINKED_ISSUES} linked issues",
    ),
    ("agents-and-profiles.md", f"up to {context.MAX_SKIPPED} of them"),
    ("agents-and-profiles.md", f"last {textutil.OUTPUT_TAIL:,}"),
    ("agents-and-profiles.md", f"last {prompt.FIX_OUTPUT_TAIL:,} characters"),
    ("agents-and-profiles.md", f"last {prompt.REVIEW_OUTPUT_TAIL:,}"),
    ("agents-and-profiles.md", f"one of {runtimes.SECRET_SLOTS} slots"),
    ("agents-and-profiles.md", f"at most {config.MAX_STAGE_MINUTES} minutes"),
    ("how-it-works.md", f"last {textutil.OUTPUT_TAIL:,} characters"),
    ("how-it-works.md", f"at most {checks.RESULTS_LIMIT // 1_000_000} MB per round"),
    ("how-it-works.md", f"{checks.JOB_RESULTS_FLOOR // 1_000_000} MB per job"),
    ("work-without-a-command.md", f"last {github.DOWNLOAD_LIMIT // 1_000_000} MB"),
    ("work-without-a-command.md", f"at most {NUMBERS[context.MAX_EARLIER]} of those parts"),
    ("work-without-a-command.md", f"first {NUMBERS[context.MAX_EARLIER * 2]} blockers"),
    ("work-without-a-command.md", f"at most {autopilot.ISSUES_LIMIT:,} of each"),
    ("work-without-a-command.md", f"{restart_delays()} after its last start"),
    ("failure-handling.md", f"{restart_delays()} after its last start"),
)


class LayoutTests(unittest.TestCase):
    def test_the_plan_layout_lists_the_sections_the_code_writes(self):
        block = block_after(STAGES, "**Plan.**")
        listed = [
            re.split(r"\s{2,}", line[4:])[0]
            for line in block.splitlines()
            if line.startswith("### ")
        ]
        written = [
            line[4:]
            for line in publish.render_plan(full_plan(), replan=True).splitlines()
            if line.startswith("### ")
        ]
        self.assertEqual(
            listed,
            written,
            f"docs/stages.md says the plan's sections are {listed}; "
            f"publish.render_plan writes {written}",
        )

    def test_the_review_layout_lists_the_parts_the_code_writes(self):
        listed = parts(block_after(STAGES, "The pull request review starts with"))
        body = report.review_body(full_review(), [{"name": "test", "passed": True}])
        written = parts(body.split("<details>", 1)[0])  # the evidence repeats some parts
        self.assertEqual(
            listed,
            written,
            f"docs/stages.md says the review's parts are {listed}; "
            f"report.review_body writes {written}",
        )


class ValuesTests(unittest.TestCase):
    def test_each_value_a_page_repeats_is_its_constant(self):
        for name, phrase in VALUES:
            with self.subTest(page=name, phrase=phrase):
                # Lines are wrapped, so the page is compared with its spaces made one each.
                text = " ".join((DOCS / name).read_text().split())
                self.assertIn(
                    phrase, text, f"docs/{name} does not say {phrase!r}, the code's value"
                )


if __name__ == "__main__":
    unittest.main()

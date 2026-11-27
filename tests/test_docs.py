"""The checks of the docs, one test each, are those that rule 9 of
docs/writing-docs.md#how-the-docs-stay-correct lists. ChecksListTests compares that list with
the test methods of this file."""

import argparse
import ast
import contextlib
import copy
import importlib.util
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
    host,
    maintain,
    progress,
    publish,
    redact,
    report,
    route,
    runlog,
    runtimes,
    scaffold,
    state,
    status,
)
from nexkit.engine import machine, prompt, textutil
from nexkit.runtimes import claude_code
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
RELEASE_FILES = re.compile(
    r"^- A release pull request sets the version in (.+?),\s+and adds", re.M | re.S
)
# A runner such as ubuntu-24.04, or a version such as 3.12, 2.1.323 or 3.12 or newer.
VERSION = re.compile(r"ubuntu-\d+\.\d+|\d+\.\d+(?:\.\d+)?(?: or newer)?")
ARCH = DOCS / "architecture.md"
HOW_IT_WORKS_PAGE = DOCS / "how-it-works.md"
TROUBLESHOOTING_PAGE = DOCS / "troubleshooting.md"
MENTIONS_HEADER = "| Outcome | `@owner a person is needed: …` | It also names |"
STAGES = DOCS / "stages.md"
SECURITY = DOCS / "security.md"
OUTCOMES_HEADER = "| Outcome | Meaning |"
DELIVERY = DOCS / "delivery.md"
LABELS_HEADER = "| Label | Kind |"
# The labels of a kind, from the bullet of the delivery page that names them.
KIND_LABELS = re.compile(r"label of its kind \(((?:`[\w-]+`(?:, | or )?)+)\)")
COMMANDS_HEADER = "| Where | Comment | What happens |"
# The README's *Where* cells, by the place where route.decide reads the command.
PLACES = {"Issue": "issue", "NexKit PR": "pull request", "Your PR": "pull request"}
LOCAL_COMMANDS_HEADER = "| Command | What it does |"
COMMAND = re.compile(r"`nexkit ([\w-]+)`")
CONFIG_PAGE = DOCS / "configuration.md"
CHECKS_PAGE = DOCS / "checks.md"
AUTOPILOT_PAGE = DOCS / "autopilot.md"
# A field, as the bullets of a field list name it: a check's on the checks page, a cache entry's
# in the configuration reference.
FIELD = re.compile(r"^- `([a-z_]+)`:", re.M)
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
STAGE_KEYS_HEADER = "| Key | Meaning |"
RENAMED_HEADER = "| Before | Now |"
TRIGGERS_HEADER = "| Trigger | Starts NexKit for |"
WORKFLOW_TEMPLATE = ROOT / "nexkit/templates/workflow.yml"
# Step 5 of the App upgrade: the workflow permissions as names, then their one level.
UPGRADE_PERMISSIONS = re.compile(
    r"`permissions:` may shrink to (.+?), all `(\w+)`, as `nexkit init` writes it", re.S
)
RUNTIMES_HEADER = "| Runtime | Providers | `effort` |"
PROVIDERS_HEADER = "| Provider | Settings | Secret |"
# A field's name in a cell of the provider table, in backticks: `none` and `claude setup-token`
# have no such name.
FIELD_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")
LIMITS = re.compile(r"((?:`[\w-]+`(?:, | and )?)+) reports? usage limits")
# Run on text with its whitespace collapsed, since the sentence wraps across lines.
ONE_BY_ONE = re.compile(r"lists secrets one by one can pass only ((?:`\w+`(?:, | and )?)+)")
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
PLUGIN_SECTION = re.compile(r"^## The plugin\n(.*?)(?=^## |\Z)", re.M | re.S)
SKILL_BULLET = re.compile(r"^- \[`([\w-]+)`\]\(\.\./skills/([\w-]+)/SKILL\.md\)", re.M)
DECISION_FILE = re.compile(r"d(\d+)-[a-z0-9-]+\.md")
DECISION_TITLE = re.compile(r"# D(\d+)\. (.+)$", re.M)
DECISION_STATUS = re.compile(r"^Status: \*\*(Agreed|Proposed)\*\*$", re.M)
DECISION_LINK = re.compile(r"\[D(\d+)\]\(decisions/([^)]+)\)")
SKILL_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.M)
AUTOPILOT_PAGE = DOCS / "autopilot.md"
CI_TITLES_HEADER = "| Failure | Title |"
WRITING_DOCS = DOCS / "writing-docs.md"
# Rule 9 of how the docs stay correct, with its indented lines, up to the next unindented line.
RULE_9 = re.compile(r"^9\. \*\*Checks catch what a machine can\.\*\*(.*?)(?=^\S|\Z)", re.M | re.S)


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


def docs_tests():
    """The test methods that the classes of tests/test_docs.py define."""
    tree = ast.parse(Path(__file__).read_text())
    return {
        f.name
        for c in tree.body
        if isinstance(c, ast.ClassDef)
        for f in c.body
        if isinstance(f, ast.FunctionDef) and f.name.startswith("test_")
    }


class ChecksListTests(unittest.TestCase):
    def test_rule_9_lists_each_check_of_this_file_and_no_other(self):
        match = RULE_9.search(WRITING_DOCS.read_text())
        self.assertIsNotNone(match, "docs/writing-docs.md has no rule 9 that lists the checks")
        listed = TEST_NAME.findall(match.group(1))
        self.assertTrue(listed, "docs/writing-docs.md rule 9 lists no check")
        for name in sorted({n for n in listed if listed.count(n) > 1}):
            with self.subTest(test=name):
                self.fail(f"docs/writing-docs.md rule 9 lists {name} more than once")
        defined = docs_tests()
        for name in sorted(defined - set(listed)):
            with self.subTest(test=name):
                self.fail(f"docs/writing-docs.md rule 9 does not list {name}")
        for name in sorted(set(listed) - defined):
            with self.subTest(test=name):
                self.fail(
                    f"docs/writing-docs.md rule 9 lists {name}, "
                    "which tests/test_docs.py does not define"
                )


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
        defined = plugin_skills(self)
        for name in sorted(defined):
            self.assertIn(name, listed, f"docs/README.md does not list the skill {name}")
        for name in sorted(listed):
            self.assertIn(
                name,
                defined,
                f"docs/README.md lists the skill {name}, which skills/ does not define",
            )

    def test_the_architecture_page_names_each_skill_of_the_plugin(self):
        section = PLUGIN_SECTION.search(ARCH.read_text())
        self.assertTrue(section, "docs/architecture.md has no section The plugin")
        bullets = SKILL_BULLET.findall(section.group(1))
        self.assertTrue(bullets, "docs/architecture.md names no skills of the plugin")
        listed = set()
        for name, folder in bullets:
            self.assertEqual(
                name, folder, f"docs/architecture.md links the skill {name} to {folder}"
            )
            listed.add(name)
        defined = plugin_skills(self)
        for name in sorted(defined):
            self.assertIn(name, listed, f"docs/architecture.md does not list the skill {name}")
        for name in sorted(listed):
            self.assertIn(
                name,
                defined,
                f"docs/architecture.md lists the skill {name}, which skills/ does not define",
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


def release_files():
    """The files that the Releases bullet of the delivery page lists as holding the version."""
    found = RELEASE_FILES.search((DOCS / "delivery.md").read_text())
    assert found, (
        "docs/delivery.md has no Releases bullet that lists where a release sets the version "
        "(the test reads the bullet by its opening words, so change it with the bullet)"
    )
    text = " ".join(found.group(1).split())
    files = {t for t in re.findall(r"`([^`]+)`", text) if (ROOT / t).is_file()}
    if "README's `pipx install` line" in text:
        files.add("README.md")
    return files


def files_with_version():
    """The files a release ships that hold nexkit.__version__."""
    pattern = re.compile(rf"(?<![\d.]){re.escape(nexkit.__version__)}(?![\d.])")
    paths = [ROOT / "pyproject.toml", README]
    for top in ("nexkit", ".claude-plugin", "skills"):
        paths.extend(sorted((ROOT / top).rglob("*")))
    found = set()
    for path in paths:
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pattern.search(body):
            found.add(path.relative_to(ROOT).as_posix())
    return found


class ReleaseVersionTests(unittest.TestCase):
    def test_the_release_list_names_each_file_that_holds_the_version(self):
        listed, found = release_files(), files_with_version()
        version = nexkit.__version__
        for name in sorted(listed | found):
            with self.subTest(file=name):
                self.assertIn(
                    name,
                    found,
                    f"docs/delivery.md lists {name}, which does not hold version {version}",
                )
                self.assertIn(
                    name,
                    listed,
                    f"{name} holds version {version}, "
                    "which docs/delivery.md#releases does not list",
                )

    def test_the_readme_installs_the_tag_of_the_version(self):
        lines = [line for line in README.read_text().splitlines() if "pipx install git+" in line]
        self.assertEqual(len(lines), 1, "README.md needs one `pipx install git+` line")
        self.assertRegex(
            lines[0],
            rf"@v{re.escape(nexkit.__version__)}(?![\d.])",
            "the pipx install line of README.md does not install the tag of nexkit.__version__",
        )
        example = ROOT / "nexkit/protocol/v1/examples/calls/describe.answer.json"
        self.assertEqual(json.loads(example.read_text())["engine"], nexkit.__version__)


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


def indented_table(path, header):
    """The rows of the table in a list item, whose header line is `header` once stripped."""
    lines = [line.strip() for line in path.read_text().splitlines()]
    start = next((i + 1 for i, line in enumerate(lines) if line == header), len(lines))
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

    def test_each_nexkit_option_the_readme_names_exists(self):
        commands = people_commands()
        text = README.read_text()
        # A code span such as `nexkit init --app-id ID`, and a line of a code block.
        spans = re.findall(r"`nexkit ([\w-]+)([^`]*)`", text) + re.findall(
            r"^nexkit ([\w-]+)(.*)$", text, re.M
        )
        checked = []
        for name, rest in spans:
            if name not in commands:  # a placeholder such as `nexkit COMMAND --help`
                continue
            for option in sorted(set(re.findall(r"--[\w-]+", rest))):
                checked.append((name, option))
                with self.subTest(command=name, option=option):
                    self.assertIn(
                        option,
                        commands[name],
                        f"README.md names nexkit {name} {option}, which it lacks",
                    )
        self.assertTrue(checked, "README.md names no option of a nexkit command")


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


# Loaded as tests/test_docs_audit.py loads it, since that module imports this one.
spec = importlib.util.spec_from_file_location("docs_audit", ROOT / "scripts" / "docs_audit.py")
docs_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs_audit)


class DeliveryLabelsTests(unittest.TestCase):
    def test_the_label_table_lists_each_label_the_code_creates_and_no_other(self):
        code = {autopilot.CI_LABEL, autopilot.FLAKY_LABEL, autopilot.DEPENDABOT_LABEL}
        code.add(docs_audit.LABEL)
        # The table is indented in a list item, so its lines are stripped first.
        lines = [line.strip() for line in DELIVERY.read_text().splitlines()]
        start = next(
            (i + 1 for i, line in enumerate(lines) if line.startswith(LABELS_HEADER)),
            len(lines),
        )
        rows = rows_from(lines, start)
        self.assertTrue(rows, "docs/delivery.md has no table of labels")
        bullet = KIND_LABELS.search(" ".join(DELIVERY.read_text().split()))
        self.assertIsNotNone(
            bullet, "docs/delivery.md has no bullet that names the labels of a kind"
        )
        kinds = set(re.findall(r"`([\w-]+)`", bullet.group(1)))
        self.assertTrue(kinds, "docs/delivery.md's bullet names no labels of a kind")
        listed = {name for row in rows for name in re.findall(r"`([\w:-]+)`", row[0])}
        for name in sorted(code):
            with self.subTest(label=name):
                self.assertIn(name, listed, f"docs/delivery.md's label table has no row for {name}")
        for name in sorted(listed - kinds):
            with self.subTest(row=name):
                self.assertIn(
                    name,
                    code,
                    f"docs/delivery.md's label table lists {name}, which the code does not create",
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
        "`agent_blocked`, nothing to answer": (
            report.BLOCKED_NOTHING[0],
            report.BLOCKED_NOTHING[1][:1].upper() + report.BLOCKED_NOTHING[1][1:],
            report.BLOCKED_NOTHING_PULL_DO,
        ),
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


def autopilot_block():
    """The autopilot settings of docs/autopilot.md: its first ```json block, under '## Autopilot'.

    The block holds the members of "autopilot" without the braces of a whole object, so they
    are wrapped in braces before they are parsed."""
    text = AUTOPILOT_PAGE.read_text()
    start = text.index("```json\n", text.index("\n## Autopilot\n")) + len("```json\n")
    return json.loads("{" + text[start : text.index("\n```", start)] + "}")["autopilot"]


def key_rows():
    """The rows of the three key tables of the configuration reference, by their key."""
    return {cells[0].strip("`"): cells for cells in tables(CONFIG_PAGE, KEYS_HEADER)}


def stage_key_rows():
    """The rows of the table of a stage's keys under '## Stages' of the configuration reference.

    Empty when that section has no such table, so the test fails rather than skips."""
    parts = CONFIG_PAGE.read_text().split("\n## Stages\n", 1)
    if len(parts) < 2:
        return []
    lines = parts[1].split("\n## ", 1)[0].splitlines()
    for i, line in enumerate(lines):
        if line == STAGE_KEYS_HEADER:
            return rows_from(lines, i + 1)
    return []


def dotted(cell):
    """The dotted key of a table cell: `"fix": {"max_auto_rounds": 2}` gives fix.max_auto_rounds."""
    value, path = json.loads("{" + cell.strip("`") + "}"), []
    while isinstance(value, dict) and len(value) == 1:
        key, value = next(iter(value.items()))
        path.append(key)
    return ".".join(path)


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


def workflow_permissions():
    """The workflow-level `permissions:` of the workflow template, by name."""
    block = re.search(r"^permissions:\n((?:  .*\n)+)", WORKFLOW_TEMPLATE.read_text(), re.M)
    return dict(re.findall(r"^  ([\w-]+): (\w+)", block.group(1), re.M)) if block else {}


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

    def test_the_provider_table_lists_each_provider_the_code_defines_and_no_other(self):
        rows = {cells[0].strip("`"): cells for cells in table(CONFIG_PAGE, PROVIDERS_HEADER)}
        self.assertTrue(rows, "docs/configuration.md has no provider table")
        for name in sorted(set(rows) | set(runtimes.PROVIDERS)):
            with self.subTest(provider=name):
                self.assertIn(
                    name,
                    runtimes.PROVIDERS,
                    f"docs/configuration.md has a row for {name}, which the code does not define",
                )
                self.assertIn(name, rows, f"docs/configuration.md has no row for provider {name}")
                fields = runtimes.PROVIDERS[name].fields
                self.assertEqual(
                    set(FIELD_NAME.findall(rows[name][1])),
                    {key for key, f in fields.items() if f.kind != "secret"},
                    f"the settings of {name} in docs/configuration.md differ from the code",
                )
                self.assertEqual(
                    set(FIELD_NAME.findall(rows[name][2])),
                    {key for key, f in fields.items() if f.kind == "secret"},
                    f"the secret fields of {name} in docs/configuration.md differ from the code",
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

    def test_the_stage_table_lists_each_stage_key_the_code_defines_and_no_other(self):
        rows = stage_key_rows()
        self.assertTrue(
            rows, "docs/configuration.md has no table of a stage's keys under ## Stages"
        )
        listed = {cells[0].strip("`") for cells in rows}
        for key in sorted(config.STAGE_KEYS - listed):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md does not list the stage key {key}")
        for key in sorted(listed - config.STAGE_KEYS):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md lists {key}, which STAGE_KEYS does not define")

    def test_the_cache_field_list_names_each_cache_key_the_code_defines_and_no_other(self):
        section = CONFIG_PAGE.read_text().split("\n## Caches\n", 1)[1].split("\n## ", 1)[0]
        listed = set(FIELD.findall(section))
        self.assertTrue(listed, "docs/configuration.md has no list of a cache entry's fields")
        for key in sorted(config.CACHE_KEYS - listed):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md does not list the cache field {key}")
        for key in sorted(listed - config.CACHE_KEYS):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md lists {key}, which CACHE_KEYS does not define")

    def test_the_renamed_table_lists_each_key_the_code_renames_and_no_other(self):
        rows = table(CONFIG_PAGE, RENAMED_HEADER)
        self.assertTrue(rows, "docs/configuration.md has no table of the keys renamed in 2.0")
        listed = {dotted(row[0]): dotted(row[1]) for row in rows}
        for key in sorted(set(config.RENAMED) - set(listed)):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md does not list the renamed key {key}")
        for key in sorted(set(listed) - set(config.RENAMED)):
            with self.subTest(key=key):
                self.fail(f"docs/configuration.md lists {key}, which RENAMED does not define")
        for key in sorted(set(listed) & set(config.RENAMED)):
            with self.subTest(key=key):
                self.assertEqual(
                    listed[key],
                    config.RENAMED[key],
                    f"docs/configuration.md moves {key} to {listed[key]}, "
                    f"but RENAMED moves it to {config.RENAMED[key]}",
                )

    def test_the_pages_list_each_secret_an_older_workflow_can_pass_and_no_other(self):
        code = {*runtimes.DECLARED_SECRETS, *config.APP_SECRETS.values()}
        for page in (CONFIG_PAGE, TROUBLESHOOTING_PAGE):
            with self.subTest(page=page.name):
                text = " ".join(page.read_text().split())
                match = ONE_BY_ONE.search(text)
                self.assertIsNotNone(
                    match,
                    f"{page.name} does not list the secrets an older workflow can pass one by one",
                )
                listed = set(re.findall(r"`(\w+)`", match.group(1)))
                for name in sorted(listed - code):
                    with self.subTest(secret=name):
                        self.fail(
                            f"{page.name} lists {name}, which DECLARED_SECRETS and APP_SECRETS "
                            "do not define"
                        )
                for name in sorted(code - listed):
                    with self.subTest(secret=name):
                        self.fail(f"{page.name} does not list the secret {name}")

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

    def test_the_app_upgrade_lists_each_workflow_permission_of_the_template(self):
        match = UPGRADE_PERMISSIONS.search(CONFIG_PAGE.read_text())
        self.assertIsNotNone(
            match,
            "docs/configuration.md has no list of workflow permissions in step 5 "
            "of the App upgrade",
        )
        template = workflow_permissions()
        self.assertTrue(
            template, "nexkit/templates/workflow.yml has no top-level permissions block"
        )
        listed = set(re.findall(r"`([\w-]+)`", match.group(1)))
        level = match.group(2)
        for name in sorted(listed - set(template)):
            with self.subTest(permission=name):
                self.fail(
                    f"step 5 of the App upgrade lists {name}, which nexkit/templates/workflow.yml "
                    "does not set"
                )
        for name in sorted(set(template) - listed):
            with self.subTest(permission=name):
                self.fail(
                    f"nexkit/templates/workflow.yml sets {name}, which step 5 of the App upgrade "
                    "does not list"
                )
        for name in sorted(listed & set(template)):
            with self.subTest(permission=name):
                self.assertEqual(
                    template[name],
                    level,
                    f"nexkit/templates/workflow.yml gives {name} as {template[name]}, "
                    f"but step 5 of the App upgrade says {level}",
                )


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
        listed = set(FIELD.findall(section))
        self.assertTrue(listed, "docs/checks.md has no list of a check's fields")
        for key in sorted(config.CHECK_KEYS - listed):
            with self.subTest(key=key):
                self.fail(f"docs/checks.md does not list the check field {key}")
        for key in sorted(listed - config.CHECK_KEYS):
            with self.subTest(key=key):
                self.fail(f"docs/checks.md lists {key}, which CHECK_KEYS does not define")

    def test_the_page_names_the_limits_of_check_heads(self):
        # The list under NEXKIT_CHECK_VERIFIED_SHA, up to the next section.
        parts = CHECKS_PAGE.read_text().split("Each check also gets `NEXKIT_CHECK_VERIFIED_SHA`", 1)
        self.assertEqual(
            len(parts), 2, "docs/checks.md does not describe NEXKIT_CHECK_VERIFIED_SHA"
        )
        section = parts[1].split("\n### ", 1)[0]
        self.assertIn(f"{state.MAX_PASSED} checks", section)
        self.assertIn(f"{state.MAX_COMMANDS} commands", section)


class AutopilotPageTests(unittest.TestCase):
    def test_the_settings_block_lists_each_autopilot_key_the_code_defines_and_no_other(self):
        listed = autopilot_block()
        defined = config.DEFAULTS["autopilot"]
        for key in sorted(set(defined) - set(listed)):
            with self.subTest(key=key):
                self.fail(f"docs/autopilot.md does not list autopilot.{key}")
        for key in sorted(set(listed) - set(defined)):
            with self.subTest(key=key):
                self.fail(
                    f"docs/autopilot.md lists autopilot.{key}, "
                    "which config.DEFAULTS does not define"
                )

    def test_the_settings_block_holds_each_default_but_enabled(self):
        listed = autopilot_block()
        defined = config.DEFAULTS["autopilot"]
        # The block turns autopilot on to show it, so `enabled` differs from its default on purpose.
        for key in sorted((set(listed) & set(defined)) - {"enabled"}):
            with self.subTest(key=key):
                self.assertEqual(
                    listed[key],
                    defined[key],
                    f"docs/autopilot.md shows autopilot.{key} as {listed[key]!r}, "
                    f"but config.DEFAULTS defaults it to {defined[key]!r}",
                )


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

    def test_the_job_diagram_shows_each_job_of_the_pipeline_and_the_order_it_waits_in(self):
        lines = job_diagram(ARCH)
        self.assertIsNotNone(lines, "docs/architecture.md has no mermaid diagram of its jobs")
        self.assertEqual(lines[0], "flowchart LR", "the job diagram is not a flowchart LR")
        edges = set()
        for line in lines[1:]:
            self.assertIsNotNone(
                re.fullmatch(r"\w+(?:\s*-->\s*\w+)+", line),
                f"the job diagram line `{line}` is not a plain chain of arrows",
            )
            names = [name.strip() for name in line.split("-->")]
            edges.update(zip(names, names[1:], strict=False))
        nodes = {name for edge in edges for name in edge}
        jobs = jobs_of((ROOT / ".github/workflows/pipeline.yml").read_text())
        needs = {}
        for name, body in jobs.items():
            self.assertIsNone(
                re.search(r"^    needs:\s*$", body, re.M),
                f"pipeline.yml job {name} sets its needs: over several lines, which the test "
                "does not read",
            )
            found = re.search(r"^    needs: (\[.*\]|\w+)$", body, re.M)
            needs[name] = set(re.findall(r"\w+", found.group(1))) if found else set()
        for name in sorted(jobs):
            with self.subTest(job=name):
                self.assertIn(
                    name, nodes, f"the job diagram has no entry for pipeline.yml job {name}"
                )
        for name in sorted(nodes - set(jobs)):
            with self.subTest(job=name):
                self.fail(f"the job diagram names {name}, which pipeline.yml does not define")
        for before, after in sorted(edges):
            with self.subTest(arrow=f"{before} --> {after}"):
                self.assertIn(
                    before,
                    needs.get(after, set()),
                    f"the job diagram's arrow {before} --> {after} is not in the needs: "
                    f"of {after} in pipeline.yml",
                )
        waits = {}
        for before, after in edges:
            waits.setdefault(after, set()).add(before)
        for name in sorted(nodes | set(jobs)):
            shown, wanted = waited_for(waits, name), waited_for(needs, name)
            for other in sorted(shown ^ wanted):
                with self.subTest(job=name, waits_for=other):
                    side = "the job diagram" if other in shown else "pipeline.yml"
                    self.fail(f"{name} waits for {other} in {side} only, not in the other")


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
            if len(cells) == 3 and cells[2].startswith("Superseded by"):
                # A superseded decision has no file: its row names the live decisions that
                # replaced it and links its last text in git (docs/writing-docs.md#decisions).
                self.assertRegex(cells[0], r"^D\d+$", f"a superseded row links its file: {cells}")
                for _, name in DECISION_LINK.findall(cells[2]) or [(None, None)]:
                    self.assertIsNotNone(name, f"a superseded row names no decision: {cells}")
                    self.assertTrue((DECISIONS / name).exists(), f"{cells[0]} names {name}")
                self.assertRegex(
                    cells[2],
                    rf"\[last text\]\(https://github\.com/phuongnse/nexkit/blob/[0-9a-f]{{40}}/"
                    rf"docs/decisions/d{cells[0][1:]}-[a-z0-9-]+\.md\)$",
                )
                continue
            link = DECISION_LINK.fullmatch(cells[0]) if len(cells) == 3 else None
            self.assertIsNotNone(link, f"a row of docs/decisions.md links no decision: {cells}")
            number, name = link.groups()
            listed.add(name)
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


def plugin_skills(test):
    """The names that `skills/*/SKILL.md` define, each read from its `name:` field."""
    defined = set()
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        match = SKILL_NAME.search(skill.read_text())
        test.assertIsNotNone(match, f"{skill.relative_to(ROOT)} has no name: field")
        defined.add(match.group(1))
    return defined


def block_after(path, marker):
    """The first ```markdown block of `path` after the text `marker`, without its fences."""
    text = path.read_text()
    opening = text.index("```markdown\n", text.index(marker)) + len("```markdown\n")
    return text[opening : text.index("\n```", opening)]


def job_diagram(path):
    """The lines of the first mermaid block under `## The workflows and their jobs`, stripped and
    without blank lines, or None when the section has no mermaid block."""
    section = re.search(
        r"^## The workflows and their jobs\n(.*?)(?=^## |\Z)", path.read_text(), re.M | re.S
    )
    block = re.search(r"```mermaid\n(.*?)```", section.group(1), re.S) if section else None
    if block is None:
        return None
    return [line.strip() for line in block.group(1).splitlines() if line.strip()]


def waited_for(waits, job):
    """Every job `job` waits for, directly or through others, given the direct waits."""
    found, todo = set(), list(waits.get(job, ()))
    while todo:
        other = todo.pop()
        if other not in found:
            found.add(other)
            todo.extend(waits.get(other, ()))
    return found


def parts(text):
    """The bold parts that start a line of `text`, except **Checks**, which a review writes last."""
    return [part for part in re.findall(r"^\*\*([^*]+)\*\*", text, re.M) if part != "Checks"]


def restart_delays():
    """The restart delays of a command that did not run, as the pages word them: 30, 60 and 120
    minutes."""
    minutes = [int(delay.total_seconds() // 60) for delay in autopilot.AGAIN[1:]]
    return ", ".join(str(m) for m in minutes[:-1]) + f" and {minutes[-1]} minutes"


def artifact_days():
    """The `retention-days` of the pipeline workflows' artifacts, as the page words them: one
    value, or all of them when they differ, which no page says."""
    days = set()
    for path in sorted((ROOT / ".github/workflows").glob("pipeline*.yml")):
        days |= set(re.findall(r"retention-days: (\d+)", path.read_text()))
    return ", ".join(sorted(days))


def retry_delays():
    """The retry delays of the failure handling row that starts with PUBLISH_ROW, as
    (publish's delays, its word for tries in all, the other calls' word for tries more, their
    delays). Change tests/test_docs.py together with that row when its wording changes."""
    rows = [row for row in table(FAILURE_PAGE, FAILURE_HEADER) if row[0].startswith(PUBLISH_ROW)]
    assert len(rows) == 1, (
        f"docs/failure-handling.md needs one row that starts with {PUBLISH_ROW!r}; "
        "change tests/test_docs.py with that row"
    )
    cell = " ".join(rows[0][1].split())
    publish_match = PUBLISH_DELAYS.search(cell)
    assert publish_match, (
        "the publish row of docs/failure-handling.md must say 'Tries again after N, N and N "
        "seconds, WORD tries in all'; change tests/test_docs.py with the row"
    )
    other_match = OTHER_DELAYS.search(cell)
    assert other_match, (
        "the publish row of docs/failure-handling.md must say 'every other GitHub API call ... "
        "tries WORD more ... after N and N seconds'; change tests/test_docs.py with the row"
    )
    return (
        numbers(publish_match.group(1)),
        publish_match.group(2),
        other_match.group(1),
        numbers(other_match.group(2)),
    )


def numbers(text):
    """The numbers in `text`, as ints."""
    return [int(n) for n in re.findall(r"\d+", text)]


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
    ("how-it-works.md", f"values of {redact.MIN_LENGTH} characters or more"),
    ("how-it-works.md", f"(first {runlog.MESSAGE_CHARS} characters)"),
    (
        "how-it-works.md",
        f"first and last {runlog.HEAD_LINES} lines of the output, at most {runlog.MAX_CHARS:,} "
        "characters",
    ),
    ("how-it-works.md", f"(`▸ Tool detail`, {runlog.CALL_CHARS} characters)"),
    ("how-it-works.md", f"kept {artifact_days()} days"),
    ("how-it-works.md", f"first {textutil.FAILURE_LINES} lines before them"),
    (
        "how-it-works.md",
        f"up to {report.MAX_FINDINGS} of its findings: past {report.MAX_FINDINGS},",
    ),
    ("how-it-works.md", f"last {NUMBERS[report.MAX_FIXES]} fix rounds"),
    ("how-it-works.md", f"GitHub's {textutil.TEXT_LIMIT:,} characters"),
    ("how-it-works.md", f"down to {progress.FIT_STEPS[0][1]:,} characters each"),
    (
        "how-it-works.md",
        f"blocking ones (keeping their start, down to {progress.FIT_STEPS[1][1]} characters)",
    ),
    (
        "how-it-works.md",
        "the blocking findings (keeping their start, down to "
        f"{progress.FIT_STEPS[4][1]} characters)",
    ),
    ("work-without-a-command.md", f"last {github.DOWNLOAD_LIMIT // 1_000_000} MB"),
    ("work-without-a-command.md", f"at most {NUMBERS[context.MAX_EARLIER]} of those parts"),
    ("work-without-a-command.md", f"first {NUMBERS[context.MAX_EARLIER * 2]} blockers"),
    ("work-without-a-command.md", f"at most {autopilot.ISSUES_LIMIT:,} of each"),
    ("work-without-a-command.md", f"{restart_delays()} after its last start"),
    ("failure-handling.md", f"{restart_delays()} after its last start"),
    ("autopilot.md", f"more than {NUMBERS[textutil.MAX_PARTS]} parts"),
    ("autopilot.md", f"within {NUMBERS[machine.CHECKS_END // 3600]} hours"),
    ("autopilot.md", f"more than {NUMBERS[autopilot.CI_NEW_ISSUES]} issues"),
    ("autopilot.md", f"GitHub's {autopilot.CI_TITLE} characters"),
    ("autopilot.md", f"up to {NUMBERS[autopilot.CI_TITLE_FAILURES]} of one kind"),
    ("autopilot.md", f"A sweep opens at most {NUMBERS[autopilot.DEPENDABOT_NEW_ISSUES]}"),
    ("autopilot.md", f"oldest {github.DEPENDABOT_ALERTS} alerts"),
    ("stages.md", f"at most {NUMBERS[textutil.MAX_PARTS]} parts"),
    ("stages.md", f"cut to {publish.PLAN_FLOOR} characters"),
    ("stages.md", f"more than {checks.FAILED_TESTS_LIMIT} failing tests"),
    ("stages.md", f"deletes up to {NUMBERS[maintain.STALE_BRANCHES]}"),
    ("configuration.md", f"At most {config.MAX_CACHES} entries"),
    (
        "configuration.md",
        f"in at most {config.MAX_CHECKLIST_ITEM} characters; a list holds at most "
        f"{config.MAX_CHECKLIST}",
    ),
    (
        "configuration.md",
        f"plan {config.DEFAULTS['stages']['plan']['timeout_minutes']}, "
        f"implement {config.DEFAULTS['stages']['implement']['timeout_minutes']}, "
        f"review {config.DEFAULTS['stages']['review']['timeout_minutes']} and "
        f"triage {config.TRIAGE_DEFAULTS['timeout_minutes']} by default; "
        f"triage at most {config.MAX_TRIAGE_MINUTES}, other stages at most "
        f"{config.MAX_STAGE_MINUTES}",
    ),
    ("configuration.md", f"up to {config.MAX_STAGE_MINUTES} minutes for a stage"),
    ("configuration.md", f"toward the {runtimes.SECRET_SLOTS} per job"),
    ("configuration.md", f"at most {runtimes.SECRET_SLOTS} per job"),
    ("configuration.md", f"download artifacts for {artifact_days()} days"),
    ("configuration.md", f"first and last {runlog.HEAD_LINES} lines of its output"),
)
# The redaction list of how it works: its heading, and the regex that finds the words of its first
# bullet. Its other bullets name the token shapes and the passwords, each in a code span.
REDACTED_HEADING = "### What is redacted"
SECRET_WORDS_LIST = re.compile(r"whose name contains ((?:`\w+`(?:, |,? or )?)+)")
# Each shape the list names, as its code span, with a sample that redact.PATTERNS redacts. Where
# tests/test_redact.py has a sample of a shape, this uses the same one.
SHAPES = {
    "sk-ant-…": "sk-ant-api03-AbCdEf_123-456",
    "sk-…": "sk-" + "a1B2c3D4" * 4,
    "sk-proj-…": "sk-proj-" + "Ab1_" * 12,
    "sk-svcacct-…": "sk-svcacct-" + "Zx9-" * 10,
    "sk-admin-…": "sk-admin-" + "Qw8_" * 10,
    "ghp_": "ghp_" + "a1B2" * 9,
    "gho_": "gho_" + "Z9" * 18,
    "ghu_": "ghu_" + "x" * 36,
    "ghs_": "ghs_" + "y" * 36,
    "ghr_": "ghr_" + "z" * 36,
    "github_pat_": "github_pat_11ABCDEFG0_" + "q" * 40,
    "eyJ…": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n",
    "Authorization:": "Authorization: token opaque-value-99",
    "Bearer": "Bearer abc.def-123456",
    "-----BEGIN … PRIVATE KEY-----": (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----"
    ),
    "password=": "Server=db;Password=S3cret!;",
    "pwd=": "host=db PWD=abc dbname=x",
    "scheme://user:password@host": "postgres://app:s3cret@db:5432/app",
}


def redacted_bullets():
    """The three top-level bullets of the redaction list, each on one line with spaces one each."""
    text = HOW_IT_WORKS_PAGE.read_text()
    start = text.find(REDACTED_HEADING)
    if start < 0:
        raise AssertionError(f"docs/how-it-works.md has no {REDACTED_HEADING!r} section")
    lines = text[start:].split("\n#", 1)[0].splitlines()[1:]
    bullets, open_bullet = [], False
    for line in lines:
        if line.startswith("- "):
            bullets.append(line)
            open_bullet = True
        elif line.startswith("  ") and open_bullet:
            bullets[-1] += " " + line
        else:
            open_bullet = False
    if len(bullets) != 3:
        raise AssertionError(
            f"docs/how-it-works.md has {len(bullets)} bullets under {REDACTED_HEADING!r}, not 3"
        )
    return [" ".join(bullet.split()) for bullet in bullets]


# The pages that list the words which mark a failing line, and the regex that finds each list.
FAILURE_WORDS_PAGES = ("agents-and-profiles.md", "failure-handling.md")
FAILURE_WORDS = re.compile(r"contains? (`[^`]+`(?:(?:, | or )`[^`]+`)*), in any case")
# The autopilot page, and the regexes that find its two job lists: the jobs in which a round can
# be stopped, and those in which it never is.
STOP_PAGE = DOCS / "work-without-a-command.md"
STOPPED_JOBS = re.compile(r"so that only ((?:`[\w-]+`(?:, |,? or )?)+) jobs still run")
KEPT_JOBS = re.compile(r"A round in ((?:`[\w-]+`(?:, |,? or )?)+) is never stopped")
# The page and the row of its access table that give the tools of the plan and review stages.
ACCESS_PAGE = DOCS / "agents-and-profiles.md"
READ_ACCESS_ROW = "| plan, review |"
# The status skill's list of issue states: the `issues` bullet names them after "a `state` (". The
# lookahead keeps the match inside that bullet, so a bullet without the list is not read as another.
STATUS_SKILL = ROOT / "skills" / "status" / "SKILL.md"
ISSUE_STATES = re.compile(r"- `issues`: (?:(?!- `).)*?a `state` \(([^)]*)\)")
# The status skill's list of pull request states, in its `pull_requests` bullet, read the same way.
PULL_STATES = re.compile(r"- `pull_requests`: (?:(?!- `).)*?a `state` \(([^)]*)\)")
LISTED_STATE = re.compile(r"(?:^|, )`(\w+)`")
# The status skill's list of autopilot states: the `autopilot` bullet names them after
# "a `state` (".
AUTOPILOT_STATES = re.compile(r"- `autopilot`(?:(?!- `).)*?a `state` \(([^)]*)\)")
# The page and the sentence that list the outcomes after which a writer's approval merges. The
# group keeps the word `as` before the first outcome, which the outcomes are found after.
APPROVAL_PAGE = DOCS / "work-without-a-command.md"
APPROVAL_OUTCOMES = re.compile(r"latest round ended (as .+?), merges it on the head")
# The states in which GitHub reports a pull request as mergeable, in the brackets of the sentence
# of Automatic merge. The text is searched with its whitespace collapsed, since the sentence wraps.
MERGE_PAGE = DOCS / "merges-and-waits.md"
MERGEABLE_LIST = re.compile(r"reports the pull request as mergeable \(([^)]*)\)")
# The row of failure handling that gives the retry delays of `publish` and of every other GitHub API
# call. Its first cell starts with PUBLISH_ROW; its second cell is the one the regexes read.
FAILURE_PAGE = DOCS / "failure-handling.md"
FAILURE_HEADER = "| What happened | NexKit does |"
PUBLISH_ROW = "GitHub answers `publish`"
PUBLISH_DELAYS = re.compile(r"Tries again after ([\d, and]+?) seconds, (\w+) tries in all")
OTHER_DELAYS = re.compile(
    r"[Ee]very other GitHub API call .*?tries (\w+) more .*?after ([\d, and]+?) seconds"
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


class RedactionTests(unittest.TestCase):
    def test_the_page_lists_the_secret_words_and_names_the_code_has(self):
        first = redacted_bullets()[0]
        match = SECRET_WORDS_LIST.search(first)
        self.assertIsNotNone(match, "docs/how-it-works.md has no list after 'whose name contains'")
        listed_words = set(re.findall(r"`([^`]+)`", match.group(1)))
        named = set(re.findall(r"`([^`]+)`", first)) - listed_words
        for word in sorted(set(redact.SECRET_WORDS) - listed_words):
            with self.subTest(word=word):
                self.fail(
                    f"docs/how-it-works.md does not list {word!r}, which redact.SECRET_WORDS has"
                )
        for word in sorted(listed_words - set(redact.SECRET_WORDS)):
            with self.subTest(word=word):
                self.fail(
                    f"docs/how-it-works.md lists {word!r}, which redact.SECRET_WORDS does not have"
                )
        for name in sorted(set(redact.SECRET_NAMES) - named):
            with self.subTest(name=name):
                self.fail(
                    f"docs/how-it-works.md does not name {name!r}, which redact.SECRET_NAMES has"
                )
        for name in sorted(named - set(redact.SECRET_NAMES)):
            with self.subTest(name=name):
                self.fail(
                    f"docs/how-it-works.md names {name!r}, which redact.SECRET_NAMES does not have"
                )

    def test_the_page_lists_each_token_shape_the_code_redacts_and_no_other(self):
        listed = set()
        for bullet in redacted_bullets()[1:]:
            listed |= set(re.findall(r"`([^`]+)`", bullet))
        for shape in sorted(listed - set(SHAPES)):
            with self.subTest(shape=shape):
                self.fail(
                    f"docs/how-it-works.md lists {shape!r}, which the test has no sample for: "
                    "add one that redact.PATTERNS redacts"
                )
        for shape in sorted(set(SHAPES) - listed):
            with self.subTest(shape=shape):
                self.fail(
                    f"docs/how-it-works.md does not list {shape!r}, which the test has a sample for"
                )

    def test_the_code_redacts_each_listed_shape_and_each_pattern_has_one(self):
        redactor = redact.Redactor({})
        for shape, sample in SHAPES.items():
            with self.subTest(shape=shape):
                redacted = redactor(sample)
                self.assertNotEqual(redacted, sample, f"redact.Redactor leaves {shape!r} as it is")
                self.assertIn(redact.MASK, redacted)
        for pattern, _ in redact.PATTERNS:
            with self.subTest(pattern=pattern.pattern):
                self.assertTrue(
                    any(pattern.search(sample) for sample in SHAPES.values()),
                    f"redact.PATTERNS has {pattern.pattern!r}, which redacts no shape that "
                    "docs/how-it-works.md lists",
                )


class FailureWordsTests(unittest.TestCase):
    def test_each_page_lists_the_words_of_failure_line_and_no_other(self):
        code = sorted(textutil.FAILURE_LINE.pattern.split("|"))
        for word in code:
            self.assertEqual(
                re.escape(word),
                word,
                f"FAILURE_LINE's word {word!r} has regex syntax; this test reads plain words",
            )
        for name in FAILURE_WORDS_PAGES:
            # Lines are wrapped, so the page is searched with its spaces made one each.
            text = " ".join((DOCS / name).read_text().split())
            match = FAILURE_WORDS.search(text)
            if match is None:
                with self.subTest(page=name):
                    self.fail(f"docs/{name} has no list of the words that mark a failing line")
                continue
            self.assertTrue(
                bool(textutil.FAILURE_LINE.flags & re.IGNORECASE),
                f"docs/{name} says the words match in any case; FAILURE_LINE does not ignore case",
            )
            listed = sorted(re.findall(r"`([^`]+)`", match.group(1)))
            for word in sorted(set(code) - set(listed)):
                with self.subTest(page=name, word=word):
                    self.fail(f"docs/{name} does not list {word!r}, which FAILURE_LINE matches")
            for word in sorted(set(listed) - set(code)):
                with self.subTest(page=name, word=word):
                    self.fail(f"docs/{name} lists {word!r}, which FAILURE_LINE does not match")


class StopJobsTests(unittest.TestCase):
    def test_the_page_lists_the_check_jobs_and_no_other(self):
        # Lines are wrapped, so the page is searched with its spaces made one each.
        text = " ".join(STOP_PAGE.read_text().split())
        page = f"docs/{STOP_PAGE.name}"
        stopped = STOPPED_JOBS.search(text)
        if stopped is None:
            self.fail(f"{page} has no list of the jobs in which a round can be stopped")
        kept = KEPT_JOBS.search(text)
        if kept is None:
            self.fail(f"{page} has no list of the jobs in which a round is never stopped")
        listed = set(re.findall(r"`([^`]+)`", stopped.group(1)))
        never = set(re.findall(r"`([^`]+)`", kept.group(1)))
        jobs = jobs_of((ROOT / ".github/workflows/pipeline.yml").read_text())
        for job in sorted(listed - maintain.CHECK_JOBS):
            with self.subTest(job=job):
                self.fail(f"{page} lists {job!r}, which maintain.CHECK_JOBS lacks")
        for job in sorted(maintain.CHECK_JOBS - listed):
            with self.subTest(job=job):
                self.fail(f"{page} does not list {job!r}, which maintain.CHECK_JOBS has")
        for job in sorted(never & maintain.CHECK_JOBS):
            with self.subTest(job=job):
                self.fail(f"{page} lists {job!r} as never stopped, but it is in CHECK_JOBS")
        for job in sorted((never | maintain.CHECK_JOBS) - set(jobs)):
            with self.subTest(job=job):
                self.fail(f"pipeline.yml has no job {job!r}, which {page} or CHECK_JOBS names")


class RetryDelayTests(unittest.TestCase):
    def test_the_failure_table_lists_the_retry_delays_the_code_has(self):
        publish_delays, _, _, other_delays = retry_delays()
        waits = (
            ("publish", publish_delays, list(publish.WAITS), "publish.WAITS"),
            (
                "every other GitHub API call",
                other_delays,
                list(github.GitHub.waits),
                "github.GitHub.waits",
            ),
        )
        for calls, listed, code, name in waits:
            for delay in sorted(set(listed) | set(code)):
                with self.subTest(calls=calls, delay=delay):
                    self.assertIn(
                        delay,
                        code,
                        f"docs/failure-handling.md says {calls} waits {delay} seconds, "
                        f"which {name} lacks",
                    )
                    self.assertIn(
                        delay,
                        listed,
                        f"{name} waits {delay} seconds, which docs/failure-handling.md "
                        f"does not say for {calls}",
                    )
            self.assertEqual(
                listed,
                code,
                f"docs/failure-handling.md lists the waits of {calls} as {listed}; "
                f"{name} has {code}, in order",
            )

    def test_the_failure_table_counts_the_tries_the_code_makes(self):
        _, publish_tries, other_tries, _ = retry_delays()
        # The waits are the tries after the first one, so a call makes one try more than its waits.
        publish_expected = NUMBERS[len(publish.WAITS) + 1]
        self.assertEqual(
            publish_tries,
            publish_expected,
            f"docs/failure-handling.md says publish makes {publish_tries!r} tries in all; "
            f"publish.WAITS allows {publish_expected!r}",
        )
        count = len(github.GitHub.waits)
        other_expected = {1: "once", 2: "twice"}.get(count, f"{NUMBERS[count]} times")
        self.assertEqual(
            other_tries,
            other_expected,
            f"docs/failure-handling.md says other GitHub API calls try {other_tries!r} more; "
            f"github.GitHub.waits allows {other_expected!r}",
        )


class ReadOnlyToolsTests(unittest.TestCase):
    def test_the_access_table_lists_the_read_only_tools_and_no_other(self):
        code = sorted(claude_code.READ_ONLY_TOOLS.split(","))
        lines = ACCESS_PAGE.read_text().splitlines()
        rows = [line for line in lines if line.startswith(READ_ACCESS_ROW)]
        if len(rows) != 1:
            self.fail("docs/agents-and-profiles.md has no single `plan, review` row in its table")
        # The Claude Code column is the last cell of the row.
        cells = rows[0].strip().strip("|").split("|")
        listed = sorted(re.findall(r"`([^`]+)`", cells[-1]))
        for tool in sorted(set(code) - set(listed)):
            with self.subTest(tool=tool):
                self.fail(
                    f"docs/agents-and-profiles.md does not list {tool!r}, "
                    "which READ_ONLY_TOOLS gives Claude Code"
                )
        for tool in sorted(set(listed) - set(code)):
            with self.subTest(tool=tool):
                self.fail(
                    f"docs/agents-and-profiles.md lists {tool!r}, "
                    "which READ_ONLY_TOOLS does not give Claude Code"
                )


def state_values(node):
    """The strings that a value can be: the branches of a conditional, or the string itself.
    The test of a conditional is not a value, so its strings do not count."""
    if isinstance(node, ast.IfExp):
        return state_values(node.body) + state_values(node.orelse)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return []


def states_set_in(function):
    """The states that a function sets: the value of each `state` key, subscript assignment or
    keyword in its source."""
    states = set()
    for node in ast.walk(ast.parse(inspect.getsource(function))):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "state"
                ):
                    states.update(state_values(node.value))
        elif isinstance(node, ast.keyword) and node.arg == "state":
            states.update(state_values(node.value))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "state":
                    states.update(state_values(value))
    return states


def autopilot_states_of_code():
    """The states that autopilot's steps take. `_step` takes its state second, in `autopilot`
    and in `host`. The `step` method that `host` wraps `_step` in takes it first. Only
    `host.sweep` gives a step a `state=` keyword; elsewhere the keyword sets an issue's
    document, which is not a step."""
    states = set()
    for module in (autopilot, host):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name == "_step" and len(node.args) >= 2:
                states.update(state_values(node.args[1]))
            elif name == "step" and node.args:
                states.update(state_values(node.args[0]))
    for node in ast.walk(ast.parse(inspect.getsource(host.sweep))):
        if isinstance(node, ast.keyword) and node.arg == "state":
            states.update(state_values(node.value))
    return states


def _allowed_by_enclosing_if(node, name, parents):
    """The strings of the tuple in the nearest `if name in (...)` whose body holds the return
    `node`, or [] when that tuple holds a value the test cannot read."""
    child, parent = node, parents.get(node)
    while parent is not None:
        test = parent.test if isinstance(parent, ast.If) else None
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and isinstance(test.left, ast.Name)
            and test.left.id == name
            and isinstance(test.comparators[0], ast.Tuple)
            and child in parent.body
        ):
            elts = test.comparators[0].elts
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
                return [e.value for e in elts]
            return []
        child, parent = parent, parents.get(parent)
    return []


def _returned_states(node, value, parents):
    """The states that a return's value can be: the strings of a conditional's branches, and
    the strings that the `if` around the return allows for a name. Any other value fails."""
    if isinstance(value, ast.IfExp):
        return _returned_states(node, value.body, parents) | _returned_states(
            node, value.orelse, parents
        )
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.Name):
        allowed = _allowed_by_enclosing_if(node, value.id, parents)
        if allowed:
            return set(allowed)
    raise AssertionError(f"pull_state returns something the test cannot read at line {node.lineno}")


def pull_states_of_code():
    """The states that `status.pull_state` returns, and those that `status.pull_item` sets when
    a pull request has no rounds."""
    tree = ast.parse(inspect.getsource(status.pull_state))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    states = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            states.update(_returned_states(node, node.value, parents))
    return states | states_set_in(status.pull_item)


class StatusSkillTests(unittest.TestCase):
    def test_the_skill_lists_each_issue_state_the_code_sets_and_no_other(self):
        # The page wraps its lines, so the regex reads the text with one space between words.
        match = ISSUE_STATES.search(" ".join(STATUS_SKILL.read_text().split()))
        if not match:
            self.fail("skills/status/SKILL.md has no list of issue states in its `issues` bullet")
        listed = set(LISTED_STATE.findall(match.group(1)))
        if not listed:
            self.fail("skills/status/SKILL.md lists no issue state")
        code = states_set_in(status.issue_item)
        if not code:
            self.fail("status.issue_item sets no issue state that the test can read")
        for issue_state in sorted(code - listed):
            with self.subTest(state=issue_state):
                self.fail(
                    f"skills/status/SKILL.md does not list {issue_state!r}, "
                    "which status.issue_item sets"
                )
        for issue_state in sorted(listed - code):
            with self.subTest(state=issue_state):
                self.fail(
                    f"skills/status/SKILL.md lists {issue_state!r}, "
                    "which status.issue_item does not set"
                )

    def test_the_skill_lists_each_autopilot_state_the_code_gives_and_no_other(self):
        # The page wraps its lines, so the regex reads the text with one space between words.
        match = AUTOPILOT_STATES.search(" ".join(STATUS_SKILL.read_text().split()))
        if not match:
            self.fail(
                "skills/status/SKILL.md has no list of autopilot states in its `autopilot` bullet"
            )
        listed = set(LISTED_STATE.findall(match.group(1)))
        if not listed:
            self.fail("skills/status/SKILL.md lists no autopilot state")
        code = autopilot_states_of_code()
        if not code:
            self.fail("autopilot's steps give no state that the test can read")
        for step_state in sorted(code - listed):
            with self.subTest(state=step_state):
                self.fail(
                    f"skills/status/SKILL.md does not list {step_state!r}, "
                    "which autopilot._step gives"
                )
        for step_state in sorted(listed - code):
            with self.subTest(state=step_state):
                self.fail(
                    f"skills/status/SKILL.md lists {step_state!r}, "
                    "which autopilot._step does not give"
                )

    def test_the_skill_lists_each_pull_request_state_the_code_gives_and_no_other(self):
        # The page wraps its lines, so the regex reads the text with one space between words.
        match = PULL_STATES.search(" ".join(STATUS_SKILL.read_text().split()))
        if not match:
            self.fail(
                "skills/status/SKILL.md has no list of pull request states in its "
                "`pull_requests` bullet"
            )
        listed = set(LISTED_STATE.findall(match.group(1)))
        if not listed:
            self.fail("skills/status/SKILL.md lists no pull request state")
        code = pull_states_of_code()
        if not code:
            self.fail("status.pull_state gives no pull request state that the test can read")
        for pull_state in sorted(code - listed):
            with self.subTest(state=pull_state):
                self.fail(
                    f"skills/status/SKILL.md does not list {pull_state!r}, "
                    "which status.pull_state or status.pull_item gives"
                )
        for pull_state in sorted(listed - code):
            with self.subTest(state=pull_state):
                self.fail(
                    f"skills/status/SKILL.md lists {pull_state!r}, "
                    "which status.pull_state or status.pull_item does not give"
                )


class ApprovableTests(unittest.TestCase):
    def test_the_page_lists_each_outcome_an_approval_merges_after_and_no_other(self):
        # Lines are wrapped, so the page is searched with its spaces made one each.
        text = " ".join(APPROVAL_PAGE.read_text().split())
        match = APPROVAL_OUTCOMES.search(text)
        if match is None:
            self.fail(
                "docs/work-without-a-command.md has no list of the outcomes after which "
                "an approval merges"
            )
        listed = sorted(set(re.findall(r"\bas `(\w+)`", match.group(1))))
        code = sorted(set(maintain.APPROVABLE))
        for outcome in sorted(set(code) - set(listed)):
            with self.subTest(outcome=outcome):
                self.fail(
                    f"docs/work-without-a-command.md does not list {outcome!r}, after which "
                    "maintain.APPROVABLE lets an approval merge"
                )
        for outcome in sorted(set(listed) - set(code)):
            with self.subTest(outcome=outcome):
                self.fail(
                    f"docs/work-without-a-command.md lists {outcome!r}, which "
                    "maintain.APPROVABLE does not hold"
                )


class MergeableStatesTests(unittest.TestCase):
    def test_the_page_lists_each_mergeable_state_the_code_accepts_and_no_other(self):
        # Lines are wrapped, so the page is searched with its spaces made one each.
        text = " ".join(MERGE_PAGE.read_text().split())
        match = MERGEABLE_LIST.search(text)
        if match is None:
            self.fail(
                "docs/merges-and-waits.md has no list of the states in which GitHub reports "
                "a pull request as mergeable"
            )
        listed = set(re.findall(r"`([^`]+)`", match.group(1)))
        code = set(autopilot.MERGEABLE_STATES)
        for mergeable in sorted(code - listed):
            with self.subTest(mergeable=mergeable):
                self.fail(
                    f"docs/merges-and-waits.md does not list {mergeable!r}, which "
                    "autopilot.MERGEABLE_STATES has"
                )
        for mergeable in sorted(listed - code):
            with self.subTest(mergeable=mergeable):
                self.fail(
                    f"docs/merges-and-waits.md lists {mergeable!r}, which "
                    "autopilot.MERGEABLE_STATES lacks"
                )


def _ci_note(title="", path="e2e/order-checkout.spec.ts", line=12):
    """An error annotation of a failed job, made by autopilot's own code."""
    note = {"annotation_level": "failure", "title": title, "path": path, "start_line": line}
    return autopilot._annotated("[e2e](https://job)", note)


def _ci_job(name, *steps):
    """A failed job of ci.yml whose steps all failed, made by autopilot's own code."""
    failed = [{"name": step, "conclusion": "failure"} for step in steps]
    return autopilot._job_failure("ci.yml", {"name": name, "steps": failed}, "job")


def _ci_start():
    """The failure of a run in which no job failed, made by autopilot's own code."""
    gh = mock.Mock(attempt_jobs=lambda *_: [])
    [start] = autopilot._attempt_failures(gh, "ci.yml", {"id": 50}, 1)
    return start


def _ci_shared(step, count):
    """The one failure of a step that failed the same way in `count` jobs of ci.yml."""
    found = {}
    for n in range(count):
        autopilot._merged(found, _ci_job(f"Build {n}", step), "ci.yml")
    [failure] = found.values()
    return failure


def _ci_run(count):
    """The failure of a run with `count` new failures, made by autopilot's own code."""
    failures = [{"attempts": {1: [f"- line {n}"]}} for n in range(count)]
    return autopilot._run_failure("ci.yml", {"id": 123}, failures)


def _ci_title(failure):
    """The title `ci_title` writes for a failure, with the kind of the failure."""
    return ("ci_title", {failure["kind"]}, autopilot.ci_title(failure, "main"))


def _red_title(*failures):
    """The title `red_title` writes for a branch's failures, with their kinds."""
    kinds = {failure["kind"] for failure in failures}
    return ("red_title", kinds, autopilot.red_title("ci.yml", list(failures), "main"))


TEST_NOTE = (
    "[chromium] › order-checkout.spec.ts:12:5 › order checkout › "
    "a customer pays for an order and the shop shows the receipt"
)
VITEST_NOTE = "OrderHistory > pages through more than twenty orders"

# The Failure cell of each row of the table, as the page writes it, with a builder of the titles
# that the code writes for its failures: each gives (function, kinds, title), for branch main.
CI_TITLE_ROWS = {
    "A test": lambda: [_ci_title(_ci_note(TEST_NOTE))],
    "A Vitest test": lambda: [
        _ci_title(_ci_note(VITEST_NOTE, "src/x/OrderHistory.test.tsx")),
    ],
    "An annotation with only a file and line": lambda: [
        _ci_title(_ci_note("", "src/orders.ts", 12)),
    ],
    "An annotation with only a file": lambda: [_ci_title(_ci_note("", "src/orders.ts", None))],
    "A job, or a step named like its job": lambda: [
        _ci_title(_ci_job("Integration tests")),
        _ci_title(_ci_job("Integration tests", "Integration tests")),
    ],
    "A step": lambda: [_ci_title(_ci_job("Lint and unit tests", "Unit tests"))],
    "A step that failed the same way in several jobs": lambda: [
        _ci_title(_ci_shared("Run actions/setup-dotnet@v5", 3)),
    ],
    "Runs with no failed job, two within a day": lambda: [_ci_title(_ci_start())],
    "A run with more than five new failures": lambda: [_ci_title(_ci_run(7))],
    "A red branch": lambda: [
        _red_title(_ci_job("Static checks", "Lint"), _ci_job("Lint and unit tests", "Unit tests")),
    ],
    "A red branch, failures of several kinds or more than three": lambda: [
        _red_title(*[_ci_job(f"Job {n}", f"Step {n}") for n in range(4)]),
        _red_title(
            _ci_job("Lint and unit tests", "Unit tests"),
            _ci_job("Static checks", "Lint"),
            _ci_note(VITEST_NOTE, "src/x/OrderHistory.test.tsx"),
            _ci_note("", "src/a.ts", 3),
        ),
    ],
    "A red branch with no failed job, also when run again": lambda: [_red_title(_ci_start())],
}


def ci_kinds():
    """The kinds that autopilot.py gives a failure: the strings of each `"kind"` key and each
    `kind=` keyword in its source."""
    kinds = set()
    for node in ast.walk(ast.parse(inspect.getsource(autopilot))):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "kind":
                    kinds.update(_kind_strings(value))
        elif isinstance(node, ast.keyword) and node.arg == "kind":
            kinds.update(_kind_strings(node.value))
    return kinds


def _kind_strings(value):
    """The strings that a kind can be, which must be readable: a string or conditional strings."""
    strings = state_values(value)
    if not strings:
        raise AssertionError(f"autopilot.py gives a kind the test cannot read at {value.lineno}")
    return strings


def title_forms(function):
    """A pattern for each title that `function` returns as an f-string: its constant text
    as it is, and each `{...}` as any text. Any other return fails."""
    forms = []
    for node in ast.walk(ast.parse(inspect.getsource(function))):
        if not isinstance(node, ast.Return):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_fit"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.JoinedStr)
        ):
            raise AssertionError(
                f"{function.__name__} returns a title the test cannot read at {node.lineno}"
            )
        forms.append(
            "".join(
                re.escape(part.value) if isinstance(part, ast.Constant) else ".+"
                for part in value.args[0].values
            )
        )
    return forms


class CiTitlesTests(unittest.TestCase):
    def test_the_title_table_lists_each_title_the_code_writes_and_no_other(self):
        rows = indented_table(AUTOPILOT_PAGE, CI_TITLES_HEADER)
        self.assertTrue(rows, "docs/autopilot.md has no table of CI issue titles")
        listed = {"ci_title": [], "red_title": []}  # the Title cells, by the function that wrote
        kinds = set()  # the kinds of the failures that ci_title writes a title for
        for failure, title in rows:
            with self.subTest(row=failure):
                if failure not in CI_TITLE_ROWS:
                    self.fail(f"docs/autopilot.md has a row {failure!r} the test cannot build")
                for function, row_kinds, built in CI_TITLE_ROWS[failure]():
                    self.assertEqual(
                        title,
                        built,
                        f"docs/autopilot.md row {failure!r} says {title!r}, "
                        f"but {function} writes {built!r}",
                    )
                    listed[function].append(title)
                    if function == "ci_title":
                        kinds |= row_kinds
        for failure in sorted(set(CI_TITLE_ROWS) - {row[0] for row in rows}):
            with self.subTest(builder=failure):
                self.fail(f"docs/autopilot.md has no row for {failure!r}, which the test builds")
        for kind in sorted(ci_kinds()):
            with self.subTest(kind=kind):
                if kind not in kinds:
                    self.fail(
                        f"docs/autopilot.md has no row for a {kind!r} failure, which "
                        "autopilot.py gives a kind and ci_title writes a title for"
                    )
        for name in ("ci_title", "red_title"):
            for form in title_forms(getattr(autopilot, name)):
                with self.subTest(form=form):
                    if not any(re.fullmatch(form, title) for title in listed[name]):
                        self.fail(
                            f"docs/autopilot.md has no row for a title of the form {form!r}, "
                            f"which autopilot.{name} writes"
                        )


if __name__ == "__main__":
    unittest.main()

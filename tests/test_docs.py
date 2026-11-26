"""The docs' map lists every doc, with the decisions listed through their index. The links of the
map, of the docs standard, of the architecture page, of the security page, of the decisions, of the
maintenance skills and of AGENTS.md resolve. Each task of the nexkit-maintain skill links a section
of the delivery page. AGENTS.md keeps within its word limits, and the architecture page's job and
module tables match the workflows and nexkit/. The decisions index matches the decision files, and
each decision is linked. Each test the security page names exists, as
docs/writing-docs.md#how-the-docs-stay-correct says."""

import re
import unittest
from pathlib import Path

from tests.test_cli import jobs_of, permissions_of

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+?)\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
AGENTS = ROOT / "AGENTS.md"
ARCH = DOCS / "architecture.md"
SECURITY = DOCS / "security.md"
DECISIONS = DOCS / "decisions"
DECISIONS_INDEX = DOCS / "decisions.md"
TEST_FILE = re.compile(r"tests/[\w/]+\.py")
TEST_NAME = re.compile(r"`(test_\w+)`")
WORKFLOWS = ("pipeline.yml", "pipeline-agent.yml", "pipeline-review.yml")
JOBS_HEADER = "| Workflow | Job | Job token | App token | Secrets |"
MODULES_HEADER = "| Module | Purpose |"
DECISIONS_HEADER = "| Decision | Title | Status |"
WORD = re.compile(r"\S*\w\S*")
RULES = re.compile(r"^## Rules\n(.*?)(?=^## |\Z)", re.M | re.S)
DECISION_FILE = re.compile(r"d(\d+)-[a-z0-9-]+\.md")
DECISION_TITLE = re.compile(r"# D(\d+)\. (.+)$", re.M)
DECISION_STATUS = re.compile(r"^Status: \*\*(Agreed|Proposed)\*\*$", re.M)
DECISION_LINK = re.compile(r"\[D(\d+)\]\(decisions/([^)]+)\)")


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

    def test_the_links_of_the_map_the_docs_standard_the_decisions_and_agents_md_resolve(self):
        pages = (DOCS / "README.md", DOCS / "writing-docs.md", AGENTS, ARCH, SECURITY)
        for page in (*pages, DECISIONS_INDEX, *sorted(DECISIONS.glob("*.md"))):
            for file, anchor in links(page):
                self.assertTrue(file.exists(), f"{page.name} links {file}, which does not exist")
                if anchor and file.suffix == ".md":
                    self.assertIn(
                        anchor, anchors(file), f"{page.name} links #{anchor} of {file.name}"
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


def table(path, header):
    """The rows under the table line that starts with `header`, as lists of stripped cells."""
    lines = path.read_text().splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith(header)), len(lines))
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        if not re.fullmatch(r"[|\s:-]+", line):  # the row of dashes under the header
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


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


if __name__ == "__main__":
    unittest.main()

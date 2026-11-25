"""The docs' map lists every doc, the links of the map, of the docs standard and of AGENTS.md
resolve, and AGENTS.md keeps within its word limits, as
docs/writing-docs.md#how-the-docs-stay-correct says."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+?)\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
AGENTS = ROOT / "AGENTS.md"
WORD = re.compile(r"\S*\w\S*")
RULES = re.compile(r"^## Rules\n(.*?)(?=^## |\Z)", re.M | re.S)


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
        for doc in sorted(DOCS.rglob("*.md")):
            if doc.name != "README.md":
                self.assertIn(doc.resolve(), listed, f"docs/README.md has no row for {doc.name}")

    def test_the_links_of_the_map_the_docs_standard_and_agents_md_resolve(self):
        for page in (DOCS / "README.md", DOCS / "writing-docs.md", AGENTS):
            for file, anchor in links(page):
                self.assertTrue(file.exists(), f"{page.name} links {file}, which does not exist")
                if anchor and file.suffix == ".md":
                    self.assertIn(
                        anchor, anchors(file), f"{page.name} links #{anchor} of {file.name}"
                    )


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


if __name__ == "__main__":
    unittest.main()

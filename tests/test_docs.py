"""The docs' map lists every doc, and the links of the map and of the docs standard resolve, as
docs/writing-docs.md#how-the-docs-stay-correct says."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+?)\s*$", re.M)
FENCE = re.compile(r"^```.*?^```", re.M | re.S)


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


class DocsMapTests(unittest.TestCase):
    def test_the_map_lists_every_doc(self):
        listed = {file for file, _ in links(DOCS / "README.md")}
        for doc in sorted(DOCS.rglob("*.md")):
            if doc.name != "README.md":
                self.assertIn(doc.resolve(), listed, f"docs/README.md has no row for {doc.name}")

    def test_the_links_of_the_map_and_the_docs_standard_resolve(self):
        for page in (DOCS / "README.md", DOCS / "writing-docs.md"):
            for file, anchor in links(page):
                self.assertTrue(file.exists(), f"{page.name} links {file}, which does not exist")
                if anchor and file.suffix == ".md":
                    self.assertIn(
                        anchor, anchors(file), f"{page.name} links #{anchor} of {file.name}"
                    )


if __name__ == "__main__":
    unittest.main()

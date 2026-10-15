#!/usr/bin/env python3
"""Print the GitHub release notes of a NexKit version.

    release_notes.py [VERSION]    default: the version in nexkit/__init__.py

The notes are the version's CHANGELOG.md entry, with links made absolute at the tag, then
`## Upgrade` and `## Install`. Upgrade always changes both refs of the caller workflow and
ends with `nexkit doctor`; the steps in between come from the entry's `### Upgrade`
subsection, a list. An entry without one changes neither the configuration nor the
caller's permissions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nexkit import __version__  # noqa: E402
from nexkit.scaffold import KIT_REPOSITORY, WORKFLOW_PATH  # noqa: E402

CHANGELOG = ROOT / "CHANGELOG.md"
INSTALL = """## Install

```text
/plugin marketplace add phuongnse/nexkit
/plugin install nexkit@nexkit
```

Then run `/nexkit:setup` in your repository. See the [README]({readme}) for manual setup."""
# A link to a file of the repository, such as `](docs/configuration.md#agents)`.
RELATIVE_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")
ITEM = re.compile(r"^(?:[-*]|\d+\.) ")


class NotesError(ValueError):
    pass


def entry(text, version):
    """The text under `## VERSION` in the changelog."""
    match = re.search(rf"^## {re.escape(version)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise NotesError(f"CHANGELOG.md has no entry for {version}.")
    return match.group(1).strip()


def upgrade_steps(section):
    """(the changes, each item of the `### Upgrade` subsection as a list of lines). An item's
    later lines keep their indentation under its first line, so it can hold a code block."""
    changes, *rest = re.split(r"^### Upgrade[ \t]*\n", section, maxsplit=1, flags=re.M)
    steps, indent = [], 0
    for line in (rest[0] if rest else "").splitlines():
        marker = ITEM.match(line)
        if marker:
            indent = marker.end()
            steps.append([line[indent:]])
        elif not line.strip():
            if steps:
                steps[-1].append("")
        elif steps and line.startswith(" " * indent):
            steps[-1].append(line[indent:])
        else:
            raise NotesError("### Upgrade must be a list, each item continued under its text.")
    for step in steps:
        while step[-1] == "":
            step.pop()
    return changes.strip(), steps


def absolute(text, tag, repo=KIT_REPOSITORY):
    return RELATIVE_LINK.sub(
        lambda m: f"](https://github.com/{repo}/blob/{tag}/{m.group(1)})", text
    )


def notes(version, text):
    tag = f"v{version}"
    changes, steps = upgrade_steps(entry(text, version))
    refs = (
        f"In `{WORKFLOW_PATH}`, change both the `uses:` ref and `nexkit_ref` to `{tag}`. "
        f"Or run `nexkit init --force --kit-ref {tag}` and restore your config."
    )
    items = [[refs], *steps, ["Run `nexkit doctor`."]]
    lines = ["## Upgrade", ""]
    for number, item in enumerate(items, 1):
        marker = f"{number}. "
        lines.append(marker + item[0])
        lines += [" " * len(marker) + line if line else "" for line in item[1:]]
    if not steps:
        lines += ["", "The configuration and the caller's permissions do not change."]
    readme = f"https://github.com/{KIT_REPOSITORY}#readme"
    body = "\n\n".join([changes, "\n".join(lines), INSTALL.format(readme=readme)])
    return absolute(body, tag) + "\n"


def main(argv):
    version = argv[0] if argv else __version__
    try:
        sys.stdout.write(notes(version, CHANGELOG.read_text()))
    except NotesError as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Mutation checks against NexKit 6: break one rule in a copy of the tree, and see the
differential check report it. A check that reports nothing for a broken rule does not check that
rule.

usage: python3 tests/differential/mutants.py --v6 PATH [--sample K] [--jobs J] [NAME ...]

Each mutant replaces one piece of code, which must occur exactly once. It prints, for each
mutant, how many scenarios differ, and exits 1 when the differential check does not see one that
it should. The mutants here are those that only this check sees, which break host code, and the
engine's mutants that the differential check sees too: those are `tests/mutants.py`'s, played
here with the families that cover them. The engine's mutation check, which needs no NexKit 6 and
stays after 7.0, plays the conformance suite and the walker against them. A change that moves a
decision into the engine adds mutants of its own rules there.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parent.parent
sys.path.insert(0, str(TREE))

from tests import mutants as engine_mutants  # noqa: E402

MACHINE = "nexkit/engine/machine.py"
HOST = "nexkit/host.py"
SIZE = "nexkit/protocol/size.py"
MAINTAIN = "nexkit/maintain.py"

# The engine's mutants (`tests/mutants.py`) that the differential check sees, with the
# families that cover each.
ENGINE = {
    "held-round-ignores-fixRounds": ("sweep", "round"),
    "compare-without-a-fix-round": ("diag", "chains"),
    "every-fix-round-diagnoses": ("diag", "chains"),
    "hold-ignored-on-conflict": ("conflict",),
    "no-give-back": ("give", "stop", "report", "round"),
    "conflict-rounds-plus-one": ("conflict", "released", "chains", "conflicted"),
    "conflict-at-the-accept-always-a-person": ("conflicted",),
    "behind-exception-to-the-six-hours-dropped": ("behind", "waits"),
    "person-updates-a-branch-behind": ("behind",),
    "hold-ignored-on-fix": ("report", "round", "diag", "chains"),
    "diagnosis-skips-the-first": ("diag", "chains"),
    "failure-key-name-only": ("diag", "chains", "many"),
    "required-checks-never-end": ("waits",),
    "required-checks-start-again-at-each-refusal": ("waits",),
    "tick-under-hold-waits": ("sweep", "round", "pause"),
    "head-moved-tries-again": ("refusals",),
    "transient-waits-for-checks": ("refusals",),
    "lost-once-ignored": ("lost",),
    "lost-person-restarts": ("lost",),
    "lost-waits-shifted": ("lost",),
    "transient-waits-shifted": ("pause",),
    "parent-end-ignores-the-policy": ("ended",),
    "split-keeps-the-change": ("replan",),
    "an-emptied-change-is-accepted": ("ended",),
    "failure-comment-ignored": ("failure",),
    "failure-round-failed-ignored": ("failure",),
    "refused-acceptable-version-is-tried-again": ("cleared",),
    "vouch-plans-nothing-for-an-item-with-no-change": ("vouch",),
    "vouch-starts-a-fix-for-a-stopped-merge": ("vouch",),
    "new-start-plans-nothing": ("new",),
    "sweep-key-milestone-before-priority": ("start",),
    "sweep-awaited-waits-at-the-limit": ("start",),
    "behind-in-checks-ignores-the-updates-left": ("moved",),
    "approval-accepts-under-person": ("approvals",),
    "approval-of-another-version-accepts": ("approvals",),
    "request-refused-on-a-ready-item": ("command",),
}

MUTANTS = {
    # The state keeps only the first 30 failing checks, so later ones never repeat (#347).
    "failing-capped-at-30": {
        "path": SIZE,
        "old": '    "failing": 300,',
        "new": '    "failing": 30,',
        "seen": ("harness",),
        "families": ("many", "chains"),
    },
    # The host's release of a held round reads no head a person pushed during the hold (#344).
    "release-ignores-a-push": {
        "path": HOST,
        "old": """    if head != run.get("head"):""",
        "new": """    if False:""",
        "seen": ("harness",),
        "families": ("sweep",),
    },
    # The host never says that required checks fail as the base branch does (#344).
    "required-checks-ignore-the-base": {
        "path": HOST,
        "old": """        "baseFailure": str(wait["bug"]) if wait else None,""",
        "new": """        "baseFailure": None,""",
        "seen": ("harness",),
        "families": ("required",),
    },
    # The conflict rounds used never reach the engine at a merge, so it starts a round past
    # `conflicts.max_rounds` (#423).
    "accept-answer-ignores-conflict-rounds": {
        "path": HOST,
        "old": """    doc["rounds"]["conflicts"] = max(int(conflicts or 0), 0)""",
        "new": """    doc["rounds"]["conflicts"] = 0""",
        "seen": ("harness",),
        "families": ("conflicted",),
    },
    # A `dirty` state that GitHub could not explain sooner starts a conflict round, where
    # NexKit 6 leaves it to a person (#423).
    "unknown-dirty-sent-as-conflict": {
        "path": "nexkit/report.py",
        "old": '''        refusal_kind = "needsPerson" if auto_resolve else "conflict"''',
        "new": '''        refusal_kind = "conflict"''',
        "seen": ("harness",),
        "families": ("conflicted",),
    },
    # The updates used never reach the engine at a merge, so it asks for an update past
    # `conflicts.max_rounds` (#424).
    "accept-answer-ignores-updates": {
        "path": HOST,
        "old": """    doc["rounds"]["conflicts"] = max(int(conflicts or 0), 0)
    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
""",
        "new": """    doc["rounds"]["conflicts"] = max(int(conflicts or 0), 0)
""",
        "seen": ("harness",),
        "families": ("behind",),
    },
    # A review paused at a usage limit resumes at another time (#347).
    "resume-at-another-time": {
        "path": "nexkit/report.py",
        "old": """    at = result.get("resume_at")""",
        "new": """    at = result.get("resume_at")
    at = at and at.replace("04:40", "07:10")""",
        "seen": ("harness",),
        "families": ("report", "review", "diag", "chains"),
    },
    # The plan that a round that ran out of time asked for is sent without `supersede`, so a
    # too-large one waits for a person, where NexKit 6 splits the issue (#413).
    "replan-without-supersede": {
        "path": HOST,
        "old": """            doc["asked"].append("supersede")""",
        "new": """            pass""",
        "seen": ("harness",),
        "families": ("replan",),
    },
    # A start resumes a blocked item's work when its change is observed conflicting. No check
    # sees it today: no host sends a start for a blocked item, and 2,000 walks of seed 349 break
    # no invariant of it. It is played and listed, and no check is required to see it.
    "start-resumes-a-blocked-item-on-a-conflict": {
        "path": MACHINE,
        "old": """or not self.waiting("start") or self.waiting("blocker"):
                return False""",
        "new": """or not self.waiting("start"):
                return False
            if self.waiting("blocker") and not (self.observed() or {}).get("conflicts"):
                return False""",
        "seen": (),
        "families": ("sweep",),
    },
    # The updates used never reach the engine after the base branch moved, so it asks for an
    # update past `conflicts.max_rounds` (#530).
    "base-moved-ignores-updates": {
        "path": HOST,
        "old": """    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    if checking:""",
        "new": """    if checking:""",
        "seen": ("harness",),
        "families": ("moved",),
    },
    # The conflict rounds used never reach the engine after the base branch moved, so it
    # starts a round past `conflicts.max_rounds` (#530).
    "base-moved-ignores-conflict-rounds": {
        "path": HOST,
        "old": """    doc["rounds"]["conflicts"] = max(int(used or 0), 0)
    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    if checking:""",
        "new": """    if int(updates or 0) > 0:
        doc["rounds"]["updates"] = int(updates)
    if checking:""",
        "seen": ("harness",),
        "families": ("moved",),
    },
    # The host never says that a branch is behind its base after the base branch moved, so the
    # engine stops no checks for an update (#530).
    "base-moved-never-says-behind": {
        "path": HOST,
        "old": """    if behind:
        seen["behind"] = True
""",
        "new": "",
        "seen": ("harness",),
        "families": ("moved",),
    },
    # An open sub-issue that the parent's stored parts lack does not keep the parent open, so
    # the engine closes a parent of more than ten sub-issues while one of them is open, where
    # NexKit 6 leaves it open (#720).
    "unlisted-open-sub-issue-closes-the-parent": {
        "path": HOST,
        "old": """        and str(sub["number"]) not in listed
    ]
    if unlisted:""",
        "new": """        and str(sub["number"]) not in listed
    ]
    if False:""",
        "seen": ("harness",),
        "families": ("parents",),
    },
    # A parent closes when the engine answers that a person decides, where NexKit 6 leaves one
    # whose parts were all not planned open with a note (#531).
    "parent-closes-when-a-person-decides": {
        "path": MAINTAIN,
        "old": """    if answer == "person":
        _note_once(""",
        "new": """    if False:
        _note_once(""",
        "seen": ("harness",),
        "families": ("parents",),
    },
}


def mutants():
    """Every mutant played here: the engine's that the differential check sees, then the rest."""
    engine = {
        name: {**engine_mutants.MUTANTS[name], "seen": ("harness",), "families": families}
        for name, families in ENGINE.items()
    }
    return {**engine, **MUTANTS}


def build(name, folder):
    mutant = mutants()[name]
    root = Path(folder) / name
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache")
    shutil.copytree(TREE, root, ignore=ignore)
    path = root / mutant["path"]
    text = path.read_text()
    count = text.count(mutant["old"])
    if count != 1:
        raise SystemExit(f"{name}: what it replaces occurs {count} times in {mutant['path']}")
    path.write_text(text.replace(mutant["old"], mutant["new"]))
    return root


def harness(root, v6, sample, families, jobs):
    command = [sys.executable, str(HERE / "harness.py"), "--v6", str(v6), "--tree", str(root)]
    command += ["--sample", str(sample), "--jobs", str(jobs)]
    for family in families:
        command += ["--family", family]
    found = subprocess.run(command, capture_output=True, text=True)
    last = found.stdout.strip().splitlines()[-1:] or [found.stderr[-300:]]
    match = re.search(r"(\d+) differences not listed", last[0])
    return (int(match.group(1)) if match else -1), last[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--v6", required=True, help="a checkout of NexKit 6 at 8bf5190")
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--all-families", action="store_true", help="not only the mutant's own")
    parser.add_argument("names", nargs="*", metavar="NAME", help="a mutant to play, or all")
    args = parser.parse_args(argv)
    every = mutants()
    unknown = [name for name in args.names if name not in every]
    if unknown:
        parser.error(f"no mutant {', '.join(unknown)}")
    missed = []
    with tempfile.TemporaryDirectory(prefix="nexkit-mutants-") as folder:
        for name in args.names or list(every):
            mutant = every[name]
            root = build(name, folder)
            families = [] if args.all_families else list(mutant["families"])
            differ, line = harness(root, args.v6, args.sample, families, args.jobs)
            print(f"{name}: {differ} scenarios differ ({line})")
            if "harness" in mutant["seen"] and differ <= 0:
                missed.append(name)
            shutil.rmtree(root)
    for name in missed:
        print(f"not seen by the differential check: {name}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

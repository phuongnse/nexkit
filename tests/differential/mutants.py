"""Mutation checks: break one rule in a copy of the tree, and see the differential check or
the walker report it. A check that reports nothing for a broken rule does not check that rule.

usage: python3 tests/differential/mutants.py --v6 PATH [--sample K] [--walks N] [NAME ...]

Each mutant replaces one piece of code, which must occur exactly once, and names what must
see it: the differential check (`harness`), the walker (`walker`), or both. It prints, for
each mutant, how many scenarios differ and how many invariants broke, and exits 1 when one
that should be seen is not. A change that moves a decision into the engine adds mutants of
its own rules here.
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
MACHINE = "nexkit/engine/machine.py"
HOST = "nexkit/host.py"
SIZE = "nexkit/protocol/size.py"

MUTANTS = {
    # A fix round held back, or waiting for the base branch, starts without reading
    # `fixRounds` as the host gives it now (#344).
    "held-round-ignores-fixRounds": {
        "path": MACHINE,
        "old": """        if rounds["counted"] >= self.p["fixRounds"]:
            self.used_up()
            return
        rounds["counted"] += 1""",
        "new": """        rounds["counted"] += 1""",
        "seen": ("harness", "walker"),
        "families": ("sweep", "round"),
    },
    # A round whose checks or review judged it stays `automatic`, so a later stop could give
    # its count back (#344, #347).
    "judged-round-keeps-automatic": {
        "path": MACHINE,
        "old": """        rounds = self.s["rounds"]
        # The round's checks or review judged it, so its count stands.
        rounds.pop("automatic", None)
""",
        "new": """        rounds = self.s["rounds"]
""",
        "seen": ("walker",),
        "families": ("report", "round", "diag", "give"),
    },
    # The state keeps only the first 30 failing checks, so later ones never repeat (#347).
    "failing-capped-at-30": {
        "path": SIZE,
        "old": '    "failing": 300,',
        "new": '    "failing": 30,',
        "seen": ("harness",),
        "families": ("many", "chains"),
    },
    # A counting rule: any stopped work gives a count back, automatic or not (#344).
    "give-back-any": {
        "path": MACHINE,
        "old": """        if work and rounds.pop("automatic", None):""",
        "new": """        if work and (rounds.pop("automatic", None) or rounds["counted"]):""",
        "seen": ("walker",),
        "families": ("give",),
    },
    # Checks compare with the round before even when no fix round came between (#347).
    "compare-without-a-fix-round": {
        "path": MACHINE,
        "old": """after_fix = rounds.pop("fixed", None) == self.version""",
        "new": """rounds.pop("fixed", None); after_fix = True""",
        "seen": ("harness", "walker"),
        "families": ("diag", "chains"),
    },
    # Every fix round diagnoses what repeated, a person's and a conflict round too (#347).
    "every-fix-round-diagnoses": {
        "path": MACHINE,
        "old": """fields = {"diagnose": repeated[:DIAGNOSES] if diagnose else []}""",
        "new": """fields = {"diagnose": repeated[:DIAGNOSES]}""",
        "seen": ("harness", "walker"),
        "families": ("diag", "chains"),
    },
    # A hold no longer keeps a conflict round back (#347).
    "hold-ignored-on-conflict": {
        "path": MACHINE,
        "old": """        if self.s["hold"] and policy != "person":
            return self.conflict_held()
""",
        "new": "",
        "seen": ("harness", "walker"),
        "families": ("conflict",),
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
    # Each of the following came from the independent review of #352.
    # A stopped automatic fix round never gives its count back (#344).
    "no-give-back": {
        "path": MACHINE,
        "old": """if work and rounds.pop("automatic", None):""",
        "new": """if work and rounds.pop("automatic", None) and False:""",
        "seen": ("harness", "walker"),
        "families": ("give", "stop", "report", "round"),
    },
    # One conflict round more than `conflictRounds` (#347).
    "conflict-rounds-plus-one": {
        "path": MACHINE,
        "old": """elif policy == "resolve" and rounds["conflicts"] < self.p["conflictRounds"]:""",
        "new": """elif policy == "resolve" and rounds["conflicts"] <= self.p["conflictRounds"]:""",
        "seen": ("harness", "walker"),
        "families": ("conflict", "released", "chains"),
    },
    # A judged round's next fix round starts even under a hold (#341).
    "hold-ignored-on-fix": {
        "path": MACHINE,
        "old": """            elif not self.s["hold"]:
                self.issue_fix()""",
        "new": """            else:
                self.issue_fix()""",
        "seen": ("harness", "walker"),
        "families": ("report", "round", "diag", "chains"),
    },
    # A diagnosis round skips the first check that repeated (#347).
    "diagnosis-skips-the-first": {
        "path": MACHINE,
        "old": """fields = {"diagnose": repeated[:DIAGNOSES] if diagnose else []}""",
        "new": """fields = {"diagnose": repeated[1:DIAGNOSES] if diagnose else []}""",
        "seen": ("harness", "walker"),
        "families": ("diag", "chains"),
    },
    # Checks compare by name only, not by the way they fail (#347).
    "failure-key-name-only": {
        "path": SIZE,
        "old": """found = f"{name}\\n{text}".encode("utf-8", "surrogatepass")""",
        "new": """found = f"{name}".encode("utf-8", "surrogatepass")""",
        "seen": ("harness",),
        "families": ("diag", "chains", "many"),
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
}


def build(name, folder):
    mutant = MUTANTS[name]
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


def walker(root, walks, jobs):
    command = [sys.executable, str(HERE / "walk.py"), "--tree", str(root)]
    command += ["--walks", str(walks), "--jobs", str(jobs)]
    found = subprocess.run(command, capture_output=True, text=True)
    lines = found.stdout.strip().splitlines()
    match = re.search(r"(\d+) broken invariants", lines[-1] if lines else "")
    broken = [line.strip() for line in lines if " × " in line and "listed:" not in line]
    return (int(match.group(1)) if match else -1), broken[:3] or [found.stderr[-300:]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--v6", required=True, help="a checkout of NexKit 6 at 8bf5190")
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--walks", type=int, default=1000)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--all-families", action="store_true", help="not only the mutant's own")
    parser.add_argument("names", nargs="*", metavar="NAME", help=", ".join(MUTANTS))
    args = parser.parse_args(argv)
    unknown = [name for name in args.names if name not in MUTANTS]
    if unknown:
        parser.error(f"no mutant {', '.join(unknown)}")
    missed = []
    with tempfile.TemporaryDirectory(prefix="nexkit-mutants-") as folder:
        for name in args.names or list(MUTANTS):
            mutant = MUTANTS[name]
            root = build(name, folder)
            families = [] if args.all_families else list(mutant["families"])
            differ, line = harness(root, args.v6, args.sample, families, args.jobs)
            broken, examples = walker(root, args.walks, args.jobs)
            print(f"{name}: {differ} scenarios differ ({line}); {broken} invariants broke")
            for example in examples:
                print(f"    {example}")
            if "harness" in mutant["seen"] and differ <= 0:
                missed.append(f"{name}: the differential check")
            if "walker" in mutant["seen"] and broken <= 0:
                missed.append(f"{name}: the walker")
            shutil.rmtree(root)
    for item in missed:
        print(f"not seen by {item}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

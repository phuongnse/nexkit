"""Mutants of #344's new rules, each run against the full suite and the differential.

Usage: python3 mutate.py [name ...]
"""

import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.path.expanduser("~/repos/nexkit-r344"))
M = "nexkit/engine/machine.py"
H = "nexkit/host.py"

MUTANTS = {
    # issue_fix no longer checks fixRounds when the waiting round starts.
    "no-recheck-at-issue": (M, '''        if rounds["counted"] >= self.p["fixRounds"]:
            self.used_up()
            return
        rounds["counted"] += 1''', '''        rounds["counted"] += 1'''),
    # A held round starts with the round's old policies, not those given now.
    "no-fresh-policies": (M, '''        if fresh:
            self.s["policies"] = dict(self.given)
            self.p = self.given''', '''        if False:
            pass'''),
    # The engine never gives a count back.
    "no-give-back": (M, '''        if work and rounds.pop("automatic", None):
            rounds["counted"] = max(rounds["counted"] - 1, 0)''', '''        if work and rounds.pop("automatic", None):
            pass'''),
    # Any stopped work gives a count back, automatic or not.
    "give-back-any": (M, '''        if work and rounds.pop("automatic", None):''', '''        if work and (rounds.pop("automatic", None) or rounds["counted"]):'''),
    # The review's end does not make the count stand.
    "review-keeps-automatic": (M, '''        # The round's review ended, so its count stands.
        rounds.pop("automatic", None)''', ""),
    # A person's request leaves recheck.
    "request-keeps-recheck": (M, '''        self.s["rounds"].pop("recheck", None)
        return True

    def take_place''', '''        return True

    def take_place'''),
    # A person's fix or review is refused during a wait.
    "no-request-in-wait": (M, '''        return self.state == "fixing" and not any(a["kind"] in busy for a in self.s["pending"])''', '''        return False'''),
    # A person's request is taken in fixing even while its run is pending.
    "request-while-busy": (M, '''        return self.state == "fixing" and not any(a["kind"] in busy for a in self.s["pending"])''', '''        return self.state == "fixing"'''),
    # Count at the decision, as before #344.
    "count-at-decision": (M, '''            self.move("fixing")
            rounds["due"] = True''', '''            self.move("fixing")
            rounds["due"] = True
            rounds["counted"] += 1'''),
    # The accept's checkFailed ignores the base failure.
    "accept-ignores-base": (M, '''            rounds["baseFailure"] = result.get("baseFailure") or None
            rounds.pop("recheck", None)''', '''            rounds["baseFailure"] = None
            rounds.pop("recheck", None)'''),
    # The accept's checkFailed keeps recheck.
    "accept-keeps-recheck": (M, '''            rounds["baseFailure"] = result.get("baseFailure") or None
            rounds.pop("recheck", None)''', '''            rounds["baseFailure"] = result.get("baseFailure") or None'''),
    # A move keeps `due`.
    "move-keeps-due": (M, '''        self.s["rounds"].pop("due", None)
        if name not in COUNTING:''', '''        if name not in COUNTING:'''),
    # changesRequested keeps recheck.
    "changes-keep-recheck": (M, '''        self.s["rounds"].pop("recheck", None)
        self.move("fixing")
        self.run("fix")
        return True

    def on_changeApproved''', '''        self.move("fixing")
        self.run("fix")
        return True

    def on_changeApproved'''),
    # The host's `released` ignores a head pushed during the hold.
    "released-no-push": (H, '''    if head != run.get("head"):
        fields = {"changeId": str(pull), "version": str(head or "none")}''', '''    if False:
        fields = {"changeId": str(pull), "version": str(head or "none")}'''),
    # The host's `required_failed` never names the base failure.
    "required-no-base": (H, '''        "baseFailure": str(wait["bug"]) if wait else None,''', '''        "baseFailure": None,'''),
    # The host's `stopped_counts` marks every round automatic.
    "stopped-all-automatic": (H, '''    if automatic and counted:
        doc["rounds"]["automatic"] = True''', '''    if counted:
        doc["rounds"]["automatic"] = True'''),
    # The host's `stopped_counts` treats an agent's own stop as the host's.
    "stopped-always-host": (H, '''    if by_host:
        text = "The host stopped the round before its review."''', '''    if True:
        text = "The host stopped the round before its review."'''),
    # The host's `base_passes` names another bug, so the wait does not end.
    "base-passes-wrong-bug": (H, '''    event = _event("blockerClosed", issue, None, f"base-{pull}-{bug}", blocker=bug, completed=True)''', '''    event = _event("blockerClosed", issue, None, f"base-{pull}-{bug}", blocker=bug + "x", completed=True)'''),
}


def build(name):
    path, old, new = MUTANTS[name]
    root = HERE / "mut" / name
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(SRC, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "cache"))
    file = root / path
    text = file.read_text()
    assert text.count(old) == 1, (name, text.count(old))
    file.write_text(text.replace(old, new))
    return root


def suite(root):
    proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                          cwd=root, capture_output=True, text=True)
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    failed = re.findall(r"^(?:FAIL|ERROR): (\S+) \((\S+)\)", proc.stderr, re.M)
    return proc.returncode, tail, failed


def differential(root, name):
    env = {**os.environ, "CHUNKS": "4"}
    proc = subprocess.run([sys.executable, str(HERE / "run.py"), "--tree", str(root), "--out", f"mut_{name}"],
                          capture_output=True, text=True, env=env)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("differences")]
    return lines[0] if lines else proc.stderr[-300:]


def one(name):
    root = build(name)
    code, tail, failed = suite(root)
    diff = differential(root, name)
    tests = sorted({f"{cls.rsplit('.', 1)[-1]}.{test}" for test, cls in failed})
    return name, code, tail, tests[:6], diff


def main():
    names = sys.argv[1:] or list(MUTANTS)
    with ThreadPoolExecutor(4) as pool:
        for name, code, tail, tests, diff in pool.map(one, names):
            print(f"{name}: suite {'FAILS' if code else 'PASSES'} ({tail}); {tests}; diff: {diff}", flush=True)


if __name__ == "__main__":
    main()

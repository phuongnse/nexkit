"""Mutation check of the engine: break one rule in a copy of the tree, and see the conformance
suite or the walker report it. A check that reports nothing for a broken rule does not check that
rule. It needs no other checkout.

usage: python3 tests/mutants.py [--walks N] [--jobs J] [NAME ...]

Each mutant replaces one piece of code under `nexkit/engine/` or `nexkit/protocol/`, which must
occur exactly once. For each, it plays the conformance suite (`tests.test_conformance` and
`tests.test_sweep`) and `tests/walk.py` on the copy, prints what each saw, and exits 1 when
neither saw the mutant. A change to an engine rule adds mutants of its rules here, before
NexKit 7.0 and after it. It takes a few minutes, so no CI job runs it.
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
ROOT = HERE.parent
MACHINE = "nexkit/engine/machine.py"
SIZE = "nexkit/protocol/size.py"
SWEEP = "nexkit/engine/sweep.py"
FOLDERS = ("nexkit/engine/", "nexkit/protocol/")
SUITE = ["tests.test_conformance", "tests.test_sweep"]

MUTANTS = {
    # A fix round held back, or waiting for the base branch, starts without reading
    # `fixRounds` as the host gives it now (#344).
    "held-round-ignores-fixRounds": {
        "path": MACHINE,
        "old": """        if rounds["counted"] >= self.p["fixRounds"]:
            self.used_up()
            return
        if self.stop_for_budget():""",
        "new": """        if self.stop_for_budget():""",
    },
    # A round whose checks or review judged it stays `automatic`, so a later stop could give
    # its count back (#344, #347). Only the walker sees it: the GitHub host builds no document
    # through the import, it starts each call from a fresh one, and no call sends a stop after
    # a round's checks or review judged it, so a flag kept by mistake never reaches GitHub.
    "judged-round-keeps-automatic": {
        "path": MACHINE,
        "old": """        rounds = self.s["rounds"]
        # The round's checks or review judged it, so its count stands.
        rounds.pop("automatic", None)
""",
        "new": """        rounds = self.s["rounds"]
""",
    },
    # A person's push after a fix round published gives its count back, where NexKit 6 keeps
    # it (#345). No host sends pushes yet, so only the walker sees it.
    "push-gives-back-a-published-round": {
        "path": MACHINE,
        "old": """            if self.state != "fixing":
""",
        "new": """            if False:
""",
    },
    # A counting rule: any stopped work gives a count back, automatic or not (#344).
    "give-back-any": {
        "path": MACHINE,
        "old": """        if work and rounds.pop("automatic", None):""",
        "new": """        if work and (rounds.pop("automatic", None) or rounds["counted"]):""",
    },
    # Checks compare with the round before even when no fix round came between (#347).
    "compare-without-a-fix-round": {
        "path": MACHINE,
        "old": """after_fix = rounds.pop("fixed", None) == self.version""",
        "new": """rounds.pop("fixed", None); after_fix = True""",
    },
    # Every fix round diagnoses what repeated, a person's and a conflict round too (#347).
    "every-fix-round-diagnoses": {
        "path": MACHINE,
        "old": """fields = {"diagnose": repeated[:DIAGNOSES] if diagnose else []}""",
        "new": """fields = {"diagnose": repeated[:DIAGNOSES]}""",
    },
    # A hold no longer keeps a conflict round back (#347).
    "hold-ignored-on-conflict": {
        "path": MACHINE,
        "old": """        if self.s["hold"] and policy != "person":
            return self.conflict_held()
""",
        "new": "",
    },
    # Each of the following came from the independent review of #352.
    # A stopped automatic fix round never gives its count back (#344).
    "no-give-back": {
        "path": MACHINE,
        "old": """if work and rounds.pop("automatic", None):""",
        "new": """if work and rounds.pop("automatic", None) and False:""",
    },
    # One conflict round more than `conflictRounds` (#347).
    "conflict-rounds-plus-one": {
        "path": MACHINE,
        "old": """return self.s["rounds"]["conflicts"] < self.p["conflictRounds"]""",
        "new": """return self.s["rounds"]["conflicts"] <= self.p["conflictRounds"]""",
    },
    # A conflict at the merge always waits for a person, whatever the policy and the rounds
    # left (#423).
    "conflict-at-the-accept-always-a-person": {
        "path": MACHINE,
        "old": """        elif result["refusal"] == "conflict":
            self.on_conflict_found()
""",
        "new": """        elif result["refusal"] == "conflict":
            self.move("ready")
            self.need("acceptRefused", result["text"] or "The accept was refused.")
""",
    },
    # An update is counted with the conflict rounds, not apart from them (#424). The host sends
    # no update's result to the engine when it succeeds, so only the walker sees it.
    "updates-counted-as-conflict-rounds": {
        "path": MACHINE,
        "old": """        rounds["updates"] = rounds.get("updates", 0) + 1
""",
        "new": """        rounds["conflicts"] += 1
""",
    },
    # A branch behind its base ends the six hours of a merge that waits for required checks,
    # where NexKit 6 keeps trying it under `conflicts.auto_resolve` (#424).
    "behind-exception-to-the-six-hours-dropped": {
        "path": MACHINE,
        "old": """if checks and self.checks_waited() and not self.behind_exempt():""",
        "new": """if checks and self.checks_waited():""",
    },
    # Under `person`, a branch behind its base is updated, where NexKit 6 leaves it to a
    # person (#424).
    "person-updates-a-branch-behind": {
        "path": MACHINE,
        "old": """            if self.p["conflicts"] == "person":
                self.move("ready")
                self.need("acceptRefused", result["text"] or "The accept was refused.")
            elif self.update_left():""",
        "new": """            if self.update_left():""",
    },
    # An automatic fix request that NexKit dispatches starts as a person's round: not counted,
    # not marked automatic, and no diagnosis (#765).
    "automatic-request-counts-nothing": {
        "path": MACHINE,
        "old": """            if event.get("automatic"):""",
        "new": """            if False:""",
    },
    # A judged round's next fix round starts even under a hold (#341).
    "hold-ignored-on-fix": {
        "path": MACHINE,
        "old": """            elif not self.s["hold"]:
                self.issue_fix()""",
        "new": """            else:
                self.issue_fix()""",
    },
    # A diagnosis round skips the first check that repeated (#347).
    "diagnosis-skips-the-first": {
        "path": MACHINE,
        "old": """fields = {"diagnose": repeated[:DIAGNOSES] if diagnose else []}""",
        "new": """fields = {"diagnose": repeated[1:DIAGNOSES] if diagnose else []}""",
    },
    # Checks compare by name only, not by the way they fail (#347).
    "failure-key-name-only": {
        "path": SIZE,
        "old": """found = f"{name}\\n{text}".encode("utf-8", "surrogatepass")""",
        "new": """found = f"{name}".encode("utf-8", "surrogatepass")""",
    },
    # The wait for required checks never ends (#390).
    "required-checks-never-end": {
        "path": MACHINE,
        "old": """and now - began > CHECKS_END""",
        "new": """and False""",
    },
    # It ends at six hours, not after them (#390).
    "required-checks-end-at-six-hours": {
        "path": MACHINE,
        "old": """now - began > CHECKS_END""",
        "new": """now - began >= CHECKS_END""",
    },
    # Each refusal starts the six hours again (#390).
    "required-checks-start-again-at-each-refusal": {
        "path": MACHINE,
        "old": """            if not since or since["version"] != self.version:
                self.s["checksSince"]""",
        "new": """            if True:
                self.s["checksSince"]""",
    },
    # The start outlives the item's stay in `accepting`, so a new version keeps it (#390).
    "required-checks-start-kept-across-a-new-version": {
        "path": MACHINE,
        "old": """        if name != "accepting":
            self.s.pop("checksSince", None)""",
        "new": """        if False:
            self.s.pop("checksSince", None)""",
    },
    # The count of runs after a transient stop is not started afresh by a new run (#396). Only
    # the walker sees it: no GitHub path sends a transient result to the engine yet.
    "transient-count-never-resets": {
        "path": MACHINE,
        "old": """            self.forget_retries()
        self.s["rounds"]["started"]""",
        "new": """            pass
        self.s["rounds"]["started"]""",
    },
    # One wait of `retryWaits` is never used (#396).
    "transient-one-wait-short": {
        "path": MACHINE,
        "old": """        if count >= len(waits):""",
        "new": """        if count >= len(waits) - 1:""",
    },
    # A hold keeps back a run that waited for a time or a usage limit, where NexKit 6's resume
    # reads no hold label (#396).
    "tick-under-hold-waits": {
        "path": MACHINE,
        "old": """        self.resume(hold=not paused)""",
        "new": """        self.resume()""",
    },
    # A run again after a transient stop counts as a new round (#396).
    "transient-run-again-counts": {
        "path": MACHINE,
        "old": """        return self.run(stage, again=True, diagnose=automatic)""",
        "new": """        return self.run(stage, again=False, diagnose=automatic)""",
    },
    # A merge refused because the head moved tries the accept again, where it waits for a
    # person (#391).
    "head-moved-tries-again": {
        "path": MACHINE,
        "old": """        elif result["refusal"] in ("transient", "headMoved", "needsPerson"):""",
        "new": """        elif result["refusal"] == "headMoved":
            self.do("accept", changeVersion=self.version, by="engine")
        elif result["refusal"] in ("transient", "needsPerson"):""",
    },
    # A refusal after the base branch kept moving waits for the required checks, as if they
    # had not passed (#391).
    "transient-waits-for-checks": {
        "path": MACHINE,
        "old": """        elif result["refusal"] == "waitingForChecks":""",
        "new": """        elif result["refusal"] in ("waitingForChecks", "transient"):""",
    },
    # A refusal only a person clears fails the item, where it is the person's to carry on
    # (#391). Only the walker sees it: the host reads a person's turn from the answer's
    # notice, which a failed item gives too.
    "needs-person-fails": {
        "path": MACHINE,
        "old": """        elif result["refusal"] == "notAccepted":""",
        "new": """        elif result["refusal"] in ("notAccepted", "needsPerson"):""",
    },
    # The count of lost work is not started afresh when work finishes, so a later loss finds
    # the waits used up (#397). Only the walker sees it: no GitHub path sends a loss to the
    # engine yet.
    "lost-count-never-resets": {
        "path": MACHINE,
        "old": """            self.forget_lost()
        # The round of a person's work""",
        "new": """            pass
        # The round of a person's work""",
    },
    # A hold does not keep back the restart of lost work at its tick, where autopilot's sweep
    # skips held issues (#397).
    "lost-restart-ignores-hold": {
        "path": MACHINE,
        "old": """            and not restart
""",
        "new": "",
    },
    # A lost run whose agent ran starts as often as the waits allow, not only once more
    # (#397).
    "lost-once-ignored": {
        "path": MACHINE,
        "old": """        if count >= (min(1, len(waits)) if once else len(waits)):""",
        "new": """        if count >= len(waits):""",
    },
    # A person's lost work starts again as the engine's own does, where NexKit 6's
    # `_reportless` waits for a person (#397).
    "lost-person-restarts": {
        "path": MACHINE,
        "old": """        if action.get("by") == "person":
            return False
        if action["kind"] == "runAgent":""",
        "new": """        if action["kind"] == "runAgent":""",
    },
    # Lost work waits as long as the last wait, not the next (#399).
    "lost-waits-shifted": {
        "path": MACHINE,
        "old": """        if waits[count] == 0:
            self.resume()
            return
        until = _later(self.at, waits[count])""",
        "new": """        if waits[count] == 0:
            self.resume()
            return
        until = _later(self.at, waits[-1])""",
    },
    # A transient stop waits as long as the last wait, not the next (#398).
    "transient-waits-shifted": {
        "path": MACHINE,
        "old": """            self.run_again(stage)
            return
        until = _later(self.at, waits[count])""",
        "new": """            self.run_again(stage)
            return
        until = _later(self.at, waits[-1])""",
    },
    # A split parent ends whatever `parentEnd` says, where NexKit 6 closes a parent only with
    # `close_parent_issues` (#393). The host sends `person` without it, so the merge's parent
    # close differs on GitHub.
    "parent-end-ignores-the-policy": {
        "path": MACHINE,
        "old": """        elif self.p["parentEnd"] == "whenPartAccepted":""",
        "new": """        elif True:""",
    },
    # A change that became empty ends the item as accepted, not as `noChange`, so the host
    # closes no pull request on its base branch (#393).
    # A split of an item with a change leaves it open, with no action that withdraws it. The
    # host refuses it, so on GitHub the issue waits for a person, where NexKit 6 splits it and
    # closes its pull request (#413).
    "split-keeps-the-change": {
        "path": MACHINE,
        "old": """            self.do(
                "withdrawChange",
                changeId=change["id"],
                reason="The parts of the split carry the work.",
            )""",
        "new": """            pass""",
    },
    "an-emptied-change-is-accepted": {
        "path": MACHINE,
        "old": """        elif observed["empty"]:
            self.move("noChange")""",
        "new": """        elif observed["empty"]:
            self.move("accepted")""",
    },
    # A person's comment no longer plans again an item that failed for nothing (#415). The
    # sweep of NexKit 6 does, and the walker sees it.
    "failure-comment-ignored": {
        "path": MACHINE,
        "old": """        if self.fails_for_nothing():
            self.start_plan()
            return True
        plan = self.s["plan"]""",
        "new": """        plan = self.s["plan"]""",
    },
    # An item with a change fails for nothing too and is planned again by a comment (#415).
    # On GitHub an issue's `/nexkit go` never has a change, so only the walker sees it.
    "failure-ignores-change": {
        "path": MACHINE,
        "old": """            and not self.s["change"]
            and (self.s["attention"] or {}).get("reason") in (""",
        "new": """            and (self.s["attention"] or {}).get("reason") in (""",
    },
    # An implement round that failed on an error is no longer planned again by a comment or a
    # blocker's close (#537). NexKit 6 plans it again, so both checks see it.
    "failure-round-failed-ignored": {
        "path": MACHINE,
        "old": """.get("reason") in ("blocked", "roundFailed")""",
        "new": """.get("reason") == "blocked\"""",
    },
    # A refused accept is tried again for a version that was refused while it was observed
    # acceptable, where NexKit 6 never tries that head again (#570). Both checks see it: the
    # host sends `refused_clean` as `refusedAcceptable`.
    "refused-acceptable-version-is-tried-again": {
        "path": MACHINE,
        "old": """            and self.s.get("refusedAcceptable") != self.version
""",
        "new": "",
    },
    # A trust from anyone but a writer vouches too (#538). The host sends only a writer's, so
    # only the walker sees it.
    "vouch-ignores-the-role": {
        "path": MACHINE,
        "old": """            or by["role"] not in WRITERS
""",
        "new": "",
    },
    # A writer's trust vouches on any stop, not only an untrusted one (#538). The host builds
    # only untrusted stops, so only the walker sees it.
    "vouch-ignores-the-attention": {
        "path": MACHINE,
        "old": """            or (self.s["attention"] or {}).get("reason") != "untrusted"
""",
        "new": "",
    },
    # The vouched fix round runs without counting, so it goes past `fixRounds` when none is
    # left (#538). NexKit 6 counts it, but the host counts only when it starts the round, so
    # only the walker sees it.
    "vouch-fix-round-is-not-counted": {
        "path": MACHINE,
        "old": """            if not self.s["hold"]:
                self.issue_fix(fresh=True)
        elif self.state == "ready":""",
        "new": """            if not self.s["hold"]:
                self.s["rounds"].pop("due", None)
                self.by = None
                self.run("fix")
        elif self.state == "ready":""",
    },
    # An item with no change is not planned again by a writer's vouch, so the issue waits
    # for a person on GitHub, where NexKit 6 plans it (#538).
    "vouch-plans-nothing-for-an-item-with-no-change": {
        "path": MACHINE,
        "old": """        if self.state == "failed" and not self.s["change"]:
            self.start_plan()
        elif self.state == "failed":
            # The round that stopped""",
        "new": """        if self.state == "failed" and not self.s["change"]:
            return False
        elif self.state == "failed":
            # The round that stopped""",
    },
    # A stopped merge starts a fix round after a writer's vouch instead of its checks and
    # review (#538).
    "vouch-starts-a-fix-for-a-stopped-merge": {
        "path": MACHINE,
        "old": """            self.move("checking")
            self.check()
        else:
            return False
        return True

    def takes_request""",
        "new": """            self.move("fixing")
            self.run("fix")
        else:
            return False
        return True

    def takes_request""",
    },
    # A new item's start plans nothing, so the issue waits for a person on GitHub, where
    # NexKit 6 plans it (#438).
    "new-start-plans-nothing": {
        "path": MACHINE,
        "old": """            if state == "new" and not self.s["hold"]:
                self.start_plan()""",
        "new": """            if state == "new" and not self.s["hold"]:
                return False""",
    },
    # A held new item starts too (#438). The GitHub host never sends a held document, as
    # NexKit 6's checks stop a held issue first, so only the walker sees it.
    "held-new-item-starts": {
        "path": MACHINE,
        "old": """if state == "new" and not self.s["hold"]:""",
        "new": """if state == "new":""",
    },
    # A start ignores a blocker wait, so a blocked new item plans (#438). The GitHub host never
    # sends a blocked document, as NexKit 6's checks stop a blocked issue first, so only the
    # walker sees it.
    "start-ignores-a-blocker-wait": {
        "path": MACHINE,
        "old": """ or not self.waiting("start") or self.waiting("blocker"):""",
        "new": """ or not self.waiting("start"):""",
    },
    # The sweep orders by group before priority, so an item of a later group with a priority
    # tag no longer goes first (#437).
    "sweep-key-group-before-priority": {
        "path": SWEEP,
        "old": "return (rank, group is None, group or 0, wave, position)",
        "new": "return (group is None, group or 0, rank, wave, position)",
    },
    # An awaited item waits for a place at the limit like any other, so the bug issue that pull
    # requests wait for does not start beyond `max_parallel` (#437).
    "sweep-awaited-waits-at-the-limit": {
        "path": SWEEP,
        "old": 'elif full and not item.get("awaited", False):',
        "new": "elif full:",
    },
    # Checks that run on a branch behind its base stop and update it past `conflictRounds`,
    # where NexKit 6 leaves them to their end (#530).
    "behind-in-checks-ignores-the-updates-left": {
        "path": MACHINE,
        "old": """            and self.update_left()
            and any(a["kind"] == "runChecks" for a in self.s["pending"])""",
        "new": """            and any(a["kind"] == "runChecks" for a in self.s["pending"])""",
    },
    # Checks that run on a branch behind its base stop and update it under `person`, where
    # NexKit 6 never starts one without `conflicts.auto_resolve` (#530).
    "behind-in-checks-ignores-person": {
        "path": MACHINE,
        "old": """            and self.p["conflicts"] != "person"
            and self.update_left()""",
        "new": """            and self.update_left()""",
    },
    # A run's budget is its stage's, not capped by what is left of the item's. Only the walker
    # sees it: the GitHub host sends no item budget.
    "item-budget-uncapped": {
        "path": MACHINE,
        "old": """                if left is not None and (budget[key] is None or left < budget[key]):""",  # noqa: E501
        "new": """                if False:""",
    },
    # An agent run starts on an item whose own budget is spent. Only the walker sees it.
    "item-budget-spent-run-starts": {
        "path": MACHINE,
        "old": """        if stage != "plan" and self.stop_for_budget():
            return False
""",
        "new": "",
    },
    # A person's approval accepts a change under any accept policy, not only `autoWhenPassed`,
    # where NexKit 6 merges only with `merge.auto` (#569).
    "approval-accepts-under-person": {
        "path": MACHINE,
        "old": """            or self.p["accept"] != "autoWhenPassed"
""",
        "new": "",
    },
    # A new item's blockers add no wait (#914).
    "created-drops-blockers": {
        "path": MACHINE,
        "old": """        for blocker in dict.fromkeys(event.get("blockers") or []):
            self.wait("blocker", on=blocker)
""",
        "new": "",
    },
    # A part whose item above asked for a split asks again (#914).
    "created-drops-splitAsked": {
        "path": MACHINE,
        "old": """        if event.get("splitAsked") and "split" not in self.s["asked"]:
            self.s["asked"].append("split")
""",
        "new": "",
    },
    # An approval of another version accepts a change that waits for its accept, where NexKit 6
    # merges a ready round only on the head that passed (#569).
    "approval-of-another-version-accepts": {
        "path": MACHINE,
        "old": """            or (reason == "accept" and event["changeVersion"] != self.version)
""",
        "new": "",
    },
    # A person's fix or review request is refused on a ready item, so no command starts a
    # round on a NexKit pull request, where NexKit 6 starts it (#568).
    "request-refused-on-a-ready-item": {
        "path": MACHINE,
        "old": """        if self.state in states:
            return True
        busy =""",
        "new": """        if self.state in states and self.state != "ready":
            return True
        busy =""",
    },
    # A start resumes a blocked item's work when its change is observed conflicting, where a
    # blocker keeps the item waiting whatever its change shows. The case
    # `a-start-keeps-a-blocked-item-waiting-whatever-its-change-shows` sees it.
    "start-resumes-a-blocked-item-on-a-conflict": {
        "path": MACHINE,
        "old": """or not self.waiting("start") or self.waiting("blocker"):
                return False""",
        "new": """or not self.waiting("start"):
                return False
            if self.waiting("blocker") and not (self.observed() or {}).get("conflicts"):
                return False""",
    },
    # A round that ran out of time asks for a plan without `supersede`, so the plan of an item
    # with a change that is split does not withdraw that change (#413). The conformance cases
    # that name a timed-out fix round in their titles see it.
    "timed-out-change-asks-no-supersede": {
        "path": MACHINE,
        "old": """            if self.s["change"] and "supersede" not in self.s["asked"]:""",
        "new": """            if False:""",
    },
}


def _ignored(folder, names):
    """What the copy leaves out: no history or cache."""
    return shutil.ignore_patterns(".git", "__pycache__", ".ruff_cache")(folder, names)


def build(name, folder):
    """A copy of the tree in `folder` with the mutant `name` applied."""
    mutant = MUTANTS[name]
    root = Path(folder) / name
    shutil.copytree(ROOT, root, ignore=_ignored)
    path = root / mutant["path"]
    text = path.read_text()
    count = text.count(mutant["old"])
    if count != 1:
        raise SystemExit(f"{name}: what it replaces occurs {count} times in {mutant['path']}")
    mutated = text.replace(mutant["old"], mutant["new"])
    try:
        compile(mutated, mutant["path"], "exec")
    except SyntaxError as error:
        raise SystemExit(f"{name}: the mutated code is not Python: {error}") from error
    path.write_text(mutated)
    return root


def start_suite(root):
    command = [sys.executable, "-m", "unittest", "-q", *SUITE]
    return subprocess.Popen(
        command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )


def finish_suite(process):
    """Whether the suite failed, and the line that says why."""
    _, errors = process.communicate()
    lines = [line for line in errors.strip().splitlines() if line.strip()]
    failed = [line for line in lines if line.startswith(("FAIL:", "ERROR:"))]
    return process.returncode != 0, (failed or lines[-1:] or [""])[0]


def walker(root, walks, jobs):
    """How many invariants broke in the walks, and the first lines that name them."""
    command = [sys.executable, str(HERE / "walk.py"), "--tree", str(root)]
    command += ["--walks", str(walks), "--jobs", str(jobs)]
    found = subprocess.run(command, capture_output=True, text=True)
    lines = found.stdout.strip().splitlines()
    match = re.search(r"(\d+) broken invariants", lines[-1] if lines else "")
    broken = [line.strip() for line in lines if " × " in line and "listed:" not in line]
    return (int(match.group(1)) if match else -1), broken[:3] or [found.stderr[-300:]]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--walks", type=int, default=1000)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("names", nargs="*", metavar="NAME", help=", ".join(MUTANTS))
    args = parser.parse_args(argv)
    unknown = [name for name in args.names if name not in MUTANTS]
    if unknown:
        parser.error(f"no mutant {', '.join(unknown)}")
    missed = []
    with tempfile.TemporaryDirectory(prefix="nexkit-mutants-") as folder:
        for name in args.names or list(MUTANTS):
            root = build(name, folder)
            suite = start_suite(root)
            broken, examples = walker(root, args.walks, args.jobs)
            failed, line = finish_suite(suite)
            print(
                f"{name}: conformance {'sees it' if failed else 'does not'}"
                f" ({line}); {broken} invariants broke"
            )
            for example in examples if broken > 0 else []:
                print(f"    {example}")
            if not failed and broken <= 0:
                missed.append(name)
            shutil.rmtree(root)
    for name in missed:
        print(f"not seen: {name}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

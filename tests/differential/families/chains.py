"""#347's review: random chains of 1-4 rounds on a NexKit pull request or a person's own,
with check names that are plain, 40 characters long, sharded past 40 characters (two shards
that clip to the same name), 36 checks, or a mix, a check that appears twice, outputs with no
first line or a long one; the round an automatic, a person's, a conflict, a resumed or a
retried fix round, a review autopilot or a person asks for, an implement round or a person's
own pull request; its checks failing, failing the same way, passing or not run; its review
approving, requesting changes, failing, transient, paused with a known or unknown reset, or
missing; a commit that conflicts, a fix round that changed nothing, a hold label (or one
NexKit cannot read), the base branch failing the same way, conflict rounds used. Records
`report` and GitHub after each round, and what `route` decides for the next fix round as five
kinds of dispatch (automatic, a person's, a conflict round, resumed, autopilot's)."""

import datetime as _dt
import json
import random
import sys
from unittest import mock

from common import TOK, WORKFLOW, Artifacts, attempt, quiet, snapshot

from nexkit import autopilot, maintain, progress  # noqa: F401
from nexkit import report as report_mod
from nexkit import route as route_mod
from nexkit.state import empty_state, read_state, render_state
from tests.support import FakeGitHub, make_config

COUNT = 30000
SEED = 347
FIXED = _dt.datetime(2026, 11, 24, 12, 0, 0, tzinfo=_dt.UTC)


class _Frozen(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz else FIXED.replace(tzinfo=None)


# The time stands still in every module of NexKit's that reads it, so a chain plays alike on
# both trees whenever it runs.
for _module in list(sys.modules.values()):
    if getattr(_module, "__name__", "").startswith("nexkit"):
        if getattr(_module, "datetime", None) is _dt.datetime:
            _module.datetime = _Frozen

LONG = "x" * 36  # the longest sharded check name: shards add up to three characters
NAMESETS = {
    "plain": ["test", "lint"],
    "long": ["a" * 40, "b" * 40],
    "shard41": [LONG + "-1", LONG + "-2"],  # both clip to the same 40 characters
    "shard43": ["y" * 36 + "-10", "y" * 36 + "-11"],
    "many": [f"c{i:02d}" for i in range(36)],
    "mixed": ["test", LONG + "-1", LONG + "-2", "lint"],
}
OUTPUTS = ["AssertionError", "TypeError: x", "", "E" * 200, "AssertionError\nmore"]
ROUND_KINDS = [
    "auto_fix",
    "auto_fix",
    "auto_fix",
    "person_fix",
    "conflict_fix",
    "resumed_fix",
    "retry_fix",
    "auto_review",
    "person_review",
    "implement",
    "person_pr",
]
REVIEW_KINDS = [
    "approve",
    "approve",
    "request_changes",
    "request_changes",
    "error",
    "transient",
    "paused",
    "paused_unknown",
    "missing",
    "blocking_approve",
]


def _first_checks(rng, names, mode):
    if mode == "none":
        return None
    found = []
    for i, name in enumerate(names):
        if mode == "pass":
            passed = True
        elif mode == "allfail":
            passed = False
        elif mode == "tailfail":  # only the later checks fail
            passed = i < len(names) - 4
        else:
            passed = rng.random() < 0.4
        output = "" if passed else rng.choice(OUTPUTS)
        found.append(
            {
                "name": name,
                "run": f"run {name}",
                "exit_code": 0 if passed else 1,
                "passed": passed,
                "output": output,
            }
        )
    if found and rng.random() < 0.1:
        found.append(dict(found[0], passed=False, exit_code=1, output="dup"))  # a name twice
    return found


def _round(rng):
    return {
        "kind": rng.choice(ROUND_KINDS),
        "checks": rng.choice(
            ["fail", "fail", "allfail", "same", "same", "pass", "none", "tailfail"]
        ),
        "review": rng.choice(REVIEW_KINDS),
        "conflicts": rng.random() < 0.25,
        "recheck": rng.random() < 0.1,
        "held": rng.choice([None, None, None, "on-hold", "UNREAD"]),
        "base_failing": rng.random() < 0.15,
        "used_conflicts": rng.choice([0, 0, 1, 2, 3]),
    }


def scenarios():
    rng = random.Random(SEED)
    for _ in range(COUNT):
        nameset = rng.choice(list(NAMESETS))
        names = NAMESETS[nameset]
        before_mode = rng.choice(
            ["fail", "allfail", "pass", "none", "nofeedback", "nosig", "tailfail"]
        )
        mode = {"nosig": "allfail"}.get(before_mode, before_mode)
        before = None if before_mode == "nofeedback" else _first_checks(rng, names, mode)
        if before and before_mode != "nosig":
            for check in before:
                if not check["passed"]:
                    check["signature"] = check["output"].splitlines()[0] if check["output"] else ""
        sc = {
            "nameset": nameset,
            "before": before,
            "before_mode": before_mode,
            "prior_repeated": rng.choice([None, None, names[:1], names[:3]]),
            "auto_fixes": rng.choice([0, 0, 1, 2, 3]),
            "cfg": {
                "max_auto_rounds": rng.choice([0, 1, 2, 3]),
                "merge_auto": rng.random() < 0.5,
                "autopilot": rng.random() < 0.6,
                "auto_resolve": rng.random() < 0.6,
                "max_rounds": rng.choice([1, 2, 3]),
                "resume": rng.random() < 0.6,
                "retry": rng.choice([[0], [], [5]]),
            },
            "rounds": [_round(rng) for _ in range(rng.choice([1, 2, 2, 3, 3, 4]))],
        }
        if not sc["cfg"]["auto_resolve"]:
            # `publish` reports a conflict only with `conflicts.auto_resolve` on.
            for found in sc["rounds"]:
                found["conflicts"] = False
        yield sc


def _review(kind):
    def result(verdict="approve", blocking=False):
        findings = []
        if blocking or verdict == "request_changes":
            findings = [{"severity": "blocking", "file": "a.py", "line": 1, "body": "Bad."}]
        criterion = {"criterion": "add works", "status": "met", "test": "test_add"}
        output = {
            "verdict": verdict,
            "summary": "Looks right.",
            "criteria": [{**criterion, "evidence": "test_calc.py:5"}],
            "findings": findings,
        }
        tokens = {"input": 300, "cached": 100, "output": 50}
        return {"status": "done", "tokens": tokens, "output": output}

    limit = {"status": "paused", "error": "usage limit"}
    return {
        "approve": lambda: result(),
        "blocking_approve": lambda: result(blocking=True),
        "request_changes": lambda: result("request_changes"),
        "error": lambda: {"status": "error", "error": "Review crashed", "tokens": TOK},
        "transient": lambda: {
            "status": "error",
            "error": "Overloaded",
            "transient": True,
            "tokens": TOK,
        },
        "paused": lambda: {
            **limit,
            "limit": "You've hit your limit · resets 4:40am (UTC)",
            "resume_at": "2099-10-11T04:40:00Z",
            "reset_known": True,
        },
        "paused_unknown": lambda: {
            **limit,
            "limit": "You've hit your limit",
            "resume_at": "2099-10-11T18:00:00Z",
            "reset_known": False,
        },
        "missing": lambda: None,
    }[kind]()


def _config(sc):
    c = sc["cfg"]
    checks, seen = [], set()
    for name in NAMESETS[sc["nameset"]]:
        base = name
        if name.startswith(LONG + "-"):
            base = LONG
        if name.startswith("y" * 36 + "-"):
            base = "y" * 36
        if base in seen:
            continue
        seen.add(base)
        check = {"name": base, "run": f"run {base}"}
        if base != name:
            check["shards"] = {"count": "echo 2", "max": 20}
        checks.append(check)
    return make_config(
        checks=checks,
        parallel_checks=True,
        fix={"max_auto_rounds": c["max_auto_rounds"]},
        merge={"auto": c["merge_auto"]},
        autopilot={"enabled": c["autopilot"]},
        conflicts={"auto_resolve": c["auto_resolve"], "max_rounds": c["max_rounds"]},
        usage_limit={"resume": c["resume"]},
        retry={"wait_minutes": c["retry"]},
    )


def _checks_now(sc, mode, before, index):
    names = NAMESETS[sc["nameset"]]
    if mode == "none":
        return None
    if mode == "same":
        if not before:
            mode = "allfail"
        else:
            return [{k: v for k, v in c.items() if k != "signature"} for c in before]
    found = []
    for i, name in enumerate(names):
        if mode == "pass":
            passed = True
        elif mode == "allfail":
            passed = False
        elif mode == "tailfail":
            passed = i < len(names) - 4
        else:
            passed = (i + index) % 3 == 0
        output = "" if passed else ["AssertionError", "TypeError: x", ""][(i + index) % 3]
        found.append(
            {
                "name": name,
                "run": f"run {name}",
                "exit_code": 0 if passed else 1,
                "passed": passed,
                "output": output,
            }
        )
    return found


def _held(label):
    if label == "UNREAD":
        return lambda gh, cfg, number: report_mod.UNREAD_HOLD
    return lambda gh, cfg, number: label


def _decision(kind, base):
    if kind in ("auto_fix", "conflict_fix", "resumed_fix", "retry_fix"):
        d = {**base, "action": "fix", "auto": True, "autopilot": True}
        if kind == "conflict_fix":
            d["conflicts"] = True
            d["base_sha"] = "d" * 40
        if kind == "resumed_fix":
            d["resumed"] = True
        if kind == "retry_fix":
            d["retry"] = 1
        return d, True
    if kind == "person_fix":
        return {**base, "action": "fix"}, True
    if kind == "auto_review":
        return {**base, "action": "review", "auto": True, "autopilot": True}, False
    if kind == "person_review":
        return {**base, "action": "review"}, False
    if kind == "implement":
        return {**base, "action": "implement", "pr": None, "target": 5, "head": None}, True
    return {**base, "action": "review", "issue": 7, "person": True}, False  # person_pr


def run(sc):
    cfg = _config(sc)
    gh = FakeGitHub()
    gh.add_issue(5)
    head = "c" * 40
    gh.add_pull(6, 5, head_sha=head)
    gh.add_pull(7, 0, head_sha="e" * 40, branch="feature/x")
    gh.branches["main"] = "d" * 40
    state = {**empty_state(5), "auto_fixes": sc["auto_fixes"]}
    if sc["before"] is not None or sc["before_mode"] != "nofeedback":
        state["feedback"] = {"head": head, "checks": sc["before"], "review": None}
        if sc["prior_repeated"]:
            state["feedback"]["repeated"] = sc["prior_repeated"]
    gh.comment(6, render_state(state))
    person = {**empty_state(7), "person": True, "feedback": state.get("feedback")}
    gh.comment(7, render_state(person))
    res = {"steps": []}
    for i, rd in enumerate(sc["rounds"]):
        pr = 7 if rd["kind"] == "person_pr" else 6
        current = gh.pulls[pr]["head"]["sha"]
        new_head = current if rd["recheck"] else f"{i + 1:x}" * 40
        base = {
            "issue": 5,
            "pr": pr,
            "target": pr,
            "branch": gh.pulls[pr]["head"]["ref"],
            "base": "main",
            "head": current,
            "note": "",
            "auto": False,
            "run_id": 101 + i,
        }
        d, publishes = _decision(rd["kind"], base)
        publish = {"published": True, "pr": pr, "head": new_head} if publishes else None
        if publish is not None and d["action"] == "fix" and rd["recheck"]:
            publish["recheck"] = True
        if publish is not None and rd["conflicts"]:
            publish["conflicts"] = ["a.py"]
        if publish is not None:
            gh.pulls[pr]["head"]["sha"] = new_head
        for n in range(rd["used_conflicts"] if rd["conflicts"] and i == 0 else 0):
            gh.round_comment(pr, 90 + n, "b" * 40, "failed", conflicts=True, base_sha="d" * 40)
        art = Artifacts()
        _, found = read_state(gh.comments(pr))
        before = ((found or {}).get("feedback") or {}).get("checks")
        art.agent({"status": "done", "tokens": TOK})
        conflicting = publish is not None and rd["conflicts"]
        checks = _checks_now(sc, rd["checks"], before, i)
        if not conflicting:
            art.checks(checks)
            art.review(_review(rd["review"]))
        needs = {}
        if publish is not None:
            needs["publish"] = {"outputs": {"result": json.dumps(publish)}}
        wait = None
        if rd["base_failing"]:
            wait = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 5}

        def ci_wait(gh_, cfg_, d_, checks_, branch_, wait=wait):
            return wait if any(not c.get("passed") for c in checks_ or []) else None

        step = {"kind": rd["kind"], "decision": d}
        with (
            mock.patch.dict("os.environ", {"GITHUB_RUN_ID": str(101 + i)}),
            mock.patch.object(report_mod, "_held", _held(rd["held"])),
            mock.patch.object(report_mod, "_ci_wait", ci_wait),
            quiet(),
        ):
            step["outcome"] = attempt(
                report_mod.report,
                gh,
                d,
                cfg,
                needs,
                art.path,
                workflow_ref=WORKFLOW,
                default_branch="main",
            )
        art.close()
        step["gh"] = snapshot(gh)
        # What route decides for the next fix round: automatic, a person's, a conflict round.
        routes = {}
        for auto in ("true", "", "conflicts", "true,resumed", "autopilot"):
            inputs = {"command": "fix", "number": str(pr), "note": "", "auto": auto}
            event = {"inputs": inputs, "sender": {"login": "alice"}}

            def decide(inputs=inputs, event=event):
                found = route_mod.route(gh, "workflow_dispatch", event, inputs)
                return {k: found.get(k) for k in ("action", "diagnose", "conflicts", "reason")}

            with quiet():
                routes[auto] = attempt(decide)
        step["routes"] = routes
        res["steps"].append(step)
    return res

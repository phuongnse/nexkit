"""#347: chains of 2-4 rounds on one pull request, each started through `route` as NexKit
dispatches it (an automatic fix round, a person's, a conflict round, a resumed or retried
automatic round, a review autopilot or a person asks for), then its checks and review and
`report`. The checks pass or fail with the same signature, another one, on another check, two
checks, or did not run, after earlier checks that failed or not and a `feedback.repeated`
already recorded; the review requests changes, approves, fails or pauses; a fix round may
change nothing. Rounds used 0-2 × `fix.max_auto_rounds` 0-3 × a hold label × the base branch
failing the same way (no, yes, an error reading it) × autopilot × `merge.auto`. Records each
`route` decision (with `diagnose`) and `report`, then a last automatic and a person's
`route`, the preview, a sweep, GitHub and `nexkit status`.

`many.py` plays the same chains with sets of 40 checks: more than 30 failing results, and
more than ten that repeat."""

import json
import random
from unittest import mock

from common import (
    APPROVE,
    FAILED,
    PAUSED,
    REQUEST_CHANGES,
    TOK,
    WAIT,
    WORKFLOW,
    Artifacts,
    after,
    attempt,
    chk,
    quiet,
    seed,
    snapshot,
    walk,
)

from nexkit import autopilot, progress
from nexkit.github import GitHubError
from nexkit.report import report
from nexkit.route import route
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state
from tests.support import FakeGitHub, make_config

# How many chains `scenarios` makes: they are random, so there is no product to exhaust.
COUNT = 60000
SEED = 312

CHECKS = {
    "pass": [chk("test", True), chk("lint", True)],
    "A": [chk("test", False, "FAIL A"), chk("lint", True)],
    "B": [chk("test", False, "FAIL B"), chk("lint", True)],
    "AB": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL B")],
    "LA": [chk("test", True), chk("lint", False, "FAIL A")],
    "AA": [chk("test", False, "FAIL A"), chk("lint", False, "FAIL A")],
    "none": None,
    # Many checks: more than 30 results, and more than ten that repeat.
    "M_all": [chk(f"c{n:02}", False, f"FAIL {n}x") for n in range(40)],
    "M_some": [chk(f"c{n:02}", n not in (31, 32, 33, 34, 35), f"FAIL {n}x") for n in range(40)],
    "M_many": [chk(f"c{n:02}", not 5 <= n < 22, f"FAIL {n}x") for n in range(40)],
    "M_other": [chk(f"c{n:02}", n < 20, f"FAIL {n}y") for n in range(40)],
}
PREV = {
    "none": None,
    "pass": [{"name": "test", "passed": True}],
    "A": [{"name": "test", "passed": False, "signature": "FAIL A"}],
    "AB": [
        {"name": "test", "passed": False, "signature": "FAIL A"},
        {"name": "lint", "passed": False, "signature": "FAIL B"},
    ],
    "LA": [{"name": "lint", "passed": False, "signature": "FAIL A"}],
    "M_all": "M_all",
}
REVIEWS = {"approve": APPROVE, "rc": REQUEST_CHANGES, "failed": FAILED, "paused": PAUSED}
KINDS = {
    "auto": ("fix", "true"),
    "person": ("fix", "false"),
    "conflicts": ("fix", "conflicts"),
    "resumed": ("fix", "true,resume"),
    "retry": ("fix", "true,retry"),
    "review": ("review", "true"),
    "review_person": ("review", "false"),
}


def chains(count, seed, many):
    rng = random.Random(seed)
    for _ in range(count):
        steps = []
        for _ in range(rng.choice([2, 2, 3, 3, 4])):
            kind = rng.choice([*KINDS, "auto", "auto", "auto"])
            checks = ["pass", "A", "B", "AB", "LA", "AA", "A", "AB", "none"]
            if many:
                checks += ["M_all", "M_some", "M_many", "M_other"] * 3
            steps.append(
                {
                    "kind": kind,
                    "checks": rng.choice(checks),
                    "review": rng.choice(["rc", "rc", "approve", "failed", "paused"]),
                    "recheck": KINDS[kind][0] == "fix" and rng.random() < 0.15,
                }
            )
        if many:
            prev = rng.choice([*PREV, "M_all", "M_all", "M_all"])
        else:
            prev = rng.choice([k for k in PREV if k != "M_all"])
        yield {
            "prev": prev,
            "prev_repeated": rng.choice([None, ["test"], ["lint", "test"]]),
            "fixes": rng.choice([0, 1, 2]),
            "rounds": rng.choice([0, 1, 2, 3]),
            "hold": rng.choice([False, False, True]),
            "base": rng.choice(["no", "no", "yes", "error"]),
            "ap": rng.choice([True, True, False]),
            "merge": rng.choice([False, True]),
            "steps": steps,
        }


def scenarios():
    yield from chains(COUNT, SEED, many=False)


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] else ())
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(5, render_run({"url": f"{runs}/1", **plan}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(5, render_run({"url": f"{runs}/2", **go}, "D."))
    head = "a" * 40
    gh.add_pull(6, 5, head_sha=head)
    state = {**empty_state(5), "auto_fixes": sc["fixes"]}
    if PREV[sc["prev"]] is not None or sc["prev_repeated"]:
        prev = PREV[sc["prev"]]
        if prev == "M_all":
            from nexkit.checks import recorded

            prev = recorded(CHECKS["M_all"])
        state["feedback"] = {"head": head, "checks": prev, "review": None}
        if sc["prev_repeated"]:
            state["feedback"]["repeated"] = sc["prev_repeated"]
    gh.comment(6, render_state(state))
    gh.round_comment(
        6, 1, head, "failed", outcome="auto_fix", trigger="fix (auto)", action="fix", kinds=["auto"]
    )
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        conflicts={"auto_resolve": True},
        notify=["olivia"],
    )

    def ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    def dispatch(kind):
        command, auto = KINDS[kind]
        inputs = {"command": command, "number": "6", "note": "", "auto": auto}
        event = {"inputs": inputs, "sender": {"login": "nexkit[bot]"}}
        try:
            return route(gh, "workflow_dispatch", event, inputs)
        except Exception as exc:  # noqa: BLE001
            return f"EXC {type(exc).__name__}: {exc}"

    seed(gh, cfg)
    res = {"rounds": []}
    with (
        mock.patch.object(autopilot, "ci_wait", ci_wait),
        mock.patch.object(autopilot, "ci_wait_over", lambda g, w: None),
        quiet(),
    ):
        for number, step in enumerate(sc["steps"], 2):
            found = {}
            d = dispatch(step["kind"])
            found["route"] = walk(d)
            if not isinstance(d, dict) or d.get("action") not in ("fix", "review"):
                res["rounds"].append(found)
                continue
            d = dict(d, run_id=4000 + number)
            art = Artifacts()
            needs = {}
            if d["action"] == "fix":
                agent = {"status": "done", "tokens": TOK, "output": {"cause": "Env"}}
                if step["kind"] == "conflicts":
                    agent["start_base"] = "e" * 40
                art.agent(agent)
                if not step["recheck"]:
                    head = f"{number:x}" * 40
                    gh.pulls[6]["head"]["sha"] = head
                pub = {"published": True, "pr": 6, "head": head}
                if step["recheck"]:
                    pub["recheck"] = True
                needs["publish"] = {"outputs": {"result": json.dumps(pub)}}
            art.checks(CHECKS[step["checks"]])
            art.review(REVIEWS[step["review"]])
            try:
                progress.start(gh, d)
            except Exception as exc:  # noqa: BLE001
                found["start"] = f"EXC {type(exc).__name__}: {exc}"
            found["report"] = attempt(
                report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
            )
            art.close()
            res["rounds"].append(found)
        res["last_auto"] = walk(dispatch("auto"))
        res["last_person"] = walk(dispatch("person"))
        res["after_rounds"] = snapshot(gh)
        after(gh, cfg, res)
    return res

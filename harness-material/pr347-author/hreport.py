"""Differential harness for `report` after a round's checks and review.

usage: python3 hreport.py ROOT OUT [shard nshards]
Each scenario runs on a fresh fake GitHub; prints one JSON line per scenario to OUT.
"""

import base64
import contextlib
import io
import itertools
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from nexkit import autopilot  # noqa: E402
from nexkit.github import GitHubError  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import empty_state, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}


def crit(status, name="add works"):
    return {"criterion": name, "status": status, "test": "t", "evidence": f"ev {status}"}


def out(verdict="approve", criteria=None, findings=(), checklist=None):
    o = {
        "verdict": verdict,
        "summary": "S.",
        "criteria": criteria if criteria is not None else [crit("met")],
        "findings": list(findings),
    }
    if checklist is not None:
        o["checklist"] = checklist
    return o


BLOCK = {"severity": "blocking", "file": "a.py", "line": 3, "body": "Breaks x."}
SUGG = {"severity": "suggestion", "file": "a.py", "line": 3, "body": "Nicer y."}
PITEM = {"item": "Docs updated by owner", "answer": "not_met", "evidence": "none", "person": True}
NITEM = {"item": "Tests added", "answer": "not_met", "evidence": "missing"}
MITEM = {"item": "Tests added", "answer": "met", "evidence": "yes"}


def done(o):
    return {"status": "done", "tokens": TOK, "output": o}


REVIEWS = {
    "approve": done(out()),
    "rc": done(out("request_changes", findings=[BLOCK])),
    "rc_nofind": done(out("request_changes")),
    "approve_block": done(out(findings=[BLOCK])),
    "approve_sugg": done(out(findings=[SUGG])),
    "unmet": done(out(criteria=[crit("met", "a"), crit("unmet", "b")])),
    "unverif": done(out(criteria=[crit("met", "a"), crit("unverifiable", "b")])),
    "unmet_unverif": done(out(criteria=[crit("unmet", "a"), crit("unverifiable", "b")])),
    "pitem": done(out(checklist=[PITEM])),
    "pitem_unverif": done(out(criteria=[crit("unverifiable")], checklist=[PITEM])),
    "pitem_block": done(out(findings=[BLOCK], checklist=[PITEM])),
    "rc_pitem": done(out("request_changes", checklist=[PITEM])),
    "rc_pitem_block": done(out("request_changes", findings=[BLOCK], checklist=[PITEM])),
    "nitem": done(out(checklist=[NITEM])),
    "nitem_pitem": done(out(checklist=[NITEM, PITEM])),
    "mitem": done(out(checklist=[MITEM])),
    "bool_met": done(out(criteria=[{"criterion": "x", "met": True}])),
    "bool_unmet": done(out(criteria=[{"criterion": "x", "met": False}])),
    "no_criteria": done(out(criteria=[])),
    "failed": {"status": "error", "error": "Review crashed", "tokens": TOK},
    "missing": None,
    "paused": {
        "status": "paused",
        "error": "Claude hit the account's usage limit: limit",
        "limit": "limit · resets 4:40am (UTC)",
        "resume_at": "2099-10-11T04:40:00Z",
        "reset_known": True,
    },
}


def chk(name, run, passed):
    return {"name": name, "run": run, "exit_code": 0 if passed else 1, "passed": passed,
            "output": "" if passed else "FAIL"}


CHECKS = {
    "pass": [chk("test", "make test", True)],
    "fail": [chk("test", "make test", False)],
    "two_fail": [chk("test", "make test", False), chk("lint", "make lint", False)],
    "mixed": [chk("test", "make test", True), chk("lint", "make lint", False)],
    "none": None,
    "noname": [chk("", "make x", False)],
}

WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}

TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
MARK = re.compile(r"<!-- nexkit:([a-z-]+) ([A-Za-z0-9+/=]+) -->")


def norm(text):
    def dec(m):
        try:
            data = json.loads(base64.b64decode(m.group(2)))
        except Exception:  # noqa: BLE001
            return m.group(0)
        if isinstance(data, dict):
            data.pop("at", None)
        return f"<{m.group(1)} " + json.dumps(data, sort_keys=True) + ">"

    return TS.sub("T", MARK.sub(dec, text or ""))


SKIP = {"calls", "_ids", "next_id"}


def walk(x):
    if isinstance(x, str):
        return norm(x)
    if isinstance(x, dict):
        return {str(k): walk(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        items = [walk(v) for v in x]
        return sorted(items, key=repr) if isinstance(x, set) else items
    if isinstance(x, (int, float, bool)) or x is None:
        return x
    return norm(str(x))


def snapshot(gh):
    data = {k: v for k, v in vars(gh).items() if k not in SKIP and not callable(v)}
    return walk(data)


def scenarios():
    dims = dict(
        action=["implement", "fix_auto", "fix_person", "review_auto", "review_person"],
        checks=list(CHECKS),
        review=list(REVIEWS),
        recheck=[False, True],
        prior=[None, 0, 1, 2],
        rounds=[0, 1, 2],
        merge=[False, True],
        hold=["no", "label", "error"],
        base=["no", "yes", "error"],
        ap=[True, False],
    )
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        sc = dict(zip(keys, values))
        if sc["recheck"] and not sc["action"].startswith("fix"):
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1)
    if sc["hold"] == "label":
        gh.issues[5]["labels"] = [{"name": "on-hold"}]
    gh.add_pull(6, 5, head_sha=HEAD)
    if sc["prior"] is not None:
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": sc["prior"]}))
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        fix={"max_auto_rounds": sc["rounds"]},
        notify=["olivia"],
    )
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)

    def write(folder, name, data):
        (art / folder).mkdir(exist_ok=True)
        (art / folder / name).write_text(json.dumps(data))

    action = sc["action"]
    write("nexkit-agent", "result.json", {"status": "done", "tokens": TOK,
                                           **({"output": {"cause": "Flaky env"}} if sc["recheck"] else {})})
    if CHECKS[sc["checks"]] is not None:
        write("nexkit-checks", "checks.json", CHECKS[sc["checks"]])
    if REVIEWS[sc["review"]] is not None:
        write("nexkit-review", "result.json", REVIEWS[sc["review"]])
    kind = action.split("_")[0]
    auto = action.endswith("auto")
    d = {
        "action": kind,
        "issue": 5,
        "pr": None if kind == "implement" else 6,
        "target": 5 if kind == "implement" else 6,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": None if kind == "implement" else HEAD,
        "note": "",
        "auto": auto,
    }
    if auto:
        d["autopilot"] = "true"
    needs = {}
    if kind != "review":
        pub = {"published": True, "pr": 6, "head": HEAD}
        if sc["recheck"]:
            pub["recheck"] = True
        needs["publish"] = {"outputs": {"result": json.dumps(pub)}}

    def fake_ci_wait(gh_, cfg_, decision_, checks_, branch_):
        failing = [c for c in checks_ or [] if not c.get("passed")]
        if not failing or not autopilot.takes(cfg_):
            return None
        if sc["base"] == "error":
            raise GitHubError(502, "Bad gateway")
        return dict(WAIT) if sc["base"] == "yes" else None

    real_held = autopilot.held

    def fake_held(gh_, cfg_, number):
        if sc["hold"] == "error" and autopilot.takes(cfg_):
            raise GitHubError(502, "Bad gateway")
        return real_held(gh_, cfg_, number)

    result = {}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    try:
        with mock.patch.dict(os.environ, env), mock.patch.object(
            autopilot, "ci_wait", fake_ci_wait
        ), mock.patch.object(autopilot, "held", fake_held), contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            result = report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW, default_branch="main")
    except Exception as e:  # noqa: BLE001
        result = f"EXC {type(e).__name__}: {e}"
    status = None
    try:
        from nexkit.status import collect

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data = collect(gh, cfg, workers=1)
        status = walk(data)
    except Exception as e:  # noqa: BLE001
        status = f"EXC {type(e).__name__}: {e}"
    tmp.cleanup()
    return {
        "sc": sc,
        "result": walk(result),
        "gh": snapshot(gh),
        "status": status,
    }


def main():
    outp = sys.argv[2]
    shard = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    import gzip
    with gzip.open(outp, "wt", compresslevel=3) as f:
        flt = os.environ.get("FILTER")
        for i, sc in enumerate(scenarios()):
            if i % n != shard:
                continue
            if flt and not eval(flt, {}, dict(sc)):
                continue
            f.write(json.dumps(run(sc), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

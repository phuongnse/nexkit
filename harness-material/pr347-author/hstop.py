"""Differential harness: a fix round or /nexkit go that stopped (blocked, timed out, error),
then report, the preview, a sweep and `nexkit status`.

usage: python3 hstop.py ROOT OUT [shard n]
"""

import base64
import contextlib
import copy
import gzip
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

from nexkit import autopilot, progress  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import PLAN_MARKER, answers_marker, empty_state, render_run, render_state  # noqa: E402
from tests.support import BOT_LOGIN, FakeGitHub, make_config  # noqa: E402

HEAD = "c" * 40
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"


def q(text, opts, rec, closes=0, replans=None):
    item = {"question": text, "options": opts, "recommended": rec}
    if closes:
        item["closes"] = closes
    if replans:
        item["replans"] = replans
    return item


Q_REC = q("Which port?", ["8080", "9090"], 1)
Q_NOREC = q("Which db?", ["pg", "my"], 0)
Q_CLOSE = q("Already fixed. Close?", ["Close it", "Keep it"], 1, closes=1)
Q_CLOSE_NOT = q("Close?", ["Close it", "Keep it"], 2, closes=1)
Q_REPLAN = q("Scope?", ["Narrow", "Wide", "Split it"], 3, replans=[3])
Q_REPLAN_NOT = q("Scope2?", ["Narrow", "Wide", "Split it"], 1, replans=[3])
Q_EMPTY = q("   ", ["a", "b"], 1)

QSETS = {
    "none": [],
    "rec": [Q_REC],
    "norec": [Q_NOREC],
    "close": [Q_CLOSE],
    "close_not": [Q_CLOSE_NOT],
    "replan": [Q_REPLAN],
    "replan_not": [Q_REPLAN_NOT],
    "close_replan": [Q_CLOSE, Q_REPLAN],
    "replan_close": [Q_REPLAN, Q_CLOSE],
    "rec_norec": [Q_REC, Q_NOREC],
    "rec_close": [Q_REC, Q_CLOSE],
    "empty": [Q_EMPTY],
    "conflict": "conflict",
}

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


SKIP = {"calls"}


def snapshot(gh):
    return walk({k: v for k, v in vars(gh).items() if k not in SKIP and not callable(v)})


def scenarios():
    dims = dict(
        place=["pull", "go"],
        kind=["blocked", "timed_out", "error"],
        qset=list(QSETS),
        protected=[False, True],
        answer=["recommended", "never"],
        answered=["no", "yes", "yes_comment"],
        record=[False, True],
        depth=["top", "deep3", "parent_asked", "deep1"],
        auto=[True, False],
    )
    keys = list(dims)
    for values in itertools.product(*dims.values()):
        sc = dict(zip(keys, values))
        if sc["kind"] != "blocked" and (sc["qset"] != "none" or sc["protected"]):
            continue
        yield sc


def record_comment(gh, number, rec):
    gh.comment(number, "NexKit autopilot record\n\n" + autopilot.record_marker(rec))


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    d = sc["depth"]
    if d == "top":
        gh.add_issue(5, title="Cache", milestone=1)
    else:
        levels = {"deep3": 3, "parent_asked": 1, "deep1": 1}[d]
        top = 5 - levels
        for n in range(top, 6):
            body = "Please add it." if n == top else f"<!-- nexkit:part {n - 1} -->\nPart."
            user = "alice" if n == top else BOT_LOGIN
            gh.add_issue(n, title=f"Issue {n}", body=body, user=user, milestone=1)
            if n > top:
                gh.parents[n] = n - 1
        if d == "parent_asked":
            record_comment(gh, 4, {"split_asked": True, "command": "plan", "basis": [0, 0]})
    if sc["record"]:
        cmd = "fix" if sc["place"] == "pull" else "go"
        record_comment(gh, 5, {"split_asked": True, "command": cmd, "basis": [0, 0]})
    runs = itertools.count(1)

    def run_comment(number, **fields):
        url = f"https://github.com/acme/app/actions/runs/{next(runs)}"
        return gh.comment(number, render_run({"url": url, **fields}, "Done."))

    run_comment(5, command="plan", status="success", outcome="planned")
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    target = 5
    if sc["place"] == "pull":
        run_comment(5, command="go", status="success", outcome="pr_opened")
        gh.add_pull(6, 5, head_sha=HEAD)
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
        target = 6
        run_comment(
            6, round=1, trigger="implement", status="failure", outcome="auto_fix",
            head=HEAD, checks="failed", verdict="request_changes",
        )
    if sc["answered"] != "no":
        if sc["place"] == "pull":
            run_comment(6, round=2, trigger="fix", status="failure", outcome="agent_blocked",
                        head=HEAD, questions=[{**Q_REC, "recommended": "8080"}])
        else:
            run_comment(5, command="go", status="failure", outcome="agent_blocked",
                        questions=[{**Q_REC, "recommended": "8080"}])
        rec = {"answers": [{"question": "Which port?", "answer": "8080"}]}
        gh.comment(target, f"NexKit autopilot answers\n\n{answers_marker(rec)}")
        if sc["answered"] == "yes_comment":
            gh.human_comment(5, "I attached the files.")
    return gh, target


def agent_result(sc):
    if sc["kind"] == "timed_out":
        return {"status": "error", "error": "Claude did not finish within 30 minutes.",
                "timed_out": True}
    if sc["kind"] == "error":
        return {"status": "error", "error": "Claude stopped (error_max_turns)."}
    out = {"status": "blocked"}
    qs = QSETS[sc["qset"]]
    if qs == "conflict":
        out["questions"] = []
        out["open_conflicts"] = [{"file": "a.py", "base_change": "x", "pr_change": "y", **Q_REC}]
    else:
        out["questions"] = qs
    if sc["protected"]:
        out["protected_paths"] = [".github/workflows/x.yml"]
    return {"status": "blocked", "error": "Stuck", "output": out}


def run(sc):
    gh, target = build(sc)
    cfg = make_config(autopilot={"enabled": True, "answer": sc["answer"]}, notify=["olivia"])
    pull = sc["place"] == "pull"
    d = {
        "action": "fix" if pull else "implement",
        "command": "fix" if pull else "go",
        "issue": 5,
        "pr": 6 if pull else None,
        "target": target,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD if pull else None,
        "note": "",
        "auto": sc["auto"],
        "run_id": 4242,
    }
    if sc["auto"]:
        d["autopilot"] = "true"
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)
    (art / "nexkit-agent").mkdir()
    (art / "nexkit-agent" / "result.json").write_text(json.dumps(agent_result(sc)))
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    res = {"sc": sc}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(
        io.StringIO()
    ), contextlib.redirect_stderr(io.StringIO()):
        try:
            progress.start(gh, d)
        except Exception as e:  # noqa: BLE001
            res["start"] = f"EXC {type(e).__name__}: {e}"
        try:
            res["report"] = walk(
                report(gh, d, cfg, {}, art, workflow_ref=WORKFLOW, default_branch="main")
            )
        except Exception as e:  # noqa: BLE001
            res["report"] = f"EXC {type(e).__name__}: {e}"
        res["after_report"] = snapshot(gh)
        try:
            _, steps = autopilot.preview(gh, cfg)
            res["step"] = walk({s["number"]: s for s in steps}.get(5))
        except Exception as e:  # noqa: BLE001
            res["step"] = f"EXC {type(e).__name__}: {e}"
        g2 = copy.deepcopy(gh)
        try:
            res["lines"] = walk(autopilot.sweep(g2, cfg, "nexkit.yml", "main"))
            res["after_sweep"] = snapshot(g2)
        except Exception as e:  # noqa: BLE001
            res["lines"] = f"EXC {type(e).__name__}: {e}"
        try:
            from nexkit.status import collect

            res["status"] = walk(collect(gh, cfg, workers=1))
        except Exception as e:  # noqa: BLE001
            res["status"] = f"EXC {type(e).__name__}: {e}"
    tmp.cleanup()
    return res


def main():
    outp = sys.argv[2]
    shard = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 1
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

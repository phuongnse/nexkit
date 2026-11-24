"""Play scenarios on one NexKit checkout and print one JSON record per scenario.
python3 drive.py <repo> scenarios.jsonl > out.jsonl"""
import datetime as _dt
import io, json, os, sys, tempfile, contextlib, traceback
from pathlib import Path
from unittest import mock

REPO = sys.argv[1]
SCEN = os.path.abspath(sys.argv[2])
sys.path.insert(0, REPO)
os.chdir(REPO)
os.environ["GITHUB_REPOSITORY"] = "acme/app"

FIXED = _dt.datetime(2026, 11, 24, 12, 0, 0, tzinfo=_dt.UTC)
class FrozenDT(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED if tz else FIXED.replace(tzinfo=None)

import nexkit.report as report_mod
import nexkit.route as route_mod
import nexkit.autopilot as autopilot_mod
import nexkit.maintain as maintain_mod
import nexkit.progress as progress_mod
from nexkit.state import empty_state, render_state, read_state
from tests.support import FakeGitHub, make_config
for module in list(sys.modules.values()):
    if getattr(module, "__name__", "").startswith("nexkit") and getattr(module, "datetime", None) is _dt.datetime:
        module.datetime = FrozenDT

TOKENS = {"input": 1000, "cached": 800, "output": 200}
WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
LONG = "x" * 36

def review_result(verdict="approve", blocking=False):
    return {"status": "done", "tokens": {"input": 300, "cached": 100, "output": 50},
            "output": {"verdict": verdict, "summary": "Looks right.",
                       "criteria": [{"criterion": "add works", "status": "met", "test": "test_add",
                                     "evidence": "test_calc.py:5"}],
                       "findings": ([{"severity": "blocking", "file": "a.py", "line": 1, "body": "Bad."}]
                                    if blocking or verdict == "request_changes" else [])}}

REVIEWS = {
    "approve": lambda: review_result(),
    "blocking_approve": lambda: review_result(blocking=True),
    "request_changes": lambda: review_result("request_changes"),
    "error": lambda: {"status": "error", "error": "Review crashed", "tokens": TOKENS},
    "transient": lambda: {"status": "error", "error": "Overloaded", "transient": True, "tokens": TOKENS},
    "paused": lambda: {"status": "paused", "error": "usage limit", "limit": "You've hit your limit · resets 4:40am (UTC)",
                       "resume_at": "2026-11-25T04:40:00Z", "reset_known": True},
    "paused_unknown": lambda: {"status": "paused", "error": "usage limit", "limit": "You've hit your limit",
                               "resume_at": "2026-11-24T18:00:00Z", "reset_known": False},
    "missing": lambda: None,
}

def cfg_of(sc):
    c = sc["cfg"]
    names = sc["names"]
    checks, seen = [], set()
    for name in names:
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

def checks_now(sc, mode, before, rng_i):
    names = sc["names"]
    if mode == "none":
        return None
    if mode == "same":
        if not before:
            mode = "allfail"
        else:
            return [{k: v for k, v in c.items() if k != "signature"} for c in before]
    out = []
    for i, name in enumerate(names):
        if mode == "pass":
            passed = True
        elif mode == "allfail":
            passed = False
        elif mode == "tailfail":
            passed = i < len(names) - 4
        else:
            passed = (i + rng_i) % 3 == 0
        output = "" if passed else ["AssertionError", "TypeError: x", ""][(i + rng_i) % 3]
        out.append({"name": name, "run": f"run {name}", "exit_code": 0 if passed else 1,
                    "passed": passed, "output": output})
    return out

def snapshot(gh):
    comments = {}
    for number, items in gh.issue_comments.items():
        if items:
            comments[number] = [c["body"] for c in items]
    return {
        "comments": comments,
        "statuses": [(s.get("sha"), s.get("context"), s.get("state"), s.get("description")) for s in gh.statuses],
        "reviews": [json.dumps(r, sort_keys=True, default=str) for r in gh.created_reviews],
        "dispatches": [json.dumps(d, sort_keys=True, default=str) for d in gh.dispatches],
        "merged": [json.dumps(m, sort_keys=True, default=str) for m in gh.merged],
        "labels": [json.dumps(l, sort_keys=True, default=str) for l in gh.labeled],
        "issues": {n: (i.get("state"), [l["name"] for l in i.get("labels") or []]) for n, i in gh.issues.items()},
    }

def held_fn(label):
    if label == "UNREAD":
        return lambda gh, cfg, number: report_mod.UNREAD_HOLD
    return lambda gh, cfg, number: label

def play(sc):
    cfg = cfg_of(sc)
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
    gh.comment(7, render_state({**empty_state(7), "person": True, "feedback": state.get("feedback")}))
    record = {"steps": []}
    run_id = 100
    for i, rd in enumerate(sc["rounds"]):
        kind = rd["kind"]
        pr = 7 if kind == "person_pr" else 6
        cur_head = gh.pulls[pr]["head"]["sha"]
        new_head = f"{i + 1:x}" * 40 if not rd["recheck"] else cur_head
        base = {"issue": 5, "pr": pr, "target": pr, "branch": gh.pulls[pr]["head"]["ref"], "base": "main",
                "head": cur_head, "note": "", "auto": False}
        publish = {"published": True, "pr": pr, "head": new_head}
        if kind in ("auto_fix", "conflict_fix", "resumed_fix", "retry_fix"):
            d = {**base, "action": "fix", "auto": True, "autopilot": True}
            if kind == "conflict_fix":
                d["conflicts"] = True; d["base_sha"] = "d" * 40
            if kind == "resumed_fix":
                d["resumed"] = True
            if kind == "retry_fix":
                d["retry"] = 1
        elif kind == "person_fix":
            d = {**base, "action": "fix"}
        elif kind == "auto_review":
            d = {**base, "action": "review", "auto": True, "autopilot": True}
            publish = None
        elif kind == "person_review":
            d = {**base, "action": "review"}
            publish = None
        elif kind == "implement":
            d = {**base, "action": "implement", "pr": None, "target": 5, "head": None}
        else:  # person_pr
            d = {**base, "action": "review", "issue": 7, "person": True}
            publish = None
        if publish is not None and d["action"] == "fix" and rd["recheck"]:
            publish["recheck"] = True
        if publish is not None and rd["conflicts"]:
            publish["conflicts"] = ["a.py"]
        if publish is not None:
            gh.pulls[pr]["head"]["sha"] = new_head
        for n in range(rd["used_conflicts"] if rd["conflicts"] and i == 0 else 0):
            gh.round_comment(pr, 90 + n, "b" * 40, "failed", conflicts=True, base_sha="d" * 40)
        tmp = tempfile.TemporaryDirectory()
        art = Path(tmp.name)
        def write(a, name, data):
            p = art / a / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data))
        _, st = read_state(gh.comments(pr))
        before = ((st or {}).get("feedback") or {}).get("checks")
        write("nexkit-agent", "result.json", {"status": "done", "tokens": TOKENS})
        chk = checks_now(sc, rd["checks"], before, i)
        conflicting = publish is not None and rd["conflicts"]
        if chk is not None and not conflicting:
            write("nexkit-checks", "checks.json", chk)
        rv = REVIEWS[rd["review"]]()
        if rv is not None and not conflicting:
            write("nexkit-review", "result.json", rv)
        needs = {}
        if publish is not None:
            needs["publish"] = {"outputs": {"result": json.dumps(publish)}}
        run_id += 1
        os.environ["GITHUB_RUN_ID"] = str(run_id)
        wait = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 5} if rd["base_failing"] else None
        ci = lambda gh_, cfg_, d_, checks_, branch_: wait if any(not c.get("passed") for c in checks_ or []) else None
        step = {"kind": kind, "decision": d}
        err = io.StringIO()
        try:
            with mock.patch.object(report_mod, "_held", held_fn(rd["held"])), \
                 mock.patch.object(report_mod, "_ci_wait", ci), contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                out = report_mod.report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW, default_branch="main")
            step["outcome"] = json.loads(json.dumps(out, default=str))
        except Exception as exc:
            step["crash"] = f"{type(exc).__name__}: {exc}"
            step["tb"] = traceback.format_exc().splitlines()[-3:]
        tmp.cleanup()
        step["gh"] = snapshot(gh)
        # What route decides for the next fix round: automatic, a person's, a conflict round.
        routes = {}
        for auto in ("true", "", "conflicts", "true,resumed", "autopilot"):
            inputs = {"command": "fix", "number": str(pr), "note": "", "auto": auto}
            event = {"inputs": inputs, "sender": {"login": "alice"}}
            try:
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    dec = route_mod.route(gh, "workflow_dispatch", event, inputs)
                routes[auto] = {k: dec.get(k) for k in ("action", "diagnose", "conflicts", "reason")}
            except Exception as exc:
                routes[auto] = f"crash {type(exc).__name__}: {exc}"
        step["routes"] = routes
        record["steps"].append(step)
        # A round that completed with an outcome that dispatched a fix starts the next as dispatched.
    return record

import hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dec import norm
ONLY = set(int(x) for x in os.environ.get("ONLY", "").split(",") if x)
for line in open(SCEN):
    sc = json.loads(line)
    if ONLY and sc["index"] not in ONLY:
        continue
    try:
        rec = play(sc)
    except Exception as exc:
        rec = {"fatal": f"{type(exc).__name__}: {exc}", "tb": traceback.format_exc().splitlines()[-4:]}
    rec = norm(json.loads(json.dumps(rec, default=str)))
    if os.environ.get("FULL"):
        print(json.dumps({"index": sc["index"], **rec}, sort_keys=True))
    else:
        text = json.dumps(rec, sort_keys=True)
        brief = [(s.get("kind"), (s.get("outcome") or {}).get("outcome"), s.get("crash")) for s in rec.get("steps", [])]
        print(json.dumps({"index": sc["index"], "h": hashlib.sha256(text.encode()).hexdigest()[:16], "brief": brief, "fatal": rec.get("fatal")}))

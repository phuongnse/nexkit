"""Run differential scenarios inside one NexKit tree.

Usage: python3 drive.py TREE < scenarios.jsonl > results.jsonl
Each scenario is a JSON object with a `family` and its parameters; each result is what
the command returned, everything on the fake GitHub after each step, the preview and
`nexkit status`.
"""

import base64
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

TREE = sys.argv[1]
sys.path.insert(0, TREE)
os.chdir(TREE)

from nexkit import autopilot, maintain, report as report_mod  # noqa: E402
from nexkit.github import GitHubError  # noqa: E402
from nexkit.state import read_state, render_run, render_state  # noqa: E402
from nexkit.status import collect, render  # noqa: E402
from tests.support import BOT_LOGIN, FakeGitHub, make_config  # noqa: E402
from tests.test_autopilot import WORKFLOW, CIWaitTests  # noqa: E402

MARK = re.compile(r"<!-- nexkit:([a-z-]+) ([A-Za-z0-9+/=]{8,}) -->")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?|\d\d:\d\d UTC on \d{4}-\d\d-\d\d")
HEAD = "c" * 40


def decode(text):
    def one(match):
        try:
            data = json.loads(base64.b64decode(match.group(2)))
            return f"<!-- nexkit:{match.group(1)} {json.dumps(data, sort_keys=True)} -->"
        except Exception:
            return match.group(0)

    return TIME.sub("<T>", MARK.sub(one, text))


def norm(value):
    return json.loads(TIME.sub("<T>", json.dumps(value, sort_keys=True, default=str)))


def snapshot(gh):
    return {
        "issues": norm({n: {k: i.get(k) for k in ("state", "state_reason", "labels", "body", "title", "milestone")} for n, i in sorted(gh.issues.items())}),
        "comments": {n: [decode(c["body"]) for c in cs] for n, cs in sorted(gh.issue_comments.items()) if cs},
        "pulls": norm(gh.pulls),
        "statuses": norm(gh.statuses),
        "dispatches": norm(gh.dispatches),
        "merged": norm(gh.merged),
        "updated": norm(gh.updated),
        "reviews": norm(gh.created_reviews),
        "labeled": norm(gh.labeled),
        "updates": norm(gh.updates),
        "cancelled": norm(gh.cancelled),
        "deleted": norm(gh.deleted),
        "reruns": norm(gh.reruns),
    }


def status_of(gh, cfg):
    try:
        data = collect(gh, cfg, workers=1)
        return TIME.sub("<T>", render(data))
    except Exception as error:
        return f"status raised {type(error).__name__}: {error}"


def preview_of(gh, cfg):
    try:
        _, steps = autopilot.preview(gh, cfg)
        return norm([{k: s.get(k) for k in ("number", "state", "reason", "command", "auto", "count", "person", "target")} for s in steps])
    except Exception as error:
        return f"preview raised {type(error).__name__}: {error}"


def quiet(fn, *args, **kwargs):
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            value = fn(*args, **kwargs)
        return {"value": norm(value), "out": TIME.sub("<T>", out.getvalue())}
    except Exception as error:
        return {"raised": f"{type(error).__name__}: {error}", "tb": traceback.format_exc().splitlines()[-3:]}


def cfg_of(p, labels=True):
    auto = {"enabled": p.get("autopilot", True)}
    if labels:
        auto["labels"] = {"on-hold": {"hold": True}}
    return make_config(
        checks=[{"name": "test", "run": "make test"}, {"name": "lint", "run": "make lint"}],
        autopilot=auto,
        merge={"after_workflows": ["ci.yml"], "auto": p.get("merge_auto", True)},
        fix={"max_auto_rounds": p.get("max", 2)},
        conflicts={"auto_resolve": p.get("resolve", True)},
        notify=["olivia"],
    )


def set_used(gh, pull, used, issue=5):
    comment_id, state = read_state(gh.comments(pull))
    if used is None:
        if comment_id is not None:
            gh.issue_comments[pull] = [c for c in gh.issue_comments[pull] if c["id"] != comment_id]
        return
    if state is None:
        gh.comment(pull, render_state({"version": 2, "issue": issue, "auto_fixes": used, "feedback": None}))
    else:
        state["auto_fixes"] = used
        gh.update_comment(comment_id, render_state(state))


def sweeps(case, gh, cfg, p, record, between=None):
    for n in range(p.get("sweeps", 2)):
        if n == 1 and between:
            between()
        cfg_now = cfg_of({**p, "max": p.get("max2", p.get("max", 2))}) if n == 1 and "max2" in p else cfg
        record.append({"sweep": quiet(autopilot.sweep, gh, cfg_now, "nexkit.yml", "main"), "gh": snapshot(gh), "preview": preview_of(gh, cfg_now)})
    record.append({"status": status_of(gh, cfg)})


# -- families -------------------------------------------------------------------------


def fam_hold(p):
    """A round that a hold label held back (`_released`)."""
    gh = FakeGitHub()
    cfg = cfg_of(p)
    labels = ("on-hold",) if p["label"] else ()
    gh.add_milestone(1)
    gh.add_issue(5, title="Issue 5", milestone=1, labels=labels)
    gh.add_issue(6, title="Issue 6", milestone=1)
    pull = gh.add_pull(7, 5, head_sha=HEAD)
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1", "command": "go", "status": "success", "outcome": "pr_opened"}, "Done."))
    if p["used"] is not None:
        gh.comment(7, render_state({"version": 2, "issue": 5, "auto_fixes": p["used"], "feedback": None}))
    fields = {"outcome": "held"}
    if p["back"]:
        fields["held_back"] = p["back"]
    extra = {}
    if p["back"] == "merge":
        extra = {"verdict": "approve"}
    gh.round_comment(7, 1, HEAD, "failed" if p["back"] != "merge" else "passed", action="fix", kinds=["auto"], **fields, **extra)
    if p["moved"]:
        pull["head"]["sha"] = "d" * 40
    if p["conflict"]:
        pull["mergeable"] = False
    if p["closed"]:
        gh.issues[5]["state"] = "closed"
    between = BETWEEN.get(p.get("between"))
    record = []
    sweeps(p, gh, cfg, p, record, between and (lambda: between(gh)))
    return record


def ci_case():
    case = CIWaitTests("test_tests_named_on_one_side_only_are_another_failure")
    case.setUp()
    return case


def fam_ci(p):
    """A round that waits for the base branch's failure (`_ci_step`)."""
    case = ci_case()
    try:
        gh = case.gh
        case.cfg = cfg_of({**p, "max": 3})
        case.open_pull(5, 10)
        with contextlib.redirect_stdout(io.StringIO()):
            first = case.report_round(5, 10, action=p.get("action", "implement"))
        record = [{"report": norm({k: first.get(k) for k in ("outcome", "attention")}), "gh": snapshot(gh)}]
        set_used(gh, 10, p["used"])
        cfg = cfg_of(p)
        case.cfg = cfg
        if p["end"] == "closed":
            gh.issues[case.bug]["state"] = "closed"
        elif p["end"] == "passes":
            case.ci_run("success", 52)
        if p["moved"]:
            gh.pulls[10]["head"]["sha"] = "d" * 40
        if p["label"]:
            gh.issues[5]["labels"] = [{"name": "on-hold"}]
        if p["closed"]:
            gh.issues[5]["state"] = "closed"
        if p.get("person"):
            # A person's `/nexkit fix` or `/nexkit review` ran during the wait, and reported.
            with contextlib.redirect_stdout(io.StringIO()):
                out = person_round(case, p["person"], p.get("person_same", True))
            record.append({"person": out, "gh": snapshot(gh)})
        between = BETWEEN.get(p.get("between"))
        if p.get("between") == "end":
            between = lambda g: g.issues[case.bug].update(state="closed")  # noqa: E731
        sweeps(p, gh, cfg, p, record, between and (lambda: between(gh)))
        return record
    finally:
        case.doCleanups()


def person_round(case, command, same):
    """`report` after a person's round of `command` on pull request 10."""
    tmp = tempfile.TemporaryDirectory()
    artifacts = Path(tmp.name)

    def write(folder, name, data):
        (artifacts / folder).mkdir(exist_ok=True)
        (artifacts / folder / name).write_text(json.dumps(data))

    tokens = {"input": 1, "output": 1}
    write("nexkit-agent", "result.json", {"status": "done", "tokens": tokens})
    tests = [case.TEST] if same else ["cart.test.ts::other"]
    checks = [
        {"name": "test", "run": "make test", "exit_code": 1, "passed": False, "output": "FAIL", "failed_tests": tests},
        {"name": "lint", "run": "make lint", "exit_code": 0, "passed": True, "output": ""},
    ]
    write("nexkit-checks", "checks.json", checks)
    output = {"verdict": "request_changes", "summary": "Checks fail.", "criteria": [], "findings": []}
    write("nexkit-review", "result.json", {"status": "done", "tokens": tokens, "output": output})
    head = case.gh.pulls[10]["head"]["sha"]
    decision = {"action": command, "issue": 5, "pr": 10, "target": 10, "branch": "nexkit/issue-5", "base": "main", "head": head, "note": "", "auto": False}
    published = {"published": True, "pr": 10, "head": head}
    needs = {"publish": {"outputs": {"result": json.dumps(published)}}}
    case.runs += 1
    with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": str(300 + case.runs)}):
        result = quiet(report_mod.report, case.gh, decision, case.cfg, needs, artifacts, workflow_ref=WORKFLOW, default_branch="main")
    tmp.cleanup()
    if "value" in result:
        result["value"] = {k: result["value"].get(k) for k in ("outcome", "attention", "next")}
    return result


def fam_required(p):
    """Required checks that fail while a merge waits for them (`_required_failed`)."""
    case = ci_case()
    try:
        gh = case.gh
        cfg = cfg_of(p)
        case.cfg = cfg
        heads = {10: HEAD}
        gh.add_pull(10, 5, head_sha=HEAD)
        run = {"round": 1, "trigger": "implement (autopilot)", "status": "success", "outcome": "merge_waiting", "head": HEAD, "waiting_since": autopilot.iso(autopilot.datetime.now(autopilot.UTC))}
        gh.comment(10, render_run(run))
        if p["used"] is not None:
            gh.comment(10, render_state({"version": 2, "issue": 5, "auto_fixes": p["used"], "feedback": None}))
        job = 910
        gh.commit_checks[HEAD] = [{"id": job, "name": "Unit tests", "conclusion": "failure", "html_url": "https://j"}]
        gh.logs[job] = case.LOG.encode()
        title = "Orders > adds tax" if p["same"] == "yes" else "Cart > empties"
        file = "orders" if p["same"] == "yes" else "cart"
        gh.annotations[job] = [{"annotation_level": "failure", "title": title, "path": f"web/src/{file}.test.ts"}]
        gh.required = {"Unit tests"}
        named = 'Required status check "Unit tests" is failing.' if p["named"] else "Repository rule violations found"
        gh.merge_error = GitHubError(405, named)
        if p["same"] == "error":
            original = gh.workflow_runs

            def broken(*a, **k):
                raise GitHubError(500, "boom")

            gh.workflow_runs = broken
        if p["label"]:
            gh.issues[5]["labels"] = [{"name": "on-hold"}]
        between = BETWEEN.get(p.get("between"))
        record = []
        sweeps(p, gh, cfg, p, record, between and (lambda: between(gh)))
        if p["same"] == "error":
            gh.workflow_runs = original
        return record
    finally:
        case.doCleanups()


def fam_own_merge(p):
    """Required checks that fail at the round's own merge, in `report`."""
    case = ci_case()
    try:
        gh = case.gh
        cfg = cfg_of({**p, "merge_auto": True})
        case.cfg = cfg
        gh.add_pull(10, 5, head_sha=HEAD)
        if p["used"] is not None and p["action"] == "fix":
            gh.comment(10, render_state({"version": 2, "issue": 5, "auto_fixes": p["used"], "feedback": None}))
        job = 910
        gh.commit_checks[HEAD] = [{"id": job, "name": "Unit tests", "conclusion": "failure", "html_url": "https://j"}]
        gh.logs[job] = case.LOG.encode()
        title = "Orders > adds tax" if p["same"] == "yes" else "Cart > empties"
        file = "orders" if p["same"] == "yes" else "cart"
        gh.annotations[job] = [{"annotation_level": "failure", "title": title, "path": f"web/src/{file}.test.ts"}]
        gh.required = {"Unit tests"}
        gh.merge_error = GitHubError(405, 'Required status check "Unit tests" is failing.' if p["named"] else "Repository rule violations found")
        if p["label"]:
            gh.issues[5]["labels"] = [{"name": "on-hold"}]
        tmp = tempfile.TemporaryDirectory()
        artifacts = Path(tmp.name)

        def write(folder, name, data):
            (artifacts / folder).mkdir(exist_ok=True)
            (artifacts / folder / name).write_text(json.dumps(data))

        tokens = {"input": 1, "output": 1}
        write("nexkit-agent", "result.json", {"status": "done", "tokens": tokens})
        checks = [{"name": n, "run": r, "exit_code": 0, "passed": True, "output": ""} for n, r in (("test", "make test"), ("lint", "make lint"))]
        write("nexkit-checks", "checks.json", checks)
        output = {"verdict": "approve", "summary": "Good.", "criteria": [], "findings": []}
        write("nexkit-review", "result.json", {"status": "done", "tokens": tokens, "output": output})
        fix = p["action"] == "fix"
        decision = {"action": p["action"], "issue": 5, "pr": 10 if fix else None, "target": 10 if fix else 5, "branch": "nexkit/issue-5", "base": "main", "head": HEAD if fix else None, "note": "", "auto": p["auto"] if fix else False}
        published = {"published": True, "pr": 10, "head": HEAD}
        needs = {"publish": {"outputs": {"result": json.dumps(published)}}}
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "401"}):
            out = quiet(report_mod.report, gh, decision, cfg, needs, artifacts, workflow_ref=WORKFLOW, default_branch="main")
        tmp.cleanup()
        if "value" in out:
            out["value"] = {k: out["value"].get(k) for k in ("outcome", "attention", "next")}
        record = [{"report": out, "gh": snapshot(gh)}]
        gh.merge_error = None
        sweeps(p, gh, cfg, p, record)
        return record
    finally:
        case.doCleanups()


def fam_stop(p):
    """A pull request round that NexKit stopped (`report._stop_round`), through `report`."""
    gh = FakeGitHub()
    cfg = cfg_of(p)
    gh.add_milestone(1)
    gh.add_issue(5, title="Issue 5", milestone=1)
    gh.add_pull(10, 5, head_sha=HEAD)
    if p["used"] is not None:
        gh.comment(10, render_state({"version": 2, "issue": 5, "auto_fixes": p["used"], "feedback": None}))
    gh.round_comment(10, 1, HEAD, "failed", action="implement", outcome="auto_fix")
    tmp = tempfile.TemporaryDirectory()
    artifacts = Path(tmp.name)
    (artifacts / "nexkit-agent").mkdir()
    agent = {"status": p["status"], "tokens": {"input": 1, "output": 1}}
    if p["status"] == "already_on_base":
        agent["on_base"] = "f" * 40
        gh.on_base.add(HEAD)
        if p.get("issue_closed"):
            gh.issues[5]["state"] = "closed"
    if p["status"] == "publish_failed":
        agent = {"status": "done", "tokens": {"input": 1, "output": 1}}
    if p["status"] == "error":
        agent = {"status": "error", "error": "boom", "tokens": {"input": 1, "output": 1}}
    if p["status"] == "blocked":
        agent = {"status": "blocked", "output": {"status": "blocked", "summary": "s", "blocker": "Stuck.", "questions": [], "protected_paths": [], "checks_run": [], "cause": "", "cause_outside": False, "open_conflicts": []}, "tokens": {"input": 1, "output": 1}}
    (artifacts / "nexkit-agent" / "result.json").write_text(json.dumps(agent))
    decision = {"action": p["action"], "issue": 5, "pr": 10, "target": 10, "branch": "nexkit/issue-5", "base": "main", "head": HEAD, "note": "", "auto": p["auto"]}
    if p["conflicts"]:
        decision["conflicts"] = True
    if p["retry"]:
        decision["retry"] = 1
    publish = {"published": False} if p["status"] == "publish_failed" else {"published": True, "pr": 10, "head": HEAD}
    needs = {"publish": {"outputs": {"result": json.dumps(publish)}, "result": "failure" if p["status"] == "publish_failed" else "success"}}
    with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "401", "GITHUB_REPOSITORY": "acme/app"}):
        out = quiet(report_mod.report, gh, decision, cfg, needs, artifacts, workflow_ref=WORKFLOW, default_branch="main")
    tmp.cleanup()
    if "value" in out:
        out["value"] = {k: out["value"].get(k) for k in ("outcome", "attention", "next")}
    record = [{"report": out, "gh": snapshot(gh)}]
    sweeps(p, gh, cfg, p, record)
    return record


def fam_superseded(p):
    """Checks that the base branch's move stopped (`maintain.stop_superseded`, `_give_back`)."""
    gh = FakeGitHub()
    cfg = cfg_of(p)
    gh.add_milestone(1)
    gh.add_issue(5, title="Issue 5", milestone=1)
    pull = gh.add_pull(10, 5, head_sha=HEAD)
    if p["used"] is not None:
        gh.comment(10, render_state({"version": 2, "issue": 5, "auto_fixes": p["used"], "feedback": None}))
    fields = {"action": p["action"], "status": "running"}
    kinds = list(p["kinds"])
    if kinds:
        fields["kinds"] = kinds
    for flag in ("conflicts", "retry", "resumed"):
        if p.get(flag):
            fields[flag] = True if flag != "retry" else 1
    run = {"round": 2, "trigger": "fix", "url": "https://github.com/acme/app/actions/runs/77", "head": HEAD, "checks": "running", "verdict": "pending", **fields}
    gh.comment(10, render_run(run, "Running."))
    gh.runs[77] = {"id": 77, "status": "in_progress", "run_attempt": 1}
    gh.jobs[77] = [{"name": "nexkit / verify (unit)", "status": "in_progress"}]
    gh.compares[("f" * 40, HEAD)] = {"behind_by": 1, "ahead_by": 1, "status": "diverged"}
    if p["conflicting"]:
        pull["mergeable"] = False
    strict = p.get("strict", True)
    args = (gh, pull, "main", p["conflicting"], lambda: strict, "nexkit.yml", "main", p.get("max_rounds", 2))
    out = quiet(maintain.stop_superseded, *args)
    record = [{"stop": out, "gh": snapshot(gh)}]
    sweeps({**p, "sweeps": 1}, gh, cfg, p, record)
    return record


def remove_label(gh):
    gh.issues[5]["labels"] = []


def push(gh):
    for number in (7, 10):
        if number in gh.pulls:
            gh.pulls[number]["head"]["sha"] = "9" * 40


def close_issue(gh):
    gh.issues[5]["state"] = "closed"


def reopen_issue(gh):
    gh.issues[5]["state"] = "open"


BETWEEN = {"unlabel": remove_label, "push": push, "close": close_issue, "reopen": reopen_issue, "label": lambda gh: gh.issues[5].update(labels=[{"name": "on-hold"}])}

FAMILIES = {"hold": fam_hold, "ci": fam_ci, "required": fam_required, "own_merge": fam_own_merge, "stop": fam_stop, "superseded": fam_superseded}


TALLY = {}


def instrument():
    try:
        from nexkit import host
    except ImportError:
        return
    for name in ("released", "base_passes", "required_failed", "stopped_counts"):
        fn = getattr(host, name, None)
        if fn is None:
            continue

        def wrapped(*a, _fn=fn, _name=name, **k):
            try:
                value = _fn(*a, **k)
            except Exception as error:
                key = f"{_name}:raised {type(error).__name__}"
                TALLY[key] = TALLY.get(key, 0) + 1
                raise
            if _name == "stopped_counts":
                key = f"{_name}:{'gave back' if value != max(int(a[3] or 0), 0) else 'kept'} auto={a[4]} host={a[5]}"
            else:
                key = f"{_name}:{value}"
            TALLY[key] = TALLY.get(key, 0) + 1
            return value

        setattr(host, name, wrapped)


def main():
    os.environ["GITHUB_REPOSITORY"] = "acme/app"
    if os.environ.get("TALLY"):
        instrument()
    for line in sys.stdin:
        p = json.loads(line)
        try:
            out = FAMILIES[p["family"]](p)
        except Exception as error:
            out = {"crash": f"{type(error).__name__}: {error}", "tb": traceback.format_exc().splitlines()[-4:]}
        print(json.dumps({"p": p, "out": out}, sort_keys=True, default=str), flush=True)
    if os.environ.get("TALLY"):
        with open(os.environ["TALLY"] + f".{os.getpid()}", "w") as f:
            json.dump(TALLY, f)


if __name__ == "__main__":
    main()

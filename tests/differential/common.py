"""What every family of the differential check shares: the tree it plays, the record of a
fake GitHub with NexKit's markers decoded, and the steps that follow a command (the preview, a
sweep, `nexkit status`).

A family module imports this first: `enter(root)` puts the tree to play on `sys.path` before
anything of NexKit is imported, so `nexkit` and `tests.support` are that tree's own.
"""

import base64
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
HEAD = "c" * 40
# A failure the base branch has too, as `autopilot.ci_wait` finds it.
WAIT = {"bug": 77, "workflow": "ci.yml", "branch": "main", "run": 50}
ENV = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}

TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
MARK = re.compile(r"<!-- nexkit:([a-z-]+) ([A-Za-z0-9+/=]+) -->")
HUMAN = re.compile(r"\d\d:\d\d UTC on \d{4}-\d\d-\d\d|\d{4}-\d\d-\d\d \d\d:\d\d UTC")
# What a fake GitHub keeps that is no state of GitHub: the reads it saw, and the issue events
# that a test lists for it, which only the engine's host reads and NexKit 6's fake lacks.
SKIP = {"calls", "_next", "events"}


def enter(root):
    """Play `root`: its `nexkit` and `tests` come first on the path, and it is the working
    directory, as its own tests run."""
    root = os.path.abspath(root)
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ.update(ENV)


def norm(text):
    """`text` with each NexKit marker decoded, and the times the clock gives gone: they differ
    between two runs. Times a scenario fixes are in 2099 and stay."""

    def decode(match):
        try:
            data = json.loads(base64.b64decode(match.group(2)))
        except Exception:  # noqa: BLE001
            return match.group(0)
        if isinstance(data, dict):
            data.pop("at", None)
        return f"<{match.group(1)} " + json.dumps(data, sort_keys=True) + ">"

    def time(match):
        # A time the scenario fixed, all in 2099, is compared: a wrong resume time is a
        # difference. One that the clock gives is not.
        found = match.group(0)
        if "2099" in found:
            return found
        return "TIME" if "UTC" in found else "T"

    return HUMAN.sub(time, TS.sub(time, MARK.sub(decode, text or "")))


def walk(value):
    """`value` as plain JSON, every text normalized (`norm`) and sets sorted."""
    if isinstance(value, str):
        return norm(value)
    if isinstance(value, dict):
        return {str(key): walk(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = [walk(item) for item in value]
        return sorted(items, key=repr) if isinstance(value, set) else items
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return norm(str(value))


def snapshot(gh):
    """Everything on the fake GitHub `gh`: comments, statuses, reviews, dispatches, merges,
    issues, pull requests, labels and the rest it keeps."""
    return walk({k: v for k, v in vars(gh).items() if k not in SKIP and not callable(v)})


def attempt(fn, *args, **kwargs):
    """`fn`'s result, normalized, or the exception it raised, as text: a crash on one tree
    and not the other is a difference like any other."""
    try:
        return walk(fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001
        return f"EXC {type(exc).__name__}: {exc}"


@contextlib.contextmanager
def quiet():
    """No output of NexKit's on the console: it would only interleave across shards."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def chk(name, passed, line="FAIL", run=None):
    """A check's result as `checks.json` holds it."""
    return {
        "name": name,
        "run": run or f"make {name}",
        "exit_code": 0 if passed else 1,
        "passed": passed,
        "output": "" if passed else f"ok\n{line}\n",
    }


def crit(status, name="add works"):
    return {"criterion": name, "status": status, "test": "t", "evidence": f"ev {status}"}


def review_output(verdict="approve", criteria=None, findings=(), checklist=None):
    found = {
        "verdict": verdict,
        "summary": "S.",
        "criteria": criteria if criteria is not None else [crit("met")],
        "findings": list(findings),
    }
    if checklist is not None:
        found["checklist"] = checklist
    return found


def done(output):
    return {"status": "done", "tokens": TOK, "output": output}


BLOCK = {"severity": "blocking", "file": "a.py", "line": 1, "body": "Breaks."}
APPROVE = done(review_output())
REQUEST_CHANGES = done(review_output("request_changes", [crit("unmet")], [BLOCK]))
FAILED = {"status": "error", "error": "Review crashed", "tokens": TOK}
TRANSIENT = {"status": "error", "error": "API overloaded", "transient": True, "tokens": TOK}
PAUSED = {
    "status": "paused",
    "error": "limit",
    "limit": "limit · resets 4:40am (UTC)",
    "resume_at": "2099-10-11T04:40:00Z",
    "reset_known": True,
}
PAUSED_UNKNOWN = {**PAUSED, "limit": "limit", "reset_known": False}


class Artifacts:
    """The artifacts a round's `report` job downloads, in a folder of their own."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)

    def write(self, folder, name, data):
        (self.path / folder).mkdir(exist_ok=True)
        (self.path / folder / name).write_text(json.dumps(data))

    def agent(self, data):
        self.write("nexkit-agent", "result.json", data)

    def checks(self, data):
        if data is not None:
            self.write("nexkit-checks", "checks.json", data)

    def review(self, data):
        if data is not None:
            self.write("nexkit-review", "result.json", data)

    def close(self):
        self._tmp.cleanup()


def planned_issue(gh, number=5, labels=()):
    """Issue `number` in milestone 1, planned and then gone: its plan and `/nexkit go` runs."""
    from nexkit.state import PLAN_MARKER, render_run

    gh.add_milestone(1)
    gh.add_issue(number, milestone=1, labels=list(labels))
    runs = "https://github.com/acme/app/actions/runs"
    plan = {"url": f"{runs}/1", "command": "plan", "status": "success", "outcome": "planned"}
    gh.comment(number, render_run(plan, "D."))
    gh.comment(number, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    go = {"url": f"{runs}/2", "command": "go", "status": "success", "outcome": "pr_opened"}
    gh.comment(number, render_run(go, "D."))


def sweep(gh, cfg):
    """A sweep and the `step` runs it sent, as the pipeline plays them, and its log. NexKit 6
    sends no `step`, so there it is the plain sweep. The `step` dispatches leave `gh`, so
    what the two trees compare is what the sweeps and their steps wrote."""
    from nexkit import autopilot

    if not hasattr(autopilot, "request_step"):
        return autopilot.sweep(gh, cfg, "nexkit.yml", "main")
    from tests.support import play_sweep

    return play_sweep(gh, cfg)


def resume(gh, cfg, now):
    """The run that resumes paused commands and the `step` runs it sent, as the pipeline plays
    them: (its log, the next resume time). NexKit 6 sends no `step`, so there it is `resume_paused`
    itself. The `step` dispatches leave `gh`, so what the two trees compare is what the resume
    and its steps wrote."""
    from nexkit import maintain

    if not hasattr(maintain, "resume_one"):
        return maintain.resume_paused(gh, "nexkit.yml", "main", now, cfg)
    from tests.support import play_resume

    return play_resume(gh, cfg, now)


# `after` has an argument that hides `sweep`.
_sweep = sweep


def after(gh, cfg, res, tag="", sweep=True, status=True):
    """The preview of issue 5's step, a sweep's log, GitHub after it, and `nexkit status`,
    into `res` under keys ending in `tag`."""
    from nexkit import autopilot

    def preview():
        _, steps = autopilot.preview(gh, cfg)
        return {s["number"]: s for s in steps}.get(5)

    res["step" + tag] = attempt(preview)
    if sweep:
        res["lines" + tag] = attempt(_sweep, gh, cfg)
        res["gh" + tag] = snapshot(gh)
    if status:
        from nexkit.status import collect

        res["status" + tag] = attempt(collect, gh, cfg, workers=1)

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
from collections import defaultdict
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
# The App's state comment (`nexkit:item-state`), which only the engine's host posts: the marker
# ends the comment (`item_state.PATTERN`). NexKit 6 never posts it, so it is not compared.
STATE = re.compile(r"<!-- nexkit:item-state [A-Za-z0-9+/=]+ -->\s*\Z")
# What `seed` and the tree record for a scenario, which `play.py` writes beside its record and
# `harness.py` reads: each write of a state comment as (item, revision), each decision that
# `host.resync` took from the markers' document as [function, why], and the items that were
# seeded.
STATES = []
TRACKED = []  # the fake GitHub that `seed` watches
FALLBACKS = []
SEEDED = 0
OWN_BRANCH = re.compile(r"nexkit/issue-\d+")


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


def is_state(comment):
    """Whether `comment` is the App's state comment."""
    return (comment.get("user") or {}).get("type") == "Bot" and bool(
        STATE.search(comment.get("body") or "")
    )


def snapshot(gh):
    """Everything on the fake GitHub `gh`: comments, statuses, reviews, dispatches, merges,
    issues, pull requests, labels and the rest it keeps. Not the App's state comment, which
    NexKit 6 never posts (`seed` records it apart), and no issue with no comment left: reading
    an issue's comments makes an empty list of them in the fake, which is not GitHub's."""
    found = {}
    for key, value in vars(gh).items():
        if key in SKIP or callable(value):
            continue
        if key == "issue_comments":
            value = {
                n: kept
                for n, held in value.items()
                if (kept := [c for c in held if not is_state(c)])
            }
        found[key] = value
    return walk(found)


def reset():
    """Forget what the last scenario recorded."""
    global SEEDED
    STATES.clear()
    TRACKED.clear()
    FALLBACKS.clear()
    SEEDED = 0


def items(gh):
    """The numbers of the work items that a run in an item's group starts from, in order, from
    what the fake holds alone, so that both trees agree: the open issues, and the open pull
    requests that are not a `nexkit/issue-<n>` branch of the repository, which belong to their
    issue (`route.queue_item`)."""
    found = {
        n
        for n, one in gh.issues.items()
        if one.get("state") == "open" and "pull_request" not in one
    }
    for number, pull in gh.pulls.items():
        if pull.get("state") != "open":
            continue
        head = pull.get("head") or {}
        own = (head.get("repo") or {}).get("full_name") == gh.repository
        if not (own and OWN_BRANCH.fullmatch(head.get("ref") or "")):
            found.add(number)
    return sorted(found)


def _recording(gh, item_state):
    """Make each state comment that is written to `gh` noted as (item, revision) in `STATES`.
    It wraps `comment` and `update_comment` of its class and notes only the writes to `gh`
    itself, not to a copy of it, which a family may make."""
    cls = type(gh)
    TRACKED[:] = [gh]
    if getattr(cls, "_nexkit_recording", False):
        return
    real_post, real_edit = cls.comment, cls.update_comment

    def note(self, number, comment):
        if not TRACKED or TRACKED[0] is not self:
            return
        if item_state.PREFIX not in (comment.get("body") or ""):
            return
        try:
            state = item_state.find([comment])[1]
        except Exception:  # noqa: BLE001
            return
        if state is not None:
            STATES.append((number, state["revision"]))

    def post(self, number, body):
        comment = real_post(self, number, body)
        note(self, number, comment)
        return comment

    def edit(self, comment_id, body):
        comment = real_edit(self, comment_id, body)
        found = [n for n, each in self.issue_comments.items() if comment in each]
        note(self, found[0] if found else None, comment)
        return comment

    cls.comment, cls.update_comment, cls._nexkit_recording = post, edit, True


def seed(gh, cfg):
    """Store the import of each open item (`items`) on the tree as the App's state comment at
    revision 1, as a run in the item's group does before its first event (`host.store_import`),
    and record the comment's writes in `STATES`. Call it once, after the fixtures and before
    the first event, or, for a stage that stops, just after the run's `progress.start`, so that
    the stored state holds the run on its way as a run in the item's group stores it. NexKit 6
    has no stored state, so nothing is stored there. Each item uses up one comment id on both
    trees, so that the comments after it have the same ids, and the issue keeps its
    `updated_at`: the seed must leave nothing that NexKit 6's tree lacks."""
    global SEEDED
    try:
        from nexkit import host, item_state

        store = getattr(host, "store_import", None)
    except ImportError:
        store = item_state = None
    if store is not None:
        _recording(gh, item_state)
    missing = object()
    for number in items(gh):
        before = gh._next
        stamp = (gh.issues.get(number) or {}).get("updated_at", missing)
        kept = {k: set(v) for k, v in vars(gh).items() if isinstance(v, defaultdict)}
        if store is not None:
            try:
                if store(gh, cfg, number):
                    SEEDED += 1
            except Exception:  # noqa: BLE001
                pass
            # What the import read makes empty entries in the fake's defaultdicts, which are no
            # state of GitHub, and which NexKit 6's tree, that reads nothing here, lacks.
            for name, keys in kept.items():
                held = getattr(gh, name)
                for key in set(held) - keys:
                    if not held[key]:
                        del held[key]
            if number in gh.issues:
                if stamp is missing:
                    gh.issues[number].pop("updated_at", None)
                else:
                    gh.issues[number]["updated_at"] = stamp
        gh._next = before + 1


def count_dispatched_round(gh, number):
    """Store the conflict round that is about to report as the run of its dispatch stores it,
    one more conflict round used in the item's state (`host.command`): the round's comment is a
    fixture that the import before it did not count, and the report of a round that NexKit's
    own dispatch started finds the round counted. NexKit 6 has no stored state, so nothing is
    stored there."""
    try:
        from nexkit import item_state
    except ImportError:
        return
    comment, stored = item_state.read(gh, number)
    if stored is None:
        return
    found = {**stored, "revision": stored["revision"] + 1}
    found["rounds"] = {**stored["rounds"], "conflicts": stored["rounds"]["conflicts"] + 1}
    item_state.write(gh, number, found, (comment, stored))


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

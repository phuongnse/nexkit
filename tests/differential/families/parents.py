"""#531: how every parent issue ends, at every level and however its last part closed: a person
closes the part (the `parents` task of `maintain`), the round's merge (a writer's approval of
the merge that was refused), a round that finds no change is needed, a pull request whose base
branch already holds the change, or nobody runs anything and autopilot's sweep finds the parts
closed × one parent or two levels of them × the reason a person closed the part (completed or
not planned) × a sibling of the part (none, open, completed, not planned) × `close_parent_issues`
× more than ten sub-issues, a plan with a part never filed, or an open part of the grandparent.
Records the command, GitHub after it, the preview, two sweeps and `nexkit status`."""

import itertools
from datetime import UTC, datetime, timedelta
from unittest import mock

from common import HEAD, attempt, planned_issue, quiet, snapshot, sweep

import tests.support as support
from nexkit import autopilot
from nexkit import report as report_module
from nexkit.maintain import close_on_base, maintain
from nexkit.state import PLAN_DATA_PREFIX, PLAN_MARKER, _encode, render_run
from tests.support import BOT_LOGIN, FakeGitHub, make_config

# The part, the parent, its sibling parts, the grandparent and the grandparent's other part.
PART, PARENT, SIBLING, GRAND, UNCLE = 5, 1, 2, 3, 4
MANY = range(20, 29)


def scenarios():
    dims = dict(
        path=["person", "merge", "no_change", "base", "sweep"],
        depth=[1, 2],
        reason=["completed", "not_planned"],
        sibling=["none", "open", "completed", "not_planned"],
        close=[False, True],
        extra=["none", "many", "unfiled", "uncle"],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True))
        # Only a person's close, or a close that nobody ran anything for, has another reason.
        if sc["reason"] != "completed" and sc["path"] not in ("person", "sweep"):
            continue
        # More than ten sub-issues: the part, nine not planned, and the sibling.
        if sc["extra"] == "many" and (sc["sibling"] == "none" or sc["depth"] != 1):
            continue
        if sc["extra"] == "unfiled" and (sc["sibling"] == "open" or sc["depth"] != 1):
            continue
        if sc["extra"] == "uncle" and (sc["depth"] != 2 or sc["sibling"] == "open"):
            continue
        yield sc


def _sibling(gh, number, state, parent):
    gh.add_issue(number, title="Part", milestone=1)
    gh.parents[number] = parent
    if state != "open":
        gh.issues[number].update(state="closed", state_reason=state)


def _unfiled(gh):
    """Parts of the parent that autopilot filed, and a plan that lists more of them."""
    bot = {"user": {"login": BOT_LOGIN}, "body": f"<!-- nexkit:part {PARENT} -->\nDo it."}
    for number in (PART, SIBLING):
        if number in gh.issues:
            gh.issues[number].update(bot)
    split = [{"title": f"P{n}", "body": f"Do {n}.", "depends_on": []} for n in range(1, 5)]
    data = {"questions": [], "too_large": True, "split": split}
    listed = "\n".join(f"{n}. **{p['title']}**: {p['body']}" for n, p in enumerate(split, 1))
    text = f"{PLAN_MARKER}\n## Plan\n\nAdd it.\n\n### Suggested split\n\n{listed}\n\n---\n"
    gh.comment(
        PARENT, f"{text}This looks too large for one session.\n\n{_encode(PLAN_DATA_PREFIX, data)}"
    )


def _ticking():
    """`datetime` for the fake GitHub, whose clock moves one second at each read, so that the
    issue that closes last is the one that closes last, and not a matter of which second a
    close fell in."""
    clock = [datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)]

    class Ticking(datetime):
        @classmethod
        def now(cls, tz=None):
            clock[0] += timedelta(seconds=1)
            return clock[0]

    return Ticking


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    gh.add_issue(PARENT, title="Epic", milestone=1)
    gh.parents[PART] = PARENT
    if sc["sibling"] != "none" and sc["extra"] != "many":
        _sibling(gh, SIBLING, sc["sibling"], PARENT)
    if sc["extra"] == "many":
        # Nine parts not planned, and the sibling, last, in the state of the scenario.
        for number in MANY:
            _sibling(gh, number, "not_planned", PARENT)
        _sibling(gh, 29, sc["sibling"], PARENT)
    if sc["depth"] == 2:
        gh.add_issue(GRAND, title="Goal", milestone=1)
        gh.parents[PARENT] = GRAND
    if sc["extra"] == "uncle":
        _sibling(gh, UNCLE, sc["sibling"] if sc["sibling"] != "none" else "completed", GRAND)
    if sc["extra"] == "unfiled":
        _unfiled(gh)
    if sc["path"] in ("merge", "base"):
        gh.add_pull(6, PART, head_sha=HEAD)
        gh.pulls[6]["mergeable_state"] = "clean"
    if sc["path"] == "base":
        gh.on_base.add(HEAD)
    cfg = make_config(
        autopilot={"enabled": True},
        merge={"auto": True},
        close_parent_issues=sc["close"],
        notify=["olivia"],
    )
    res = {}
    closed = {"state": "closed", "state_reason": sc["reason"]}
    with (
        mock.patch.object(report_module.time, "sleep", lambda s: None),
        mock.patch.object(support, "datetime", _ticking()),
        quiet(),
    ):
        if sc["path"] == "person":
            gh.issues[PART].update(closed)
            decision = {"action": "maintain", "task": "parents", "issue": PART}
            res["out"] = attempt(maintain, gh, decision, cfg, workflow="nexkit.yml", ref="main")
        elif sc["path"] == "sweep":
            gh.issues[PART].update(closed)
        elif sc["path"] == "no_change":
            agent = {"output": {"no_change": "Fixed by 8b9cb87."}}
            res["out"] = attempt(report_module._no_change, gh, cfg, {"issue": PART}, agent)
        elif sc["path"] == "base":
            target = {"pr": 6, "issue": PART, "base": "main", "merge_base": "c" * 40}
            res["out"] = attempt(close_on_base, gh, cfg, target, commit="d" * 40)
        else:
            row = {
                "round": 1,
                "trigger": "implement (autopilot)",
                "status": "success",
                "outcome": "merge_refused",
                "head": HEAD,
                "checks": "passed",
                "verdict": "approve",
                "url": "https://github.com/acme/app/actions/runs/3",
            }
            gh.comment(6, render_run(row))
            decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": PART}
            decision.update(actor="olivia", commit=HEAD)
            res["out"] = attempt(maintain, gh, decision, cfg, workflow="nexkit.yml", ref="main")
        res["gh"] = snapshot(gh)
        _, steps = autopilot.preview(gh, cfg)
        res["steps"] = attempt(lambda: {s["number"]: s for s in steps})
        for n in (1, 2):
            res[f"lines{n}"] = attempt(sweep, gh, cfg)
            res[f"gh{n}"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    return res

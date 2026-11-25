"""#398: a plan, implement or fix stage whose agent stopped at the usage limit, with a known or
unknown reset, or on a transient error, or whose job ended without a result, or whose publish
stopped on a transient error × `retry.wait_minutes` and how many runs again were used ×
`usage_limit.resume` × a run that was resumed blind × a hold label × autopilot. Records
`report`, the preview, a sweep and `nexkit status`, then the scheduled resume of what came
due, with GitHub after it."""

import itertools
import json
from datetime import UTC, datetime
from unittest import mock

from common import (
    PAUSED,
    PAUSED_UNKNOWN,
    TOK,
    TRANSIENT,
    WORKFLOW,
    Artifacts,
    after,
    attempt,
    planned_issue,
    quiet,
    snapshot,
)

from nexkit import progress
from nexkit import report as reports
from nexkit.maintain import resume_paused
from nexkit.report import report
from nexkit.state import empty_state, render_state
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
KINDS = {
    "plan": ("plan", {}),
    "go": ("implement", {}),
    "fix_auto": ("fix", {"auto": True, "autopilot": "true"}),
    "fix_person": ("fix", {}),
}
STOPS = ("paused", "paused_unknown", "transient", "missing", "publish_transient")
WAITS = {"none": [], "now": [0], "later": [15], "three": [15, 30, 60]}
# After every resume came due, which a time in 2099 does as the 2100 sweep finds it.
LATER = datetime(2100, 1, 1, tzinfo=UTC)


class Clock(datetime):
    """The clock `report` reads: a run again after a wait is then at a time in 2099, which the
    comparison keeps, so a wrong wait is a difference."""

    @classmethod
    def now(cls, tz=None):
        return cls(2099, 6, 1, 12, 0, tzinfo=UTC)


def scenarios():
    dims = dict(
        kind=list(KINDS),
        stop=list(STOPS),
        retry=[0, 1, 2, 3],
        waits=list(WAITS),
        resume=[True, False],
        resumed=[False, True],
        held=[False, True],
        ap=[False, True],
    )
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh, labels=["on-hold"] if sc["held"] else [])
    action, extra = KINDS[sc["kind"]]
    pull = action == "fix"
    if pull:
        gh.add_pull(6, 5, head_sha=HEAD)
        gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
        gh.round_comment(
            6,
            1,
            "b" * 40,
            "failed",
            outcome="auto_fix",
            trigger="fix (auto)",
            action="fix",
            kinds=["auto"],
        )
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        retry={"wait_minutes": WAITS[sc["waits"]]},
        usage_limit={"resume": sc["resume"]},
        notify=["olivia"],
    )
    d = {
        "action": action,
        "issue": 5,
        "pr": 6 if pull else None,
        "target": 6 if pull else 5,
        "branch": "nexkit/issue-5",
        "base": "main",
        "head": HEAD if pull else None,
        "note": "Keep it small." if sc["kind"] == "go" else "",
        "auto": False,
        "run_id": 4242,
        **extra,
    }
    if sc["ap"] and not extra:
        d["autopilot"] = "true"
    if sc["retry"]:
        d["retry"] = sc["retry"]
    if sc["resumed"]:
        d["resumed"] = True
    res = {}
    with quiet(), mock.patch.object(reports, "datetime", Clock):
        res["report"] = one_stage(gh, cfg, d, sc["stop"])
        res["after_report"] = snapshot(gh)
        after(gh, cfg, res)
        res["resume"] = attempt(resume_paused, gh, "nexkit.yml", "main", LATER, cfg)
        res["end"] = snapshot(gh)
    return res


def one_stage(gh, cfg, d, stop):
    art = Artifacts()
    needs = {}
    if stop == "paused":
        art.agent(PAUSED)
    elif stop == "paused_unknown":
        art.agent(PAUSED_UNKNOWN)
    elif stop == "transient":
        art.agent(TRANSIENT)
    elif stop == "missing":
        needs["agent"] = {"result": "failure"}
    else:
        art.agent({"status": "done", "tokens": TOK, "output": {}})
        error = "git push --quiet origin failed: The requested URL returned error: 500"
        result = {"published": False, "error": error, "transient": True}
        needs["publish"] = {"outputs": {"result": json.dumps(result)}}
    try:
        progress.start(gh, d)
    except Exception as exc:  # noqa: BLE001
        art.close()
        return f"EXC start {type(exc).__name__}: {exc}"
    found = attempt(
        report, gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
    )
    art.close()
    return found

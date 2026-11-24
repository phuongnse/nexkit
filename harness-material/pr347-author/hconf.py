"""Differential harness: a round whose commit conflicts with the base branch's tip, as
`publish` found, so its checks and review did not run (`report._skipped_conflicting`); for an
implement round, an automatic, a person's, a conflict or a resumed fix round; with earlier
conflict rounds (some that do not count), the latest tried on this base commit or still
running, `conflicts.max_rounds` 1-3, a hold label (or one NexKit cannot read), autopilot and
`merge.auto`. Then the preview, a sweep, and, for a held round, the label taken off with the
head moved or not and the pull request conflicting, not or unknown, `auto_resolve` turned off
or not and `max_rounds` lowered or not, two sweeps more; GitHub and `nexkit status` after
each.

usage: python3 hconf.py ROOT OUT [shard n]
"""
import contextlib, io, itertools, json, os, sys, tempfile
from pathlib import Path
from unittest import mock

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
from common import main, snapshot, walk  # noqa: E402
from nexkit import autopilot, maintain, progress  # noqa: E402
from nexkit.github import GitHubError  # noqa: E402
from nexkit.report import report  # noqa: E402
from nexkit.state import PLAN_MARKER, empty_state, render_run, render_state  # noqa: E402
from tests.support import FakeGitHub, make_config  # noqa: E402

WORKFLOW = "acme/app/.github/workflows/nexkit.yml@refs/heads/main"
TOK = {"input": 3, "cached": 1, "output": 2}
HEAD = "c" * 40
BASE = "e" * 40
KINDS = {
    "implement": ("implement", False, ()),
    "auto": ("fix", True, ("auto",)),
    "person": ("fix", False, ()),
    "conflicts": ("fix", True, ("auto", "conflicts")),
    "resumed": ("fix", True, ("auto", "conflicts", "resumed")),
}


def scenarios():
    dims = dict(
        kind=list(KINDS),
        earlier=[0, 1, 2, 3],
        uncounted=[False, True],
        last=["ended", "tried_here", "running"],
        max_rounds=[1, 2, 3],
        hold=["no", "label", "error"],
        ap=[True, False],
        merge=[False, True],
        after=["none", "same_conflicts", "same_clean", "same_unknown", "moved"],
        later=["same", "off", "lower"],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values))
        if sc["hold"] == "no" and (sc["after"] != "none" or sc["later"] != "same"):
            continue
        if sc["uncounted"] and not sc["earlier"]:
            continue
        yield sc


def run(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, milestone=1, labels=["on-hold"] if sc["hold"] == "label" else ())
    gh.branches["main"] = BASE
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1",
                              "command": "plan", "status": "success", "outcome": "planned"}, "D."))
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/2",
                              "command": "go", "status": "success", "outcome": "pr_opened"}, "D."))
    gh.add_pull(6, 5, head_sha=HEAD if sc["kind"] == "implement" else "b" * 40)
    gh.pulls[6]["mergeable"] = False
    gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1}))
    for n in range(sc["earlier"]):
        fields = {"conflicts": True, "base_sha": "d" * 40, "trigger": "fix (auto, conflicts)",
                  "action": "fix", "kinds": ["auto", "conflicts"], "outcome": "needs_human"}
        if sc["uncounted"] and n == 0:
            fields["outcome"] = "up_to_date"
        gh.round_comment(6, n + 1, "b" * 40, "failed", **fields)
    if sc["kind"] != "implement":
        last = {"trigger": "fix (auto)", "action": "fix", "kinds": ["auto"],
                "outcome": "needs_human"}
        if sc["last"] == "tried_here":
            last["conflicts_started"] = BASE
        gh.round_comment(6, sc["earlier"] + 1, "b" * 40, "failed", **last)
    if sc["last"] == "running":
        gh.comment(6, render_run({"round": sc["earlier"] + 2, "trigger": "review",
                                  "action": "review", "status": "running", "head": "b" * 40,
                                  "url": "https://github.com/acme/app/actions/runs/99"}, "R."))
    cfg = make_config(
        autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
        merge={"auto": sc["merge"]},
        conflicts={"auto_resolve": True, "max_rounds": sc["max_rounds"]},
        notify=["olivia"],
    )
    action, auto, kinds = KINDS[sc["kind"]]
    d = {"action": action, "issue": 5, "pr": None if action == "implement" else 6,
         "target": 5 if action == "implement" else 6, "branch": "nexkit/issue-5",
         "base": "main", "head": None if action == "implement" else "b" * 40, "note": "",
         "auto": auto, "run_id": 4242}
    if "conflicts" in kinds:
        d["conflicts"] = True
        d["base_sha"] = "d" * 40
    if "resumed" in kinds:
        d["resumed"] = True
    tmp = tempfile.TemporaryDirectory()
    art = Path(tmp.name)
    (art / "nexkit-agent").mkdir()
    (art / "nexkit-agent" / "result.json").write_text(
        json.dumps({"status": "done", "tokens": TOK, "output": {}}))
    pub = {"published": True, "pr": 6, "head": HEAD, "conflicts": ["a.py", "b.py"]}
    needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}
    real_held = autopilot.held

    def fake_held(gh_, cfg_, number):
        if sc["hold"] == "error" and autopilot.takes(cfg_):
            raise GitHubError(502, "Bad gateway")
        return real_held(gh_, cfg_, number)

    res = {"sc": sc}
    env = {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "4242"}
    with mock.patch.dict(os.environ, env), mock.patch.object(autopilot, "held", fake_held), \
            mock.patch.object(maintain.time, "sleep", lambda s: None), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            progress.start(gh, d)
        except Exception as e:  # noqa: BLE001
            res["start"] = f"EXC {type(e).__name__}: {e}"
        try:
            res["report"] = walk(report(gh, d, cfg, needs, art, workflow_ref=WORKFLOW,
                                        default_branch="main"))
        except Exception as e:  # noqa: BLE001
            res["report"] = f"EXC {type(e).__name__}: {e}"
        res["after_report"] = snapshot(gh)
        sweep(gh, cfg, res, "1")
        if sc["hold"] == "label":
            gh.issues[5]["labels"] = []
            if sc["after"] == "moved":
                gh.pulls[6]["head"]["sha"] = "9" * 40
            gh.pulls[6]["mergeable"] = {"same_clean": True, "same_unknown": None}.get(
                sc["after"], False)
            if sc["later"] == "off":
                cfg = make_config(
                    autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
                    merge={"auto": sc["merge"]}, conflicts={"auto_resolve": False},
                    notify=["olivia"])
            elif sc["later"] == "lower":
                cfg = make_config(
                    autopilot={"enabled": sc["ap"], "labels": {"on-hold": {"hold": True}}},
                    merge={"auto": sc["merge"]},
                    conflicts={"auto_resolve": True, "max_rounds": 1}, notify=["olivia"])
            sweep(gh, cfg, res, "2")
            sweep(gh, cfg, res, "3")
    tmp.cleanup()
    return res


def sweep(gh, cfg, res, tag):
    try:
        _, steps = autopilot.preview(gh, cfg)
        res["step" + tag] = walk({s["number"]: s for s in steps}.get(5))
    except Exception as e:  # noqa: BLE001
        res["step" + tag] = f"EXC {type(e).__name__}: {e}"
    try:
        res["lines" + tag] = walk(autopilot.sweep(gh, cfg, "nexkit.yml", "main"))
    except Exception as e:  # noqa: BLE001
        res["lines" + tag] = f"EXC {type(e).__name__}: {e}"
    res["gh" + tag] = snapshot(gh)
    try:
        from nexkit.status import collect

        res["status" + tag] = walk(collect(gh, cfg, workers=1))
    except Exception as e:  # noqa: BLE001
        res["status" + tag] = f"EXC {type(e).__name__}: {e}"


if __name__ == "__main__":
    main(scenarios, run)

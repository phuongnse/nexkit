"""#391: a merge refused because the head moved, or for a reason only a person clears: the
round's own merge after an implement round, a sweep over a merge that waits (`merge_waiting`),
or a writer's approval (`maintain.merge_approved`) × the refusal (the issue closed, edited after
a vouch or unreadable; GitHub's 409 with a new head, with the checked head or with a head it
cannot read; a 405 with a new head; the review that cannot start; the base branch moving on
every try or on the first only; a 405 in a `blocked` or a `clean` pull request) × autopilot.
Records the command, GitHub after it, the preview, two sweeps and `nexkit status`."""

import contextlib
import itertools
import json
from unittest import mock

from common import (
    HEAD,
    TOK,
    WORKFLOW,
    Artifacts,
    attempt,
    planned_issue,
    quiet,
    seed,
    snapshot,
    sweep,
)

from nexkit import autopilot, progress
from nexkit import report as report_module
from nexkit.github import GitHubError
from nexkit.maintain import maintain
from nexkit.report import report
from nexkit.state import render_run
from tests.support import FakeGitHub, make_config

NEW = "d" * 40
MODIFIED = GitHubError(405, "Base branch was modified. Review and try the merge again.")
REFUSALS = [
    "closed",
    "edited",
    "unreadable",
    "409_new_head",
    "409_same_head",
    "405_new_head",
    "head_unreadable",
    "dispatch_fails",
    "base_moves_3",
    "base_moves_1",
    "405_blocked",
    "405_clean",
]


def scenarios():
    dims = dict(path=["first", "sweep", "approved"], refusal=REFUSALS, ap=[True, False])
    for values in itertools.product(*dims.values()):
        yield dict(zip(dims, values, strict=True))


def run(sc):
    gh = FakeGitHub()
    planned_issue(gh)
    refusal = sc["refusal"]
    moved = refusal in ("409_new_head", "405_new_head", "dispatch_fails")
    gh.add_pull(6, 5, head_sha=NEW if moved else HEAD)
    gh.pulls[6]["mergeable_state"] = "clean" if refusal == "405_clean" else "blocked"
    gh.required = set()
    cfg = make_config(
        autopilot={"enabled": sc["ap"]},
        merge={"auto": True},
        conflicts={"auto_resolve": refusal != "head_unreadable"},
        notify=["olivia"],
    )
    if sc["path"] != "first":
        # The round that approved the merge is on the pull request before the import: the item
        # is stored as that round left it, `accepting` while checks wait or `ready` for a person.
        row = {
            "round": 1,
            "trigger": "implement (autopilot)",
            "status": "success",
            "outcome": "merge_waiting" if sc["path"] == "sweep" else "merge_refused",
            "head": HEAD,
            "checks": "passed",
            "verdict": "approve",
            "url": "https://github.com/acme/app/actions/runs/3",
        }
        gh.comment(6, render_run(row))
    # Before the patches that make GitHub fail: the import is of the work as it stood.
    seed(gh, cfg)
    res = {}
    art = Artifacts()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(report_module.time, "sleep", lambda s: None))
        stack.enter_context(quiet())
        refuse(gh, refusal, stack)
        if sc["path"] == "first":
            art.agent({"status": "done", "tokens": TOK})
            art.checks(
                [{"name": "test", "run": "make test", "exit_code": 0, "passed": True, "output": ""}]
            )
            review = {
                "verdict": "approve",
                "summary": "S.",
                "findings": [],
                "criteria": [{"criterion": "x", "status": "met", "test": "t", "evidence": "e"}],
            }
            art.review({"status": "done", "tokens": TOK, "output": review})
            d = {
                "action": "implement",
                "issue": 5,
                "pr": None,
                "target": 5,
                "branch": "nexkit/issue-5",
                "base": "main",
                "head": None,
                "note": "",
                "auto": True,
                "autopilot": "true",
                "run_id": 4242,
            }
            pub = {"published": True, "pr": 6, "head": HEAD}
            needs = {"publish": {"outputs": {"result": json.dumps(pub)}}}

            def first_round():
                progress.start(gh, d)
                return report(
                    gh, d, cfg, needs, art.path, workflow_ref=WORKFLOW, default_branch="main"
                )

            res["report"] = attempt(first_round)
        else:
            if sc["path"] == "approved":
                decision = {"action": "maintain", "task": "approved", "pr": 6, "issue": 5}
                decision.update(actor="olivia", commit=HEAD)
                res["approved"] = attempt(
                    maintain, gh, decision, cfg, workflow="nexkit.yml", ref="main"
                )
        res["gh"] = snapshot(gh)
        _, steps = autopilot.preview(gh, cfg)
        res["step"] = attempt(lambda: {s["number"]: s for s in steps}.get(5))
        for n in (1, 2):
            res[f"lines{n}"] = attempt(sweep, gh, cfg)
            res[f"gh{n}"] = snapshot(gh)
        from nexkit.status import collect

        res["status"] = attempt(collect, gh, cfg, workers=1)
    art.close()
    return res


def refuse(gh, refusal, stack):
    """Make GitHub refuse the merge as `refusal` says, for the whole scenario, which `stack`
    ends."""
    if refusal == "closed":
        gh.issues[5].update(state="closed", state_reason="not_planned")
    elif refusal == "edited":
        gh.issues[5]["user"] = {"login": "bob"}
        gh.human_comment(5, "/nexkit trust", "alice")
        gh.edits[5] = [("bob", "2026-01-02T00:00:00Z")]
    elif refusal == "unreadable":
        down = GitHubError(502, "down")
        stack.enter_context(mock.patch.object(autopilot, "edited_after_vouch", side_effect=down))
    elif refusal in ("409_new_head", "409_same_head", "dispatch_fails"):
        gh.merge_error = GitHubError(409, "Head branch was modified. Review and try again.")
        if refusal == "dispatch_fails":
            gh.dispatch_errors["nexkit.yml"] = GitHubError(503, "Service Unavailable")
    elif refusal == "head_unreadable":
        gh.merge_error = GitHubError(409, "Head branch was modified. Review and try again.")
        # The two reads that follow the first refusal fail: that of the pull request's end,
        # and that of its head.
        read, merge, down = gh.pull, gh.merge, []

        def pull(number):
            if down and down[0]:
                down[0] -= 1
                raise GitHubError(502, "down")
            return read(number)

        def refusing(number, sha, method="squash"):
            if not down:
                down.append(2)
            return merge(number, sha, method)

        gh.pull, gh.merge = pull, refusing
    elif refusal in ("base_moves_3", "base_moves_1"):
        merge, left = gh.merge, [3 if refusal == "base_moves_3" else 1]

        def moving(number, sha, method="squash"):
            if not left[0]:
                return merge(number, sha, method)
            left[0] -= 1
            gh.branches["main"] = f"{left[0]}" * 40
            raise MODIFIED

        gh.merge = moving
    else:
        gh.merge_error = GitHubError(405, "Merge refused.")

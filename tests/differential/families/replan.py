"""#413: the plan that autopilot asked for after a pull request's round ran out of time:
too large or not × its parts (none, two, eleven) × whether they are titled like the issue ×
the issue's place (top, a part one level deep, a part three levels deep) × autopilot's record
(which of `fewer`, `parts` and `titles` it asked before) × questions × a no-change finding ×
protected paths × `autopilot.answer`; and a plan whose run failed, and one from before its
record. Records the preview, a sweep and GitHub after it, and `nexkit status`."""

import itertools

from common import HEAD, after, quiet

from families.stop import QSETS
from nexkit import autopilot
from nexkit.state import PLAN_MARKER, plan_data_record, render_run
from tests.support import BOT_LOGIN, FakeGitHub, make_config

FLAGS = {
    "none": {},
    "fewer": {"fewer_asked": True},
    "parts": {"parts_asked": True},
    "titles": {"titles_asked": True},
}
QUESTIONS = ["none", "rec", "norec", "close"]
RUNS = "https://github.com/acme/app/actions/runs"


def scenarios():
    dims = dict(
        depth=["top", "deep1", "deep3"],
        too_large=[False, True],
        parts=[0, 2, 11],
        titles=[False, True],
        flags=list(FLAGS),
        qset=QUESTIONS,
        no_change=[False, True],
        protected=[False, True],
        answer=["recommended", "never"],
    )
    for values in itertools.product(*dims.values()):
        sc = dict(zip(dims, values, strict=True), variant="plan")
        if sc["titles"] and not sc["parts"]:
            continue
        if not sc["too_large"] and (sc["parts"] or sc["titles"]):
            continue
        yield sc
    for variant in ("failed", "no_record"):
        for depth, answer in itertools.product(["top", "deep3"], ["recommended", "never"]):
            yield dict(
                depth=depth,
                too_large=True,
                parts=2,
                titles=False,
                flags="none",
                qset="none",
                no_change=False,
                protected=False,
                answer=answer,
                variant=variant,
            )


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    top = 5 - {"top": 0, "deep1": 1, "deep3": 3}[sc["depth"]]
    for n in range(top, 6):
        body = "Please add it." if n == top else f"<!-- nexkit:part {n - 1} -->\nPart."
        user = "alice" if n == top else BOT_LOGIN
        title = "Cache" if n == 5 and top == 5 else f"Issue {n}"
        gh.add_issue(n, title=title, body=body, user=user, milestone=1)
        if n > top:
            gh.parents[n] = n - 1
    runs = itertools.count(1)

    def run_comment(number, **fields):
        url = f"{RUNS}/{next(runs)}"
        return gh.comment(number, render_run({"url": url, **fields}, "Done."))

    run_comment(5, command="plan", status="success", outcome="planned")
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    run_comment(5, command="go", status="success", outcome="pr_opened")
    gh.add_pull(6, 5, head_sha=HEAD)
    timed_out = dict(
        round=2, trigger="fix", status="failure", outcome="agent_error", timed_out=True
    )
    run_comment(6, **timed_out)
    record = {"split_asked": True, "command": "plan", "basis": [0, 0], **FLAGS[sc["flags"]]}
    gh.comment(5, "NexKit autopilot record\n\n" + autopilot.record_marker(record))
    if sc["variant"] == "failed":
        run_comment(5, command="plan", status="failure", outcome="agent_error")
        return gh
    run_comment(5, command="plan", status="success", outcome="planned")
    if sc["variant"] == "no_record":
        gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it again.")
        return gh
    title = gh.issues[5]["title"]
    own = [title, f"{title} again"]
    split = [
        {"title": own[i % 2] if sc["titles"] else f"Part {i + 1}", "body": "Do it."}
        for i in range(sc["parts"])
    ]
    output = {
        "questions": QSETS[sc["qset"]],
        "too_large": sc["too_large"],
        "split": split,
        "protected_paths": [".github/workflows/x.yml"] if sc["protected"] else [],
        "no_change": "It already works." if sc["no_change"] else "",
    }
    gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it again.\n\n{plan_data_record(output)}")
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(autopilot={"enabled": True, "answer": sc["answer"]}, notify=["olivia"])
    res = {}
    with quiet():
        after(gh, cfg, res)
    return res

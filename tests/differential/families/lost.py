"""#399: a command that autopilot started and that was lost, played over three sweeps: a
dispatched command whose run never ran (its run ended, or cannot be read), an issue's run or a
pull request's round whose comment still says running or says `lost`, a round that started the
next one by itself, and a continued run whose new run did not start × the command × an
automatic run or a person's × autopilot's record (the same command or another, its tries, one
more start only, its age) × whether the agent ran. Each sweep that starts a command ends its
run, so the next sweep finds it lost again. Records the preview, the sweep and GitHub after
each, and `nexkit status`."""

from datetime import UTC, datetime, timedelta
from unittest import mock

from common import after, quiet, seed

from nexkit import autopilot
from nexkit.state import PLAN_MARKER, render_run
from tests.support import FakeGitHub, make_config

HEAD = "c" * 40
RUNS = "https://github.com/acme/app/actions/runs"
# The run that a dispatch names, and the one a record names that GitHub never had.
DISPATCHED, UNKNOWN = 900, 901
ZERO = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
# The sweeps: a minute, half an hour and three hours after the first.
SWEEPS = [timedelta(minutes=1), timedelta(minutes=31), timedelta(hours=3)]
# How long before the first sweep the last start, or the comment that waits, was.
AGES = {"10m": timedelta(minutes=10), "2h": timedelta(hours=2), "7h": timedelta(hours=7)}
TRIES = [0, 1, 2, 3, 4]
AGENTS = ["ran", "not", "unknown"]


class Clock(datetime):
    """The clock a sweep reads, which each sweep sets."""

    now_at = ZERO

    @classmethod
    def now(cls, tz=None):
        return cls.now_at


def iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def scenarios():
    for age in AGES:
        for command in ("plan", "go", "fix", "review"):
            for tries in TRIES:
                for once in (False, True):
                    for run in ("ended", "unknown"):
                        yield dict(
                            where="dispatched",
                            command=command,
                            tries=tries,
                            once=once,
                            run=run,
                            age=age,
                        )
    for where in ("issue_stale", "issue_lost", "pull_stale", "pull_lost"):
        commands = ("plan", "go") if where.startswith("issue") else ("fix", "review")
        for command in commands:
            yield dict(where=where, command=command, auto=False, age="10m")
            for age in AGES:
                for record in ("same", "other"):
                    for tries in TRIES:
                        for once in (False, True):
                            for agent in AGENTS:
                                yield dict(
                                    where=where,
                                    command=command,
                                    auto=True,
                                    record=record,
                                    tries=tries,
                                    once=once,
                                    agent=agent,
                                    age=age,
                                )
    for where, nexts in (
        ("pull_chained", ("fix", "review", None)),
        ("issue_continued", ("go", "plan", "close", None)),
    ):
        for found in nexts:
            for outcome in ("auto_fix", "retry") if where == "pull_chained" else (None,):
                for age in AGES:
                    yield dict(where=where, next=found, outcome=outcome, age=age)


def build(sc):
    gh = FakeGitHub()
    gh.add_milestone(1)
    gh.add_issue(5, title="Cache", milestone=1)
    gh.dispatch_runs["nexkit.yml"] = f"{RUNS}/{DISPATCHED}"
    where, command = sc["where"], sc.get("command")
    pull = where.startswith("pull") or command in ("fix", "review")
    count = iter(range(1, 99))

    def run_comment(number, **fields):
        url = f"{RUNS}/{next(count)}"
        return gh.comment(number, render_run({"url": url, **fields}, "Done.")), url

    if command != "plan" or where == "dispatched":
        run_comment(5, command="plan", status="success", outcome="planned")
        gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
    if pull:
        run_comment(5, command="go", status="success", outcome="pr_opened")
        gh.add_pull(6, 5, head_sha=HEAD)
        gh.comment(6, "NexKit pull request.")
        run_comment(
            6, round=1, trigger="implement", status="failure", outcome="auto_fix", head=HEAD
        )
    target = 6 if pull else 5
    old = ZERO - AGES[sc["age"]]
    record = None
    if where == "dispatched":
        url = f"{RUNS}/{DISPATCHED if sc['run'] == 'ended' else UNKNOWN}"
        record = {"command": command, "target": target, "url": url}
        if sc["run"] == "ended":
            gh.runs[DISPATCHED] = {"status": "completed", "conclusion": "failure"}
    elif where in ("issue_stale", "issue_lost", "pull_stale", "pull_lost"):
        fields = {"status": "running"} if where.endswith("stale") else {"status": "failure"}
        if where.endswith("lost"):
            fields["outcome"] = "lost"
        if where.startswith("issue"):
            comment, url = run_comment(5, command=command, autopilot=sc["auto"], **fields)
        else:
            kinds = ["auto"] if sc["auto"] else []
            trigger = "fix (auto)" if sc["auto"] and command == "fix" else command
            comment, url = run_comment(
                6, round=2, trigger=trigger, action=command, kinds=kinds, head=HEAD, **fields
            )
        number = int(url.rsplit("/", 1)[1])
        gh.runs[number] = {"status": "completed", "conclusion": "failure"}
        if sc["auto"] and sc["agent"] != "unknown":
            names = {"ran": "run / agent", "not": "run / plan"}
            gh.jobs[number] = [{"name": names[sc["agent"]], "conclusion": "success"}]
        elif sc["auto"]:
            del gh.runs[number]
        if sc["auto"]:
            other = "go" if command == "plan" else "review" if command == "fix" else "fix"
            same = sc["record"] == "same"
            record = {"command": command if same else other, "target": target}
    else:
        found = {"command": sc["next"], "number": "6" if pull else "5", "auto": "true"}
        found = found if sc["next"] else None
        extra = {"next": found} if found else {}
        if where == "pull_chained":
            comment, _ = run_comment(
                6, round=2, trigger="fix (auto)", action="fix", kinds=["auto"], head=HEAD,
                status="success", outcome=sc["outcome"], **extra,
            )  # fmt: skip
        else:
            comment, _ = run_comment(5, command="go", status="continued", outcome="retry", **extra)
        comment["updated_at"] = iso(old)
    if record:
        current = autopilot.basis(gh.comments(5), gh.comments(6) if pull else ())
        record = {**record, "basis": current, "note": "", "at": iso(old)}
        if sc.get("tries"):
            record["tries"] = sc["tries"]
        if sc.get("once"):
            record["once"] = True
        if sc["where"] == "dispatched" and sc["command"] == "review":
            record["auto"] = "true"
        gh.comment(5, "NexKit autopilot record\n\n" + autopilot.record_marker(record))
    return gh


def run(sc):
    gh = build(sc)
    cfg = make_config(autopilot={"enabled": True}, notify=["olivia"])
    seed(gh, cfg)
    res = {}
    with quiet(), mock.patch.object(autopilot, "datetime", Clock):
        for n, later in enumerate(SWEEPS, 1):
            Clock.now_at = ZERO + later
            after(gh, cfg, res, tag=str(n), status=n == len(SWEEPS))
            # A command that the sweep started ends without a run comment: lost again.
            gh.runs[DISPATCHED] = {"status": "completed", "conclusion": "failure"}
    return res

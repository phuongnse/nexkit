"""Run one agent session for a pipeline stage and collect its result."""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from importlib import resources
from pathlib import Path
from string import Template

from . import config as configuration
from .checks import BASE_SHA, RESULTS_LIMIT, environment, known_base
from .gitutil import GitError, git, is_ancestor, merge_tree, names
from .redact import Redactor
from .runlog import RunLog, add_tokens, summary, write_summary
from .runtimes import PROVIDERS, RUNTIMES
from .runtimes.base import AgentConfigError, Spec
from .schema import problems
from .usage import schedule

MAX_DIFF = 80_000
NO_EARLIER_FIXES = "None. This is the first fix round of this pull request."
# A conflict marker at the start of a line: `<<<<<<< ours`, `>>>>>>> theirs`, `||||||| base`.
CONFLICT_MARKER = re.compile(rb"^(<{7}|>{7}|\|{7})( |$)", re.M)
BOT = ("-c", "user.name=nexkit", "-c", "user.email=nexkit@localhost")

_STRING_LIST = {"type": "array", "items": {"type": "string"}}
# A question for a person, with the answers the agent sees and the number of the one it
# recommends (from 1), or 0 when none is clearly better. A number cannot be copied loosely,
# as an option's text can. With `autopilot.answer: "recommended"`, NexKit answers it.
_QUESTION = {
    "question": {"type": "string"},
    "options": _STRING_LIST,
    "recommended": {"type": "integer"},
}
_QUESTIONS = {
    "type": "array",
    "items": {"type": "object", "properties": _QUESTION, "required": list(_QUESTION)},
}
_CHANGE_RESULT = {
    "type": "object",
    "properties": {
        "status": {"enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "blocker": {"type": "string"},
        # The decisions a blocked round needs from a person.
        "questions": _QUESTIONS,
        "checks_run": _STRING_LIST,
    },
    "required": ["status", "summary", "blocker", "questions", "checks_run"],
}
# The pull request description is built from these, in this order, after `summary`.
_PULL_FIELDS = {
    "changes": _STRING_LIST,
    "testing": {"type": "string"},
    "outside_plan": _STRING_LIST,
    "reviewer_notes": _STRING_LIST,
}
SCHEMAS = {
    "plan": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "changes": _STRING_LIST,
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"decision": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["decision", "reason"],
                },
            },
            "risks": _STRING_LIST,
            "acceptance_criteria": _STRING_LIST,
            "implementation_notes": _STRING_LIST,
            "questions": _QUESTIONS,
            "too_large": {"type": "boolean"},
            "split": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        # The 1-based numbers of the earlier parts this part needs first.
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["title", "body", "depends_on"],
                },
            },
            # How a re-plan relates to the latest plan; shown only on a re-plan.
            "revision": {
                "type": "object",
                "properties": {"started_over": {"type": "boolean"}, "changes": _STRING_LIST},
                "required": ["started_over", "changes"],
            },
        },
        "required": [
            "summary",
            "changes",
            "decisions",
            "risks",
            "acceptance_criteria",
            "implementation_notes",
            "questions",
            "too_large",
            "split",
            "revision",
        ],
    },
    "implement": {
        **_CHANGE_RESULT,
        "properties": {**_CHANGE_RESULT["properties"], **_PULL_FIELDS},
        "required": [*_CHANGE_RESULT["required"], *_PULL_FIELDS],
    },
    "fix": {
        **_CHANGE_RESULT,
        "properties": {
            **_CHANGE_RESULT["properties"],
            # The cause of the failures and the evidence that shows it, or "". A diagnosis
            # round must name it before it changes code.
            "cause": {"type": "string"},
            # Conflicts with the base branch that need a person's decision.
            "open_conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "base_change": {"type": "string"},
                        "pr_change": {"type": "string"},
                        **_QUESTION,
                    },
                    "required": ["file", "base_change", "pr_change", *_QUESTION],
                },
            },
        },
        "required": [*_CHANGE_RESULT["required"], "cause", "open_conflicts"],
    },
    # NexKit adds the configured profile names as an enum when it runs triage.
    "triage": {
        "type": "object",
        "properties": {"profile": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["profile", "reason"],
    },
    "review": {
        "type": "object",
        "properties": {
            "verdict": {"enum": ["approve", "request_changes"]},
            "summary": {"type": "string"},
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "met": {"type": "boolean"},
                        "test": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["criterion", "met", "test", "evidence"],
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"enum": ["blocking", "suggestion"]},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "body": {"type": "string"},
                    },
                    "required": ["severity", "file", "line", "body"],
                },
            },
            "previous_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding": {"type": "string"},
                        "severity": {"enum": ["blocking", "suggestion"]},
                        "resolution": {"enum": ["resolved", "unresolved", "rejection_accepted"]},
                        "evidence": {"type": "string"},
                    },
                    "required": ["finding", "severity", "resolution", "evidence"],
                },
            },
        },
        "required": ["verdict", "summary", "criteria", "findings", "previous_findings"],
    },
}


def _check_lines(checks, indent="  "):
    return [f"{indent}- `{c['name']}`: `{c['run']}`" for c in checks]


def _checks_text(cfg):
    if not cfg["checks"]:
        return "  - No checks are configured. Run the project's usual tests if there are any."
    return "\n".join(_check_lines(cfg["checks"]))


def _round_checks_text(cfg):
    """The checks of the implement and fix prompts. The agent runs the fast ones in full; of
    a slow check, only the tests that cover its change, since NexKit runs every check after
    the round. With no check marked slow, the list is the same as `_checks_text`."""
    slow = [c for c in cfg["checks"] if c["slow"]]
    if not slow:
        return _checks_text(cfg)
    lines = _check_lines([c for c in cfg["checks"] if not c["slow"]])
    lines.append(
        "  - NexKit runs these slow checks after your round. Do not run them in full. Run\n"
        "    only the tests in them that cover your change or that failed, as narrowly as\n"
        "    the repository's instructions allow (for example one test file):"
    )
    return "\n".join(lines + _check_lines(slow, indent="    "))


def check_results_text(results):
    if not results:
        return "No checks are configured."
    lines = []
    for item in results:
        state = "passed" if item["passed"] else f"FAILED (exit {item['exit_code']})"
        lines.append(f"- `{item['name']}` (`{item['run']}`): {state}")
        if not item["passed"]:
            lines.append(f"  End of output:\n```\n{item['output'][-3000:]}\n```")
    return "\n".join(lines)


def _clip_diff(diff, limit=MAX_DIFF):
    if len(diff) > limit:
        diff = diff[:limit] + "\n[diff truncated; read the changed files directly]"
    return diff


def _files(files):
    return "\n".join(f"- `{name}`" for name in files)


def _since(repo, commit, base):
    """What the review diffs against for "changes since the previous review": the reviewed
    commit, or, when base commits were merged in since, the automatic merge of the reviewed
    commit with them, so that changes from the base branch stay out."""
    try:
        merged = git(repo, "merge-base", "HEAD", f"origin/{base}").strip()
        if is_ancestor(repo, merged, commit):
            return commit
        return merge_tree(repo, commit, merged)[0]
    except GitError:
        return commit


def review_diff(repo, base):
    return _clip_diff(git(repo, "diff", f"origin/{base}...HEAD")) or "(no changes)"


def previous_round(repo, context, base):
    """The review prompt's section about the previous review round, or "" for the first."""
    commit = context.get("previous_head") or ""
    if not commit:
        return ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        changes = "(the previously reviewed commit is unknown; use the full diff)"
    else:
        try:
            changes = _clip_diff(git(repo, "diff", _since(repo, commit, base), "HEAD"))
            changes = changes or "(no changes since the previous review)"
        except GitError:
            changes = f"(commit {commit[:7]} is not in this checkout; use the full diff)"
    return Template(_template("review_previous")).safe_substitute(
        previous_head=commit[:7],
        previous_round=context["previous_round"],
        changes=changes,
        base=base,
    )


def previous_plan(context):
    """The plan prompt's section about the latest plan, or "" for a first plan."""
    if "since_plan" not in context:
        return ""
    return Template(_template("plan_previous")).safe_substitute(
        plan=context["plan"], since_plan=context["since_plan"]
    )


def merge_review(repo, context, base):
    """The review prompt's section about a merge of the base branch in this run's fix round,
    with what each side changed in the conflicted files, or ""."""
    merged = context.get("merged") or {}
    head, commit, files = merged.get("head"), merged.get("base"), merged.get("conflicts")
    if not files or not all(re.fullmatch(r"[0-9a-f]{40}", c or "") for c in (head, commit)):
        return ""
    try:
        fork = git(repo, "merge-base", head, commit).strip()
        sides = [
            _clip_diff(git(repo, "diff", fork, side, "--", *files), MAX_DIFF // 4) or "(none)"
            for side in (commit, head)
        ]
    except GitError:
        sides = ["(not available in this checkout; read the files directly)"] * 2
    return Template(_template("review_merge")).safe_substitute(
        base=base,
        base_commit=commit[:7],
        files=_files(files),
        base_side=sides[0],
        pr_side=sides[1],
    )


def conflicts_with_base(repo, base):
    """Whether HEAD conflicts with `origin/<base>`, decided by git without changing anything."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    return bool(merge_tree(repo, "HEAD", commit)[1])


def up_to_date(base):
    """The result of an automatic conflict round whose branch no longer conflicts: nothing
    to do, so the agent does not run."""
    return {
        "stage": "fix",
        "status": "up_to_date",
        "summary": "",
        "error": f"The pull request no longer conflicts with `{base}`.",
    }


def merge_base_branch(repo, base, moved=False):
    """When the branch conflicts with its base branch, or with `moved` when the base branch
    has commits the branch lacks, start merging the base branch and leave any conflicts in
    the working tree. Return (merged commit, conflicted files), or (None, []) when nothing
    was changed."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    if not merge_tree(repo, "HEAD", commit)[1] and not (
        moved and not is_ancestor(repo, commit, "HEAD")
    ):
        return None, []
    git(repo, *BOT, "merge", "--no-commit", "--no-ff", "-q", commit, ok=(0, 1))
    conflicts = names(git(repo, "diff", "--name-only", "--diff-filter=U", "-z"))
    return commit, list(dict.fromkeys(conflicts))


def conflicts_text(base, commit, files):
    """The fix prompt's section about a merge of the base branch, or ""."""
    if not commit:
        return ""
    text = Template(_template("fix_conflicts" if files else "fix_merged")).safe_substitute(
        base=base, base_commit=commit[:7], files=_files(files)
    )
    return text + "\n"


def evidence_text(found):
    """The fix prompt's section about the failing checks' result files, or "" without any.
    `found` comes from `context.evidence`."""
    if not found:
        return ""
    lines = []
    for check in found["checks"]:
        name, run, folder = check["name"], check["run"], check["folder"]
        lines.append(f"- Failing check `{name}` (`{run}`): `{folder}/`")
        for item in check["paths"]:
            if item["folder"]:
                files = "1 file" if item["files"] == 1 else f"{item['files']} files"
                lines.append(f"  - `{item['path']}/`: a folder with {files}")
            else:
                lines.append(f"  - `{item['path']}`: a file")
        if check.get("left_out"):
            lines.append(
                f"  - {check['left_out']} more files were left out: a round keeps at most "
                f"{RESULTS_LIMIT // 1_000_000} MB of result files."
            )
    text = Template(_template("fix_evidence")).safe_substitute(
        folder=found["folder"], checks="\n".join(lines)
    )
    return text + "\n"


def diagnosis_text(repeated):
    """The fix prompt's section for a diagnosis round, or "" for any other round.
    `repeated` comes from `context.diagnosis`."""
    if not repeated:
        return ""
    items = "\n".join(f"- `{item['name']}`: `{item['signature']}`" for item in repeated)
    return Template(_template("fix_diagnosis")).safe_substitute(repeated=items)


def _cause(result):
    return str((result.get("output") or {}).get("cause") or "").strip()


def no_cause(result, repeated):
    """A diagnosis round that found no cause and changed nothing ends as `blocked`, with
    what the evidence shows as the question for a person."""
    output = result["output"]
    names = ", ".join(f"`{item['name']}`" for item in repeated)
    shown = str(output.get("blocker") or output.get("summary") or "").strip()
    blocker = f"The diagnosis round found no cause for the repeated failure of {names}."
    if shown:
        blocker += f" What the evidence shows: {shown}"
    questions = output.get("questions") or [
        {
            "question": f"{names} failed the same way after two fix rounds, and the agent "
            "found no cause in the evidence. How should NexKit go on?",
            "options": [
                "Add logging or a smaller test that shows the failure, then comment `/nexkit fix`",
                "Name the cause or a direction in a note: `/nexkit fix <note>`",
                "Fix it yourself and push a commit",
            ],
            "recommended": 0,
        }
    ]
    output.update(status="blocked", blocker=blocker, questions=questions)
    result.update(status="blocked", error=blocker)


def own_changes(repo, files, merged, conflicts):
    """The files the agent changed itself: without what a merge of the base branch brought,
    and without the conflicted files it had to resolve. Call after `collect_changes`."""
    if not merged:
        return files
    tree = merge_tree(repo, "HEAD", merged)[0]
    edits = names(git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", tree))
    return [name for name in edits if name not in conflicts]


def leftover_markers(repo, files):
    """The conflicted files that still contain conflict markers."""
    found = []
    for name in files:
        path = Path(repo) / name
        if path.is_file() and CONFLICT_MARKER.search(path.read_bytes()):
            found.append(name)
    return found


def _template(name):
    return resources.files("nexkit").joinpath(f"prompts/{name}.md").read_text()


def build_prompt(stage, context, cfg, extra=None):
    template = _template(stage)
    values = {
        **context,
        "checks": _checks_text(cfg),
        "round_checks": _round_checks_text(cfg),
        "writing": _template("writing"),
        "protected": ", ".join(f"`{p}`" for p in cfg["protected_paths"]),
        "implement_minutes": cfg["stages"]["implement"]["timeout_minutes"],
        **(extra or {}),
    }
    return Template(template).safe_substitute({k: str(v) for k, v in values.items()})


def stage_schema(stage, cfg):
    """The schema of a stage's result. Triage may name only the configured profiles."""
    schema = SCHEMAS[stage]
    if stage == "triage":
        schema = copy.deepcopy(schema)
        schema["properties"]["profile"]["enum"] = list(cfg["profiles"])
    return schema


def stage_spec(stage, cfg, home):
    """What the runtime needs to run `stage`, with `home` for the CLI's own files."""
    settings = configuration.stage(cfg, stage)
    return Spec(
        stage=stage,
        model=settings["model"],
        effort=settings["effort"],
        budget_usd=settings["max_budget_usd"],
        schema=stage_schema(stage, cfg),
        home=Path(home),
    )


def run_process(prompt, argv, *, env, cwd, timeout_seconds, log):
    """Start the CLI with `prompt` on stdin and pass each output line to `log`, which
    shows it and hands it to the runtime's reader."""
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(5)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout_seconds, kill)
    timer.start()
    stderr_chunks = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()))
    reader.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            log.line(line)
        proc.wait()
    finally:
        timer.cancel()
        reader.join()
        log.close()
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out.is_set(),
        "stderr": "".join(stderr_chunks)[-4000:],
        "seconds": round(time.monotonic() - started, 1),
    }


def run_agent(
    stage, prompt, cfg, *, cwd, redact, out, title, commits=None, program=None, restore=None
):
    """Run the agent of `stage` on `prompt`. Return the interpreted result and the tool
    calls for the summary. `program` replaces the runtime's executable, for tests.

    When the agent stops at a usage limit, the stage runs again from the start on each of
    its fallbacks in turn, after `restore` returned the checkout to where the agent started.
    Nothing else moves to a fallback. The stage pauses when every agent stopped at its
    limit, until the earliest reset. The result names the agent that ran last and lists
    every agent's run in `attempts`.

    `commits` gives NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA, so checks that the agent runs
    see the same commits as in the verify job."""
    settings = configuration.stage(cfg, stage)
    names = configuration.chain(cfg, settings["agent"])
    deadline = time.monotonic() + configuration.stage_minutes(cfg, stage) * 60
    attempts, calls, limited = [], [], []
    for index, name in enumerate(names):
        seconds = min(settings["timeout_minutes"] * 60, deadline - time.monotonic())
        if index and seconds < 60:
            skipped = {"skipped": True, "error": "No time was left in the stage."}
            attempts.append(_attempt_record(cfg, name, settings, skipped))
            break
        if index:
            if restore:
                restore()
            print(
                f"! The `{limited[-1][0]}` agent stopped at its usage limit; running the "
                f"{stage} stage on `{name}`.",
                flush=True,
            )
        attempt, attempt_calls = _run_one(
            stage,
            prompt,
            cfg,
            name,
            fallback=bool(index),
            seconds=seconds,
            cwd=cwd,
            redact=redact,
            out=Path(out) / f"agent-{name}" if index else Path(out),
            title=title,
            commits=commits,
            program=program,
        )
        attempts.append(_attempt_record(cfg, name, settings, attempt))
        if attempt.get("skipped"):
            continue
        result, calls = attempt, attempt_calls
        if attempt["status"] != "paused":
            break
        limited.append((name, attempt))
    # The agent's own run is never skipped, so some run ended the stage.
    if result["status"] == "paused" and len(limited) > 1:
        _all_limited(result, limited)
    _totals(result, attempts)
    ran = next(a["agent"] for a in reversed(attempts) if a["status"] != "skipped")
    result.update(agent=ran, attempts=attempts)
    return result, calls


def _attempt_record(cfg, name, settings, result):
    """One agent's run of a stage, for the run summary and the run comment."""
    chosen = configuration.agent(cfg, name)
    record = {
        "agent": name,
        "runtime": chosen["runtime"],
        "provider": chosen["provider"],
        "model": settings["model"],
        "status": "skipped" if result.get("skipped") else result["status"],
    }
    for key in ("seconds", "tokens", "limit"):
        if result.get(key) is not None:
            record[key] = result[key]
    if record["status"] in ("skipped", "error") and result.get("error"):
        record["error"] = result["error"]
    return record


def _all_limited(result, limited):
    """Pause until the earliest reset of the agents that stopped at their limits."""
    # `resume_at` is `YYYY-MM-DDTHH:MM:SSZ`, so text order is time order.
    first = min((attempt for _, attempt in limited), key=lambda a: a["resume_at"])
    text = "; ".join(f"`{name}`: {attempt['limit']}" for name, attempt in limited)
    result.update(
        error=f"Every agent stopped at its usage limit: {text}",
        limit=text,
        resume_at=first["resume_at"],
        reset_known=first["reset_known"],
    )


def _totals(result, attempts):
    """The stage's tokens and duration add up every agent's run."""
    ran = [a for a in attempts if a["status"] != "skipped"]
    if len(ran) < 2:
        return
    seconds = [a["seconds"] for a in ran if isinstance(a.get("seconds"), (int, float))]
    result["seconds"] = round(sum(seconds), 4) if seconds else None
    result["tokens"] = add_tokens(*(a.get("tokens") for a in ran))


def _run_one(
    stage, prompt, cfg, name, *, fallback, seconds, cwd, redact, out, title, commits, program
):
    """Run the agent `name` once. A fallback without its secret is skipped: the result has
    `skipped` and says why."""
    agent = configuration.agent(cfg, name)
    runtime = RUNTIMES[agent["runtime"]]
    provider = PROVIDERS[agent["provider"]]
    groups = provider.secrets(agent)
    # Unset secrets arrive as empty strings. The CLI gets only its agent's secrets.
    secrets = {key: os.environ[key] for group in groups for key in group if os.environ.get(key)}
    missing = [group for group in groups if not any(key in secrets for key in group)]
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    error = None
    # A fallback never runs on a person's own login: that is the login the agent used.
    if missing and (on_actions or fallback or not provider.local_login):
        error = (
            f"No secret for the `{name}` agent: set the {' or '.join(missing[0])} repository "
            "secret and pass it in the `secrets:` of the NexKit workflow."
        )
    else:
        try:
            provider.check_secrets(agent, secrets)
        except AgentConfigError as exc:
            error = f"{exc} (the `{name}` agent)."
    if error:
        result = {"stage": stage, "status": "error", "error": error}
        if fallback:
            result["skipped"] = True
        return result, []
    # Whatever names the configuration gave them.
    redact.add(secrets.values())
    out.mkdir(parents=True, exist_ok=True)
    log = RunLog(
        redact,
        reader=runtime.reader(),
        title=f"{title} ({name})" if fallback else title,
        speaker=runtime.label,
        tool_output=cfg["log"]["tool_output"],
        transcript_dir=out if cfg["transcript"] else None,
    )
    # Without any agent credential: the CLI gets only its own, through `launch.env`.
    env = environment(commits)
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as home:
        spec = stage_spec(stage, cfg, home)
        launch = runtime.launch(agent, spec, secrets, program)
        for path, text in launch.files.items():
            Path(path).write_text(text, encoding="utf-8")
        run = run_process(
            prompt,
            launch.argv,
            env={**env, **launch.env},
            cwd=cwd,
            timeout_seconds=seconds,
            log=log,
        )
    outcome = log.reader.outcome(run["returncode"], run["stderr"])
    result = interpret(
        stage,
        run,
        outcome,
        max(1, round(seconds / 60)),
        cfg["usage_limit"]["retry_minutes"],
        label=runtime.label,
        # NexKit's own schema: triage names an unknown profile in its error, not here.
        schema=SCHEMAS[stage],
    )
    return result, log.calls


def paused(result, limit, label="The agent"):
    """Mark a stage result as paused at the usage limit."""
    result.update(
        status="paused",
        error=f"{label} hit the account's usage limit: {limit['message']}",
        limit=limit["message"],
        resume_at=limit["resume_at"],
        reset_known=limit["reset_known"],
    )
    return result


def interpret(
    stage, run, outcome, timeout_minutes, retry_minutes=60, *, label="The agent", schema=None
):
    """Map a run and the runtime's outcome to a stage result: done, blocked, paused or
    error. With `schema`, the structured result must fit it."""
    result = {
        "stage": stage,
        "seconds": run["seconds"],
        "output": None,
        "status": "error",
        "summary": "",
        "error": "",
    }
    if outcome.tokens:
        result["tokens"] = outcome.tokens
    found = problems(outcome.output, schema) if schema and outcome.output is not None else []
    if run["timed_out"]:
        result["error"] = f"{label} did not finish within {timeout_minutes} minutes."
        result["timed_out"] = True
    elif outcome.error:
        result["error"] = f"{label} {outcome.error}"[:2000].strip()
    elif not isinstance(outcome.output, dict):
        result["error"] = f"{label} finished without the required structured result."
    elif found:
        result["error"] = (
            f"{label} returned a result that does not fit the {stage} schema: "
            + "; ".join(found[:5])
        )
    else:
        output = outcome.output
        result["output"] = output
        result["summary"] = output.get("summary", "")
        if stage in ("implement", "fix"):
            result["status"] = output.get("status")
            if result["status"] == "blocked":
                result["error"] = output.get("blocker") or "The agent reported it was blocked."
        else:
            result["status"] = "done"
    if result["status"] == "error" and not run["timed_out"] and outcome.limit:
        paused(result, schedule(outcome.limit, retry_minutes=retry_minutes), label)
    return result


# Caches and build output that tests leave behind. Ignored only in the agent's checkout,
# through .git/info/exclude; the repository's own .gitignore is unchanged.
GENERATED = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".venv/",
    ".coverage",
    "node_modules/",
    ".DS_Store",
)


def baseline(repo):
    """Commit setup side effects so the patch contains only the agent's changes."""
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = Path(repo) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n# NexKit: generated files\n" + "\n".join(GENERATED) + "\n")
    start = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "add", "-A")
    git(
        repo,
        *BOT,
        "commit",
        "-q",
        "--allow-empty",
        "--no-verify",
        "-m",
        "nexkit: setup baseline",
    )
    return start, git(repo, "rev-parse", "HEAD").strip()


def collect_changes(repo, base_commit, out_dir):
    git(repo, "add", "-A")
    patch = git(repo, "diff", "--cached", "--binary", base_commit, binary=True)
    names = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", base_commit)
    files = [name for name in names.split("\0") if name]
    (Path(out_dir) / "changes.patch").write_bytes(patch)
    return files, len(patch)


def run_triage(context, cfg, previous, out_dir, *, program=None):
    """Choose the profile for a plan with one agent call that cannot see the repository.

    Triage never stops the plan: after an error or an unknown name, the plan uses the
    `previous` profile, or `default_profile` when there is none."""
    out = Path(out_dir) / "triage"
    out.mkdir(parents=True, exist_ok=True)
    profiles = cfg["profiles"]
    extra = {
        "requests": context.get("requests") or context["discussion"],
        "profiles": "\n".join(f"- `{name}`: {p['when'].strip()}" for name, p in profiles.items()),
        "previous": f"The latest plan of this issue used the profile `{previous}`."
        if previous
        else "None. This is the first plan of this issue with a profile.",
    }
    prompt = build_prompt("triage", context, cfg, extra)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    redact = Redactor()
    # An empty directory, so the repository's agent settings and instructions do not load.
    with tempfile.TemporaryDirectory() as empty:
        result, calls = run_agent(
            "triage",
            prompt,
            cfg,
            cwd=empty,
            redact=redact,
            out=out,
            title="NexKit triage",
            program=program,
        )
    output = result.pop("output", None) or {}
    result.pop("summary", None)
    name = output.get("profile")
    if result["status"] == "done" and name in profiles:
        result.update(profile=name, chosen_by="triage", reason=str(output.get("reason") or ""))
    else:
        if result["status"] == "done":
            result.update(status="error", error=f"Triage chose an unknown profile: {name!r}.")
        fallback = "previous" if previous in profiles else "default"
        result.update(
            profile=previous if fallback == "previous" else cfg["default_profile"],
            chosen_by=fallback,
            reason="",
        )
    result = redact.data(result)
    write_summary(summary(result, calls), redact)
    return result


def run_stage(
    stage,
    context,
    cfg,
    repo,
    out_dir,
    *,
    program=None,
    checks=None,
    base=None,
    commits=None,
    triage=None,
):
    """Run one stage. A plan's `triage` result is stored with it.

    `commits` gives NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA (see `checks.find_commits`)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    extra = {}
    start = base_commit = merged = None
    conflicts = []
    if stage in ("implement", "fix"):
        start, base_commit = baseline(repo)
    if stage == "fix":
        merged, conflicts = merge_base_branch(repo, base, context.get("merge_moved_base"))
        extra["conflicts"] = conflicts_text(base, merged, conflicts)
        extra["evidence"] = evidence_text(context.get("evidence"))
        extra["earlier_fixes"] = context.get("earlier_fixes") or NO_EARLIER_FIXES
        extra["diagnosis"] = diagnosis_text(context.get("diagnose"))
        if merged:
            # Once committed, the merge has the merged base commit as its merge base.
            commits = {**(commits or {}), BASE_SHA: known_base(repo, merged)}
    if stage == "plan":
        extra["previous"] = previous_plan(context)
    if stage == "review":
        extra = {
            "base": base,
            "diff": review_diff(repo, base),
            "check_results": check_results_text(checks),
            "previous": previous_round(repo, context, base),
            "merge": merge_review(repo, context, base),
        }
    prompt = build_prompt(stage, context, cfg, extra)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")

    redact = Redactor()
    protected = [name for name in conflicts if configuration.is_protected(name, cfg)]
    if protected:
        result = {
            "stage": stage,
            "status": "error",
            "error": f"The pull request conflicts with `{base}` in protected paths, which NexKit "
            f"does not change: {', '.join(f'`{name}`' for name in protected)}. Merge `{base}` "
            "into the branch yourself, then comment `/nexkit review`.",
        }
        return _save(result, out, redact, [], start=start, merged=merged, conflicts=conflicts)

    def restore():
        """The checkout as the agent found it, for a fallback agent: the setup baseline,
        and the merge of the base branch with its conflicts."""
        git(repo, "reset", "-q", "--hard", base_commit)
        git(repo, "clean", "-q", "-fd")
        if merged:
            git(repo, *BOT, "merge", "--no-commit", "--no-ff", "-q", merged, ok=(0, 1))

    result, calls = run_agent(
        stage,
        prompt,
        cfg,
        cwd=repo,
        redact=redact,
        out=out,
        title=f"NexKit {stage}",
        commits=commits,
        program=program,
        restore=restore if base_commit else None,
    )
    if stage in ("implement", "fix") and result["status"] == "done":
        left = leftover_markers(repo, conflicts)
        files, size = collect_changes(repo, base_commit, out)
        result["changed_files"] = files
        if left:
            result.update(
                status="error",
                error="The agent left conflict markers in "
                + ", ".join(f"`{name}`" for name in left)
                + ".",
            )
        elif size > cfg["limits"]["max_patch_mb"] * 1_000_000:
            result.update(status="error", error=f"The change is too large ({size} bytes).")
        elif stage == "fix" and context.get("diagnose") and not _cause(result):
            changed = own_changes(repo, files, merged, conflicts)
            if changed:
                result.update(
                    status="error",
                    error="The diagnosis round changed code without naming the cause of the "
                    "repeated failure, so NexKit publishes nothing. Changed: "
                    + ", ".join(f"`{name}`" for name in changed[:20])
                    + ".",
                )
            elif not merged:
                no_cause(result, context["diagnose"])
            # A merge of a moved base branch is published alone, as in any round after
            # failing checks: the base branch may have fixed the failure, and the next
            # checks show whether it did.
        elif not files and not merged:
            # After a merge an empty patch is valid: it keeps this branch's side everywhere,
            # or publishes a clean merge alone.
            result.update(status="error", error="The agent finished without changing any files.")
    if triage:
        result.update(profile=triage["profile"], triage=triage)
    return _save(result, out, redact, calls, start=start, merged=merged, conflicts=conflicts)


def _save(result, out, redact, calls, *, start, merged, conflicts):
    """Store the redacted result and write the run summary."""
    # `start_base` is the base commit merged into the branch; publish makes it a parent.
    result.update(start_head=start, start_base=merged, conflicts=conflicts)
    result = redact.data(result)
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_summary(summary(result, calls), redact)
    return result

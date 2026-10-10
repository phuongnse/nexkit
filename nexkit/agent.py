"""Run one Claude Code session for a pipeline stage and collect its result."""

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
from .checks import environment, known_base
from .gitutil import GitError, git, is_ancestor, merge_tree, names
from .redact import Redactor
from .runlog import RunLog, summary, write_summary
from .usage import usage_limit

READ_ONLY_TOOLS = "Read,Grep,Glob"
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
MAX_DIFF = 80_000
MAX_PATCH_BYTES = 5_000_000
# A conflict marker at the start of a line: `<<<<<<< ours`, `>>>>>>> theirs`, `||||||| base`.
CONFLICT_MARKER = re.compile(rb"^(<{7}|>{7}|\|{7})( |$)", re.M)
BOT = ("-c", "user.name=nexkit", "-c", "user.email=nexkit@localhost")

_STRING_LIST = {"type": "array", "items": {"type": "string"}}
_CHANGE_RESULT = {
    "type": "object",
    "properties": {
        "status": {"enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "blocker": {"type": "string"},
        "checks_run": _STRING_LIST,
    },
    "required": ["status", "summary", "blocker", "checks_run"],
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
            "questions": _STRING_LIST,
            "too_large": {"type": "boolean"},
            "split": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["title", "body"],
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
            # Conflicts with the base branch that need a person's decision.
            "open_conflicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "base_change": {"type": "string"},
                        "pr_change": {"type": "string"},
                        "question": {"type": "string"},
                    },
                    "required": ["file", "base_change", "pr_change", "question"],
                },
            },
        },
        "required": [*_CHANGE_RESULT["required"], "open_conflicts"],
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


def _checks_text(cfg):
    if not cfg["checks"]:
        return "  - No checks are configured. Run the project's usual tests if there are any."
    return "\n".join(f"  - `{c['name']}`: `{c['run']}`" for c in cfg["checks"])


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
    to do, so Claude does not run."""
    return {
        "stage": "fix",
        "status": "up_to_date",
        "cost": None,
        "summary": "",
        "error": f"The pull request no longer conflicts with `{base}`.",
    }


def merge_base_branch(repo, base):
    """When the branch conflicts with its base branch, start merging the base branch and
    leave the conflicts in the working tree. Return (merged commit, conflicted files), or
    (None, []) when the branch merges cleanly and nothing was changed."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    if not merge_tree(repo, "HEAD", commit)[1]:
        return None, []
    git(repo, *BOT, "merge", "--no-commit", "--no-ff", "-q", commit, ok=(0, 1))
    conflicts = names(git(repo, "diff", "--name-only", "--diff-filter=U", "-z"))
    return commit, list(dict.fromkeys(conflicts))


def conflicts_text(base, commit, files):
    """The fix prompt's section about a merge of the base branch, or ""."""
    if not commit:
        return ""
    text = Template(_template("fix_conflicts")).safe_substitute(
        base=base, base_commit=commit[:7], files=_files(files)
    )
    return text + "\n"


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
        "writing": _template("writing"),
        "protected": ", ".join(f"`{p}`" for p in cfg["protected_paths"]),
        "implement_minutes": cfg["stages"]["implement"]["timeout_minutes"],
        **(extra or {}),
    }
    return Template(template).safe_substitute({k: str(v) for k, v in values.items()})


def claude_command(stage, cfg, claude="claude"):
    settings = configuration.stage(cfg, stage)
    schema = SCHEMAS[stage]
    if stage == "triage":
        schema = copy.deepcopy(schema)
        schema["properties"]["profile"]["enum"] = list(cfg["profiles"])
    cmd = [
        claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--model",
        settings["model"],
        "--json-schema",
        json.dumps(schema),
    ]
    if settings["effort"]:
        cmd += ["--effort", settings["effort"]]
    if settings["max_budget_usd"]:
        cmd += ["--max-budget-usd", str(settings["max_budget_usd"])]
    if stage == "triage":
        cmd += ["--tools", ""]
    elif stage in ("plan", "review"):
        cmd += ["--tools", READ_ONLY_TOOLS]
    else:
        # The job runs on a disposable runner without any GitHub write token.
        cmd += ["--permission-mode", "bypassPermissions"]
    return cmd


def run_claude(prompt, cmd, *, cwd, timeout_seconds, log, base_sha=None):
    """Run Claude Code, show its events through `log` and return its final result event.

    `base_sha` becomes NEXKIT_BASE_SHA, so checks that Claude runs see the same base as in
    the verify job."""
    started = time.monotonic()
    # Unset secrets arrive as empty strings; drop them so Claude picks the one that is set.
    env = {k: v for k, v in environment(base_sha).items() if v or k not in CREDENTIALS}
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if on_actions and not any(env.get(name) for name in CREDENTIALS):
        log.close()
        return {
            "event": None,
            "returncode": None,
            "timed_out": False,
            "stderr": "No Claude credential: set the CLAUDE_CODE_OAUTH_TOKEN or "
            "ANTHROPIC_API_KEY repository secret.",
            "seconds": 0.0,
        }
    proc = subprocess.Popen(
        cmd,
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
    result = None
    last_text, resets_at = "", None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            event = log.line(line)
            if not event:
                continue
            if event.get("type") == "result":
                result = event
            last_text = _text_of(event) or last_text
            resets_at = _rejected_until(event) or resets_at
        proc.wait()
    finally:
        timer.cancel()
        reader.join()
        log.close()
    return {
        "event": result,
        "returncode": proc.returncode,
        "timed_out": timed_out.is_set(),
        "stderr": "".join(stderr_chunks)[-4000:],
        "seconds": round(time.monotonic() - started, 1),
        "last_text": last_text[-2000:],
        "resets_at": resets_at,
    }


def _text_of(event):
    """The text of a message that Claude Code wrote itself, such as an API or usage limit
    error, or "". The model's own words are left out: they may mention limits."""
    message = event.get("message") if event.get("type") == "assistant" else None
    if not isinstance(message, dict) or message.get("model") != "<synthetic>":
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(str(t) for t in texts if t)


def _rejected_until(event):
    """The reset time (Unix seconds) of a rate limit event that rejected the request."""
    info = event.get("rate_limit_info") if event.get("type") == "rate_limit_event" else None
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    value = info.get("resetsAt", info.get("resets_at"))
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def paused(result, limit):
    """Mark a stage result as paused at the usage limit."""
    result.update(
        status="paused",
        error=f"Claude hit the account's usage limit: {limit['message']}",
        limit=limit["message"],
        resume_at=limit["resume_at"],
        reset_known=limit["reset_known"],
    )
    return result


def interpret(stage, run, timeout_minutes):
    """Map a Claude run to a stage result: done, blocked or error."""
    event = run["event"] or {}
    result = {
        "stage": stage,
        "cost": event.get("total_cost_usd"),
        "turns": event.get("num_turns"),
        "seconds": run["seconds"],
        "output": None,
        "status": "error",
        "summary": "",
        "error": "",
    }
    if run["timed_out"]:
        result["error"] = f"Claude did not finish within {timeout_minutes} minutes."
    elif not event:
        result["error"] = (
            f"Claude exited with code {run['returncode']} without a result. "
            f"{run['stderr'].strip()[-1500:]}"
        ).strip()
    elif event.get("is_error") or event.get("subtype") != "success":
        detail = event.get("result") or ""
        result["error"] = f"Claude stopped ({event.get('subtype')}). {detail}"[:2000].strip()
    elif not isinstance(event.get("structured_output"), dict):
        result["error"] = "Claude finished without the required structured result."
    else:
        output = event["structured_output"]
        result["output"] = output
        result["summary"] = output.get("summary", "")
        if stage in ("implement", "fix"):
            result["status"] = output.get("status")
            if result["status"] == "blocked":
                result["error"] = output.get("blocker") or "The agent reported it was blocked."
        else:
            result["status"] = "done"
    if result["status"] == "error" and not run["timed_out"]:
        # Only what Claude Code says about a failed run, never the model's own text.
        said = event.get("result") if event.get("is_error") else None
        texts = (said, run["stderr"], run.get("last_text"))
        limit = usage_limit(texts, resets_at=run.get("resets_at"))
        if limit:
            paused(result, limit)
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


def run_triage(context, cfg, previous, out_dir, *, claude="claude"):
    """Choose the profile for a plan with one Claude call that has no tools.

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
    settings = configuration.stage(cfg, "triage")
    redact = Redactor()
    log = RunLog(
        redact,
        title="NexKit triage",
        tool_output=cfg["log"]["tool_output"],
        transcript_dir=out if cfg["transcript"] else None,
    )
    # An empty directory, so the repository's Claude settings and instructions do not load.
    with tempfile.TemporaryDirectory() as empty:
        run = run_claude(
            prompt,
            claude_command("triage", cfg, claude),
            cwd=empty,
            timeout_seconds=settings["timeout_minutes"] * 60,
            log=log,
        )
    result = interpret("triage", run, settings["timeout_minutes"])
    output = result.pop("output") or {}
    del result["summary"]
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
    write_summary(summary(result, log.calls), redact)
    return result


def run_stage(
    stage,
    context,
    cfg,
    repo,
    out_dir,
    *,
    claude="claude",
    checks=None,
    base=None,
    base_sha=None,
    triage=None,
):
    """Run one stage. A plan's `triage` result is stored with it, and its cost counts.

    `base_sha` is the merge base with the base branch, for NEXKIT_BASE_SHA."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    extra = {}
    start = base_commit = merged = None
    conflicts = []
    if stage in ("implement", "fix"):
        start, base_commit = baseline(repo)
    if stage == "fix":
        merged, conflicts = merge_base_branch(repo, base)
        extra["conflicts"] = conflicts_text(base, merged, conflicts)
        if merged:
            # Once committed, the merge has the merged base commit as its merge base.
            base_sha = known_base(repo, merged)
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
            "cost": None,
            "error": f"The pull request conflicts with `{base}` in protected paths, which NexKit "
            f"does not change: {', '.join(f'`{name}`' for name in protected)}. Merge `{base}` "
            "into the branch yourself, then comment `/nexkit review`.",
        }
        return _save(result, out, redact, [], start=start, merged=merged, conflicts=conflicts)

    settings = configuration.stage(cfg, stage)
    log = RunLog(
        redact,
        title=f"NexKit {stage}",
        tool_output=cfg["log"]["tool_output"],
        transcript_dir=out if cfg["transcript"] else None,
    )
    run = run_claude(
        prompt,
        claude_command(stage, cfg, claude),
        cwd=repo,
        timeout_seconds=settings["timeout_minutes"] * 60,
        log=log,
        base_sha=base_sha,
    )
    result = interpret(stage, run, settings["timeout_minutes"])
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
        elif size > MAX_PATCH_BYTES:
            result.update(status="error", error=f"The change is too large ({size} bytes).")
        elif not files and not merged:
            # After a merge an empty patch is valid: it keeps this branch's side everywhere.
            result.update(status="error", error="The agent finished without changing any files.")
    if triage:
        costs = [c for c in (result["cost"], triage.get("cost")) if isinstance(c, (int, float))]
        result.update(profile=triage["profile"], triage=triage, cost=sum(costs) if costs else None)
    return _save(result, out, redact, log.calls, start=start, merged=merged, conflicts=conflicts)


def _save(result, out, redact, calls, *, start, merged, conflicts):
    """Store the redacted result and write the run summary."""
    # `start_base` is the base commit merged into the branch; publish makes it a parent.
    result.update(start_head=start, start_base=merged, conflicts=conflicts)
    result = redact.data(result)
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_summary(summary(result, calls), redact)
    return result

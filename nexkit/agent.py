"""Run one Claude Code session for a pipeline stage and collect its result."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path
from string import Template

from . import config as configuration
from .gitutil import git

READ_ONLY_TOOLS = "Read,Grep,Glob"
CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
MAX_DIFF = 80_000
MAX_PATCH_BYTES = 5_000_000

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
SCHEMAS = {
    "plan": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "approach": {"type": "string"},
            "acceptance_criteria": _STRING_LIST,
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
        },
        "required": [
            "summary",
            "approach",
            "acceptance_criteria",
            "questions",
            "too_large",
            "split",
        ],
    },
    "implement": _CHANGE_RESULT,
    "fix": _CHANGE_RESULT,
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
                        "evidence": {"type": "string"},
                    },
                    "required": ["criterion", "met", "evidence"],
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
        },
        "required": ["verdict", "summary", "criteria", "findings"],
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


def review_diff(repo, base):
    diff = git(repo, "diff", f"origin/{base}...HEAD")
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n[diff truncated; read the changed files directly]"
    return diff or "(no changes)"


def build_prompt(stage, context, cfg, extra=None):
    template = resources.files("nexkit").joinpath(f"prompts/{stage}.md").read_text()
    values = {
        **context,
        "checks": _checks_text(cfg),
        "protected": ", ".join(f"`{p}`" for p in cfg["protected_paths"]),
        "implement_minutes": cfg["stages"]["implement"]["timeout_minutes"],
        **(extra or {}),
    }
    return Template(template).safe_substitute({k: str(v) for k, v in values.items()})


def claude_command(stage, cfg, claude="claude"):
    settings = configuration.stage(cfg, stage)
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
        json.dumps(SCHEMAS[stage]),
    ]
    if settings["effort"]:
        cmd += ["--effort", settings["effort"]]
    if settings["max_budget_usd"]:
        cmd += ["--max-budget-usd", str(settings["max_budget_usd"])]
    if stage in ("plan", "review"):
        cmd += ["--tools", READ_ONLY_TOOLS]
    else:
        # The job runs on a disposable runner without any GitHub write token.
        cmd += ["--permission-mode", "bypassPermissions"]
    return cmd


def _describe(event):
    """One readable log line per assistant action, for the Actions log."""
    lines = []
    if event.get("type") == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append("· " + block["text"].strip().replace("\n", " ")[:240])
            elif block.get("type") == "tool_use":
                data = block.get("input") or {}
                detail = (
                    data.get("command")
                    or data.get("file_path")
                    or data.get("pattern")
                    or data.get("description")
                    or ""
                )
                lines.append(f"▸ {block.get('name')} {str(detail).replace(chr(10), ' ')[:200]}")
    return lines


def run_claude(prompt, cmd, *, cwd, timeout_seconds, transcript):
    """Run Claude Code, stream progress to stdout and return its final result event."""
    started = time.monotonic()
    # Unset secrets arrive as empty strings; drop them so Claude picks the one that is set.
    env = {k: v for k, v in os.environ.items() if v or k not in CREDENTIALS}
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if on_actions and not any(env.get(name) for name in CREDENTIALS):
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
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        with open(transcript, "w", encoding="utf-8") as log:
            for line in proc.stdout:
                log.write(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    result = event
                for text in _describe(event):
                    print(text, flush=True)
        proc.wait()
    finally:
        timer.cancel()
        reader.join()
    return {
        "event": result,
        "returncode": proc.returncode,
        "timed_out": timed_out.is_set(),
        "stderr": "".join(stderr_chunks)[-4000:],
        "seconds": round(time.monotonic() - started, 1),
    }


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
        "-c",
        "user.name=nexkit",
        "-c",
        "user.email=nexkit@localhost",
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


def run_stage(stage, context, cfg, repo, out_dir, *, claude="claude", checks=None, base=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    extra = {}
    if stage == "review":
        extra = {"diff": review_diff(repo, base), "check_results": check_results_text(checks)}
    prompt = build_prompt(stage, context, cfg, extra)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")

    start = base_commit = None
    if stage in ("implement", "fix"):
        start, base_commit = baseline(repo)
    settings = configuration.stage(cfg, stage)
    run = run_claude(
        prompt,
        claude_command(stage, cfg, claude),
        cwd=repo,
        timeout_seconds=settings["timeout_minutes"] * 60,
        transcript=out / "transcript.jsonl",
    )
    result = interpret(stage, run, settings["timeout_minutes"])
    result["start_head"] = start
    if stage in ("implement", "fix") and result["status"] == "done":
        files, size = collect_changes(repo, base_commit, out)
        result["changed_files"] = files
        if size > MAX_PATCH_BYTES:
            result.update(status="error", error=f"The change is too large ({size} bytes).")
        elif not files:
            result.update(status="error", error="The agent finished without changing any files.")
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result

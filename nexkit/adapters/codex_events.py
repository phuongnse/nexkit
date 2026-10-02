"""Translate public Codex CLI events and observe the official API action's child.

The API shim delegates every sudo operation to the real system executable.
Only the pinned action's unprivileged Codex exec is observed, with its original
arguments, account, environment and prompt input. It never handles API keys.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nexkit.common import Blocked, read_regular_json
from nexkit.observability import RunLog, run_observed
from nexkit.policy import require


def event(value):
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind in {"thread.started", "turn.started"}:
        return {"kind": "session", "status": "running", "message": kind.replace(".", " ")}
    if kind == "turn.completed":
        return {"kind": "usage", "usage": value.get("usage", {}), "message": "Agent turn completed"}
    if kind in {"error", "turn.failed"}:
        error = value.get("error", value.get("message", "Agent turn failed"))
        if isinstance(error, dict):
            error = error.get("message", "Agent turn failed")
        return {"kind": "error", "message": str(error)}
    item = value.get("item")
    if kind not in {"item.started", "item.updated", "item.completed"} or not isinstance(item, dict):
        return None
    status = {"item.started": "started", "item.updated": "updated", "item.completed": "completed"}[
        kind
    ]
    if item.get("status") == "failed":
        status = "failed"
    if item.get("type") == "command_execution":
        return {
            "kind": "command",
            "status": status,
            "command": item.get("command", ""),
            "output": item.get("aggregated_output", ""),
            "exit_code": item.get("exit_code"),
        }
    if item.get("type") == "file_change":
        changes = item.get("changes", [])
        return {
            "kind": "files",
            "status": status,
            "files": [change.get("path") for change in changes if isinstance(change, dict)]
            if isinstance(changes, list)
            else [],
        }
    if item.get("type") == "agent_message":
        return {"kind": "message", "message": item.get("text", "")}
    if item.get("type") in {"plan", "plan_update"}:
        return {"kind": "plan", "message": item.get("text", "")}
    if item.get("type") in {"mcp_tool_call", "web_search"}:
        return {
            "kind": "tool",
            "status": status,
            "message": item.get("query") or f"{item.get('server', '')}.{item.get('tool', '')}",
        }
    # Reasoning, raw protocol payloads and unknown future fields remain private.
    return None


def prepare_api_observer(data):
    directory = Path(data).absolute() / "api-observer"
    directory.mkdir(mode=0o755)
    script = directory / "sudo"
    handler = Path(__file__).absolute()
    metadata = directory.parent / "observation.json"
    script.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + " -I "
        + shlex.quote(str(handler))
        + " --metadata "
        + shlex.quote(str(metadata))
        + ' -- "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o555)
    path = os.environ.get("GITHUB_PATH")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(str(directory) + "\n")
    return directory


def api_command(arguments):
    return (
        len(arguments) > 4
        and arguments[:3] == ["-u", "nexkit-agent", "--"]
        and Path(arguments[3]).is_absolute()
        and Path(arguments[3]).name == "codex"
        and arguments[4] == "exec"
    )


def api_exec(metadata_path, arguments):
    if not api_command(arguments):
        os.execv("/usr/bin/sudo", ["/usr/bin/sudo", *arguments])
    identity = read_regular_json(metadata_path, 48000)
    directory = Path(metadata_path).parent / "diagnostics"
    log = RunLog(directory, identity)
    # Only output formatting changes. The official action still chooses
    # authentication, permissions, command arguments and the execution account.
    if "--json" not in arguments[5:]:
        arguments = [*arguments, "--json"]
    return run_observed(
        ["/usr/bin/sudo", *arguments],
        log,
        event,
        timeout=identity["timeout_seconds"],
        stdin=sys.stdin.buffer,
        env=dict(os.environ),
        terminate=terminate_api,
    )


def terminate_api(process):
    # On hosted runners the observer owns the sudo parent, while the native CLI
    # runs as the separate command account. Stop only this freshly created group.
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "pkill", "-KILL", "-g", str(process.pid)], capture_output=True
    )
    require(result.returncode in (0, 1), "Could not stop this isolated Codex process group")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    return api_exec(args.metadata, arguments)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError) as exc:
        print(
            "Codex activity observer could not start: " + str(type(exc).__name__), file=sys.stderr
        )
        raise SystemExit(2) from exc

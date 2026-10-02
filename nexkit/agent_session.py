"""Select an accepted agent adapter and collect its native workspace result.

The workflow validates current GitHub authority before this entrypoint. Runtime
paths and role come from the controller's sealed workspace preparation. This
module does not schedule work, spend another reservation or implement login.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .adapters import adapter
from .command_workspace import export
from .common import Blocked, canonical, digest, is_link, read_json
from .invocations import execution_config
from .platforms import require_native
from .policy import agent_runner, execution_settings, require
from .workspace import unchanged


def prepared(context, role, data):
    cfg = execution_settings(execution_config(context))
    native = require_native(agent_runner(cfg))
    require(role in ("request", "deliver", "review", "task"), "Unknown agent session role")
    runtime = read_json(Path(data) / "agent-runtime.json")
    require(
        runtime.get("schema") == 1
        and runtime.get("context") == digest(context)
        and runtime.get("role") == role
        and runtime.get("native") == native,
        "Prepared agent workspace differs from the accepted session",
    )
    home, work, output = (Path(runtime[name]) for name in ("home", "workspace", "output"))
    require(
        home.is_absolute() and work == home / "work" and output == home / "output/result.json",
        "Invalid prepared agent paths",
    )
    require(
        not any(is_link(path) for path in (home, work, home / "output")),
        "Prepared agent paths must not use links or reparse points",
    )
    unchanged(home, Path(data))
    return cfg, native, home, work, output


def configuration(context, role, data):
    cfg, native, home, work, output = prepared(context, role, data)
    values = {
        "native": native,
        "home": home,
        "workspace": work,
        "output": output,
        "prompt": Path(data) / "prompt.txt",
        "schema": Path(data) / "schema.json",
        **adapter(cfg["engine"]).session_settings(cfg, role),
    }
    export(values)
    return {key: str(value) for key, value in values.items()}


def minutes(context, cfg, role):
    value = (
        context["agent_minutes"]
        if role == "request" or "invocation" in context
        else max(1, min(60, cfg["limits"]["minutes"] // 2))
    )
    require(type(value) is int and 1 <= value <= 60, "Invalid session reservation")
    return value


def execute(context, role, data):
    cfg, native, home, work, output = prepared(context, role, data)
    minutes(context, cfg, role)
    return adapter(cfg["engine"]).execute(context, role, data=Path(data))


def collect(context, role, data, source, destination):
    from .ci import collect as collect_result

    _, native, _, work, output = prepared(context, role, data)
    if native == "linux":
        # The official API action uses the temporary command account. Native
        # subscription sessions already reap their separate parent identity.
        result = subprocess.run(["sudo", "-n", "pkill", "-KILL", "-u", "nexkit-agent"])
        require(result.returncode in (0, 1), "Could not stop the temporary command account")
    return collect_result(
        source, work, context, output, destination, role, Path(data) / "initial.json"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("configuration", "execute", "collect"))
    parser.add_argument("--role", required=True, choices=("request", "deliver", "review", "task"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--source")
    parser.add_argument("--out")
    args = parser.parse_args()
    location = Path(args.context).absolute()
    context = read_json(location)
    if args.operation == "collect":
        require(args.source and args.out, "Provide --source and --out for collection")
        value = collect(context, args.role, location.parent, args.source, args.out)
    else:
        method = configuration if args.operation == "configuration" else execute
        value = method(context, args.role, location.parent)
    print(canonical(value))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, subprocess.SubprocessError) as exc:
        print(f"Native agent session blocked: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

"""Prepare native agent workspaces and restore controls after dependency setup.

The caller must first validate the accepted workflow context. Consumer setup
uses the native command account and receives no controller or model credential.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .adapters import ADAPTERS
from .checks import execute
from .ci import load_context, materialize
from .command_workspace import export
from .common import Blocked, canonical, digest, is_link, run, write_json
from .invocations import execution_config
from .platforms import require_native
from .policy import agent_runner, require
from .python_runtime import prepare as prepare_python


def dependencies(context, role, work, home, values):
    if role == "request":
        return []
    cfg = execution_config(context)
    saved = {key: os.environ.get(key) for key in values}
    results = []
    try:
        os.environ.update({key: str(value) for key, value in values.items()})
        for argv in cfg["environment"]["setup"]:
            result = execute(argv, work, cfg["limits"]["command_seconds"], command_home=home)
            results.append(result)
            require(
                result["exit_code"] == 0 and not result["timed_out"],
                "Agent environment setup failed: " + result["log"][-3000:],
            )
        return results
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare_linux(source, data, context, role):
    prepare_python()
    home, work = Path("/home/nexkit-agent"), Path("/home/nexkit-agent/work")
    run(["sudo", "-n", "useradd", "--create-home", "--shell", "/bin/bash", "nexkit-agent"])
    run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", home])
    run(["sudo", "-n", "chmod", "755", home])
    materialize(source, work, context, role, data)
    kit = Path(__file__).resolve().parents[1]
    sealed = [
        "sudo",
        "-n",
        "env",
        f"PYTHONPATH={kit}",
        sys.executable,
        "-m",
        "nexkit.workspace",
    ]
    run([*sealed, "snapshot", "--data-dir", data])
    run(["sudo", "-n", "chown", "-hR", "nexkit-agent:nexkit-agent", work])
    run(["sudo", "-n", "chown", "nexkit-agent:nexkit-agent", home])
    values = {"NEXKIT_EXEC_USER": "nexkit-agent", "NEXKIT_COMMAND_HOME": home}
    results = dependencies(context, role, work, home, values)
    run([*sealed, "restore", "--role", role, "--owner", str(os.getuid()), "--data-dir", data])
    configuration_dirs = {
        home / Path(relative).parent
        for integration in ADAPTERS.values()
        for relative in integration.home_configuration()
    }
    run(["sudo", "-n", "chmod", "-R", "a+rX", data, *sorted(configuration_dirs)])
    run(["chmod", "-R", "go-w", kit])
    return home, work, values, results


def prepare(source, data, context, role):
    native = require_native(agent_runner(execution_config(context)))
    require(role in ("request", "deliver", "review", "task"), "Unknown agent workspace role")
    source, data = Path(source).absolute(), Path(data).absolute()
    require(source.is_dir() and not is_link(source), "Use an unchanged source checkout")
    data.mkdir(parents=True, exist_ok=True)
    home, work, values, results = prepare_linux(source, data, context, role)
    values.update(
        {
            "NEXKIT_AGENT_HOME": home,
            "NEXKIT_WORKSPACE": work,
            "NEXKIT_DATA_DIR": data,
            "NEXKIT_AGENT_OUTPUT": home / "output/result.json",
        }
    )
    write_json(
        data / "agent-runtime.json",
        {
            "schema": 1,
            "native": native,
            "role": role,
            "context": digest(context),
            "home": str(home),
            "workspace": str(work),
            "output": str(home / "output/result.json"),
        },
    )
    export(values)
    return {"paths": {key: str(value) for key, value in values.items()}, "setup": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("request", "deliver", "review", "task"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    result = prepare(
        args.source, Path(args.context).absolute().parent, load_context(args.context), args.role
    )
    print(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError) as exc:
        raise SystemExit(f"Native agent workspace blocked: {exc}") from exc

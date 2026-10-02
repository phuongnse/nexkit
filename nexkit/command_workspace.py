"""Prepare isolated command jobs inside the shared Linux runtime.

The controller owns configuration and collection. Only accepted consumer argv
runs as a separate command account. Windows hosts use this same adapter
inside a Linux container; a Windows host account never executes managed jobs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .common import Blocked, is_link, run
from .platforms import execution_runner, require_native
from .policy import require


def export(values):
    for destination in (os.environ.get("GITHUB_ENV"), os.environ.get("GITHUB_OUTPUT")):
        if not destination:
            continue
        with open(destination, "a", encoding="utf-8", newline="\n") as target:
            for name, value in values.items():
                require("\n" not in str(value) and "\r" not in str(value), "Invalid job path")
                target.write(f"{name}={value}\n")


def prepare(source, data, cfg):
    require_native(execution_runner(cfg))
    source, data = Path(source).absolute(), Path(data).absolute()
    require(source.is_dir() and not is_link(source), "Use an unchanged source checkout")
    data.mkdir(parents=True, exist_ok=True)
    home, work = Path("/home/nexkit-agent"), Path("/home/nexkit-agent/work")
    run(["sudo", "-n", "useradd", "--create-home", "--shell", "/bin/bash", "nexkit-agent"])
    run(["sudo", "-n", "chmod", "755", str(home)])
    require(not work.exists(), "Use a fresh native command workspace")
    run(["sudo", "-n", "cp", "-a", str(source), str(work)])
    run(["sudo", "-n", "chown", "-hR", "nexkit-agent:nexkit-agent", str(work)])
    # The hosted runner's HOME can be private. Copy just the trusted stdlib
    # helper to a root-owned readable location instead of opening that HOME.
    helper = Path("/opt/nexkit/step_io.py")
    run(
        [
            "sudo",
            "-n",
            "install",
            "-D",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "644",
            str(Path(__file__).with_name("step_io.py")),
            str(helper),
        ]
    )
    extra = {"NEXKIT_STEP_HELPER": helper}
    values = {
        "NEXKIT_EXEC_USER": "nexkit-agent",
        "NEXKIT_COMMAND_HOME": home,
        "NEXKIT_WORKSPACE": work,
        "NEXKIT_DATA_DIR": data,
        **extra,
    }
    export(values)
    return {key: str(value) for key, value in values.items()}


def main():
    from .ci import load_context

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    result = prepare(args.source, args.data_dir, load_context(args.context)["config"])
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError) as exc:
        print(f"Native command workspace blocked: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

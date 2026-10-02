"""Physical WSL lifecycle fixture; its service uses sleep, not an Actions listener."""

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.common import Blocked, run, write_json  # noqa: E402
from nexkit.policy import require  # noqa: E402
from nexkit.runner_host import host_kind, supervise_windows, wsl_path, wsl_prefix  # noqa: E402


def worker(name):
    require(host_kind() == "windows-wsl" and os.geteuid() == 0, "Use the WSL fixture host")
    spec = importlib.util.spec_from_file_location(
        "manage_runner", ROOT / "scripts/manage_runner.py"
    )
    management = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(management)
    unit = Path("/etc/systemd/system", name + ".service")
    require(not unit.exists(), "Lifecycle fixture already exists")
    with tempfile.TemporaryDirectory(prefix="nexkit-lifecycle-") as directory:
        root = Path(directory)
        state = root / "host.json"
        write_json(state, {"schema": 1, "kind": "windows-wsl", "apparmor": False})
        guard = root / "guard.py"
        guard.write_text(
            "import ipaddress,json,sys\n"
            "state=json.load(open(sys.argv[1]))\n"
            "assert state['windows_host_ipv4']\n"
            "for value in state['windows_host_ipv4']: ipaddress.IPv4Address(value)\n"
        )
        unit.write_text(
            "[Service]\nType=simple\n"
            f"ExecStartPre=/usr/bin/python3 -I {guard} {state}\n"
            "ExecStart=/usr/bin/sleep 300\n"
        )
        try:
            run(["systemctl", "daemon-reload"])
            management.supervise_wsl(name, state)
        finally:
            run(["systemctl", "stop", name + ".service"], check=False)
            unit.unlink()
            run(["systemctl", "daemon-reload"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--distribution")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    require(re.fullmatch(r"nexkit-lifecycle-[a-f0-9]{16}", args.name), "Invalid fixture name")
    if args.worker:
        return worker(args.name)
    prefix = wsl_prefix(args.distribution)
    return supervise_windows(
        [*prefix, "python3", wsl_path(prefix, __file__), "--name", args.name, "--worker"]
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError) as exc:
        print(json.dumps({"fixture_failed": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc

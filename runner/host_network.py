"""Install rules scoped exclusively to the NexKit runner bridge, before startup."""

import argparse
import ipaddress
import json
import os
import re
import subprocess

NETWORK = "nexkit-runners"
CHAIN = "NEXKIT-RUNNERS"
PRIVATE = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def command(argv, check=True):
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def ensure_rule(chain, rule, *, first=False):
    if command(["iptables", "-w", "5", "-C", chain, *rule], check=False).returncode:
        command(
            [
                "iptables",
                "-w",
                "5",
                "-I" if first else "-A",
                chain,
                *(["1"] if first else []),
                *rule,
            ]
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--network", default=NETWORK)
    parser.add_argument("--chain", default=CHAIN)
    parser.add_argument("--apparmor-profile", default="/opt/nexkit-runner/apparmor.profile")
    args = parser.parse_args()
    from pathlib import Path

    path = Path(args.state)
    if not path.is_absolute() or path.is_symlink():
        raise RuntimeError("Use the installed host state file")
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o022 or os.geteuid() != 0:
        raise RuntimeError("Host network policy requires administrator-owned state")
    state = json.loads(path.read_text())
    if state.get("schema") != 1 or state.get("kind") not in {"linux", "windows-wsl"}:
        raise RuntimeError("Unsupported runner host state")
    extra = state.get("windows_host_ipv4", [])
    if not isinstance(extra, list) or len(extra) > 256:
        raise RuntimeError("Invalid Windows host addresses")
    if not all(isinstance(value, str) for value in extra):
        raise RuntimeError("Use literal Windows host IPv4 addresses")
    if type(state.get("apparmor")) is not bool:
        raise RuntimeError("Inspect Docker Engine's AppArmor support before setup")
    if state["kind"] == "windows-wsl" and not extra:
        raise RuntimeError("Inspect Windows host addresses before starting WSL jobs")
    extra = [str(ipaddress.IPv4Address(value)) + "/32" for value in extra]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", args.network):
        raise RuntimeError("Invalid runner bridge name")
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{0,27}", args.chain):
        raise RuntimeError("Invalid runner firewall chain")
    chain = args.chain
    network = json.loads(command(["docker", "network", "inspect", args.network]).stdout)[0]
    if network.get("EnableIPv6"):
        raise RuntimeError("This runner network integration requires IPv4-only Docker networking")
    bridge = network["Options"].get("com.docker.network.bridge.name", "br-" + network["Id"][:12])
    if not command(["iptables", "-w", "5", "-S", chain], check=False).returncode == 0:
        command(["iptables", "-w", "5", "-N", chain])
    for subnet in (*PRIVATE, *extra):
        ensure_rule(chain, ["-d", subnet, "-j", "REJECT"])
    # Other containers and private services are blocked in FORWARD; every
    # address on the VPS itself is blocked in INPUT, including its public IP.
    ensure_rule("DOCKER-USER", ["-i", bridge, "-j", chain], first=True)
    ensure_rule("INPUT", ["-i", bridge, "-j", "REJECT"], first=True)
    if state.get("apparmor") is True:
        command(["apparmor_parser", "-r", args.apparmor_profile])
    print("NexKit runner bridge policy is installed")


if __name__ == "__main__":
    main()

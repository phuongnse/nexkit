"""Install rules scoped exclusively to the NexKit runner bridge, before startup."""

import json
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
    network = json.loads(command(["docker", "network", "inspect", NETWORK]).stdout)[0]
    if network.get("EnableIPv6"):
        raise RuntimeError("This runner network integration requires IPv4-only Docker networking")
    bridge = network["Options"].get("com.docker.network.bridge.name", "br-" + network["Id"][:12])
    if not command(["iptables", "-w", "5", "-S", CHAIN], check=False).returncode == 0:
        command(["iptables", "-w", "5", "-N", CHAIN])
    for subnet in PRIVATE:
        ensure_rule(CHAIN, ["-d", subnet, "-j", "REJECT"])
    # Other containers and private services are blocked in FORWARD; every
    # address on the VPS itself is blocked in INPUT, including its public IP.
    ensure_rule("DOCKER-USER", ["-i", bridge, "-j", CHAIN], first=True)
    ensure_rule("INPUT", ["-i", bridge, "-j", "REJECT"], first=True)
    command(["apparmor_parser", "-r", "/opt/nexkit-runner/apparmor.profile"])
    print("NexKit runner bridge policy is installed")


if __name__ == "__main__":
    main()

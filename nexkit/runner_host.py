"""Docker Engine host checks and Windows-to-WSL administration.

Windows only starts the Linux adapter and supplies public host addresses.
Registration and Codex login stay in the Linux environment; no login cache
or administrator token is copied between operating systems.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from . import __version__
from .common import Blocked, run
from .policy import require


def addresses(values):
    require(
        isinstance(values, list) and 0 < len(values) <= 256, "Supply Windows host IPv4 addresses"
    )
    require(
        all(isinstance(value, str) for value in values), "Use literal Windows host IPv4 addresses"
    )
    try:
        return sorted({str(ipaddress.IPv4Address(value)) for value in values})
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise Blocked("Invalid Windows host IPv4 address") from exc


def windows_addresses():
    """Read IPv4 interfaces through IP Helper, without shell or CIM startup.

    https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getipaddrtable
    """
    require(os.name == "nt", "Windows host inspection requires Windows")

    class Row(ctypes.Structure):
        _fields_ = [
            ("Address", ctypes.c_ubyte * 4),
            *[(name, ctypes.c_uint32) for name in ("Index", "Mask", "Broadcast", "Reassembly")],
            ("Reserved", ctypes.c_uint16),
            ("Type", ctypes.c_uint16),
        ]

    class Table(ctypes.Structure):
        _fields_ = [("Count", ctypes.c_uint32), ("Rows", Row * 1)]

    binary = Path(os.environ["SYSTEMROOT"]) / "System32/iphlpapi.dll"
    query = ctypes.WinDLL(str(binary)).GetIpAddrTable
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    query.restype = ctypes.c_uint32
    size = ctypes.c_uint32(15000)
    for _ in range(3):
        require(ctypes.sizeof(Table) <= size.value <= 1048576, "Invalid Windows address table size")
        buffer = ctypes.create_string_buffer(size.value)
        code = query(buffer, ctypes.byref(size), 0)
        if code == 122:  # ERROR_INSUFFICIENT_BUFFER: an interface may have changed.
            continue
        require(code == 0, f"Windows IPv4 inspection failed ({code})")
        count = ctypes.c_uint32.from_buffer(buffer).value
        require(
            0 < count <= 256
            and Table.Rows.offset + count * ctypes.sizeof(Row) <= ctypes.sizeof(buffer),
            "Invalid Windows IPv4 address table",
        )
        return addresses(
            [
                str(
                    ipaddress.IPv4Address(
                        bytes(
                            Row.from_buffer(
                                buffer, Table.Rows.offset + i * ctypes.sizeof(Row)
                            ).Address
                        )
                    )
                )
                for i in range(count)
            ]
        )
    raise Blocked("Windows address table kept changing during inspection")


def wsl_prefix(distribution):
    require(
        isinstance(distribution, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", distribution) is not None,
        "Use an explicit WSL 2 distribution name",
    )
    require(os.name == "nt", "Select --wsl-distribution from the Windows host")
    binary = Path(os.environ["SYSTEMROOT"]) / "System32/wsl.exe"
    return [str(binary), "--distribution", distribution, "--user", "root", "--exec"]


def wsl_path(prefix, path):
    value = run(
        [*prefix, "wslpath", "-a", "-u", str(Path(path).absolute())], timeout=30
    ).stdout.strip()
    require(value.startswith("/") and "\n" not in value and "\r" not in value, "Invalid WSL path")
    return value


def wsl_command(distribution, script, config, pipeline, invocation=None):
    prefix = wsl_prefix(distribution)
    command = [
        *prefix,
        "python3",
        wsl_path(prefix, script),
        "--config",
        wsl_path(prefix, config),
        "--pipeline",
        pipeline,
    ]
    if invocation:
        command.extend(["--invocation", invocation])
    return command


def host_kind():
    if os.name == "nt" or "microsoft" in platform.release().lower():
        return "windows-wsl"
    return "linux"


def local_engine():
    """Host firewall rules must be installed beside the actual Docker daemon."""
    allowed = {"unix:///var/run/docker.sock", "unix:///run/docker.sock"}
    require(os.environ.get("DOCKER_HOST", "") in allowed | {""}, "Use the system Docker socket")
    require(
        os.environ.get("DOCKER_CONTEXT", "") in {"", "default"}, "Select Docker's default context"
    )
    endpoint = json.loads(
        run(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"]).stdout
    )
    require(endpoint in allowed, "Select the local system Docker Engine context")
    require(
        Path("/var/run/docker.sock").resolve() == Path("/run/docker.sock"),
        "Use Docker Engine in Ubuntu; a Docker Desktop socket proxy is not this host's Engine",
    )
    pid = run(
        ["systemctl", "show", "docker.service", "--property=MainPID", "--value"]
    ).stdout.strip()
    require(pid.isdecimal() and int(pid) > 1, "Start this Ubuntu host's Docker service")
    namespaces = run(
        ["sudo", "-n", "stat", "-L", "--format=%d:%i", f"/proc/{pid}/ns/net", "/proc/self/ns/net"]
    ).stdout.splitlines()
    require(
        len(namespaces) == 2 and namespaces[0] == namespaces[1],
        "Docker Engine and the host firewall must use the same network namespace",
    )


def inspect_host(kind):
    require(
        sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"},
        "Run Docker Engine in an x64 Linux environment; Windows hosts use WSL 2",
    )
    actual = host_kind()
    require(kind == actual, "Host selection differs from the actual Linux/WSL environment")
    release = platform.freedesktop_os_release()
    require(release.get("ID") == "ubuntu", "The shared runner host requires Ubuntu")
    require(
        Path("/proc/1/comm").read_text().strip() == "systemd", "Enable systemd before runner setup"
    )
    local_engine()
    info = json.loads(run(["docker", "info", "--format", "{{json .}}"], timeout=30).stdout)
    require(
        info.get("OSType") == "linux" and info.get("Architecture") in {"x86_64", "amd64"},
        "The runner needs Linux/x64 containers",
    )
    require(
        info.get("KernelVersion") == platform.release(),
        "Use Docker Engine in this Ubuntu host, rather than another Docker context",
    )
    security = info.get("SecurityOptions", [])
    require(
        any(value.startswith("name=seccomp") for value in security),
        "Docker Engine must support seccomp",
    )
    require(
        not any(value.startswith("name=rootless") for value in security),
        "This runner uses the system Docker Engine and a dedicated bridge",
    )
    return {
        "schema": 1,
        "kind": kind,
        "apparmor": any(value.startswith("name=apparmor") for value in security),
        "windows_host_ipv4": [],
        "kernel": platform.release(),
        "docker_version": info.get("ServerVersion"),
        "distribution": {"name": release.get("ID"), "version": release.get("VERSION_ID")},
    }


def image_build(cfg):
    from .adapters import adapter

    return adapter(cfg["engine"]).build_image(
        cfg["engine"], __version__, Path(__file__).resolve().parents[1]
    )


def security_args(state, *, policy_directory):
    require(isinstance(policy_directory, str), "Record the installed runner policy directory")
    directory = PurePosixPath(policy_directory)
    require(
        directory.parent == PurePosixPath("/opt/nexkit-runner")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", directory.name),
        "Invalid installed runner policy directory",
    )
    values = ["--security-opt", f"seccomp={directory}/seccomp.json"]
    if state["apparmor"]:
        values.extend(["--security-opt", "apparmor=nexkit-runner"])
    return values


def runner_identity(cfg):
    import hashlib

    from .policy import agent_runner, authentication

    require(
        authentication(cfg) == "chatgpt", "Select the consumer's ChatGPT authentication explicitly"
    )
    labels = agent_runner(cfg)
    require(
        isinstance(labels, list) and {"self-hosted", "linux", "x64"} <= set(labels),
        "Configure a Linux container agent runner",
    )
    custom = [label for label in labels if label not in {"self-hosted", "linux", "x64"}]
    require(len(custom) == 1, "Choose exactly one project-specific runner label for provisioning")
    name = (
        "nexkit-" + hashlib.sha256((cfg["repository"] + ":" + custom[0]).encode()).hexdigest()[:12]
    )
    return name, str(PurePosixPath("/var/lib/nexkit-runners", name)), custom[0]


def supervise_windows(command):
    """Hold WSL open and refresh host addresses; EOF stops the consumer service."""
    require(os.name == "nt", "The WSL supervisor runs on the Windows host")
    with subprocess.Popen(command, stdin=subprocess.PIPE, text=True, encoding="utf-8") as child:
        try:
            while child.poll() is None:
                child.stdin.write(json.dumps({"windows_host_ipv4": windows_addresses()}) + "\n")
                child.stdin.flush()
                try:
                    return child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            return child.returncode
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=50)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)

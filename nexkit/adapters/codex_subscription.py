"""Run the official CLI with a consumer-owned, persistent ChatGPT login.

This module never implements OAuth, reads tokens, or forwards credentials to a
model endpoint. The CLI owns its auth lifecycle. A different Unix account runs
consumer setup; native permission profiles deny the CLI's local tools access to
the persistent login directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

if os.name != "nt":
    import fcntl
    import pwd

from ..common import Blocked, canonical, digest, read_json, read_regular_json
from ..invocations import execution_config
from ..observability import RunLog, run_observed, session_identity
from ..policy import authentication, execution_settings, require
from ..workspace import unchanged
from .codex import CodexAdapter
from .codex_events import event

AUTH_HOME = Path("/var/lib/nexkit/codex")
WORKSPACE = Path("/home/nexkit-agent/work")
OUTPUT = Path("/home/nexkit-agent/output/result.json")
CLI_USER = "nexkit-codex"
BINDING = Path("/etc/nexkit/runner.json")
CODEX = "/usr/local/bin/codex"


def permission_settings(auth_home, workspace, scratch, *, writable, data=Path("/tmp/nexkit")):
    """Exact absolute paths with an explicit filesystem and network profile."""
    filesystem = {
        ":root": "read",
        str(workspace): "write" if writable else "read",
        str(scratch): "write",
        str(PurePosixPath(workspace) / ".agents"): "read",
        str(PurePosixPath(workspace) / ".git"): "read",
    }
    # A denied child is already covered by its denied parent. Some native
    # sandboxes cannot create the child mount beneath the masked parent.
    denied = sorted(
        {
            PurePosixPath(p)
            for p in (auth_home, "/opt/actions-runner", "/var/log/nexkit", "/tmp/nexkit", data)
        },
        key=lambda p: (len(p.parts), str(p)),
    )
    roots = []
    for path in denied:
        require(
            path.is_absolute() and ".." not in path.parts and str(path) != "/",
            "Use an absolute private directory",
        )
        if not any(path.is_relative_to(parent) for parent in roots):
            roots.append(path)
    for private in roots:
        require(
            not any(PurePosixPath(p).is_relative_to(private) for p in (workspace, scratch)),
            "Agent workspace and scratch must be outside private directories",
        )
        filesystem[str(private)] = "deny"
    return {
        "default_permissions": "nexkit",
        "permissions.nexkit.filesystem": filesystem,
        "permissions.nexkit.network.enabled": False,
        "approval_policy": "never",
        "forced_login_method": "chatgpt",
        "cli_auth_credentials_store": "file",
        "project_doc_max_bytes": 0,
        "allow_login_shell": False,
        "shell_environment_policy.inherit": "none",
        "shell_environment_policy.set": {
            "HOME": "/home/nexkit-agent",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TMPDIR": str(scratch),
            "CI": "true",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
        "web_search": "disabled",
        "mcp_servers": {},
        "plugins": {},
        **{
            f"features.{name}": False
            for name in (
                "apps",
                "connectors",
                "plugins",
                "remote_plugin",
                "plugin_hooks",
                "hooks",
                "codex_hooks",
                "browser_use",
                "browser_use_external",
                "computer_use",
                "in_app_browser",
                "js_repl",
                "code_mode",
                "shell_snapshot",
                "shell_snapshot_v2",
                "multi_agent",
                "multi_agent_v2",
                "collab",
                "daemon_auto_start",
                "remote_control",
                "skill_mcp_dependency_install",
                "skill_env_var_dependency_prompt",
                "memories",
                "memory_tool",
            )
        },
    }


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}={toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def config_args(settings):
    return [arg for key, value in settings.items() for arg in ("-c", f"{key}={toml(value)}")]


def cli_command(
    cfg,
    role,
    scratch,
    *,
    binary=CODEX,
    workspace=WORKSPACE,
    output=OUTPUT,
    data=Path("/tmp/nexkit"),
    profile=None,
):
    model_role = "review" if role == "review" else "implement"
    settings = (
        dict(profile)
        if profile is not None
        else permission_settings(
            AUTH_HOME, workspace, scratch, writable=role == "deliver", data=data
        )
    )
    effort = cfg.get("reasoning_effort", {}).get(model_role)
    if effort:
        settings["model_reasoning_effort"] = effort
    return [
        str(binary),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--model",
        cfg["models"][model_role],
        "-C",
        str(workspace),
        *config_args(settings),
        "--output-schema",
        str(Path(data) / "schema.json"),
        "--output-last-message",
        str(output),
        "-",
    ]


def isolation_probe(*, data=Path("/tmp/nexkit"), writable=True):
    """Exercise the installed sandbox with a fake credential, without login/model use."""
    require(os.geteuid() == 0, "Run this probe inside the isolated runner container as root")
    user = pwd.getpwnam(CLI_USER)
    parent = Path("/var/lib/nexkit/probes")
    parent.mkdir(mode=0o755, exist_ok=True)
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=parent) as directory,
        tempfile.TemporaryDirectory(dir=data) as private_directory,
    ):
        root = Path(directory)
        os.chmod(root, 0o755)
        auth, work, scratch = (root / p for p in ("auth", "work", "scratch"))
        for path in (auth, work, scratch):
            path.mkdir(mode=0o700)
            os.chown(path, user.pw_uid, user.pw_gid)
        target = auth / "auth.json"
        target.write_text("HARMLESS_NEXKIT_CREDENTIAL_CANARY")
        os.chmod(target, 0o600)
        os.chown(target, user.pw_uid, user.pw_gid)
        (work / "fixture").write_text("public fixture")
        (work / "link").symlink_to(target)
        controller = Path(private_directory) / "controller-canary"
        Path(private_directory).chmod(0o755)
        controller.write_text("HARMLESS_NEXKIT_CONTROLLER_CANARY")
        controller.chmod(0o644)
        (work / "controller-link").symlink_to(controller)
        script = """import json, os, pathlib, socket, sys
auth, work, controller = map(pathlib.Path, sys.argv[1:4])
writable = sys.argv[4] == "write"
result = {"fixture_read": (work / "fixture").read_text(encoding="utf-8") == "public fixture"}
try:
    (work / "created").write_text("allowed")
    result["workspace_role"] = writable
except OSError:
    result["workspace_role"] = not writable
for name, path in (("direct", auth), ("symlink", work / "link"),
                   ("controller", controller), ("controller_symlink", work / "controller-link"),
                   ("proc_self_root", pathlib.Path("/proc/self/root") / str(auth).lstrip("/")),
                   ("proc_init_root", pathlib.Path("/proc/1/root") / str(auth).lstrip("/"))):
    try:
        path.read_bytes()
        result[name + "_denied"] = False
    except OSError:
        result[name + "_denied"] = True
try:
    auth.write_text("changed")
    result["auth_write_denied"] = False
except OSError:
    result["auth_write_denied"] = True
try:
    socket.create_connection(("1.1.1.1", 443), timeout=0.5).close()
    result["network_denied"] = False
except OSError:
    result["network_denied"] = True
print(json.dumps(result))
sys.exit(0 if all(result.values()) else 1)
"""
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/nexkit-codex",
            "CODEX_HOME": str(auth),
            "TMPDIR": str(scratch),
        }
        settings = permission_settings(auth, work, scratch, writable=writable, data=data)
        result = subprocess.run(
            [
                CODEX,
                "sandbox",
                "-P",
                "nexkit",
                "-C",
                str(work),
                *config_args(settings),
                "--",
                "python3",
                "-c",
                script,
                str(target),
                str(work),
                str(controller),
                "write" if writable else "read",
            ],
            env=env,
            user=user.pw_uid,
            group=user.pw_gid,
            extra_groups=[],
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(result.returncode == 0, "Native sandbox probe failed: " + result.stderr[-2000:])
        value = json.loads(result.stdout)
        require(all(value.values()), "A sandbox boundary failed its canary probe")
        return {"fake_credentials_only": True, "model_calls": 0, "boundaries": value}


def binding(cfg):
    require(os.geteuid() == 0, "Subscription entrypoint requires the isolated runner administrator")
    require(BINDING.is_file() and not BINDING.is_symlink(), "Runner binding is absent")
    require(
        BINDING.stat().st_uid == 0 and not BINDING.stat().st_mode & 0o022,
        "Runner binding must be root-owned and immutable to the job account",
    )
    value = read_json(BINDING)
    require(value.get("agent") == "codex", "Runner is not bound to the Codex adapter")
    require(
        value.get("repository") == cfg["repository"], "This runner belongs to a different consumer"
    )
    require(
        value.get("default_branch") == cfg["default_branch"],
        "Runner branch binding differs from project configuration",
    )
    require(
        value.get("isolation") == "container-v1",
        "This integration requires the isolated NexKit runner deployment",
    )
    require(value.get("auth") == "chatgpt", "Runner does not permit subscription authentication")
    return value


def execute(context, role, *, data=Path("/tmp/nexkit")):
    cfg = execution_settings(execution_config(context))
    require(authentication(cfg) == "chatgpt", "This job does not select ChatGPT authentication")
    binding(cfg)
    unchanged(data=data)
    # Acquire before login checks, UID cleanup, ownership changes or CLI start.
    with open("/var/lib/nexkit/cli.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        minutes = (
            context["agent_minutes"]
            if role == "request" or "invocation" in context
            else max(1, min(60, cfg["limits"]["minutes"] // 2))
        )
        require(isinstance(minutes, int) and 1 <= minutes <= 60, "Invalid session reservation")
        return run_session(cfg, role, minutes * 60, data=data, context=context)


def run_session(cfg, role, timeout, *, data=Path("/tmp/nexkit"), context=None):
    parent_git = Path("/opt/nexkit/parent-bin/git")
    require(
        parent_git.is_file()
        and not parent_git.is_symlink()
        and parent_git.stat().st_uid == 0
        and not parent_git.stat().st_mode & 0o022,
        "The pinned runner must disable unsandboxed native Git metadata commands",
    )
    user = pwd.getpwnam(CLI_USER)
    require(AUTH_HOME.is_dir() and not AUTH_HOME.is_symlink(), "Runner login directory is absent")
    require(
        AUTH_HOME.stat().st_uid == user.pw_uid and AUTH_HOME.stat().st_mode & 0o077 == 0,
        "Runner login directory must be private to the Codex account",
    )
    # Read no credential contents. Official Codex login status performs the check.
    env = {
        "PATH": "/opt/nexkit/parent-bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/nexkit-agent",
        "CODEX_HOME": str(AUTH_HOME),
        "LANG": "C.UTF-8",
        "CI": "true",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
        "GIT_CONFIG_KEY_1": "core.fsmonitor",
        "GIT_CONFIG_VALUE_1": "false",
    }
    identity = {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": []}
    capabilities = CodexAdapter().probe(CODEX, env=env)
    # A version label alone cannot establish the private-login/tool boundary.
    # Exercise the selected CLI's actual sandbox with harmless credentials.
    isolation_probe(data=data, writable=role == "deliver")
    status = subprocess.run(
        [CODEX, "login", "status"], env=env, capture_output=True, text=True, **identity
    )
    require(
        status.returncode == 0 and "Logged in using ChatGPT" in status.stdout + status.stderr,
        "Run the official Codex login on this consumer's runner",
    )
    # Setup used a different account and must have ended before any model call.
    subprocess.run(["pkill", "-KILL", "-u", "nexkit-agent"], capture_output=True, check=False)
    subprocess.run(["pkill", "-KILL", "-u", CLI_USER], capture_output=True, check=False)
    subprocess.run(["chown", "-hR", f"{CLI_USER}:{CLI_USER}", "/home/nexkit-agent"], check=True)
    scratch = Path("/home/nexkit-codex/scratch")
    scratch.mkdir(mode=0o700)
    os.chown(scratch, user.pw_uid, user.pw_gid)
    env["TMPDIR"] = str(scratch)
    started = time.monotonic()
    log_dir = Path("/var/log/nexkit")
    log_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(log_dir, 0o700)
    with (
        open(data / "prompt.txt", "rb") as prompt,
        open(log_dir / "last-cli.log", "wb") as log,
    ):
        os.chmod(log.name, 0o600)
        metadata = session_identity(context or {"config": cfg}, role)
        if (Path(data) / "observation.json").exists():
            metadata = read_regular_json(Path(data) / "observation.json", 48000)
            require(
                context is not None
                and metadata.get("context") == digest(context)
                and metadata.get("role") == role,
                "Activity record differs from the accepted session",
            )
        metadata["timeout_seconds"] = timeout
        metadata["agent_capabilities"] = capabilities
        observation = RunLog(Path(data) / "diagnostics", metadata)
        try:
            code = run_observed(
                cli_command(cfg, role, scratch, data=data),
                observation,
                event,
                timeout=timeout,
                env=env,
                cwd=WORKSPACE,
                stdin=prompt,
                private_log=log,
                **identity,
            )
        finally:
            subprocess.run(["pkill", "-KILL", "-u", CLI_USER], capture_output=True, check=False)
        require(code == 0, f"Codex exited with status {code}; inspect the runner's private CLI log")
    require(
        OUTPUT.is_file() and not OUTPUT.is_symlink(), "CLI did not produce a regular result file"
    )
    return {
        "authentication": "chatgpt",
        "agent_capabilities": capabilities,
        "role": role,
        "seconds": round(time.monotonic() - started, 2),
        "result_produced": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context")
    parser.add_argument("--role", choices=("deliver", "review", "request", "task"))
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    try:
        if args.probe:
            result = isolation_probe()
        else:
            require(args.context and args.role, "Provide --context and --role")
            result = execute(read_json(args.context), args.role)
        print(canonical(result))
        return 0
    except (Blocked, OSError, subprocess.SubprocessError) as exc:
        print(f"NexKit subscription job blocked: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

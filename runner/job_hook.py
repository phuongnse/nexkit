"""Host-installed admission and cleanup for an isolated repository runner.

The official runner invokes this through immutable service hooks, outside the
checkout. A denied job terminates its Worker: failing a normal pre-job step is
insufficient because a later workflow step may have an `always()` condition.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BINDING = Path("/etc/nexkit/runner.json")
RUNNER = Path("/opt/actions-runner")


def admitted(binding, env):
    repo = binding["repository"]
    branch = "refs/heads/" + binding["default_branch"]
    allowed = {f"{repo}/.github/workflows/{name}@{branch}" for name in binding["workflows"]}
    ordinary = (
        env.get("GITHUB_REPOSITORY") == repo
        and env.get("GITHUB_REF") == branch
        and env.get("GITHUB_EVENT_NAME") in ("workflow_dispatch", "issue_comment", "issues")
        and env.get("GITHUB_WORKFLOW_REF") in allowed
    )
    if ordinary:
        return True
    for entry in binding.get("administration", []):
        if not isinstance(entry, dict):
            continue
        try:
            valid = datetime.fromisoformat(entry["expires_at"]) > datetime.now(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        identifier = entry.get("proposal", "")
        sha = entry.get("workflow_sha", "")
        if not isinstance(identifier, str) or not isinstance(sha, str):
            continue
        ref = "refs/heads/nexkit/setup-" + identifier[:24]
        if (
            valid
            and re.fullmatch(r"[0-9a-f]{64}", identifier)
            and re.fullmatch(r"[0-9a-f]{40}", sha)
            and env.get("GITHUB_REPOSITORY") == repo
            and env.get("GITHUB_REF") == ref
            and env.get("GITHUB_EVENT_NAME") == "push"
            and env.get("GITHUB_WORKFLOW_REF")
            == f"{repo}/.github/workflows/nexkit-administration.yml@{ref}"
            and env.get("GITHUB_WORKFLOW_SHA") == sha
            and env.get("GITHUB_SHA") == sha
        ):
            return True
    return False


def stop_checkpoint_monitor(data=Path("/tmp/nexkit")):
    metadata = data / "checkpoint-monitor.json"
    if not metadata.exists():
        return
    info = metadata.lstat()
    if metadata.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 4000:
        raise RuntimeError("Untrusted checkpoint monitor metadata")
    value = json.loads(metadata.read_text())
    deadline = time.monotonic() + 5
    while (
        isinstance(value, dict)
        and value.get("status") == "starting"
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
        value = json.loads(metadata.read_text())
    if not isinstance(value, dict) or value.get("status") == "starting":
        raise RuntimeError("Checkpoint monitor did not establish an owned process")
    if value.get("status") != "running":
        return
    pid = value.get("pid")
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("Checkpoint monitor has no owned process")
    process = Path("/proc", str(pid))
    if not process.exists():
        return
    command = (process / "cmdline").read_bytes().split(b"\0")
    if (
        process.stat().st_uid != 0
        or b"nexkit.checkpoints" not in command
        or b"monitor" not in command
        or os.getpgid(pid) != pid
    ):
        raise RuntimeError("Checkpoint process identity changed")
    (data / "checkpoint-stop.json").write_text(
        json.dumps({"stop": True, "deadline": time.time() + 45}) + "\n"
    )
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline and process.exists():
        if json.loads(metadata.read_text()).get("status") != "running":
            return
        time.sleep(0.1)
    if process.exists():
        os.killpg(pid, signal.SIGKILL)


def worker_ancestor():
    pid = os.getppid()
    for _ in range(32):
        if pid <= 1:
            return None
        process = Path("/proc", str(pid))
        if (process / "exe").resolve() == RUNNER / "bin/Runner.Worker":
            if process.stat().st_uid == os.getuid():
                return pid
            return None
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        pid = int(fields[1])
    return None


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup(workspace=None):
    for user in ("nexkit-agent", "nexkit-codex"):
        subprocess.run(
            ["pkill", "-KILL", "-u", user],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    stop_checkpoint_monitor()
    for path in (Path("/home/nexkit-agent"), Path("/home/nexkit-codex"), Path("/tmp/nexkit")):
        remove(path)
    Path("/home/nexkit-codex").mkdir(mode=0o755)
    # Pre-job cleanup runs before checkout; the current job's _actions and
    # _temp directories belong to the runner and are deliberately preserved.
    if workspace:
        target = Path(workspace)
        expected = RUNNER / "_work"
        if not target.is_absolute() or not target.parent.parent == expected:
            raise ValueError("Unexpected job workspace")
        remove(target)
        target.mkdir(parents=True, mode=0o755)


def kill_worker(pid):
    # This hook runs only inside a dedicated container. No global process scan
    # or host process IDs are accepted as kill targets.
    if pid is None:
        # The pinned image uses Tini as its private PID 1. Tini forwards USR1 to
        # run.sh, whose termination ends this container and all its processes.
        # Never send this signal to an arbitrary host init process.
        print("NexKit admission failed; runner ancestry could not be established", flush=True)
        if Path("/.dockerenv").is_file() and Path("/proc/1/exe").resolve() == Path("/usr/bin/tini"):
            os.kill(1, signal.SIGUSR1)
        # Do not return to workflow steps while container shutdown is pending.
        # An unsupported image/init configuration requires administrator repair.
        while True:
            signal.pause()
    os.kill(pid, signal.SIGKILL)


def main():
    import pwd

    worker = None
    try:
        if os.geteuid() != 0:
            raise RuntimeError("The isolated container runner must execute hooks as root")
        worker = worker_ancestor()
        if worker is None:
            raise RuntimeError("Not running under the pinned Actions Worker")
        binding = json.loads(BINDING.read_text())
        if sys.argv[1:] == ["completed"]:
            cleanup()
            print("NexKit isolated workspace removed")
            return
        if not admitted(binding, os.environ):
            raise RuntimeError("Repository, branch, event or workflow is not admitted")
        cleanup(os.environ["GITHUB_WORKSPACE"])
        # Setup uses a different UID from the parent Codex process; no auth is
        # available to repository install scripts even before the CLI sandbox.
        try:
            pwd.getpwnam("nexkit-agent")
        except KeyError:
            pass
        else:
            subprocess.run(["userdel", "nexkit-agent"], check=True)
        print("NexKit runner admitted the configured consumer workflow")
    except BaseException:
        print("NexKit runner rejected the job before executing workflow steps", flush=True)
        try:
            cleanup()
        finally:
            kill_worker(worker)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

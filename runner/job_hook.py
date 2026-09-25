"""Host-installed admission and cleanup for an isolated repository runner.

The official runner invokes this through immutable service hooks, outside the
checkout. A denied job terminates its Worker: failing a normal pre-job step is
insufficient because a later workflow step may have an `always()` condition.
"""

import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
from pathlib import Path

BINDING = Path("/etc/nexkit/runner.json")
RUNNER = Path("/opt/actions-runner")


def admitted(binding, env):
    repo = binding["repository"]
    branch = "refs/heads/" + binding["default_branch"]
    allowed = {f"{repo}/.github/workflows/{name}@{branch}" for name in binding["workflows"]}
    return (
        env.get("GITHUB_REPOSITORY") == repo
        and env.get("GITHUB_REF") == branch
        and env.get("GITHUB_EVENT_NAME") in ("workflow_dispatch", "issue_comment", "issues")
        and env.get("GITHUB_WORKFLOW_REF") in allowed
    )


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

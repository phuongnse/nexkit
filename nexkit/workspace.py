"""Preserve trusted workspace metadata across untrusted dependency setup."""

import argparse
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

from .adapters import ADAPTERS, control_directories
from .command_env import environment
from .common import Blocked, is_link, kit_root, read_json
from .policy import require

HOME = Path("/home/nexkit-agent")
DATA = Path("/tmp/nexkit")


def directory_identity(path):
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) and not is_link(path),
        f"Expected an unchanged directory: {path}",
    )
    return [info.st_dev, info.st_ino]


def unchanged(home=HOME, data=DATA):
    before = read_json(data / "setup-directories.json")
    for path in (home, home / "work"):
        require(
            directory_identity(path) == before[str(path)],
            "Dependency setup replaced a workspace directory",
        )


def snapshot(home=HOME, data=DATA):
    identities = {str(path): directory_identity(path) for path in (home, home / "work")}
    directory_identity(home / "work/.git")
    shutil.copytree(home / "work/.git", data / "trusted.git", symlinks=True)
    skills = home / "work/.agents/skills"
    if skills.is_dir():
        require(
            not any(is_link(path) for path in (skills, *skills.rglob("*"))),
            "Trusted skills cannot contain links or reparse points",
        )
        shutil.copytree(skills, data / "trusted-skills")
    (data / "setup-directories.json").write_text(json.dumps(identities), encoding="utf-8")
    if os.name == "nt":
        # Portable local file operations keep the caller's Windows permissions.
        # Managed workspace sealing runs in Linux with ownership checks below.
        return
    for path in (data / "trusted.git", *(data / "trusted.git").rglob("*")):
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode & ~0o022)
    for path in (data / "trusted-skills").rglob("*"):
        os.chmod(path, path.stat().st_mode & ~0o022)


def remove(path):
    if is_link(path):
        if os.name == "nt" and path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    elif path.is_file():
        if os.name == "nt":
            _writable_file(path)
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, onerror=_remove_readonly if os.name == "nt" else None)


def _writable_file(path):
    info = path.lstat()
    require(
        not is_link(path) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "Cannot change attributes on a linked workspace control file",
    )
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def _remove_readonly(function, path, error):
    if not isinstance(error[1], PermissionError):
        raise error[1]
    _writable_file(Path(path))
    function(path)


def remove_control(home, relative):
    # Never traverse a setup-created junction in a startup-control parent.
    path = home
    for part in Path(relative).parts:
        path /= part
        if is_link(path):
            remove(path)
            return
    remove(path)


def restore(home=HOME, data=DATA, *, uid=None, gid=None, role):
    # The caller has killed setup processes and sealed HOME against renames.
    # Check again before any privileged traversal into the writable workspace.
    unchanged(home, data)
    work = home / "work"
    remove(work / ".git")
    shutil.copytree(data / "trusted.git", work / ".git", symlinks=True)
    env = environment(Path.home()) if os.name == "nt" else {"PATH": "/usr/bin:/bin"}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    for key, value in (("core.hooksPath", os.devnull), ("core.fsmonitor", "false")):
        subprocess.run(
            ["git", "-c", f"safe.directory={work}", "-C", str(work), "config", key, value],
            env=env,
            check=True,
        )
    if os.name != "nt":
        require(uid is not None and gid is not None, "Provide the Linux command identity")
        for path in (work / ".git", *(work / ".git").rglob("*")):
            os.chown(path, uid, gid, follow_symlinks=False)
    for name in (
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".profile",
        ".gitconfig",
        ".config/git",
        ".config/powershell",
        "Documents/PowerShell",
        "Documents/WindowsPowerShell",
        "AppData/Local/Git",
        "AppData/Roaming/Git",
    ):
        remove_control(home, name)
    for name in control_directories():
        remove(home / name)
        remove(work / name)
    skill = kit_root() / "plugins/nexkit/skills" / f"nexkit-{role}"
    require(skill.is_dir(), "The trusted role skill is absent")
    if (data / "trusted-skills").is_dir():
        require(
            (data / "trusted-skills" / skill.name / "SKILL.md").is_file(),
            "Reserved role skill is absent from the sealed snapshot",
        )
        shutil.copytree(data / "trusted-skills", work / ".agents/skills")
    else:
        shutil.copytree(skill, work / ".agents/skills" / skill.name)
    remove(home / "output")
    (home / "output").mkdir()
    if os.name != "nt":
        os.chown(home / "output", uid, gid)
    for integration in ADAPTERS.values():
        for relative, content in integration.home_configuration().items():
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if os.name != "nt":
                os.chown(path.parent, uid, gid)
                os.chown(path, uid, gid)


def main():
    if os.name == "nt":
        raise Blocked("Managed workspace sealing runs in Linux; Windows hosts use WSL 2")
    import pwd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "restore"))
    parser.add_argument("--role", choices=("request", "deliver", "review", "task"))
    parser.add_argument("--owner", type=int, default=0)
    parser.add_argument("--data-dir", type=Path, default=DATA)
    args = parser.parse_args()
    require(os.geteuid() == 0, "Workspace sealing requires the runner administrator")
    if args.operation == "snapshot":
        snapshot(data=args.data_dir)
        return
    require(args.role, "Provide the role to restore")
    unchanged(data=args.data_dir)
    # /home is root-owned. Sealing this unchanged HOME prevents another rename
    # of work while the remaining setup processes are being terminated.
    os.chown(HOME, 0, 0, follow_symlinks=False)
    os.chmod(HOME, 0o755)
    result = subprocess.run(["pkill", "-KILL", "-u", "nexkit-agent"], check=False)
    require(result.returncode in (0, 1), "Could not terminate dependency setup processes")
    user = pwd.getpwnam("nexkit-agent")
    restore(data=args.data_dir, uid=user.pw_uid, gid=user.pw_gid, role=args.role)
    os.chown(HOME, args.owner, -1, follow_symlinks=False)


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Workspace setup blocked: {exc}") from exc

"""Preserve trusted workspace metadata across untrusted dependency setup."""

import argparse
import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from .common import Blocked, kit_root, read_json
from .policy import require

HOME = Path("/home/nexkit-agent")
DATA = Path("/tmp/nexkit")
CONTROLS = (".codex", ".agents", ".claude", ".gemini", ".cursor", ".agent")


def directory_identity(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), f"Expected an unchanged directory: {path}")
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
            not any(path.is_symlink() for path in (skills, *skills.rglob("*"))),
            "Trusted skills cannot contain symlinks",
        )
        shutil.copytree(skills, data / "trusted-skills")
    (data / "setup-directories.json").write_text(json.dumps(identities))
    for path in (data / "trusted.git", *(data / "trusted.git").rglob("*")):
        if not path.is_symlink():
            os.chmod(path, path.stat().st_mode & ~0o022)
    for path in (data / "trusted-skills").rglob("*"):
        os.chmod(path, path.stat().st_mode & ~0o022)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def restore(home=HOME, data=DATA, *, uid, gid, role):
    # The caller has killed setup processes and sealed HOME against renames.
    # Check again before any privileged traversal into the writable workspace.
    unchanged(home, data)
    work = home / "work"
    remove(work / ".git")
    shutil.copytree(data / "trusted.git", work / ".git", symlinks=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    for key, value in (("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false")):
        subprocess.run(
            ["git", "-c", f"safe.directory={work}", "-C", str(work), "config", key, value],
            env=env,
            check=True,
        )
    for path in (work / ".git", *(work / ".git").rglob("*")):
        os.chown(path, uid, gid, follow_symlinks=False)
    for name in (".bashrc", ".bash_profile", ".bash_login", ".profile", ".gitconfig"):
        remove(home / name)
    for name in CONTROLS:
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
    os.chown(home / "output", uid, gid)
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(
        'project_doc_max_bytes = 0\napproval_policy = "never"\n'
    )
    for path in (home / ".codex", home / ".codex/config.toml"):
        os.chown(path, uid, gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "restore"))
    parser.add_argument("--role", choices=("request", "deliver", "review", "task"))
    parser.add_argument("--owner", type=int, default=0)
    args = parser.parse_args()
    require(os.geteuid() == 0, "Workspace sealing requires the runner administrator")
    if args.operation == "snapshot":
        snapshot()
        return
    require(args.role, "Provide the role to restore")
    unchanged()
    # /home is root-owned. Sealing this unchanged HOME prevents another rename
    # of work while the remaining setup processes are being terminated.
    os.chown(HOME, 0, 0, follow_symlinks=False)
    os.chmod(HOME, 0o755)
    result = subprocess.run(["pkill", "-KILL", "-u", "nexkit-agent"], check=False)
    require(result.returncode in (0, 1), "Could not terminate dependency setup processes")
    user = pwd.getpwnam("nexkit-agent")
    restore(uid=user.pw_uid, gid=user.pw_gid, role=args.role)
    os.chown(HOME, args.owner, -1, follow_symlinks=False)


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Workspace setup blocked: {exc}") from exc

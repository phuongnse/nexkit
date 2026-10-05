"""Small wrapper around the git command line."""

from __future__ import annotations

import subprocess

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitError(RuntimeError):
    pass


def git(repo, *args, binary=False, input=None):
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input,
        capture_output=True,
        text=not binary,
    )
    if proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode(errors="replace")
        raise GitError(f"git {' '.join(args[:3])} failed: {err.strip()[-1000:]}")
    return proc.stdout

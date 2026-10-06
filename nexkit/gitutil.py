"""Small wrapper around the git command line."""

from __future__ import annotations

import subprocess

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitError(RuntimeError):
    pass


def _run(repo, args, binary=False, input=None):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input,
        capture_output=True,
        text=not binary,
    )


def _error(args, proc, binary=False):
    err = proc.stderr if not binary else proc.stderr.decode(errors="replace")
    return GitError(f"git {' '.join(args[:3])} failed: {err.strip()[-1000:]}")


def git(repo, *args, binary=False, input=None, ok=(0,)):
    """Run git and return its output. Exit codes outside `ok` raise GitError."""
    proc = _run(repo, args, binary, input)
    if proc.returncode not in ok:
        raise _error(args, proc, binary)
    return proc.stdout


def names(output):
    """File names from a `-z` listing."""
    return [name for name in output.split("\0") if name]


def merge_tree(repo, ours, theirs):
    """Merge two commits without touching the checkout. Return (tree, conflicted files).

    In conflicted files the tree holds the content with conflict markers, as `git merge`
    would leave it in the working tree."""
    out = git(
        repo,
        "merge-tree",
        "--write-tree",
        "-z",
        "--name-only",
        "--no-messages",
        ours,
        theirs,
        ok=(0, 1),
    )
    tree, *files = names(out)
    return tree, files


def is_ancestor(repo, commit, of):
    args = ("merge-base", "--is-ancestor", commit, of)
    proc = _run(repo, args)
    if proc.returncode not in (0, 1):
        raise _error(args, proc)
    return proc.returncode == 0

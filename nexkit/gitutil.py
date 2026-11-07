"""Small wrapper around the git command line."""

from __future__ import annotations

import re
import subprocess
import sys
import time


class GitError(RuntimeError):
    # Whether git failed for a reason another try later may pass (see `git_retrying`).
    transient = False


# What git says when GitHub or the network failed for a while rather than refused: a
# server error or rate limit (`HTTP 503`, `The requested URL returned error: 502`), or a
# connection that could not be made or dropped.
TRANSIENT = re.compile(
    r"(?:HTTP|returned error:?)\s*(?:5\d\d|429)\b|Internal Server Error|Bad Gateway"
    r"|Service Unavailable|Gateway Time-?out|rate limit|Could not resolve host"
    r"|Failed to connect|Connection (?:reset|refused|timed out)|Operation timed out"
    r"|RPC failed|early EOF|unexpected disconnect|remote end hung up|SSL_ERROR|gnutls",
    re.I,
)
# What git says when GitHub refused the command, which another try cannot change: a
# protected branch, a push that is not a fast-forward, a missing permission.
REFUSED = re.compile(
    r"\[(?:remote )?rejected\]|protected branch|non-fast-forward|fetch first"
    r"|Permission to \S+ denied|permission denied|\bGH0\d\d\b"
    r"|(?:HTTP|returned error:?)\s*4(?!29)\d\d\b",
    re.I,
)


def _run(repo, args, binary=False, input=None):
    # Text output is UTF-8; other bytes, such as a diff of a cp1250 file, become U+FFFD.
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input,
        capture_output=True,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
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


def transient_failure(stderr):
    """Whether git's error output says GitHub or the network failed for a while, and
    nothing says that GitHub refused the command. GitHub may turn a push down with a server
    error as its reason: `! [remote rejected] HEAD -> x (Internal Server Error)`."""
    lines = stderr.splitlines()
    refusals = [
        line for line in lines if not ("[remote rejected]" in line and TRANSIENT.search(line))
    ]
    return bool(TRANSIENT.search(stderr)) and not REFUSED.search("\n".join(refusals))


def git_retrying(repo, *args, waits):
    """`git` for a command that talks to GitHub, such as a push or a fetch. When it fails on
    a server error, a rate limit or a dropped connection, it runs again after each of
    `waits` seconds (see `publish.WAITS`). A refusal fails at once. When every try failed
    for such a reason, the GitError is `transient`."""
    for wait in (*waits, None):
        proc = _run(repo, args)
        if proc.returncode == 0:
            return proc.stdout
        error = _error(args, proc)
        error.transient = transient_failure(proc.stderr)
        if not error.transient or wait is None:
            raise error
        print(f"warning: {error} Trying again in {wait} seconds.", file=sys.stderr, flush=True)
        time.sleep(wait)
    raise AssertionError("unreachable")


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


def has_commit(repo, sha):
    """Whether `sha` is a full commit id that the checkout has."""
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return False
    return _run(repo, ("cat-file", "-e", f"{sha}^{{commit}}")).returncode == 0

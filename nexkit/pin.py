"""The release a repository's workflow pins. `nexkit status` and `nexkit doctor` judge a
repository by the rules of a release, so in a repository whose workflow pins another release
than this one they run that release, as corepack runs the package manager a project names:
fetched once into the user's cache from NexKit's own repository, then reused.

Only a release tag or a branch is fetched, each by its full ref, since only the
repository's maintainers write those: a pull request's ref or a commit can be anyone's. And
another repository's code runs only when the person names that repository in
`NEXKIT_KIT_REPOSITORY`: otherwise cloning a repository and running `nexkit status` would
run whatever code its workflow points at."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .scaffold import KIT_REPOSITORY, WORKFLOW_PATH, kit_pin

# The commands that judge a repository, and so run the release its workflow pins.
COMMANDS = ("status", "doctor")
FLAG = "--own-release"
# Set to anything but empty or 0: run this release. The pinned release runs with it set, so
# that it never looks for another release itself.
OWN = "NEXKIT_OWN_RELEASE"
# The NexKit repository other than NexKit's own whose code the person lets run here.
TRUST = "NEXKIT_KIT_REPOSITORY"
RELEASE = re.compile(r"v\d+\.\d+\.\d+")
# A branch name, fetched as refs/heads/<name>.
BRANCH = re.compile(r"(?!refs/)[A-Za-z0-9_][A-Za-z0-9._/-]*")
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
# Variables that would point git at another repository than the folder it works in.
GIT_LOCATION = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


class PinError(Exception):
    pass


def source(repository):
    """Where the code of a NexKit repository is fetched from."""
    return f"https://github.com/{repository}"


def cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    )
    return Path(base or Path.home() / ".cache") / "nexkit" / "releases"


def _repo_option(argv):
    """The value of the last `--repo` in `argv`, as argparse reads it, or None."""
    found = None
    for i, arg in enumerate(argv):
        if arg == "--repo" and i + 1 < len(argv):
            found = argv[i + 1]
        elif arg.startswith("--repo="):
            found = arg.split("=", 1)[1]
    return found


def _own(argv):
    # argparse takes any unambiguous prefix of the flag, and no other option of `status`
    # or `doctor` starts with --o.
    flag = any(len(arg) >= 3 and FLAG.startswith(arg) for arg in argv[1:])
    return flag or os.environ.get(OWN, "") not in ("", "0")


def _root(argv):
    """The checkout whose workflow counts: `--repo`, or the one around the current
    directory, as `nexkit status` finds its repository from anywhere in it."""
    given = _repo_option(argv)
    if given is not None:
        return Path(given)
    here = Path.cwd()
    return next((d for d in (here, *here.parents) if (d / ".git").exists()), here)


def _branch(ref):
    # A commit, which anyone's pull request can add, is never taken for a branch's name.
    return bool(BRANCH.fullmatch(ref)) and not (
        re.fullmatch(r"[0-9a-f]{40}", ref)
        or ".." in ref
        or "//" in ref
        or ref.endswith(("/", ".lock", "."))
    )


def pinned(argv):
    """(repository, ref) of the release to run for `argv` in place of this one, or None
    when this release runs: for another command than `status` and `doctor`, with the
    opt-out, outside a repository whose workflow calls NexKit's pipeline, when the
    workflow pins NexKit's own repository at this release, and when it pins neither a
    release tag nor a branch, which it says."""
    if not argv or argv[0] not in COMMANDS or _own(argv):
        return None
    try:
        text = (_root(argv) / WORKFLOW_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pin = kit_pin(text)
    if not pin:
        return None
    repository, ref = pin
    own = repository.lower() == KIT_REPOSITORY.lower()
    if own and ref == f"v{__version__}":
        return None
    if not REPOSITORY.fullmatch(repository) or not (RELEASE.fullmatch(ref) or _branch(ref)):
        print(
            f"nexkit: {WORKFLOW_PATH} pins NexKit as {repository}@{ref}, which is no release "
            f"tag or branch, so this NexKit {__version__} runs",
            file=sys.stderr,
        )
        return None
    if not own and os.environ.get(TRUST, "").strip().lower() != repository.lower():
        raise PinError(
            f"{WORKFLOW_PATH} runs NexKit's code from {repository} at {ref}, not from "
            f"{KIT_REPOSITORY}. To run {repository}'s code here, set {TRUST}={repository}; "
            f"to run this NexKit {__version__} instead, pass {FLAG}"
        )
    return repository, ref


def _git(*args, cwd=None):
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCATION}
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, errors="replace"
    )
    if proc.returncode:
        raise PinError(proc.stderr.strip()[-500:] or f"git {args[0]} failed")
    return proc.stdout


def _remove(path):
    """Remove a folder, also git's read-only objects on Windows."""

    def writable(function, name, _exc):
        try:
            os.chmod(name, stat.S_IWRITE)
            function(name)
        except OSError:
            pass

    shutil.rmtree(path, onexc=writable)


def _fetch(repository, what, folder):
    """Check out `what` of `repository` into `folder`, which does not exist yet."""
    folder.mkdir(parents=True)
    _git("init", "-q", str(folder))
    _git("fetch", "-q", "--depth", "1", source(repository), what, cwd=folder)
    _git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=folder)
    if not (folder / "bin" / "nexkit").is_file():
        raise PinError("it has no bin/nexkit")


def _cached(repository, what, folder):
    """`folder`, holding `what` of `repository`: fetched now unless it already does. A
    copy appears whole or not at all, also when runs fetch it at once."""
    if (folder / "bin" / "nexkit").is_file():
        return False
    folder.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=".fetch-", dir=folder.parent))
    try:
        _fetch(repository, what, partial / "kit")
        if folder.exists():
            # A broken copy, or one another run just fetched.
            if (folder / "bin" / "nexkit").is_file():
                return True
            _remove(folder)
        try:
            (partial / "kit").rename(folder)
        except OSError:
            if not (folder / "bin" / "nexkit").is_file():
                raise
    finally:
        _remove(partial)
    return True


def _release(repository, ref):
    """The folder in the cache that holds `repository` at `ref`. A release tag is fetched
    once. A branch is looked up each time and fetched again when it moved; its folder is
    named by its commit, so a run of an older one is left alone until the next fetch."""
    base = cache_root() / repository
    try:
        if RELEASE.fullmatch(ref):
            folder = base / ref
            what = f"refs/tags/{ref}"
        else:
            listed = _git("ls-remote", source(repository), f"refs/heads/{ref}").split()
            if not listed:
                raise PinError(f"{repository} has no tag or branch {ref}")
            branch = base / "heads" / ref.replace("/", "~")
            folder, what = branch / listed[0], listed[0]
        if _cached(repository, what, folder):
            print(f"nexkit: fetched NexKit {ref} from {repository}", file=sys.stderr)
            if not RELEASE.fullmatch(ref):
                for old in branch.iterdir():
                    if old != folder and not old.name.startswith("."):
                        _remove(old)
    except (PinError, OSError) as exc:
        raise PinError(
            f"cannot fetch NexKit {ref} from {repository} ({FLAG} runs this NexKit "
            f"{__version__}): {exc}"
        ) from None
    return folder


def run(argv):
    """Run the release the repository pins with `argv`, and return its exit code, or None
    when this release runs."""
    try:
        pin = pinned(argv)
        if not pin:
            return None
        repository, ref = pin
        folder = _release(repository, ref)
    except PinError as exc:
        print(f"nexkit: {exc}", file=sys.stderr)
        return 2
    print(
        f"nexkit: {WORKFLOW_PATH} pins NexKit {ref}, so it runs from {folder} "
        f"({FLAG} runs this NexKit {__version__})",
        file=sys.stderr,
    )
    env = {**os.environ, OWN: "1"}
    try:
        command = [sys.executable, str(folder / "bin" / "nexkit"), *argv]
        return subprocess.run(command, env=env).returncode
    except KeyboardInterrupt:
        return 130

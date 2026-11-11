"""The release a repository's workflow pins. `nexkit status` and `nexkit doctor` judge a
repository by the rules of a release, so in a repository whose workflow pins another release
than this one they run that release, as corepack runs the package manager a project names:
fetched once into the user's cache from NexKit's own repository, then reused.

Another repository's code runs only when the person names that repository in
`NEXKIT_KIT_REPOSITORY`: otherwise cloning a repository and running `nexkit status` would run
whatever code its workflow points at."""

from __future__ import annotations

import os
import re
import shutil
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
# Refs that never move, so a fetched copy stays right. A branch is fetched again each time.
FIXED = re.compile(r"v\d+\.\d+\.\d+|[0-9a-f]{40}")
REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")


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


def _option(argv, name):
    """The value of `name` in `argv`, as `name VALUE` or `name=VALUE`, or None."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def _own(argv):
    return FLAG in argv or os.environ.get(OWN, "") not in ("", "0")


def pinned(argv):
    """(repository, ref) of the release to run for `argv` in place of this one, or None
    when this release runs: for another command than `status` and `doctor`, with the
    opt-out, outside a repository whose workflow calls NexKit's pipeline, and when the
    workflow pins NexKit's own repository at this release."""
    if not argv or argv[0] not in COMMANDS or _own(argv):
        return None
    workflow = Path(_option(argv, "--repo") or ".") / WORKFLOW_PATH
    try:
        pin = kit_pin(workflow.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not pin:
        return None
    repository, ref = pin
    own = repository.lower() == KIT_REPOSITORY.lower()
    if own and ref == f"v{__version__}":
        return None
    if not REPOSITORY.fullmatch(repository) or not REF.fullmatch(ref) or ".." in ref:
        raise PinError(f"{WORKFLOW_PATH} pins NexKit as {repository}@{ref}, which is not a ref")
    if not own and os.environ.get(TRUST, "").strip().lower() != repository.lower():
        raise PinError(
            f"{WORKFLOW_PATH} runs NexKit's code from {repository} at {ref}, not from "
            f"{KIT_REPOSITORY}. To run {repository}'s code here, set {TRUST}={repository}; "
            f"to run this NexKit {__version__} instead, pass {FLAG}"
        )
    return repository, ref


def _git(*args, cwd=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, errors="replace"
    )
    if proc.returncode:
        raise PinError(proc.stderr.strip()[-500:] or f"git {args[0]} failed")


def _fetch(repository, ref, folder):
    """Check out `repository` at `ref` into `folder`, which does not exist yet."""
    folder.mkdir(parents=True)
    try:
        _git("init", "-q", str(folder))
        _git("fetch", "-q", "--depth", "1", source(repository), ref, cwd=folder)
        _git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=folder)
    except (PinError, OSError) as exc:
        raise PinError(
            f"cannot fetch NexKit {ref} from {repository} ({FLAG} runs this NexKit "
            f"{__version__}): {exc}"
        ) from None
    if not (folder / "bin" / "nexkit").is_file():
        raise PinError(f"NexKit {ref} from {repository} has no bin/nexkit")


def _release(repository, ref, scratch):
    """The folder that holds `repository` at `ref`: the cached copy of a ref that never
    moves, fetched the first time; for a branch, a new copy in `scratch`."""
    if not FIXED.fullmatch(ref):
        print(f"nexkit: fetching NexKit {ref} from {repository}", file=sys.stderr)
        folder = Path(scratch) / "kit"
        _fetch(repository, ref, folder)
        return folder
    folder = cache_root() / repository / ref
    if (folder / "bin" / "nexkit").is_file():
        return folder
    print(f"nexkit: fetching NexKit {ref} from {repository} once", file=sys.stderr)
    folder.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(tempfile.mkdtemp(prefix=f".{ref}-", dir=folder.parent))
    try:
        _fetch(repository, ref, partial / "kit")
        if folder.exists():
            # A broken copy, or one another run just fetched.
            if (folder / "bin" / "nexkit").is_file():
                return folder
            shutil.rmtree(folder)
        try:
            (partial / "kit").rename(folder)
        except OSError:
            if not (folder / "bin" / "nexkit").is_file():
                raise
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return folder


def run(argv):
    """Run the release the repository pins with `argv`, and return its exit code, or None
    when this release runs."""
    try:
        pin = pinned(argv)
        if not pin:
            return None
        repository, ref = pin
        with tempfile.TemporaryDirectory(prefix="nexkit-") as scratch:
            folder = _release(repository, ref, scratch)
            print(
                f"nexkit: {WORKFLOW_PATH} pins NexKit {ref}, so it runs from {folder} "
                f"({FLAG} runs this NexKit {__version__})",
                file=sys.stderr,
            )
            env = {**os.environ, OWN: "1"}
            command = [sys.executable, str(folder / "bin" / "nexkit"), *argv]
            try:
                return subprocess.run(command, env=env).returncode
            except KeyboardInterrupt:
                return 130
    except PinError as exc:
        print(f"nexkit: {exc}", file=sys.stderr)
        return 2

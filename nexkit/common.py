"""Small shared file/process helpers. Never run GitHub text through a shell."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


class Blocked(RuntimeError):
    """A condition requiring a correction, not a success or an infinite retry."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def short_summary(value, limit=240):
    """Keep progress text on one bounded, printable line."""
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    if len(text) <= limit:
        return text
    return text[:limit] if limit < 4 else text[: limit - 3].rstrip() + "..."


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Blocked(f"Cannot read JSON {path}: {exc}") from exc


def read_regular_bytes(path, maximum=512000):
    """Collect bounded data only after isolated consumer processes have ended."""
    path = Path(path).absolute()
    try:
        if os.name == "nt":
            from .windows_filesystem import read_file

            data = read_file(path, maximum)
        else:
            if any(is_link(parent) for parent in path.parents):
                raise Blocked("Result path has a linked parent")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                    raise Blocked("Expected a bounded regular file without links or reparse points")
                data = source.read(maximum + 1)
                if len(data) > maximum:
                    raise Blocked("File exceeds the accepted size limit")
        return data
    except (OSError, ValueError) as exc:
        raise Blocked(f"Cannot collect regular data {path}: {exc}") from exc


def read_regular_json(path, maximum=512000):
    """Read JSON through the same bounded, non-link file boundary."""
    try:
        return json.loads(read_regular_bytes(path, maximum))
    except ValueError as exc:
        raise Blocked(f"Cannot collect regular JSON {path}: {exc}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as out:
        json.dump(value, out, ensure_ascii=False, sort_keys=True, indent=2)
        out.write("\n")
        temporary = out.name
    os.replace(temporary, path)


def run(argv, *, cwd=None, data=None, timeout=60, check=True, env=None):
    try:
        result = subprocess.run(
            [str(x) for x in argv],
            cwd=cwd,
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Blocked(f"Command unavailable or timed out: {argv[0]} ({exc})") from exc
    if check and result.returncode:
        # Do not echo arbitrary command arguments (they may contain credentials).
        raise Blocked(f"{argv[0]} failed ({result.returncode}): {result.stderr[-3000:]}")
    return result


def safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise Blocked("Invalid repository path")
    parts = value.split("/")
    if any(
        p in ("", ".", "..")
        or p.casefold() == ".git"
        or p.endswith((".", " "))
        or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
        or re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", p)
        for p in parts
    ) or value.startswith("/"):
        raise Blocked(f"Unsafe repository path: {value!r}")
    return value


def is_link(path):
    """Include Windows junctions and other reparse points, not just symlinks."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def consumer_path(root, name):
    """A consumer-owned file must never resolve through a symlink outside it."""
    root = Path(root).resolve()
    path = root / safe_path(name)
    current = root
    for part in Path(name).parts:
        current = current / part
        if is_link(current):
            raise Blocked(f"Consumer path uses a symlink: {name}")
    if not path.resolve().is_relative_to(root):
        raise Blocked(f"Consumer path escapes the repository: {name}")
    return path


def kit_root() -> Path:
    return Path(__file__).resolve().parent.parent

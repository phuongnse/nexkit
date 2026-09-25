"""Small shared file/process helpers. Never run GitHub text through a shell."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


class Blocked(RuntimeError):
    """A condition requiring a correction, not a success or an infinite retry."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Blocked(f"Cannot read JSON {path}: {exc}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
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
            text=True,
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
    if any(p in ("", ".", "..", ".git") for p in parts) or value.startswith("/"):
        raise Blocked(f"Unsafe repository path: {value!r}")
    return value


def consumer_path(root, name):
    """A consumer-owned file must never resolve through a symlink outside it."""
    root = Path(root).resolve()
    path = root / safe_path(name)
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            raise Blocked(f"Consumer path uses a symlink: {name}")
    if not path.resolve().is_relative_to(root):
        raise Blocked(f"Consumer path escapes the repository: {name}")
    return path


def kit_root() -> Path:
    return Path(__file__).resolve().parent.parent

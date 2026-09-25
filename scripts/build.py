#!/usr/bin/env python3
"""Build a reproducible local installation candidate, without publishing."""

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit import __version__  # noqa: E402 -- allow running the unpacked source archive directly


def main():
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    name = f"nexkit-{__version__}"
    includes = (
        "bin",
        "nexkit",
        "plugins",
        "schemas",
        "actions",
        "runner",
        ".github/workflows",
        ".agents/plugins",
        "docs",
        "scripts",
        "README.md",
        "AGENTS.md",
        "pyproject.toml",
        "requirements-dev.txt",
        "tests",
    )
    paths = []
    for include in includes:
        path = ROOT / include
        paths += list(path.rglob("*")) if path.is_dir() else [path]
    paths = sorted(
        {
            p
            for p in paths
            if p.is_file()
            and not p.is_symlink()
            and "__pycache__" not in p.parts
            and p.suffix != ".pyc"
        }
    )
    manifest = {
        "version": __version__,
        "files": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths
        },
    }
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    manifest["source_commit"] = git.stdout.strip() if git.returncode == 0 else None
    manifest["source_dirty"] = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        ).stdout
    )
    archive = destination / (name + ".tar.gz")
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
    ):
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for path in paths:
                data = path.read_bytes()
                info = tarfile.TarInfo(f"{name}/{path.relative_to(ROOT)}")
                info.size = len(data)
                info.mode = 0o755 if path.parent == ROOT / "bin" else 0o644
                tar.addfile(info, io.BytesIO(data))
            data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo(name + "/candidate.json")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{checksum}  {archive.name}\n")
    (destination / "candidate.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "archive": str(archive),
                "sha256": checksum,
                "version": __version__,
                "published": False,
                "source_dirty": manifest["source_dirty"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

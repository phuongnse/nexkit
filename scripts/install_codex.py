#!/usr/bin/env python3
"""Install a selected, checksum-verified official Codex package."""

import argparse
import hashlib
import json
import os
import platform
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.adapters.codex import version as cli_version  # noqa: E402
from nexkit.policy import HASH  # noqa: E402
from nexkit.policy import VERSION as RELEASE  # noqa: E402

# CI's reproducible default, not a compatibility restriction on consumers.
VERSION = "0.156.1"
PACKAGES = {
    "win32": (
        "x86_64-pc-windows-msvc",
        "a2e017db9807e6a2269a26fea0e1d9546469cef4d472a33016bc9f3ad7d3b733",
    ),
    "linux": (
        "x86_64-unknown-linux-musl",
        "8b711520beddf385467b8da4d2c93736637c6ba1e46811cf0d8606b7c490b6f6",
    ),
}


def install(destination, *, version=VERSION, sha256=None):
    if sys.platform not in PACKAGES or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("The package verifier supports Linux and Windows x64")
    if not isinstance(version, str) or not RELEASE.fullmatch(version):
        raise RuntimeError("Select an exact Codex release version")
    target, default_checksum = PACKAGES[sys.platform]
    checksum = sha256 or (default_checksum if version == VERSION else None)
    if not isinstance(checksum, str) or not HASH.fullmatch(checksum):
        raise RuntimeError("Provide the official package SHA-256 when selecting another version")
    package = f"codex-package-{target}.tar.gz"
    url = f"https://github.com/openai/codex/releases/download/rust-v{version}/{package}"
    destination = Path(destination).absolute()
    if destination.exists():
        raise RuntimeError("Choose a fresh tool directory; existing installations are not replaced")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nexkit-download-") as temporary:
        archive = Path(temporary) / package
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        with archive.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != checksum:
                raise RuntimeError("The official Codex package checksum does not match the pin")
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(destination, filter="data")
    candidates = [
        path
        for path in destination.rglob("*")
        if path.is_file()
        and path.name in ("codex", "codex.exe", "codex-" + target, "codex-" + target + ".exe")
    ]
    if len(candidates) != 1:
        raise RuntimeError("The selected package must contain exactly one native Codex executable")
    executable = candidates[0]
    if cli_version(executable) != version:
        raise RuntimeError("The installed Codex version differs from the selected release")
    return {"codex": str(executable), "version": version, "sha256": checksum}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument(
        "--version", default=VERSION, help="Exact CLI release; defaults to CI's pin"
    )
    parser.add_argument("--sha256", help="Official package SHA-256 for the selected OS and release")
    args = parser.parse_args()
    result = install(args.destination, version=args.version, sha256=args.sha256)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as output:
            output.write("NEXKIT_CODEX_PATH=" + result["codex"] + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()

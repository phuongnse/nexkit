"""Validate exact native artifact inputs and their downloaded JSON data files."""

import argparse
import os
import re
from pathlib import Path

from .common import Blocked
from .policy import require


def identifiers(value, *, multiple=False, optional=False):
    ids = [item.strip() for item in value.split(",") if item.strip()]
    require(optional or ids, "An exact artifact ID is required")
    require(all(re.fullmatch(r"[1-9][0-9]*", item) for item in ids), "Invalid artifact ID")
    require(len(ids) == len(set(ids)), "Duplicate artifact IDs")
    require(multiple or len(ids) <= 1, "Expected exactly one input artifact")
    return ids


def downloaded(root, count):
    root = Path(root)
    paths = list(root.rglob("*")) if root.exists() else []
    require(
        not any(path.is_symlink() for path in [root, *paths]), "Artifact input contains a symlink"
    )
    files = [path for path in paths if path.is_file()]
    require(
        len(files) == count and all(path.suffix == ".json" for path in files),
        "Missing or unexpected artifact data files",
    )
    if count > 1:
        require(
            len({path.parent for path in files}) == count
            and all(path.parent.parent == root for path in files),
            "Each artifact must keep its own data file directory",
        )
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inputs", "downloaded"))
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    ids = identifiers(
        os.environ["NEXKIT_ARTIFACT_IDS"],
        multiple=os.environ.get("NEXKIT_MULTIPLE") == "true",
        optional=os.environ.get("NEXKIT_OPTIONAL") == "true",
    )
    if args.operation == "inputs":
        require(not Path(args.destination).exists(), "Artifact destination must be fresh")
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"ids={','.join(ids)}\ncount={len(ids)}\n")
    else:
        downloaded(args.destination, len(ids))


if __name__ == "__main__":
    try:
        main()
    except Blocked as exc:
        raise SystemExit(f"Artifact input blocked: {exc}") from exc

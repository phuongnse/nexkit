#!/usr/bin/env python3
"""Show or update the base image of the engine's image, pinned by the digest of its index.

    image_digest.py current    print the pinned image, such as python:3.12-slim@sha256:...
    image_digest.py latest     print the image of the pinned tag's current digest
    image_digest.py update     pin the tag's current digest if it moved; print the new image

The pin is PYTHON_IMAGE in nexkit/__init__.py, which release.yml builds the Dockerfile on.
update-image.yml runs `update` every week and proposes the change in a pull request. The
digest is read from Docker Hub's registry, as `docker pull` reads it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "nexkit" / "__init__.py"
# The pin as nexkit/__init__.py writes it: the tag, then the digest, on a line of its own.
PIN = re.compile(
    r'^(PYTHON_IMAGE = \(\n    ")([^"@\s]+:[^"@\s]+)@(sha256:[0-9a-f]{64})("\n\))$', re.M
)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REGISTRY = "https://registry-1.docker.io"
AUTH = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{}:pull"
# The digest of the index, which names one image for each platform, as the pin holds it.
INDEX = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)


def _found(init):
    found = PIN.search(init.read_text())
    if not found:
        raise ValueError(f"No PYTHON_IMAGE pinned by digest in {init}")
    return found


def current(init=INIT):
    """The pinned image, as (tag, digest), such as ("python:3.12-slim", "sha256:...")."""
    found = _found(init)
    return found.group(2), found.group(3)


def _repository(name):
    # Docker Hub keeps its official images, such as python, under library/.
    return name if "/" in name else f"library/{name}"


def read_digest(tag):
    """The digest of the index that `tag`, such as python:3.12-slim, names on Docker Hub."""
    name, _, reference = tag.rpartition(":")
    repository = _repository(name)
    with urlopen(AUTH.format(quote(repository)), timeout=30) as response:
        token = json.load(response)["token"]
    request = Request(
        f"{REGISTRY}/v2/{repository}/manifests/{quote(reference)}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": INDEX},
    )
    with urlopen(request, timeout=30) as response:
        return response.headers.get("Docker-Content-Digest", "")


def latest(init=INIT, lookup=read_digest):
    """The pinned tag's current digest, as (tag, digest)."""
    tag, _ = current(init)
    digest = lookup(tag)
    if not DIGEST.match(digest or ""):
        raise ValueError(f"Unexpected digest of {tag}: {digest!r}")
    return tag, digest


def update(init=INIT, lookup=read_digest):
    """Pin the tag's current digest when it moved. Return the new image, or None when the pin
    is already the current digest."""
    tag, digest = latest(init, lookup)
    if digest == current(init)[1]:
        return None
    text = init.read_text()
    init.write_text(PIN.sub(lambda m: f"{m[1]}{tag}@{digest}{m[4]}", text, count=1))
    return f"{tag}@{digest}"


def main(argv):
    command = argv[1] if len(argv) > 1 else "current"
    if len(argv) > 2:
        command = ""
    if command == "current":
        print("@".join(current()))
    elif command == "latest":
        print("@".join(latest()))
    elif command == "update":
        image = update()
        if image:
            print(image)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Select a published toolkit artifact while retaining exact accepted bindings."""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tarfile
import tempfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from .command_env import environment
from .common import (
    Blocked,
    consumer_path,
    digest,
    file_hash,
    read_json,
    read_regular_bytes,
    run,
    safe_path,
    write_json,
)
from .github import GitHub
from .kit_workflows import update_workflow
from .policy import HASH, SHA, VERSION, config, require
from .project import install

ARCHIVE_LIMIT = 32 * 1024 * 1024
EXPANDED_LIMIT = 128 * 1024 * 1024
MANIFEST_LIMIT = 2 * 1024 * 1024


class ArchiveStream:
    """Bound the complete tar stream, including extended headers and padding."""

    def __init__(self, source):
        self.source = source
        self.remaining = EXPANDED_LIMIT

    def read(self, size):
        data = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        require(self.remaining >= 0, "Release archive exceeds its expanded size bound")
        return data


def release_identity(gh, version):
    tag = "v" + version
    release = gh.api(f"{gh.root}/releases/tags/{quote(tag, safe='')}")
    require(
        release.get("tag_name") == tag and release.get("draft") is False,
        "Select an exact published release, not a draft",
    )
    ref = gh.api(f"{gh.root}/git/ref/tags/{quote(tag, safe='')}")["object"]
    for _ in range(8):
        require(SHA.fullmatch(ref.get("sha", "")), "Invalid release tag object")
        if ref.get("type") == "commit":
            break
        require(ref.get("type") == "tag", "Release tag does not identify a commit")
        ref = gh.api(f"{gh.root}/git/tags/{ref['sha']}")["object"]
    require(
        ref.get("type") == "commit" and SHA.fullmatch(ref.get("sha", "")),
        "Release tag nesting exceeds the supported bound",
    )
    names = {
        f"nexkit-{version}.tar.gz": ARCHIVE_LIMIT,
        "candidate.json": MANIFEST_LIMIT,
        "SHA256SUMS": 4096,
    }
    assets = {}
    for name, maximum in names.items():
        matches = [item for item in release.get("assets", []) if item.get("name") == name]
        require(len(matches) == 1, f"Release must contain exactly one {name}")
        asset = matches[0]
        require(
            type(asset.get("id")) is int
            and asset["id"] > 0
            and type(asset.get("size")) is int
            and 0 < asset["size"] <= maximum
            and asset.get("state") == "uploaded",
            f"Release asset is unavailable or exceeds its size bound: {name}",
        )
        assets[name] = {key: asset.get(key) for key in ("id", "size", "digest")}
    return {"id": release["id"], "tag": tag, "commit": ref["sha"], "assets": assets}


def download(gh, identity, destination):
    run(
        [
            "gh",
            "release",
            "download",
            identity["tag"],
            "--repo",
            gh.repository,
            "--dir",
            destination,
            *[part for name in identity["assets"] for part in ("--pattern", name)],
        ],
        timeout=120,
    )
    for name, asset in identity["assets"].items():
        data = read_regular_bytes(destination / name, asset["size"])
        require(len(data) == asset["size"], f"Downloaded release size differs: {name}")
        if asset["digest"] is not None:
            require(
                asset["digest"] == "sha256:" + hashlib.sha256(data).hexdigest(),
                f"GitHub release digest differs: {name}",
            )


def unpack(destination, version, commit):
    """Verify every archive member before extracting only bounded regular files."""
    archive = destination / f"nexkit-{version}.tar.gz"
    checksum = file_hash(archive)
    require(
        read_regular_bytes(destination / "SHA256SUMS", 4096)
        == f"{checksum}  {archive.name}\n".encode(),
        "The release archive differs from SHA256SUMS",
    )
    manifest_bytes = read_regular_bytes(destination / "candidate.json", MANIFEST_LIMIT)
    manifest = json.loads(manifest_bytes)
    require(
        manifest.get("version") == version
        and manifest.get("source_commit") == commit
        and manifest.get("source_dirty") is False,
        "Release manifest must identify the exact clean tagged source",
    )
    files = manifest.get("files")
    require(isinstance(files, dict) and 0 < len(files) < 10000, "Invalid release file manifest")
    for name, expected in files.items():
        safe_path(name)
        require(isinstance(expected, str) and HASH.fullmatch(expected), "Invalid file checksum")
    require("candidate.json" not in files, "The manifest cannot list itself")
    prefix = f"nexkit-{version}/"
    kit = destination / prefix.rstrip("/")
    kit.mkdir()
    seen, folded, total = set(), set(), 0
    try:
        with (
            gzip.open(archive, "rb") as expanded,
            tarfile.open(fileobj=ArchiveStream(expanded), mode="r|") as package,
        ):
            for member in package:
                require(
                    member.isfile() and member.name.startswith(prefix),
                    "Release archive must contain only regular files under its version directory",
                )
                name = safe_path(member.name.removeprefix(prefix))
                total += member.size
                require(
                    name not in seen
                    and name.casefold() not in folded
                    and name in set(files) | {"candidate.json"}
                    and 0 <= member.size <= ARCHIVE_LIMIT
                    and total <= EXPANDED_LIMIT,
                    "Unexpected, duplicate or oversized release archive entry",
                )
                seen.add(name)
                folded.add(name.casefold())
                data = package.extractfile(member).read(member.size + 1)
                require(len(data) == member.size, "Truncated release archive entry")
                if name == "candidate.json":
                    require(data == manifest_bytes, "Internal and external manifests differ")
                else:
                    require(
                        hashlib.sha256(data).hexdigest() == files[name],
                        f"Packaged file differs: {name}",
                    )
                path = kit / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Blocked(f"Cannot unpack verified release: {exc}") from exc
    require(seen == set(files) | {"candidate.json"}, "Release archive entries differ from manifest")
    require((kit / "plugins/nexkit/skills").is_dir(), "Release is missing project skills")
    return kit


def validate_selected(kit, cfg, destination):
    """Ask the selected trusted toolkit to validate configuration without consumer execution."""
    proposal = destination / "project.json"
    write_json(proposal, cfg)
    home = destination / "validation-home"
    home.mkdir()
    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); "
        "from nexkit import __version__; from nexkit.policy import config; "
        "cfg = config(json.load(open(sys.argv[2], encoding='utf-8'))); "
        "assert __version__ == cfg['kit']['version'], 'Packaged toolkit version differs'"
    )
    run(
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", script, kit, proposal],
        cwd=home,
        env=environment(home),
    )


def use(root, version, *, apply=True, gh=None):
    version = version.removeprefix("v")
    require(VERSION.fullmatch(version), "Choose an exact version such as 1.1.0 or 1.0.0")
    root = Path(root).resolve()
    cfg = config(read_json(consumer_path(root, ".nexkit/project.json")))
    original = deepcopy(cfg)
    old = deepcopy(cfg["kit"])
    ledger_path = consumer_path(root, ".nexkit/installation.json")
    ledger = read_json(ledger_path) if ledger_path.exists() else {"files": {}}
    require(
        not ledger.get("project") or ledger["project"] == digest(cfg),
        "Installed configuration was edited; reconcile setup before selecting a release",
    )
    # Inspect all accepted files before network access. Preserve edited controls.
    accepted = {}
    for name, record in cfg["files"].items():
        content = read_regular_bytes(consumer_path(root, name), ARCHIVE_LIMIT)
        require(
            hashlib.sha256(content).hexdigest() == record["sha256"],
            f"Consumer edits preserved: {name}. Reconcile setup first",
        )
        accepted[name] = content
    gh = gh or GitHub(old["repository"])
    require(gh.repository == old["repository"], "Release repository differs from accepted toolkit")
    identity = release_identity(gh, version)
    with tempfile.TemporaryDirectory(prefix="nexkit-release-") as temporary:
        destination = Path(temporary)
        download(gh, identity, destination)
        kit = unpack(destination, version, identity["commit"])
        selected = {**old, "version": version, "ref": identity["commit"]}
        cfg["kit"] = selected
        bundle, consumer_updates = destination / "bundle", {}
        references = 0
        for name, content in accepted.items():
            updated = content
            if name.startswith(".github/workflows/"):
                updated, count = update_workflow(content, name, old, selected, kit)
                references += count
            cfg["files"][name]["sha256"] = hashlib.sha256(updated).hexdigest()
            if cfg["files"][name]["managed"]:
                path = bundle / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(updated)
            elif updated != content:
                consumer_updates[name] = updated
        require(
            references > 0,
            "No literal NexKit reusable jobs found; reconcile copied workflows through setup",
        )
        validate_selected(kit, cfg, destination)
        # Network and validation may take time. Bind apply to the same accepted inputs.
        require(
            release_identity(gh, version) == identity, "Release changed during version selection"
        )
        require(
            read_json(ledger_path) == ledger if ledger_path.exists() else ledger == {"files": {}},
            "Installation changed during version selection",
        )
        require(
            read_json(consumer_path(root, ".nexkit/project.json")) == original,
            "Project configuration changed during download",
        )
        for name, content in accepted.items():
            require(
                read_regular_bytes(consumer_path(root, name), ARCHIVE_LIMIT) == content,
                f"Accepted file changed during version selection: {name}",
            )
        result = install(
            root,
            cfg,
            ledger.get("hosts", ["codex"]),
            apply=apply,
            bundle=bundle,
            kit=kit,
            consumer_updates=consumer_updates,
        )
        result.update(
            previous=old["version"],
            ref=selected["ref"],
            release=f"https://github.com/{gh.repository}/releases/tag/{identity['tag']}",
            note="Commit the changes through your normal PR checks. Existing runs need fresh evidence after a kit change; provisioned runners and local plugin installations are separate.",
        )
        return result

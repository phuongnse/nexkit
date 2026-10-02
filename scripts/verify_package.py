#!/usr/bin/env python3
"""Verify the archive and native installation without login or model execution."""

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nexkit.adapters.codex import version as cli_version  # noqa: E402
from nexkit.command_env import environment  # noqa: E402


def invoke(argv, *, cwd, env, timeout=60):
    result = subprocess.run(
        list(map(str, argv)),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-3000:])
    return result.stdout.decode("utf-8")


def skills(binary, kit, env):
    """Use the pinned CLI's local app-server API; no thread or turn is created."""
    server = subprocess.Popen(
        [str(binary), "app-server"],
        cwd=kit,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    messages = queue.Queue()

    def reader():
        try:
            for line in server.stdout:
                messages.put(json.loads(line))
        except BaseException as exc:
            messages.put(exc)
        finally:
            messages.put(None)

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()

    def request(identifier, method, params):
        server.stdin.write(
            (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
        )
        server.stdin.flush()
        while True:
            message = messages.get(timeout=30)
            if message is None or isinstance(message, BaseException):
                raise RuntimeError("The native Codex metadata server stopped")
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message["result"]

    try:
        request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "nexkit_package_verification", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        result = request(2, "skills/list", {"cwds": [str(kit)], "forceReload": True})
        return {item["name"] for entry in result["data"] for item in entry["skills"]}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server.stdin.close()
        server.stdout.close()
        worker.join(timeout=5)


def verify(archive, *, source=None, codex=None):
    archive = Path(archive).resolve()
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    expected_checksum = f"{checksum}  {archive.name}\n".encode()
    if (archive.parent / "SHA256SUMS").read_bytes() != expected_checksum:
        raise RuntimeError("The archive does not match SHA256SUMS")
    manifest_bytes = (archive.parent / "candidate.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if source is not None and (
        manifest["source_commit"] != source or manifest["source_dirty"] is not False
    ):
        raise RuntimeError("Expected the exact clean source commit")
    prefix = f"nexkit-{manifest['version']}"
    with tempfile.TemporaryDirectory(prefix="nexkit package \u00e9 ") as temporary:
        root = Path(temporary)
        with tarfile.open(archive, mode="r:gz") as package:
            seen = set()
            for member in package.getmembers():
                name = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or name.is_absolute()
                    or ".." in name.parts
                    or "\\" in member.name
                    or ":" in member.name
                    or len(name.parts) < 2
                    or name.parts[0] != prefix
                    or member.name in seen
                ):
                    raise RuntimeError("Unexpected installation archive entry")
                seen.add(member.name)
            if seen != {prefix + "/" + name for name in manifest["files"]} | {
                prefix + "/candidate.json"
            }:
                raise RuntimeError("The archive entries differ from its manifest")
            package.extractall(root, filter="data")
        kit = root / prefix
        if (kit / "candidate.json").read_bytes() != manifest_bytes:
            raise RuntimeError("The internal and external manifests differ")
        for name, expected in manifest["files"].items():
            if hashlib.sha256((kit / name).read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Packaged file differs: {name}")
        home, consumer = root / "fresh home", root / "consumer project"
        home.mkdir()
        consumer.mkdir()
        (consumer / "README.md").write_text("Package verification fixture\n", encoding="utf-8")
        env = environment(home)
        env["CODEX_HOME"] = str(home / ".codex")
        (home / ".codex").mkdir()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        invoke(["git", "init", "-q", "--initial-branch=main"], cwd=consumer, env=env)
        launcher = (
            [str(kit / "bin/nexkit.cmd")]
            if os.name == "nt"
            else [sys.executable, "-I", str(kit / "bin/nexkit")]
        )
        if invoke([*launcher, "--version"], cwd=consumer, env=env).strip() != manifest["version"]:
            raise RuntimeError("The extracted native launcher returned another version")
        survey = json.loads(invoke([*launcher, "survey"], cwd=consumer, env=env))
        if "README.md" not in survey["context"]:
            raise RuntimeError("The extracted CLI did not read the consumer's context")
        # Real consumer commands, updates and removal; GitHub/model responses
        # remain simulated in these package regressions.
        driver = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('unittest',run_name='__main__')"
        invoke(
            [
                sys.executable,
                "-I",
                "-c",
                driver,
                kit,
                "tests.test_consumers",
                "tests.test_portability",
                "tests.test_versions",
                "-q",
            ],
            cwd=kit,
            env=env,
            timeout=120,
        )
        installed_skills = []
        installed_version = None
        if codex is not None:
            binary = Path(codex).resolve()
            installed_version = cli_version(binary, cwd=kit, env=env)
            expected = {
                "nexkit:" + path.parent.name
                for path in (kit / "plugins/nexkit/skills").glob("*/SKILL.md")
            }
            invoke([binary, "plugin", "marketplace", "add", kit], cwd=kit, env=env)
            for _ in range(2):
                invoke([binary, "plugin", "add", "nexkit@personal"], cwd=kit, env=env)
                actual = skills(binary, kit, env)
                if not expected <= actual:
                    raise RuntimeError("Native Codex did not discover every packaged skill")
                cache = home / ".codex/plugins/cache/personal/nexkit" / manifest["version"]
                for path in (kit / "plugins/nexkit").rglob("*"):
                    if (
                        path.is_file()
                        and path.read_bytes()
                        != (cache / path.relative_to(kit / "plugins/nexkit")).read_bytes()
                    ):
                        raise RuntimeError("An installed plugin file differs from the archive")
            invoke([binary, "plugin", "remove", "nexkit@personal"], cwd=kit, env=env)
            if expected & skills(binary, kit, env):
                raise RuntimeError("Native Codex still discovers the removed plugin")
            installed_skills = sorted(expected)
    return {
        "version": manifest["version"],
        "source_commit": manifest["source_commit"],
        "source_dirty": manifest["source_dirty"],
        "sha256": checksum,
        "manifest_files": len(manifest["files"]),
        "native": "windows" if os.name == "nt" else "linux",
        "native_codex_skills": installed_skills,
        "native_codex_version": installed_version,
        "model_calls": 0,
        "github_integration": False,
        "published": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--source-commit", help="Require this exact clean Git commit")
    parser.add_argument(
        "--codex", type=Path, help="Verify native installation with the supplied CLI"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.archive, source=args.source_commit, codex=args.codex), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()

"""Real package bytes on both operating systems; source Git metadata is simulated."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from nexkit import __version__
from scripts import build
from scripts.verify_package import verify


class PortablePackageTests(unittest.TestCase):
    def test_verification_rejects_checksum_and_unaccepted_source_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / f"nexkit-{__version__}.tar.gz"
            archive.write_bytes(b"This is deliberately not an archive")
            sums = root / "SHA256SUMS"
            sums.write_bytes(b"wrong checksum\n")
            with self.assertRaisesRegex(RuntimeError, "SHA256SUMS"):
                verify(archive)
            sums.write_bytes(
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n".encode()
            )
            for commit, dirty in (("b" * 40, False), ("a" * 40, True)):
                with self.subTest(commit=commit, dirty=dirty):
                    (root / "candidate.json").write_text(
                        json.dumps({"source_commit": commit, "source_dirty": dirty}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(RuntimeError, "exact clean source"):
                        verify(archive, source="a" * 40)

    def test_verification_rejects_archive_paths_before_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / f"nexkit-{__version__}.tar.gz"
            prefix = f"nexkit-{__version__}/"
            for name in (prefix + "../escape", prefix + "linked-file", prefix + "file:stream"):
                with self.subTest(name=name):
                    with tarfile.open(archive, mode="w:gz") as package:
                        entry = tarfile.TarInfo(name)
                        if name.endswith("linked-file"):
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = "../../outside"
                        package.addfile(entry)
                    (root / "SHA256SUMS").write_bytes(
                        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n".encode()
                    )
                    (root / "candidate.json").write_text(
                        json.dumps({"version": __version__, "files": {}}), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(RuntimeError, "archive entry"):
                        verify(archive)

    def test_archive_order_and_manifests_are_identical_after_repeated_builds(self):
        with tempfile.TemporaryDirectory(prefix="nexkit package ") as temporary:
            root = Path(temporary)
            files = {
                "README.md": b"Package fixture\n",
                "AGENTS.md": b"English artifacts\n",
                "CHANGELOG.md": b"Fixture changes\n",
                "actions/fixture/action.yml": b"name: Fixture\n",
                "bin/nexkit": b"#!/usr/bin/env python3\n",
                "bin/nexkit.cmd": b"@echo off\n",
                "docs/README.md": b"Documentation\n",
                "docs/a.md": "Unicode fixture: \u00e9\n".encode("utf-8"),
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            generated = root / "nexkit/__pycache__/fixture.pyc"
            generated.parent.mkdir(parents=True)
            generated.write_bytes(b"not source")

            def git_metadata(argv, **kwargs):
                return Mock(returncode=0, stdout="a" * 40 if argv[1] == "rev-parse" else "")

            with (
                patch.object(build, "ROOT", root),
                patch.object(build.subprocess, "run", side_effect=git_metadata),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(build.main(), 0)
                first = {p.name: p.read_bytes() for p in (root / "dist").iterdir()}
                self.assertEqual(build.main(), 0)
                self.assertEqual(first, {p.name: p.read_bytes() for p in (root / "dist").iterdir()})

            archive_name = f"nexkit-{__version__}.tar.gz"
            checksum = hashlib.sha256(first[archive_name]).hexdigest()
            self.assertEqual(first["SHA256SUMS"], f"{checksum}  {archive_name}\n".encode())
            self.assertNotIn(b"\r", first["candidate.json"])
            manifest = json.loads(first["candidate.json"])
            self.assertEqual(manifest["source_commit"], "a" * 40)
            self.assertFalse(manifest["source_dirty"])
            self.assertEqual(
                manifest["files"],
                {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
            )
            prefix = f"nexkit-{__version__}/"
            with tarfile.open(fileobj=io.BytesIO(first[archive_name]), mode="r:gz") as archive:
                self.assertEqual(
                    archive.getnames(),
                    [prefix + name for name in sorted(files)] + [prefix + "candidate.json"],
                )
                self.assertEqual(
                    archive.extractfile(prefix + "candidate.json").read(), first["candidate.json"]
                )
                for name, content in files.items():
                    member = archive.getmember(prefix + name)
                    self.assertEqual(member.mtime, 0)
                    self.assertEqual(member.mode, 0o755 if name.startswith("bin/") else 0o644)
                    self.assertEqual(archive.extractfile(member).read(), content)

"""Real archives and selected-toolkit validation; GitHub downloads are simulated."""

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from nexkit.cli import parser
from nexkit.common import Blocked, kit_root, read_json, write_json
from nexkit.kit_workflows import update_workflow
from nexkit.project import install, uninstall
from nexkit.versions import release_identity, unpack, use
from tests.support import project, project_document

REPO = "phuongnse/nexkit"


def archive(root, version, commit, overrides=None):
    """Build clean, real tar fixtures with the current schema validator and interfaces."""
    root.mkdir(parents=True, exist_ok=True)
    files = {
        path.relative_to(kit_root()).as_posix(): path.read_bytes()
        for folder in ("nexkit", ".github/workflows", "plugins/nexkit/skills")
        for path in (kit_root() / folder).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    files["nexkit/__init__.py"] = f'__version__ = "{version}"\n'.encode()
    files.update(overrides or {})
    files = {key: value for key, value in files.items() if value is not None}
    manifest = {
        "version": version,
        "source_commit": commit,
        "source_dirty": False,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
    }
    manifest_bytes = (json.dumps(manifest) + "\n").encode()
    name = f"nexkit-{version}.tar.gz"
    with tarfile.open(root / name, "w:gz") as package:
        for key, data in files.items() | {"candidate.json": manifest_bytes}.items():
            info = tarfile.TarInfo(f"nexkit-{version}/{key}")
            info.size = len(data)
            package.addfile(info, io.BytesIO(data))
    (root / "candidate.json").write_bytes(manifest_bytes)
    checksum = hashlib.sha256((root / name).read_bytes()).hexdigest()
    (root / "SHA256SUMS").write_bytes(f"{checksum}  {name}\n".encode())
    assets = [
        {
            "id": i,
            "name": p.name,
            "size": p.stat().st_size,
            "state": "uploaded",
            "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for i, p in enumerate(sorted(root.iterdir()), 1)
    ]
    return {"id": 1, "draft": False, "tag_name": "v" + version, "assets": assets}, root


class Releases:
    repository = REPO
    root = "repos/" + REPO

    def __init__(self, records):
        self.records = records
        self.annotated = False

    def api(self, path):
        version = path.rsplit("/", 1)[-1].removeprefix("v")
        if "/releases/tags/" in path:
            if version not in self.records:
                raise Blocked("HTTP 404: release does not exist")
            return deepcopy(self.records[version][0])
        if "/git/ref/tags/" in path:
            commit = read_json(self.records[version][1] / "candidate.json")["source_commit"]
            return {"object": {"type": "tag" if self.annotated else "commit", "sha": commit}}
        if "/git/tags/" in path:
            return {"object": {"type": "commit", "sha": path.rsplit("/", 1)[-1]}}
        raise AssertionError(path)

    def download(self, gh, identity, destination):
        for name in identity["assets"]:
            shutil.copyfile(
                self.records[identity["tag"].removeprefix("v")][1] / name, destination / name
            )


def wrapper(adapter, ref):
    return (
        "name: Consumer workflow\non: workflow_dispatch\n"
        "# Keep this comment and the unrelated action pin.\njobs:\n"
        "  native:\n    runs-on: ubuntu-24.04\n    steps:\n"
        f"      - uses: actions/checkout@{ref}\n"
        "      - run: |\n          echo 'uses: keep this literal text'\n"
        "  agent:\n    if: github.event_name == 'workflow_dispatch'\n"
        f"    uses: '{REPO}/.github/workflows/{adapter}.yml@{ref}' # selected kit\n"
        f"    with:\n      kit_repository: {REPO}\n      kit_ref: '{ref}' # exact pin\n"
        "      pipeline: maintenance\n"
    ).encode()


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nexkit-version-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "consumer"
        self.root.mkdir()
        self.gh = Releases(
            {
                version: archive(Path(self.tmp.name) / version, version, str(i) * 40)
                for i, version in enumerate(("1.0.0", "1.1.0"), 1)
            }
        )
        self.download = patch("nexkit.versions.download", side_effect=self.gh.download)
        self.download.start()
        self.addCleanup(self.download.stop)
        self.cfg = project_document(project())
        self.cfg["kit"]["ref"] = "1" * 40
        source = Path(self.tmp.name) / "setup-bundle"
        for name in self.cfg["files"]:
            adapter = Path(name).stem.removeprefix("nexkit-")
            content = wrapper(adapter, "1" * 40)
            self.cfg["files"][name]["sha256"] = hashlib.sha256(content).hexdigest()
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        # Install the old version's actual skills, not the host's newer copies.
        old = unpack(self.gh.records["1.0.0"][1], "1.0.0", "1" * 40)
        install(self.root, self.cfg, ["codex"], apply=True, bundle=source, kit=old)
        (self.root / "app.py").write_text("print('consumer code')\n", encoding="utf-8")

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_select_higher_then_lower_release_and_repeat_without_changes(self):
        before = self.snapshot()
        preview = use(self.root, "v1.1.0", apply=False, gh=self.gh)
        self.assertFalse(preview["applied"])
        self.assertEqual(self.snapshot(), before)
        use(self.root, "1.1.0", gh=self.gh)
        selected = read_json(self.root / ".nexkit/project.json")
        self.assertEqual(selected["kit"]["ref"], "2" * 40)
        self.assertEqual(selected["defaults"], self.cfg["defaults"])
        workflow = (self.root / ".github/workflows/nexkit-delivery.yml").read_text()
        self.assertIn("actions/checkout@" + "1" * 40, workflow)
        self.assertIn("# selected kit", workflow)
        self.assertIn("    permissions:\n", workflow)
        self.assertEqual(use(self.root, "1.1.0", gh=self.gh)["changes"], [])
        result = use(self.root, "1.0.0", gh=self.gh)
        self.assertEqual(result["previous"], "1.1.0")
        self.assertEqual(read_json(self.root / ".nexkit/installation.json")["version"], "1.0.0")
        self.assertEqual(read_json(self.root / ".nexkit/project.json")["kit"]["ref"], "1" * 40)
        self.assertEqual((self.root / "app.py").read_bytes(), before["app.py"])

    def test_edited_control_or_skill_is_preserved_before_any_update(self):
        for name in (
            ".github/workflows/nexkit-delivery.yml",
            ".agents/skills/nexkit-init/SKILL.md",
        ):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b"\nUser edit\n")
                before = self.snapshot()
                with self.assertRaisesRegex(Blocked, "Consumer edits preserved"):
                    use(self.root, "1.1.0", gh=self.gh)
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)

    def test_consumer_owned_workflow_stays_owned_through_select_and_uninstall(self):
        name = ".github/workflows/nexkit-delivery.yml"
        cfg = read_json(self.root / ".nexkit/project.json")
        cfg["files"][name]["managed"] = False
        install(self.root, cfg, ["codex"], apply=True)
        use(self.root, "1.1.0", gh=self.gh)
        self.assertFalse(read_json(self.root / ".nexkit/project.json")["files"][name]["managed"])
        self.assertNotIn(name, read_json(self.root / ".nexkit/installation.json")["files"])
        uninstall(self.root)
        self.assertTrue((self.root / name).is_file())

    def test_missing_release_or_reusable_workflow_is_atomic(self):
        before = self.snapshot()
        with self.assertRaisesRegex(Blocked, "404"):
            use(self.root, "0.9.0", gh=self.gh)
        self.gh.records["1.1.0"] = archive(
            Path(self.tmp.name) / "missing",
            "1.1.0",
            "2" * 40,
            {".github/workflows/delivery.yml": None},
        )
        with self.assertRaisesRegex(Blocked, "does not provide delivery"):
            use(self.root, "1.1.0", gh=self.gh)
        self.assertEqual(self.snapshot(), before)

    def test_selected_toolkit_rejects_configuration_before_writes(self):
        self.gh.records["1.1.0"] = archive(
            Path(self.tmp.name) / "incompatible",
            "1.1.0",
            "2" * 40,
            {
                "nexkit/policy.py": b"def config(value):\n    raise ValueError('unsupported configuration feature')\n"
            },
        )
        before = self.snapshot()
        with self.assertRaisesRegex(Blocked, "unsupported configuration feature"):
            use(self.root, "1.1.0", gh=self.gh)
        self.assertEqual(self.snapshot(), before)

    def test_unsupported_called_input_is_atomic(self):
        name = ".github/workflows/nexkit-delivery.yml"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"      future_input: unsupported\n")
        cfg = read_json(self.root / ".nexkit/project.json")
        cfg["files"][name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        ledger = read_json(self.root / ".nexkit/installation.json")
        from nexkit.common import digest

        ledger["project"] = digest(cfg)
        ledger["files"][name] = cfg["files"][name]["sha256"]
        write_json(self.root / ".nexkit/project.json", cfg)
        write_json(self.root / ".nexkit/installation.json", ledger)
        before = self.snapshot()
        with self.assertRaisesRegex(Blocked, "does not support inputs.*future_input"):
            use(self.root, "1.1.0", gh=self.gh)
        self.assertEqual(self.snapshot(), before)

    def test_changed_config_during_download_is_not_overwritten(self):
        def changed(gh, identity, destination):
            self.gh.download(gh, identity, destination)
            cfg = read_json(self.root / ".nexkit/project.json")
            cfg["defaults"]["decisions"].append("Changed while downloading")
            write_json(self.root / ".nexkit/project.json", cfg)

        with patch("nexkit.versions.download", side_effect=changed):
            with self.assertRaisesRegex(Blocked, "configuration changed during download"):
                use(self.root, "1.1.0", gh=self.gh)
        self.assertIn(
            "Changed while downloading",
            read_json(self.root / ".nexkit/project.json")["defaults"]["decisions"],
        )
        self.assertEqual(read_json(self.root / ".nexkit/project.json")["kit"]["version"], "1.0.0")

    def test_annotated_tags_drafts_and_missing_checksums(self):
        self.gh.annotated = True
        self.assertEqual(release_identity(self.gh, "1.0.0")["commit"], "1" * 40)
        self.gh.records["1.1.0"][0]["draft"] = True
        with self.assertRaisesRegex(Blocked, "published release"):
            release_identity(self.gh, "1.1.0")
        self.gh.records["1.1.0"][0]["draft"] = False
        self.gh.records["1.1.0"][0]["assets"].pop()
        with self.assertRaisesRegex(Blocked, "exactly one"):
            release_identity(self.gh, "1.1.0")

    def test_use_cli_is_explicit_and_allows_lower_version(self):
        args = parser().parse_args(["use", "--version", "1.0.0", "--dry-run"])
        self.assertEqual(args.version, "1.0.0")
        self.assertTrue(args.dry_run)
        with self.assertRaisesRegex(Blocked, "exact version"):
            use(self.root, "latest", gh=self.gh)


class ArchiveTests(unittest.TestCase):
    def test_script_block_header_indicators_are_not_copied_bindings(self):
        import yaml

        old = {"repository": REPO, "ref": "1" * 40, "version": "1.0.0"}
        selected = {**old, "ref": "2" * 40, "version": "1.1.0"}
        for header in ("|2-", "|-2", ">2-", ">-2", "|+"):
            with self.subTest(header=header):
                content = wrapper("intake", old["ref"]).replace(
                    b"      - run: |\n          echo 'uses: keep this literal text'",
                    f"      - run: {header}\n          repository: {REPO}\n          ref: {old['ref']}".encode(),
                )
                updated, _ = update_workflow(content, "caller.yml", old, selected, kit_root())
                script = yaml.safe_load(updated)["jobs"]["native"]["steps"][1]["run"]
                self.assertIn("repository: " + REPO, script)
                self.assertIn("ref: " + old["ref"], script)

    def test_permission_insertion_uses_actual_property_indentation(self):
        import yaml

        old = {"repository": REPO, "ref": "1" * 40, "version": "1.0.0"}
        selected = {**old, "ref": "2" * 40, "version": "1.1.0"}
        content = b"\n".join(
            b" " * (len(line) - len(line.lstrip())) + line
            for line in wrapper("intake", old["ref"]).split(b"\n")
        )
        updated, _ = update_workflow(content, "caller.yml", old, selected, kit_root())
        parsed = yaml.safe_load(updated)
        agent = parsed["jobs"]["agent"]
        self.assertEqual(agent["with"]["kit_ref"], selected["ref"])
        self.assertIn("uses", agent)
        self.assertNotIn("uses", agent["permissions"])

    def test_mixed_copied_job_is_rejected_while_script_literals_are_preserved(self):
        old = {"repository": REPO, "ref": "1" * 40, "version": "1.0.0"}
        selected = {**old, "ref": "2" * 40, "version": "1.1.0"}
        # Unrelated checkout pins in uses are preserved, while an actual toolkit
        # checkout ref cannot silently survive the version update.
        content = (
            wrapper("intake", old["ref"])
            + (
                "  copied:\n    runs-on: ubuntu-24.04\n    steps:\n"
                "      - uses: actions/checkout@" + "a" * 40 + "\n"
                f"        with:\n          repository: {REPO}\n          ref: {old['ref']}\n"
            ).encode()
        )
        with self.assertRaisesRegex(Blocked, "copied NexKit job needs setup"):
            update_workflow(content, "caller.yml", old, selected, kit_root())
        flow = (
            wrapper("intake", old["ref"])
            + (
                "  copied:\n    runs-on: ubuntu-24.04\n"
                f"    steps: [{{uses: actions/checkout@{'a' * 40}, with: {{repository: {REPO}, ref: '{old['ref']}'}}}}]\n"
            ).encode()
        )
        with self.assertRaisesRegex(Blocked, "copied NexKit job needs setup"):
            update_workflow(flow, "caller.yml", old, selected, kit_root())
        script = wrapper("intake", old["ref"]).replace(
            b"          echo 'uses: keep this literal text'",
            f"          repository: {REPO}\n          ref: {old['ref']}".encode(),
        )
        updated, _ = update_workflow(script, "caller.yml", old, selected, kit_root())
        self.assertIn(f"          ref: {old['ref']}".encode(), updated)

    def test_archive_links_traversal_duplicate_or_wrong_source_are_rejected(self):
        for kind in ("link", "traversal", "duplicate", "source"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive(root, "1.0.0", "1" * 40)
                if kind == "source":
                    with self.assertRaisesRegex(Blocked, "exact clean tagged"):
                        unpack(root, "1.0.0", "2" * 40)
                    continue
                package_path = root / "nexkit-1.0.0.tar.gz"
                with tarfile.open(package_path, "r:gz") as package:
                    entries = [(item, package.extractfile(item).read()) for item in package]
                extra = tarfile.TarInfo(
                    "nexkit-1.0.0/../escape" if kind == "traversal" else entries[0][0].name
                )
                if kind == "link":
                    extra.type, extra.linkname = tarfile.SYMTYPE, "/tmp/escape"
                with tarfile.open(package_path, "w:gz") as package:
                    for item, data in entries:
                        package.addfile(item, io.BytesIO(data))
                    package.addfile(extra, io.BytesIO(b""))
                checksum = hashlib.sha256(package_path.read_bytes()).hexdigest()
                (root / "SHA256SUMS").write_bytes(f"{checksum}  {package_path.name}\n".encode())
                with self.assertRaises(Blocked):
                    unpack(root, "1.0.0", "1" * 40)

    def test_workflow_pin_quotes_comments_crlf_and_native_steps_are_preserved(self):
        old = {"repository": REPO, "ref": "1" * 40, "version": "1.0.0"}
        selected = {**old, "ref": "2" * 40, "version": "1.1.0"}
        content = wrapper("intake", old["ref"]).replace(b"\n", b"\r\n")
        updated, calls = update_workflow(content, "caller.yml", old, selected, kit_root())
        self.assertEqual(calls, 1)
        self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))
        self.assertIn(b"# exact pin", updated)
        self.assertIn(("actions/checkout@" + old["ref"]).encode(), updated)

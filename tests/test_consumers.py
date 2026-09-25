"""Two real local consumers. GitHub and live model runs are tested separately."""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from nexkit.checks import test_count, verify
from nexkit.common import Blocked, file_hash, read_json, write_json
from nexkit.project import install, survey, uninstall
from tests.support import project


def write(root, name, body):
    target = Path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body).lstrip())


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nexkit-consumer-")
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "--initial-branch=main", str(self.root)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_large_files_are_snapshot_by_hash_not_rejected(self):
        from nexkit.ci import file_snapshot
        from nexkit.delivery import validate_changes

        asset = self.root / "existing-image.bin"
        asset.write_bytes(b"x" * 2_000_001)
        subprocess.run(["git", "add", "existing-image.bin"], cwd=self.root, check=True)
        initial = file_snapshot(self.root, self.root)
        self.assertEqual(initial[asset.name]["content"], {"large_sha256": file_hash(asset)})
        self.assertEqual(initial, file_snapshot(self.root, self.root))
        asset.write_bytes(b"y" * 2_000_001)
        changed = file_snapshot(self.root, self.root)
        self.assertNotEqual(initial, changed)
        with self.assertRaisesRegex(Blocked, "Missing file contents"):
            validate_changes([{"path": asset.name, **changed[asset.name]}])

    def test_new_node_cli_real_commands_reproduce_bug_then_fix(self):
        cfg = project("owner/new-cli")
        cfg["application"] = "absent"
        cfg["decisions"] = ["Owner chose a Node CLI for integer sums; use native node:test"]
        cfg["checks"] = [
            {
                "name": kind,
                "kind": kind,
                "argv": ["node", "--test", "--test-reporter=tap", f"{kind}.test.mjs"],
                "timeout_seconds": 20,
                "report": {"format": "tap"},
            }
            for kind in ("test", "e2e")
        ]
        self.assertEqual(survey(self.root)["files"], [])
        install(self.root, cfg, ["codex"], apply=True)
        self.assertFalse(verify(cfg, self.root)["passed"], "A configured empty app is not ready")
        write(
            self.root, "sum.mjs", "export const sum = xs => Math.abs(xs.reduce((a,b) => a+b, 0));\n"
        )
        write(
            self.root,
            "cli.mjs",
            "import {sum} from './sum.mjs'; console.log(sum(process.argv.slice(2).map(Number)));\n",
        )
        write(
            self.root,
            "test.test.mjs",
            """
            import {test} from 'node:test';
            import assert from 'node:assert/strict';
            import {sum} from './sum.mjs';
            test('positive integers', () => assert.equal(sum([3,4]),7));
            test('negative totals stay negative', () => assert.equal(sum([-7,2]),-5));
        """,
        )
        write(
            self.root,
            "e2e.test.mjs",
            """
            import {test} from 'node:test';
            import assert from 'node:assert/strict';
            import {execFileSync} from 'node:child_process';
            test('real CLI handles negative totals', () => {
              assert.equal(execFileSync(process.execPath, ['cli.mjs','-7','2'], {encoding:'utf8'}).trim(),'-5');
            });
        """,
        )
        broken = verify(cfg, self.root)
        self.assertFalse(broken["passed"])
        self.assertIn("not ok", broken["checks"][1]["log"])
        write(self.root, "sum.mjs", "export const sum = xs => xs.reduce((a,b) => a+b, 0);\n")
        fixed = verify(cfg, self.root)
        self.assertTrue(fixed["passed"], fixed)
        self.assertEqual([c["tests"] for c in fixed["checks"]], [2, 1])

    def test_existing_python_http_api_uses_its_own_commands(self):
        write(
            self.root,
            "README.md",
            "Existing service: normalizes a label. Keep the HTTP contract.\n",
        )
        write(
            self.root,
            "service.py",
            """
            from http.server import BaseHTTPRequestHandler
            from urllib.parse import urlparse, parse_qs
            import json
            def normalize(value):
                return '-'.join(value.strip().lower().split())
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed = urlparse(self.path)
                    if parsed.path != '/normalize':
                        self.send_error(404)
                        return
                    text = parse_qs(parsed.query).get('text', [''])[0]
                    body = json.dumps({'value': normalize(text)}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
        """,
        )
        write(
            self.root,
            "test_unit.py",
            """
            import unittest
            from service import normalize
            class Unit(unittest.TestCase):
                def test_spaces(self):
                    self.assertEqual(normalize('  Hello   World '), 'hello-world')
                def test_empty(self):
                    self.assertEqual(normalize('   '), '')
        """,
        )
        write(
            self.root,
            "test_http.py",
            """
            import unittest, threading, json
            from http.server import HTTPServer
            from urllib.request import urlopen
            from urllib.error import HTTPError
            from service import Handler
            class HTTP(unittest.TestCase):
                def setUp(self):
                    self.server = HTTPServer(('127.0.0.1', 0), Handler)
                    self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                    self.thread.start()
                    self.base = 'http://127.0.0.1:' + str(self.server.server_port)
                def tearDown(self):
                    self.server.shutdown()
                    self.thread.join()
                    self.server.server_close()
                def test_real_response(self):
                    with urlopen(self.base + '/normalize?text=Hello%20World') as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response), {'value': 'hello-world'})
                def test_route_not_found(self):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(self.base + '/missing')
                    self.assertEqual(error.exception.code, 404)
        """,
        )
        cfg = project("owner/existing-api")
        cfg["decisions"] = ["Preserve the existing Python HTTP service and unittest suite"]
        cfg["checks"] = [
            {
                "name": kind,
                "kind": kind,
                "argv": ["python3", "-m", "unittest", module],
                "timeout_seconds": 20,
                "report": {"format": "unittest"},
            }
            for kind, module in (("test", "test_unit"), ("e2e", "test_http"))
        ]
        original = (self.root / "service.py").read_bytes()
        context = survey(self.root)
        self.assertIn("README.md", context["context"])
        install(self.root, cfg, ["claude"], apply=True)
        self.assertEqual(original, (self.root / "service.py").read_bytes())
        result = verify(cfg, self.root)
        self.assertTrue(result["passed"], result)
        self.assertEqual([c["tests"] for c in result["checks"]], [2, 2])

    def test_reinstall_update_and_uninstall_preserve_consumer_edits(self):
        cfg = project()
        write(self.root, "README.md", "Consumer-owned knowledge\n")
        initial = install(self.root, cfg, ["codex"], apply=True)
        self.assertGreater(len(initial["changes"]), 2)
        self.assertEqual(install(self.root, cfg, ["codex"], apply=True)["changes"], [])
        target = self.root / ".agents/skills/nexkit-init/SKILL.md"
        target.write_text(target.read_text() + "\nA user edit.\n")
        with self.assertRaises(Blocked):
            install(self.root, cfg, ["codex"], apply=True)
        result = uninstall(self.root)
        self.assertIn(".agents/skills/nexkit-init/SKILL.md", result["preserved"])
        self.assertTrue(target.exists())
        self.assertEqual((self.root / "README.md").read_text(), "Consumer-owned knowledge\n")

    def test_install_refuses_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            (self.root / ".agents").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(Blocked):
                install(self.root, project(), ["codex"], apply=True)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_uninstall_never_accepts_consumer_file_as_kit_owned(self):
        install(self.root, project(), ["codex"], apply=True)
        write(self.root, "app.py", "print('consumer code')\n")
        ledger_path = self.root / ".nexkit/installation.json"
        ledger = read_json(ledger_path)
        ledger["files"]["app.py"] = file_hash(self.root / "app.py")
        write_json(ledger_path, ledger)
        self.assertIn("app.py", uninstall(self.root)["preserved"])
        self.assertTrue((self.root / "app.py").exists())

    def test_ledger_symlink_is_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            write_json(external / "installation.json", {"files": {}})
            (self.root / ".nexkit").symlink_to(external, target_is_directory=True)
            with self.assertRaises(Blocked):
                install(self.root, project(), ["codex"], apply=True)
            self.assertFalse((self.root / ".agents").exists())
            with self.assertRaises(Blocked):
                uninstall(self.root)
            self.assertTrue((external / "installation.json").is_file())

    def test_environment_home_is_preserved_across_checks(self):
        cfg = project()
        cfg["environment"]["setup"] = [
            [
                "python3",
                "-c",
                "from pathlib import Path; Path.home().joinpath('setup-token').write_text('ready')",
            ]
        ]
        write(
            self.root,
            "test_home.py",
            """
            import unittest
            from pathlib import Path
            class Environment(unittest.TestCase):
                def test_setup_is_present(self):
                    self.assertEqual(Path.home().joinpath('setup-token').read_text(), 'ready')
        """,
        )
        cfg["checks"][0]["argv"] += ["test_home"]
        cfg["checks"][1]["argv"] += ["test_home"]
        result = verify(cfg, self.root)
        self.assertTrue(result["passed"], result)


class TestReportTests(unittest.TestCase):
    def test_zero_skipped_failed_and_missing_results_cannot_pass(self):
        cases = [
            ("unittest", "Ran 0 tests in 0.001s\n\nOK\n"),
            ("unittest", "Ran 2 tests in 0.001s\n\nOK (skipped=1)\n"),
            ("tap", "TAP version 13\n1..0\n"),
            ("tap", "1..1\nok 1 fake # SKIP\n"),
            ("tap", "# tests 2\n# pass 1\n# fail 0\n"),
            ("unittest", "All tests passed!"),
        ]
        for fmt, log in cases:
            with self.subTest(fmt=fmt, log=log):
                try:
                    count = test_count({"format": fmt}, ".", log)
                    self.assertEqual(count, 0)
                except Blocked:
                    pass

    def test_stale_junit_is_removed_and_missing_new_report_fails(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "result.xml", '<testsuite><testcase name="old"/></testsuite>')
            cfg = project()
            cfg["checks"] = [
                {
                    "name": kind,
                    "kind": kind,
                    "argv": ["python3", "-c", "pass"],
                    "timeout_seconds": 10,
                    "report": {"format": "junit", "path": "result.xml"},
                }
                for kind in ("test", "e2e")
            ]
            self.assertFalse(verify(cfg, root)["passed"])

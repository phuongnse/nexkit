"""Real command I/O across two Unix accounts; enabled explicitly in CI."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexkit import steps
from nexkit.common import Blocked


@unittest.skipUnless(
    os.name == "posix" and os.environ.get("NEXKIT_TEST_ISOLATION") == "1",
    "Linux account isolation is exercised in the Linux adapter job",
)
class IsolatedStepTests(unittest.TestCase):
    def setUp(self):
        import pwd

        self.agent = pwd.getpwnam("nexkit-agent")
        self.assertNotIn(os.geteuid(), (0, self.agent.pw_uid))
        self.temporary = tempfile.TemporaryDirectory(prefix="nexkit-step-isolation-")
        self.root = Path(self.temporary.name)
        self.root.chmod(0o755)
        self.work = self.root / "work"
        self.work.mkdir(mode=0o755)
        subprocess.run(
            ["sudo", "-n", "chown", "nexkit-agent:nexkit-agent", str(self.work)], check=True
        )
        self.secret = self.root / "controller-private"
        self.secret.write_text("test-only-private-marker")
        self.secret.chmod(0o600)
        self.environment = patch.dict(
            os.environ,
            {
                "NEXKIT_EXEC_USER": "nexkit-agent",
                "GH_TOKEN": "test-only-github-token",
                "OPENAI_API_KEY": "test-only-model-token",
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        # Reclaim only this disposable workspace, without following symlinks.
        subprocess.run(
            ["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(self.root)],
            check=True,
        )
        self.temporary.cleanup()

    def context(self, script, *, setup=()):
        return {
            "run_key": "100.1",
            "base": "b" * 40,
            "source": "b" * 40,
            "issue": {"number": 1, "title": "Inspect the command workspace"},
            "previous_outputs": [{"result": {"summary": "Recorded native tests"}}],
            "config": {
                "environment": {"setup": list(setup)},
                "limits": {"command_seconds": 5},
            },
            "step": {
                "id": "inspect-files",
                "definition": {
                    "kind": "command",
                    "argv": [sys.executable, "-I", "-c", script],
                    "timeout_seconds": 10,
                },
            },
        }

    def execute(self, script, *, setup=()):
        return steps.execute(self.context(script, setup=setup), self.work)

    def test_input_and_private_result_cross_the_account_boundary(self):
        with self.assertRaises(PermissionError):
            (self.work / "controller-write").write_text("must not succeed")
        script = f"""import json, os
from pathlib import Path
assert os.geteuid() == {self.agent.pw_uid}
assert 'GH_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
try:
    Path({str(self.secret)!r}).read_text()
except PermissionError:
    pass
else:
    raise AssertionError('Command could read controller data')
source = Path('.nexkit-step-input.json')
assert source.stat().st_uid == os.geteuid()
assert json.loads(source.read_text())['inputs'][0]['result']['summary'] == 'Recorded native tests'
os.umask(0o077)
Path('.nexkit-step-result.json').write_text(json.dumps({{'status':'done','summary':'Checked as isolated user','data':{{'uid':os.geteuid()}}}}))
"""
        report = self.execute(script)
        self.assertEqual(report["result"]["data"], {"uid": self.agent.pw_uid})
        self.assertEqual(report["evidence"]["commands"][0]["exit_code"], 0)
        self.assertEqual(self.work.stat().st_uid, self.agent.pw_uid)
        self.assertEqual(self.work.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(PermissionError):
            (self.work / ".nexkit-step-result.json").read_text()

    def test_setup_and_command_share_an_isolated_home(self):
        setup = [
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path; Path.home().joinpath('ready').write_text('yes')",
        ]
        report = self.execute(
            "from pathlib import Path; assert Path.home().joinpath('ready').read_text() == 'yes'",
            setup=[setup],
        )
        self.assertEqual(report["result"]["status"], "done")
        self.assertEqual(len(report["evidence"]["commands"]), 2)

    def test_setup_cannot_supply_a_private_result(self):
        setup = [
            sys.executable,
            "-I",
            "-c",
            "import os; from pathlib import Path; os.umask(0o077); "
            "Path('.nexkit-step-result.json').write_text('{}')",
        ]
        with self.assertRaisesRegex(Blocked, "setup cannot supply"):
            self.execute("raise AssertionError('Must not run')", setup=[setup])

    def test_symlink_result_is_rejected_without_reading_controller_data(self):
        with self.assertRaisesRegex(Blocked, "regular JSON"):
            self.execute(
                "from pathlib import Path; "
                f"Path('.nexkit-step-result.json').symlink_to({str(self.secret)!r})"
            )
        self.assertEqual(self.secret.read_text(), "test-only-private-marker")

    def test_fifo_result_is_rejected_without_waiting_for_a_writer(self):
        with self.assertRaisesRegex(Blocked, "regular JSON"):
            self.execute("import os; os.mkfifo('.nexkit-step-result.json')")

    def test_failed_command_cannot_claim_success(self):
        with self.assertRaisesRegex(Blocked, "failed command cannot report done"):
            self.execute(
                "import json; from pathlib import Path; "
                "Path('.nexkit-step-result.json').write_text(json.dumps("
                "{'status':'done','summary':'False success'})); raise SystemExit(1)"
            )

    def test_reserved_input_symlink_is_not_overwritten(self):
        subprocess.run(
            [
                "sudo",
                "-n",
                "-u",
                "nexkit-agent",
                "--",
                "ln",
                "-s",
                str(self.secret),
                str(self.work / ".nexkit-step-input.json"),
            ],
            check=True,
        )
        with self.assertRaisesRegex(Blocked, "must not exist"):
            self.execute("raise AssertionError('Must not run')")
        self.assertEqual(self.secret.read_text(), "test-only-private-marker")

    def test_helper_does_not_import_consumer_python_modules(self):
        report = self.execute(
            "from pathlib import Path; "
            "Path('json.py').write_text(\"raise AssertionError('Consumer module imported')\"); "
            "Path('.nexkit-step-result.json').write_text("
            + repr(json.dumps({"status": "done", "summary": "Read by the trusted helper"}))
            + ")"
        )
        self.assertEqual(report["result"]["status"], "done")

import json
import tempfile
import unittest
from pathlib import Path

from nexkit import checks
from tests.support import make_config


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_pass_and_fail(self):
        cfg = make_config(
            setup=["echo ready > setup.txt"],
            checks=[
                {"name": "unit", "run": "test -f setup.txt && echo fine"},
                {"name": "lint", "run": "echo bad; false | cat"},
            ],
        )
        results = checks.run_checks(cfg, self.root, self.root / "out")
        self.assertEqual([r["passed"] for r in results], [True, False])
        self.assertIn("fine", results[0]["output"])
        self.assertNotEqual(results[1]["exit_code"], 0)  # pipefail keeps the failure
        saved = json.loads((self.root / "out" / "checks.json").read_text())
        self.assertEqual(saved, results)

    def test_setup_failure_is_a_failed_check(self):
        cfg = make_config(setup=["exit 3"], checks=[{"name": "unit", "run": "true"}])
        results = checks.run_checks(cfg, self.root)
        self.assertEqual(len(results), 1)
        self.assertEqual((results[0]["name"], results[0]["exit_code"]), ("setup", 3))

    def test_timeout(self):
        code, output, _ = checks._run("sleep 30", self.root, 1)
        self.assertEqual(code, 124)
        self.assertIn("stopped the command", output)


if __name__ == "__main__":
    unittest.main()

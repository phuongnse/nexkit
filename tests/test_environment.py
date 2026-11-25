"""The tests' own environment: what they run with does not depend on the machine."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# What git sees of `nexkit.marker` once the tests package loaded, in a fresh repository.
PROBE = """
import subprocess, sys, tempfile
import tests
with tempfile.TemporaryDirectory() as folder:
    subprocess.run(["git", "init", "-q", folder], check=True)
    found = subprocess.run(["git", "config", "--get-all", "nexkit.marker"], cwd=folder,
                           capture_output=True, text=True)
sys.stdout.write(found.stdout)
"""


class GitConfigurationTest(unittest.TestCase):
    def test_git_reads_no_configuration_but_the_repository_s(self):
        # A machine's settings, such as signing every commit, would change the commits the
        # tests make, and so the SHAs that their goldens hold. Each place a machine can set
        # git's configuration sets a marker here: the tests' git sees none of them.
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".gitconfig").write_text("[nexkit]\n\tmarker = global\n")
            xdg = Path(home) / "xdg"
            (xdg / "git").mkdir(parents=True)
            (xdg / "git" / "config").write_text("[nexkit]\n\tmarker = xdg\n")
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
            env.update(
                HOME=home,
                XDG_CONFIG_HOME=str(xdg),
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="nexkit.marker",
                GIT_CONFIG_VALUE_0="count",
                GIT_CONFIG_PARAMETERS="'nexkit.marker'='parameters'",
            )
            seen = subprocess.run(
                [sys.executable, "-c", PROBE],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        self.assertEqual(seen, "")


if __name__ == "__main__":
    unittest.main()

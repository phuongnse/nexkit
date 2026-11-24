"""The tests' own environment: what they run with does not depend on the machine."""

import os
import subprocess
import tempfile
import unittest


class GitConfigurationTest(unittest.TestCase):
    def test_git_reads_no_global_or_system_configuration(self):
        # A machine's settings, such as signing every commit, would change the commits the
        # tests make, and so the SHAs that their goldens hold.
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(["git", "init", "-q", folder], check=True)
            found = subprocess.run(
                ["git", "config", "--list", "--show-scope"],
                cwd=folder,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        scopes = {line.split("\t", 1)[0] for line in found.splitlines()}
        self.assertEqual(scopes - {"local", "command"}, set(), found)
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], os.devnull)


if __name__ == "__main__":
    unittest.main()

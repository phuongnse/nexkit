import importlib.util
import unittest
from pathlib import Path

import nexkit

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "release_notes", ROOT / "scripts" / "release_notes.py"
)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## 2.0.0

- New `cache` key. See [Caches](docs/configuration.md#caches) and the
  [README](https://github.com/phuongnse/nexkit#readme).

### Upgrade

- Rename the settings:

  ```json
  "fix": {"max_auto_rounds": 2}
  ```

- Add the `schedule` trigger.

## 1.0.0

- First release.
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_the_upgrade_steps_go_between_the_refs_and_doctor(self):
        text = release_notes.notes("2.0.0", CHANGELOG)
        changes, upgrade = text.split("## Upgrade\n\n")
        upgrade, install = upgrade.split("## Install\n\n")
        self.assertEqual(
            changes,
            "- New `cache` key. See [Caches]"
            "(https://github.com/phuongnse/nexkit/blob/v2.0.0/docs/configuration.md#caches) "
            "and the\n  [README](https://github.com/phuongnse/nexkit#readme).\n\n",
        )
        self.assertEqual(
            upgrade,
            "1. In `.github/workflows/nexkit.yml`, change both the `uses:` ref and "
            "`nexkit_ref` to `v2.0.0`. Or run `nexkit init --force --kit-ref v2.0.0` and "
            "restore your config.\n"
            "2. Rename the settings:\n\n"
            "   ```json\n"
            '   "fix": {"max_auto_rounds": 2}\n'
            "   ```\n"
            "3. Add the `schedule` trigger.\n"
            "4. Run `nexkit doctor`.\n\n",
        )
        self.assertIn("/plugin install nexkit@nexkit", install)
        self.assertNotIn("1.0.0", text)

    def test_an_entry_without_upgrade_steps_changes_nothing_else(self):
        text = release_notes.notes("1.0.0", CHANGELOG)
        self.assertIn("2. Run `nexkit doctor`.\n\nThe configuration and the caller's", text)

    def test_errors(self):
        with self.assertRaisesRegex(release_notes.NotesError, "no entry for 9.9.9"):
            release_notes.notes("9.9.9", CHANGELOG)
        broken = CHANGELOG.replace("- Rename the settings:", "Rename the settings:")
        with self.assertRaisesRegex(release_notes.NotesError, "must be a list"):
            release_notes.notes("2.0.0", broken)

    def test_the_current_version_has_notes(self):
        text = release_notes.notes(nexkit.__version__, release_notes.CHANGELOG.read_text())
        self.assertIn("## Upgrade\n", text)
        self.assertIn("## Install\n", text)
        self.assertNotRegex(text, r"\]\((?!https?://|#)")


if __name__ == "__main__":
    unittest.main()

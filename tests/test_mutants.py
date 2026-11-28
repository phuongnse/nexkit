"""The mutants of the engine's mutation check (`tests/mutants.py`) still apply. The mutation
check runs only by hand, so these tests are what tells a refactor of the engine that a mutant no
longer breaks anything."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"nexkit_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutants = load("mutants")


class MutantTests(unittest.TestCase):
    def test_each_mutant_changes_a_file_of_the_engine_or_the_protocol(self):
        self.assertTrue(mutants.MUTANTS)
        for name, mutant in mutants.MUTANTS.items():
            with self.subTest(name):
                self.assertTrue(mutant["path"].startswith(mutants.FOLDERS), mutant["path"])
                self.assertNotEqual(mutant["old"], mutant["new"])

    def test_what_each_mutant_replaces_occurs_once(self):
        for name, mutant in mutants.MUTANTS.items():
            with self.subTest(name):
                text = (ROOT / mutant["path"]).read_text()
                self.assertEqual(text.count(mutant["old"]), 1, mutant["path"])

    def test_each_mutant_leaves_python_that_compiles(self):
        for name, mutant in mutants.MUTANTS.items():
            with self.subTest(name):
                text = (ROOT / mutant["path"]).read_text()
                compile(text.replace(mutant["old"], mutant["new"]), mutant["path"], "exec")

    def test_a_copy_has_the_mutant(self):
        name = next(iter(mutants.MUTANTS))
        mutant = mutants.MUTANTS[name]
        with tempfile.TemporaryDirectory() as folder:
            root = mutants.build(name, folder)
            self.assertTrue((root / "tests" / "walk.py").exists())
            self.assertTrue((root / "nexkit").is_dir())
            text = (root / mutant["path"]).read_text()
            self.assertNotIn(mutant["old"], text)
            self.assertIn(mutant["new"], text)


if __name__ == "__main__":
    unittest.main()

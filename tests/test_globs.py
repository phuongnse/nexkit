import unittest

from nexkit.globs import GlobError, compile_globs, unaffected

# The list of the issue: what a check that never reads docs, workflows or the web app lists.
LIST = [
    "docs/**",
    "**/*.md",
    ".github/**",
    "web/**",
    "!web/package.json",
    "!web/package-lock.json",
]


class MatchTests(unittest.TestCase):
    def test_paths_the_list_names_and_paths_it_does_not(self):
        cases = [
            ("docs/guide/setup.txt", True),
            ("README.md", True),
            ("src/api/README.md", True),
            (".github/workflows/ci.yml", True),
            ("web/src/app.ts", True),
            # A `!` pattern that matches last takes the path back out.
            ("web/package.json", False),
            ("web/package-lock.json", False),
            ("src/app.py", False),
            # `docs/**` is what is under the folder, not a file named so.
            ("docs", False),
            ("documentation/a.txt", False),
        ]
        for path, listed in cases:
            with self.subTest(path=path):
                self.assertEqual(unaffected([path], LIST), listed)

    def test_every_path_must_be_listed(self):
        self.assertTrue(unaffected(["docs/a.md", "web/a.ts"], LIST))
        self.assertFalse(unaffected(["docs/a.md", "src/app.py"], LIST))
        # No changed path: nothing the check reads changed.
        self.assertTrue(unaffected([], LIST))

    def test_the_last_pattern_that_matches_decides(self):
        self.assertTrue(
            unaffected(["web/package.json"], ["web/**", "!web/*.json", "web/package.json"])
        )
        self.assertFalse(unaffected(["web/a.json"], ["web/**", "!web/*.json"]))

    def test_star_stays_in_its_segment_and_double_star_crosses_folders(self):
        self.assertTrue(unaffected(["docs/a.txt"], ["docs/*"]))
        self.assertFalse(unaffected(["docs/sub/a.txt"], ["docs/*"]))
        self.assertTrue(unaffected(["a/c.py", "a/x/y/c.py"], ["a/**/c.py"]))
        self.assertFalse(unaffected(["b/a/c.py"], ["a/**/c.py"]))
        self.assertTrue(unaffected(["anything/at/all"], ["**"]))

    def test_question_marks_and_sets(self):
        self.assertTrue(unaffected(["v1.txt"], ["v?.txt"]))
        self.assertFalse(unaffected(["v10.txt"], ["v?.txt"]))
        self.assertTrue(unaffected(["a.c", "a.h"], ["a.[ch]"]))
        self.assertFalse(unaffected(["a.c"], ["a.[!ch]"]))
        self.assertTrue(unaffected(["log3"], ["log[0-9]"]))

    def test_other_characters_match_themselves(self):
        self.assertTrue(unaffected(["a+b (1).txt"], ["a+b (1).txt"]))
        self.assertFalse(unaffected(["aXb"], ["a.b"]))


class ValidationTests(unittest.TestCase):
    def test_patterns_that_are_not_globs(self):
        cases = [
            ([""], "is empty"),
            (["!"], "is only `!`"),
            (["/docs/**"], "starts with `/`"),
            (["docs/"], "use `docs/\\*\\*`"),
            (["docs/a**"], "inside a segment"),
            (["docs//a"], "empty, `.` or `..` segment"),
            (["../a"], "empty, `.` or `..` segment"),
            (["a[bc"], "without its `\\]`"),
            (["a]"], "without its `\\[`"),
            (["a[]"], "empty `\\[\\]`"),
            (["[z-a]"], "is not a valid glob"),
            (["!docs/**"], "starts with `!` but is first"),
        ]
        for patterns, message in cases:
            with self.subTest(patterns=patterns), self.assertRaisesRegex(GlobError, message):
                compile_globs(patterns)

    def test_a_later_pattern_may_start_with_an_exclamation_mark(self):
        self.assertEqual(len(compile_globs(["docs/**", "!docs/api/**"])), 2)


if __name__ == "__main__":
    unittest.main()

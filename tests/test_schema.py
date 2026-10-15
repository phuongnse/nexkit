import unittest

from nexkit.agent import SCHEMAS
from nexkit.schema import problems
from tests.test_agent import DONE, PLAN, VERDICT


class SchemaTests(unittest.TestCase):
    def test_complete_results_fit(self):
        for stage, result in (("plan", PLAN), ("implement", DONE), ("review", VERDICT)):
            self.assertEqual(problems(result, SCHEMAS[stage]), [], stage)

    def test_types_enums_and_required_keys(self):
        review = {
            **VERDICT,
            "verdict": "maybe",
            "findings": [{"severity": "blocking", "file": "a.py", "line": "3", "body": "x"}],
        }
        del review["criteria"]
        self.assertEqual(
            problems(review, SCHEMAS["review"]),
            [
                "result.criteria is missing",
                "result.verdict must be one of 'approve', 'request_changes'",
                "result.findings[0].line must be an integer",
            ],
        )
        self.assertEqual(problems([], SCHEMAS["plan"]), ["result must be an object"])
        self.assertEqual(
            problems({**PLAN, "too_large": 1}, SCHEMAS["plan"]),
            ["result.too_large must be a boolean"],
        )

    def test_booleans_are_not_numbers_and_extra_keys_stay(self):
        schema = {"type": "object", "properties": {"line": {"type": "integer"}}}
        self.assertEqual(problems({"line": True}, schema), ["result.line must be an integer"])
        self.assertEqual(problems({"line": 3, "note": "x"}, schema), [])


if __name__ == "__main__":
    unittest.main()

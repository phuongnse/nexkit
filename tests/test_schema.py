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

    def test_a_criterion_is_met_unmet_or_unverifiable(self):
        item = {"criterion": "add works", "test": "", "evidence": ""}
        for status in ("met", "unmet", "unverifiable"):
            review = {**VERDICT, "criteria": [{**item, "status": status}]}
            self.assertEqual(problems(review, SCHEMAS["review"]), [], status)
        review = {**VERDICT, "criteria": [{**item, "status": "unknown"}]}
        self.assertEqual(
            problems(review, SCHEMAS["review"]),
            ["result.criteria[0].status must be one of 'met', 'unmet', 'unverifiable'"],
        )
        # A boolean `met` from before criteria had a status is not a result.
        review = {**VERDICT, "criteria": [{**item, "met": True}]}
        self.assertEqual(
            problems(review, SCHEMAS["review"]), ["result.criteria[0].status is missing"]
        )

    def test_booleans_are_not_numbers_and_extra_keys_stay(self):
        schema = {"type": "object", "properties": {"line": {"type": "integer"}}}
        self.assertEqual(problems({"line": True}, schema), ["result.line must be an integer"])
        self.assertEqual(problems({"line": 3, "note": "x"}, schema), [])


if __name__ == "__main__":
    unittest.main()

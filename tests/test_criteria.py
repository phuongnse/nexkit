"""Approved criteria coverage, with simulated review and verification reports."""

import unittest
from copy import deepcopy

from nexkit.common import Blocked
from nexkit.criteria import specification_criteria
from nexkit.policy import candidate_key, merge_gate
from tests.support import issue, project, reviewed, verified


class CriterionTests(unittest.TestCase):
    def candidate(self):
        work = issue()
        work["body"] = (
            "## Acceptance criteria\n- [AC1] Positive sums work.\n- [AC2] Negative sums stay negative.\n"
        )
        return candidate_key(work, project(), "b" * 40, "c" * 40)

    def test_review_cannot_drop_repeat_or_invent_a_criterion(self):
        key = self.candidate()
        merge_gate(key, verified(key), reviewed(key), project())
        for mutation in ("missing", "duplicate", "unknown", "failed", "invented_completion"):
            with self.subTest(mutation=mutation):
                report = reviewed(key)
                rows = report["result"]["acceptance"]
                if mutation == "missing":
                    rows.pop()
                elif mutation == "duplicate":
                    rows.append(deepcopy(rows[0]))
                elif mutation == "unknown":
                    rows[0]["criterion"] = "Other goal"
                elif mutation == "invented_completion":
                    rows.append(
                        {
                            "criterion": "Issue #7 completion",
                            "passed": True,
                            "evidence": "Unapproved invented target",
                        }
                    )
                else:
                    rows[0]["passed"] = False
                with self.assertRaises(Blocked):
                    merge_gate(key, verified(key), report, project())

    def test_original_request_code_blocks_and_other_sections_are_excluded(self):
        work = {
            "body": "> ## Acceptance criteria\n> - [fake] Quoted request\n\n<!-- nexkit:spec -->\n```markdown\n## Acceptance criteria\n- [fake] Example\n```\n## Acceptance criteria\n1. [AC1] Actual result\n   with its edge case\n## Out of scope\n- Other work\n"
        }
        self.assertEqual(
            specification_criteria(work),
            [{"id": "AC1", "text": "Actual result\n   with its edge case"}],
        )

    def test_legacy_markdown_list_has_stable_content_ids(self):
        first = specification_criteria({"body": "## Acceptance criteria\n- First\n- Second\n"})
        second = specification_criteria({"body": "## Acceptance criteria\n- Second\n- First\n"})
        self.assertEqual(first, list(reversed(second)))

    def test_missing_ambiguous_or_duplicate_registry_is_blocked(self):
        for body in (
            "Prose without criteria",
            "## Acceptance criteria\nProse",
            "## Acceptance criteria\n- [AC1]",
            "## Acceptance criteria\n- [AC1]   ",
            "## Acceptance criteria\n- Actual behavior\n```unclosed",
            "## Acceptance criteria\n- [AC1] First\n- [AC1] Second",
            "## Acceptance criteria\n- First\n## Acceptance criteria\n- Second",
        ):
            with self.subTest(body=body), self.assertRaises(Blocked):
                specification_criteria({"body": body})

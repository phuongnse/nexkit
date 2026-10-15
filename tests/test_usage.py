import contextlib
import io
import json
import unittest
from datetime import UTC, datetime

from nexkit import agent
from nexkit.usage import parse_iso, schedule, shown
from tests.support import profile_config
from tests.test_agent import CONTEXT, StageCase

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class ScheduleTests(unittest.TestCase):
    def test_a_known_reset_is_the_resume_time(self):
        reset = datetime(2026, 10, 11, 4, 40, tzinfo=UTC)
        limit = schedule({"message": "limit", "reset": reset}, NOW)
        self.assertEqual((limit["resume_at"], limit["reset_known"]), ("2026-10-11T04:40:00Z", True))
        self.assertEqual(shown(limit["resume_at"]), "04:40 UTC on 2026-10-11")

    def test_unknown_reset_retries_after_an_hour(self):
        limit = schedule({"message": "You've reached your Fable 5 limit.", "reset": None}, NOW)
        self.assertEqual(
            (limit["resume_at"], limit["reset_known"]), ("2026-10-10T10:00:00Z", False)
        )

    def test_the_retry_delay_is_configured(self):
        limit = schedule({"message": "Usage limit reached", "reset": None}, NOW, 180)
        self.assertEqual(limit["resume_at"], "2026-10-10T12:00:00Z")

    def test_parse_iso(self):
        self.assertEqual(
            parse_iso("2026-10-11T04:40:00Z"), datetime(2026, 10, 11, 4, 40, tzinfo=UTC)
        )
        self.assertIsNone(parse_iso("tomorrow"))


class PausedStageTests(StageCase):
    LIMIT = "You've hit your session limit · resets 4:40am (UTC)"

    def test_implement_at_the_limit_publishes_nothing(self):
        self.claude.configure(
            result=None,
            is_error=True,
            text=self.LIMIT,
            events=[
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": self.LIMIT}]},
                }
            ],
            write={"calc.py": "partial"},
        )
        result = self.run_stage("implement")
        self.assertEqual(result["status"], "paused")
        self.assertFalse((self.out / "changes.patch").exists())
        saved = json.loads((self.out / "result.json").read_text())
        self.assertEqual(saved["limit"], self.LIMIT)

    def test_triage_at_the_limit_pauses(self):
        self.claude.configure(result=None, is_error=True, text=self.LIMIT)
        with contextlib.redirect_stdout(io.StringIO()):
            triage = agent.run_triage(
                CONTEXT,
                profile_config(),
                None,
                self.out,
                program=str(self.claude.path),
            )
        self.assertEqual(triage["status"], "paused")


if __name__ == "__main__":
    unittest.main()

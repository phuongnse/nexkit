import unittest
from datetime import UTC, datetime

from nexkit.runtimes.claude_code import _text_of, limit_reached, reset_time
from nexkit.usage import schedule, shown
from tests.support import interpret_claude
from tests.test_agent import DONE

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def usage_limit(texts, now=None, resets_at=None, retry_minutes=60):
    limit = limit_reached(texts, now, resets_at)
    return schedule(limit, now, retry_minutes) if limit else None


class ResetTimeTests(unittest.TestCase):
    def at(self, text):
        when = reset_time(text, NOW)
        return when.strftime("%Y-%m-%d %H:%M") if when else None

    def test_formats(self):
        cases = {
            "You've hit your session limit · resets 4:40am (UTC)": "2026-10-11 04:40",
            "You've hit your session limit · resets 11:15am (UTC)": "2026-10-10 11:15",
            "5-hour limit reached ∙ resets 3pm": "2026-10-10 15:00",
            "resets 12am (UTC)": "2026-10-11 00:00",
            "resets 12:30pm": "2026-10-10 12:30",
            "You've hit your weekly limit · resets Oct 12, 4am (UTC)": "2026-10-12 04:00",
            "Your limit will reset at 5pm (America/New_York).": "2026-10-10 21:00",
            "resets 16:40": "2026-10-10 16:40",
            "Claude AI usage limit reached|1791630000": "2026-10-10 11:00",
        }
        for text, wanted in cases.items():
            self.assertEqual(self.at(text), wanted, text)

    def test_unreadable_times(self):
        for text in ("You've hit your session limit", "resets soon", "resets 4 (UTC)"):
            self.assertIsNone(self.at(text), text)

    def test_unknown_zone_is_read_as_utc(self):
        self.assertEqual(self.at("resets 4am (Mars/Olympus)"), "2026-10-11 04:00")


class UsageLimitTests(unittest.TestCase):
    def test_recognises_the_limit_and_its_reset(self):
        limit = usage_limit(["", "You've hit your session limit · resets 4:40am (UTC)"], NOW)
        self.assertEqual(
            limit,
            {
                "message": "You've hit your session limit · resets 4:40am (UTC)",
                "resume_at": "2026-10-11T04:40:00Z",
                "reset_known": True,
            },
        )
        self.assertEqual(shown(limit["resume_at"]), "04:40 UTC on 2026-10-11")

    def test_the_event_stream_reset_time_wins(self):
        limit = usage_limit(["Usage limit reached"], NOW, resets_at=1791630000)
        self.assertEqual(limit["resume_at"], "2026-10-10T11:00:00Z")

    def test_other_errors_are_not_limits(self):
        for text in ("Credit balance is too low", "API Error: 500", "Rate limited: retry"):
            self.assertIsNone(usage_limit([text], NOW), text)


class InterpretTests(unittest.TestCase):
    def test_a_failed_run_at_the_limit_is_paused(self):
        event = {
            "subtype": "success",
            "is_error": True,
            "result": "You've hit your session limit · resets 4:40am (UTC)",
        }
        result = interpret_claude("fix", event, returncode=1)
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["reset_known"])
        self.assertIn("usage limit: You've hit your session limit", result["error"])
        missing = interpret_claude(
            "review", None, last_text="Claude AI usage limit reached|1893456000", returncode=1
        )
        self.assertEqual(missing["resume_at"], "2030-01-01T00:00:00Z")

    def test_the_models_own_words_never_pause_a_run(self):
        prose = 'The handler now returns "Rate limit reached" when the usage limit is hit.'
        stopped = {"subtype": "error_max_turns", "is_error": False, "result": prose}
        result = interpret_claude("fix", stopped, returncode=1)
        self.assertEqual(result["status"], "error")
        long = {
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "usage limit " + "x" * 2000,
        }
        self.assertEqual(interpret_claude("fix", long, returncode=1)["status"], "error")
        model = {
            "type": "assistant",
            "message": {
                "model": "claude",
                "content": [{"type": "text", "text": "Usage limit reached"}],
            },
        }
        synthetic = {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Usage limit reached"}],
            },
        }
        self.assertEqual(_text_of(model), "")
        self.assertEqual(_text_of(synthetic), "Usage limit reached")

    def test_a_finished_run_that_mentions_limits_is_not_paused(self):
        event = {
            "subtype": "success",
            "structured_output": {**DONE, "summary": "Added a usage limit."},
        }
        result = interpret_claude("implement", event)
        self.assertEqual(result["status"], "done")
        timeout = interpret_claude("plan", None, timed_out=True, last_text="session limit")
        self.assertEqual(timeout["status"], "error")


if __name__ == "__main__":
    unittest.main()

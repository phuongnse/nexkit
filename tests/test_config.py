import json
import tempfile
import unittest
from pathlib import Path

from nexkit import config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_applied(self):
        cfg = config.validate({"checks": [{"name": "unit", "run": "make test"}]})
        self.assertNotIn("runner", cfg)
        self.assertEqual(cfg["checks"][0]["timeout_minutes"], 15)
        self.assertEqual(cfg["max_auto_fixes"], 2)
        self.assertFalse(cfg["auto_merge"])
        self.assertTrue(cfg["transcript"])
        self.assertEqual(cfg["log"], {"tool_output": "truncated"})

    def test_log_settings(self):
        cfg = config.validate({"transcript": False, "log": {"tool_output": "none"}})
        self.assertFalse(cfg["transcript"])
        self.assertEqual(cfg["log"]["tool_output"], "none")
        self.assertEqual(config.validate({"log": {}})["log"]["tool_output"], "truncated")

    def test_stage_settings_fall_back_to_global_model(self):
        cfg = config.validate(
            {"model": "opus", "effort": "high", "stages": {"review": {"model": "sonnet"}}}
        )
        self.assertEqual(config.stage(cfg, "plan")["model"], "opus")
        self.assertEqual(config.stage(cfg, "review")["model"], "sonnet")
        self.assertEqual(config.stage(cfg, "review")["effort"], "high")

    def test_fix_uses_implement_limits(self):
        cfg = config.validate({"stages": {"implement": {"timeout_minutes": 60}}})
        self.assertEqual(config.stage(cfg, "fix")["timeout_minutes"], 60)

    def test_rejects_unknown_and_invalid_values(self):
        cases = [
            ({"chekcs": []}, "Unknown configuration keys"),
            ({"checks": [{"name": "Unit Tests", "run": "x"}]}, "lowercase identifier"),
            ({"checks": [{"name": "a", "run": "x"}, {"name": "a", "run": "y"}]}, "Duplicate"),
            ({"checks": [{"name": "a", "run": " "}]}, "shell command"),
            ({"stages": {"deploy": {}}}, "Unknown stage"),
            ({"stages": {"plan": {"timeout_minutes": 0}}}, "timeout_minutes"),
            ({"effort": "extreme"}, "effort"),
            ({"max_auto_fixes": 11}, "max_auto_fixes"),
            ({"protected_paths": ["src/"]}, "must include"),
            ({"runner": "ubuntu-latest"}, "Unknown configuration keys"),
            ({"claude_version": "2.0.0"}, "Unknown configuration keys"),
            ({"transcript": "no"}, "transcript must be true or false"),
            ({"log": "none"}, "log must be an object"),
            ({"log": {"tool_output": "full"}}, "log.tool_output"),
            ({"log": {"thinking": True}}, "Unknown keys in log"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                config.validate(raw)

    def test_protected_paths(self):
        cfg = config.validate({})
        self.assertTrue(config.is_protected(".github/workflows/ci.yml", cfg))
        self.assertTrue(config.is_protected("./.nexkit/config.json", cfg))
        self.assertFalse(config.is_protected("github/notes.md", cfg))
        self.assertFalse(config.is_protected("src/.github.py", cfg))

    def test_load_reports_missing_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(config.ConfigError, "nexkit init"):
                config.load(tmp)
            path = Path(tmp) / config.CONFIG_PATH
            path.parent.mkdir()
            path.write_text("{")
            with self.assertRaisesRegex(config.ConfigError, "not valid JSON"):
                config.load(tmp)
            path.write_text(json.dumps({"model": "opus"}))
            self.assertEqual(config.load(tmp)["model"], "opus")


if __name__ == "__main__":
    unittest.main()

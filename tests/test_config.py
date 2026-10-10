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
        self.assertEqual(cfg["after_merge_workflows"], [])
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
            ({"after_merge_workflows": "ci.yml"}, "workflow file names"),
            ({"after_merge_workflows": [".github/workflows/ci.yml"]}, "workflow file names"),
            ({"after_merge_workflows": ["ci"]}, "workflow file names"),
            ({"after_merge_workflows": ["ci.yml", "ci.yml"]}, "twice"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                config.validate(raw)

    def test_after_merge_workflows(self):
        cfg = config.validate({"auto_merge": True, "after_merge_workflows": ["ci.yml", "e2e.yaml"]})
        self.assertEqual(cfg["after_merge_workflows"], ["ci.yml", "e2e.yaml"])

    def test_profiles_layer_over_stages(self):
        cfg = config.validate(
            {
                "model": "sonnet",
                "effort": "high",
                "stages": {"implement": {"timeout_minutes": 60}, "triage": {"model": "fable"}},
                "default_profile": "easy",
                "profiles": {
                    "easy": {"when": "Small changes."},
                    "hard": {
                        "when": "Security.",
                        "stages": {"implement": {"model": "opus", "max_budget_usd": 20}},
                    },
                },
            }
        )
        self.assertEqual(config.stage(cfg, "triage")["model"], "fable")
        self.assertEqual(config.stage(cfg, "triage")["timeout_minutes"], 5)
        easy = config.with_profile(cfg, "easy")
        self.assertEqual(config.stage(easy, "implement")["model"], "sonnet")
        hard = config.with_profile(cfg, "hard")
        self.assertEqual(
            config.stage(hard, "fix"),
            {"model": "opus", "effort": "high", "timeout_minutes": 60, "max_budget_usd": 20},
        )
        self.assertEqual(config.stage(hard, "plan")["model"], "sonnet")
        # The configuration passes between jobs and is validated again there.
        self.assertEqual(config.validate(json.loads(json.dumps(hard))), hard)
        self.assertNotIn("triage", config.validate({})["stages"])

    def test_rejects_invalid_profiles(self):
        good = {"when": "Small changes."}

        def easy(profile, **raw):
            return {"profiles": {"easy": profile}, "default_profile": "easy", **raw}

        def stages(**settings):
            return easy({**good, "stages": settings})

        cases = [
            ({"profiles": {"easy": good}}, "default_profile is required"),
            ({"profiles": {"easy": good}, "default_profile": "hard"}, "must name one of"),
            ({"default_profile": "easy"}, "default_profile needs profiles"),
            ({"stages": {"triage": {"model": "opus"}}}, "stages.triage needs profiles"),
            (easy({}), "profiles.easy.when"),
            (easy({**good, "model": "x"}), "Unknown keys in profiles.easy"),
            ({"profiles": {"Easy": good}, "default_profile": "Easy"}, "lowercase identifier"),
            (stages(triage={}), "cannot set triage"),
            (stages(deploy={}), "Unknown stage 'deploy' in profiles.easy"),
            (stages(plan={"tools": "x"}), "Unknown keys in profiles.easy.stages.plan"),
            (stages(plan={"timeout_minutes": 0}), "profiles.easy.stages.plan.timeout_minutes"),
            (stages(plan={"effort": "huge"}), "profiles.easy.stages.plan.effort"),
            (
                easy(good, stages={"triage": {"timeout_minutes": 31}}),
                "stages.triage.timeout_minutes",
            ),
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

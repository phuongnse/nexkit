import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nexkit import config
from tests.support import CLAUDE, with_claude


def validate(raw, **kw):
    """config.validate with the Claude agent declared, as `nexkit init` writes it."""
    if isinstance(raw, dict) and isinstance(raw.get("agents", {}), dict):
        raw = with_claude(raw)
    return config.validate(raw, **kw)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_applied(self):
        cfg = validate({"checks": [{"name": "unit", "run": "make test"}]})
        self.assertNotIn("runner", cfg)
        self.assertEqual(cfg["checks"][0]["timeout_minutes"], 15)
        self.assertFalse(cfg["checks"][0]["slow"])
        self.assertEqual(cfg["fix"], {"max_auto_rounds": 2})
        self.assertEqual(cfg["merge"], {"auto": False, "after_workflows": []})
        self.assertEqual(cfg["conflicts"], {"auto_resolve": False, "max_rounds": 3})
        self.assertEqual(
            cfg["usage_limit"], {"resume": True, "retry_minutes": 60, "wait_minutes": 330}
        )
        self.assertEqual(cfg["limits"], {"setup_timeout_minutes": 30, "max_patch_mb": 5})
        self.assertFalse(cfg["close_parent_issues"])
        self.assertEqual(cfg["notify"], [])
        self.assertEqual(cfg["cache"], [])
        self.assertTrue(cfg["transcript"])
        self.assertEqual(cfg["log"], {"tool_output": "truncated"})

    def test_log_settings(self):
        cfg = validate({"transcript": False, "log": {"tool_output": "none"}})
        self.assertFalse(cfg["transcript"])
        self.assertEqual(cfg["log"]["tool_output"], "none")
        self.assertEqual(validate({"log": {}})["log"]["tool_output"], "truncated")

    def test_stage_settings_fall_back_to_global_model(self):
        cfg = validate(
            {"model": "opus", "effort": "high", "stages": {"review": {"model": "sonnet"}}}
        )
        self.assertEqual(config.stage(cfg, "plan")["model"], "opus")
        self.assertEqual(config.stage(cfg, "review")["model"], "sonnet")
        self.assertEqual(config.stage(cfg, "review")["effort"], "high")

    def test_fix_uses_implement_limits(self):
        cfg = validate({"stages": {"implement": {"timeout_minutes": 60}}})
        self.assertEqual(config.stage(cfg, "fix")["timeout_minutes"], 60)

    def test_rejects_unknown_and_invalid_values(self):
        cases = [
            ({"chekcs": []}, "Unknown configuration keys"),
            ({"checks": [{"name": "Unit Tests", "run": "x"}]}, "lowercase identifier"),
            ({"checks": [{"name": "a", "run": "x"}, {"name": "a", "run": "y"}]}, "Duplicate"),
            ({"checks": [{"name": "a", "run": " "}]}, "shell command"),
            ({"checks": [{"name": "a", "run": "x", "slow": "yes"}]}, "slow must be true or false"),
            ({"parallel_checks": "yes"}, "parallel_checks must be true or false"),
            ({"checks": [{"name": "a", "run": "x", "group": "g"}]}, "group needs parallel_checks"),
            (
                {"parallel_checks": True, "checks": [{"name": "a", "run": "x", "group": "Slow"}]},
                "group must be a short lowercase identifier",
            ),
            ({"stages": {"deploy": {}}}, "Unknown stage"),
            ({"stages": {"plan": {"timeout_minutes": 0}}}, "timeout_minutes"),
            ({"effort": "extreme"}, "effort"),
            ({"fix": {"max_auto_rounds": 11}}, "fix.max_auto_rounds"),
            ({"fix": {"max_rounds": 1}}, "Unknown keys in fix"),
            ({"merge": {"auto": "yes"}}, "merge.auto must be true or false"),
            ({"conflicts": {"max_rounds": 0}}, "conflicts.max_rounds"),
            ({"usage_limit": {"retry_minutes": 0}}, "usage_limit.retry_minutes"),
            ({"usage_limit": {"wait_minutes": 331}}, "usage_limit.wait_minutes"),
            ({"usage_limit": {"wait_minutes": -1}}, "usage_limit.wait_minutes"),
            ({"usage_limit": []}, "usage_limit must be an object"),
            ({"limits": {"setup_timeout_minutes": 500}}, "limits.setup_timeout_minutes"),
            ({"limits": {"max_patch_mb": 0}}, "limits.max_patch_mb"),
            ({"close_parent_issues": 1}, "close_parent_issues must be true or false"),
            ({"protected_paths": ["src/"]}, "must include"),
            ({"runner": "ubuntu-latest"}, "Unknown configuration keys"),
            ({"claude_version": "2.0.0"}, "Unknown configuration keys"),
            ({"transcript": "no"}, "transcript must be true or false"),
            ({"log": "none"}, "log must be an object"),
            ({"log": {"tool_output": "full"}}, "log.tool_output"),
            ({"log": {"thinking": True}}, "Unknown keys in log"),
            ({"merge": {"after_workflows": "ci.yml"}}, "workflow file names"),
            ({"merge": {"after_workflows": [".github/workflows/ci.yml"]}}, "workflow file names"),
            ({"merge": {"after_workflows": ["ci"]}}, "workflow file names"),
            ({"merge": {"after_workflows": ["ci.yml", "ci.yml"]}}, "twice"),
            ({"autopilot": {"watch_workflows": "ci.yml"}}, "watch_workflows must be a list"),
            ({"autopilot": {"watch_workflows": ["ci"]}}, "workflow file names"),
            ({"autopilot": {"watch_workflows": ["ci.yml", "ci.yml"]}}, "twice"),
            (
                {
                    "merge": {"after_workflows": ["ci.yml"]},
                    "autopilot": {"watch_workflows": ["nightly.yml", "ci.yml"]},
                },
                "watch_workflows lists ci.yml, which merge.after_workflows lists too",
            ),
            ({"autopilot": {"enabled": "yes"}}, "autopilot.enabled must be true or false"),
            ({"autopilot": {"scope": "repository"}}, 'autopilot.scope must be null .* "milestone"'),
            ({"autopilot": {"scope": "label"}}, '"label" is gone; use null .* "milestone"'),
            ({"autopilot": {"label": "go"}}, 'autopilot.label is gone.* null .* "milestone"'),
            ({"autopilot": {"label": None, "scope": "label"}}, '"label" is gone'),
            ({"autopilot": {"max_parallel": 0}}, "autopilot.max_parallel"),
            ({"autopilot": {"answer": "always"}}, "autopilot.answer must be one of"),
            ({"autopilot": {"lost_after_minutes": 4}}, "lost_after_minutes must be .* 5 to 1440"),
            ({"autopilot": {"lost_after_minutes": 1441}}, "autopilot.lost_after_minutes"),
            ({"autopilot": {"lost_after_minutes": True}}, "autopilot.lost_after_minutes"),
            ({"autopilot": {"limit": 1}}, "Unknown keys in autopilot"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                validate(raw)

    def test_check_results_are_paths_in_the_repository(self):
        cfg = validate(
            {
                "checks": [
                    {"name": "e2e", "run": "x", "results": ["test-results", "logs/app.log"]},
                    {"name": "unit", "run": "y"},
                ]
            }
        )
        self.assertEqual(cfg["checks"][0]["results"], ["test-results", "logs/app.log"])
        self.assertEqual(cfg["checks"][1]["results"], [])
        cases = [
            ("test-results", "must be a list"),
            ([""], "must be a list"),
            ([1], "must be a list"),
            (["a\nb"], "must be a list"),
            ([f"r{i}" for i in range(config.MAX_RESULTS + 1)], "at most"),
            (["/tmp/results"], "must be a path inside the repository"),
            (["~/results"], "must be a path inside the repository"),
            (["../results"], "must be a path inside the repository"),
            (["a/../../b"], "must be a path inside the repository"),
            (["logs", "logs"], "lists a path twice"),
        ]
        for results, message in cases:
            raw = {"checks": [{"name": "e2e", "run": "x", "results": results}]}
            with self.subTest(results=results), self.assertRaisesRegex(config.ConfigError, message):
                validate(raw)

    def test_a_check_lists_the_paths_it_never_reads_as_globs(self):
        listed = ["docs/**", "**/*.md", "web/**", "!web/package.json"]
        cfg = validate(
            {
                "checks": [
                    {"name": "e2e", "run": "x", "unaffected_by": listed},
                    {"name": "unit", "run": "y"},
                ]
            }
        )
        self.assertEqual(cfg["checks"][0]["unaffected_by"], listed)
        self.assertEqual(cfg["checks"][1]["unaffected_by"], [])
        cases = [
            ("docs/**", "must be a list"),
            ([1], "must be a list of path globs"),
            (["a" * 201], "at most 200 characters"),
            ([f"d{i}/**" for i in range(config.MAX_UNAFFECTED + 1)], "at most"),
            (["docs/"], r"checks\[0\]\.unaffected_by: 'docs/' ends with `/`"),
            (["a[b"], "without its"),
            (["!docs/**"], "starts with `!` but is first"),
            (["docs/**", "docs/**"], "lists a pattern twice"),
        ]
        for patterns, message in cases:
            raw = {"checks": [{"name": "e2e", "run": "x", "unaffected_by": patterns}]}
            with (
                self.subTest(patterns=patterns),
                self.assertRaisesRegex(config.ConfigError, message),
            ):
                validate(raw)

    def test_autopilot_defaults(self):
        self.assertEqual(
            validate({})["autopilot"],
            {
                "enabled": False,
                "scope": "milestone",
                "max_parallel": None,
                "answer": "never",
                "lost_after_minutes": 30,
                "watch_workflows": [],
            },
        )
        # An explicit null replaces the default: every open issue.
        cfg = validate({"autopilot": {"enabled": True, "scope": None}})
        self.assertEqual(
            (cfg["autopilot"]["scope"], cfg["autopilot"]["max_parallel"]), (None, None)
        )
        # `"label": null`, as the full example of NexKit 4.x showed, is accepted silently.
        cfg = validate({"autopilot": {"enabled": True, "scope": "milestone", "label": None}})
        self.assertNotIn("label", cfg["autopilot"])
        self.assertEqual(cfg["autopilot"]["scope"], "milestone")

    def test_cache_entries(self):
        entry = {
            "name": "gradle",
            "paths": ["~/.gradle/caches", "~/.gradle/wrapper", "!~/.gradle/caches/*.lock"],
            "key_files": ["android/gradle/libs.versions.toml", "**/*.gradle.kts", "!build/**"],
        }
        cfg = validate({"cache": [entry, {**entry, "name": "npm", "paths": ["/opt/x"]}]})
        self.assertEqual(cfg["cache"][0], entry)
        self.assertEqual(validate(json.loads(json.dumps(cfg))), cfg)

    def test_rejects_invalid_cache_entries(self):
        entry = {"name": "npm", "paths": ["~/.npm"], "key_files": ["package-lock.json"]}
        cases = [
            ({"cache": entry}, "cache must be a list"),
            ({"cache": [entry] * (config.MAX_CACHES + 1)}, f"at most {config.MAX_CACHES}"),
            ({"cache": ["npm"]}, r"cache\[0\] must be an object"),
            ({"cache": [{**entry, "save": True}]}, r"Unknown keys in cache\[0\]: save"),
            ({"cache": [{**entry, "name": "Node Modules"}]}, "lowercase identifier"),
            ({"cache": [entry, entry]}, "'npm' and 'npm' would share keys"),
            ({"cache": [entry, {**entry, "name": "npm-e2e"}]}, "would share keys"),
            ({"cache": [{**entry, "paths": []}]}, r"cache\[0\].paths must be a non-empty list"),
            ({"cache": [{**entry, "paths": "~/.npm"}]}, "non-empty list of paths"),
            ({"cache": [{**entry, "paths": ["~/a\n~/b"]}]}, "non-empty list of paths"),
            ({"cache": [{**entry, "paths": ["node_modules"]}]}, "must start with '~/' or '/'"),
            ({"cache": [{**entry, "paths": ["!target"]}]}, "must start with '~/' or '/'"),
            ({"cache": [{**entry, "paths": ["~"]}]}, "must start with '~/' or '/'"),
            ({"cache": [{**entry, "key_files": []}]}, "key_files must be a non-empty list"),
            ({"cache": [{"name": "npm", "paths": ["~/.npm"]}]}, "key_files must be"),
            ({"cache": [{**entry, "key_files": ["/etc/os-release"]}]}, "inside the repository"),
            ({"cache": [{**entry, "key_files": ["~/.npmrc"]}]}, "inside the repository"),
            ({"cache": [{**entry, "key_files": ["!../x"]}]}, "inside the repository"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                validate(raw)

    def test_sweeps_watch_the_workflows_started_after_a_merge_and_the_watched_ones(self):
        cfg = validate({"merge": {"after_workflows": ["ci.yml"]}})
        self.assertEqual(config.watched_workflows(cfg), ["ci.yml"])
        cfg = validate(
            {
                "merge": {"after_workflows": ["ci.yml"]},
                "autopilot": {"watch_workflows": ["nightly.yml", "codeql.yml"]},
            }
        )
        self.assertEqual(config.watched_workflows(cfg), ["ci.yml", "nightly.yml", "codeql.yml"])
        # Only merge.after_workflows is started after a merge.
        self.assertEqual(cfg["merge"]["after_workflows"], ["ci.yml"])

    def test_groups_merge_over_their_defaults(self):
        cfg = validate({"merge": {"after_workflows": ["ci.yml", "e2e.yaml"]}})
        self.assertEqual(cfg["merge"], {"auto": False, "after_workflows": ["ci.yml", "e2e.yaml"]})
        cfg = validate({"conflicts": {"auto_resolve": True}})
        self.assertEqual(cfg["conflicts"]["max_rounds"], 3)
        self.assertEqual(config.setting(cfg, "conflicts.auto_resolve"), True)
        # A validated configuration validates again unchanged, as the pipeline's jobs do.
        self.assertEqual(validate(json.loads(json.dumps(cfg))), cfg)

    def test_renamed_keys_say_where_they_moved(self):
        with self.assertRaises(config.ConfigError) as caught:
            validate({"auto_merge": True, "max_auto_fixes": 1})
        message = str(caught.exception)
        self.assertIn("'auto_merge' is now 'merge.auto'", message)
        self.assertIn("'max_auto_fixes' is now 'fix.max_auto_rounds'", message)
        with self.assertRaisesRegex(config.ConfigError, "'after_merge_workflows' is now"):
            validate({"after_merge_workflows": []})

    def test_profiles_layer_over_stages(self):
        cfg = validate(
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
            {
                "agent": "claude",
                "model": "opus",
                "effort": "high",
                "timeout_minutes": 60,
                "max_budget_usd": 20,
            },
        )
        self.assertEqual(config.stage(hard, "plan")["model"], "sonnet")
        # The configuration passes between jobs and is validated again there.
        self.assertEqual(validate(json.loads(json.dumps(hard))), hard)
        self.assertNotIn("triage", validate({})["stages"])

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
                validate(raw)

    def test_agents(self):
        raw = {
            "agents": {
                "backup": {
                    "runtime": "claude-code",
                    "provider": "anthropic",
                    "api_key_secret": "ANTHROPIC_API_KEY",
                }
            },
            "stages": {"review": {"agent": "backup", "model": "opus"}},
            "profiles": {
                "hard": {"when": "Security.", "stages": {"plan": {"agent": "backup", "model": "x"}}}
            },
            "default_profile": "hard",
        }
        cfg = validate(raw)
        self.assertEqual(cfg["agent"], "claude")
        self.assertEqual(config.stage(cfg, "plan")["agent"], "claude")
        self.assertEqual(config.stage(cfg, "review")["agent"], "backup")
        hard = config.with_profile(cfg, "hard")
        self.assertEqual(
            (config.stage(hard, "plan")["agent"], config.stage(hard, "plan")["model"]),
            ("backup", "x"),
        )
        self.assertEqual(config.agent(cfg, "claude"), CLAUDE["claude"])
        self.assertEqual(config.runtimes(cfg), ["claude-code"])
        self.assertEqual(validate(json.loads(json.dumps(hard))), hard)
        top = validate({**raw, "agent": "backup", "model": "opus"})
        self.assertEqual(config.stage(top, "implement")["agent"], "backup")

    def test_rejects_invalid_agents(self):
        def agents(**definitions):
            return {"agents": definitions}

        claude = {
            "runtime": "claude-code",
            "provider": "anthropic",
            "api_key_secret": "ANTHROPIC_API_KEY",
        }
        cases = [
            ({"agents": []}, "agents must be an object"),
            (agents(x={**claude, "oauth_token_secret": "T"}), "agents.x needs exactly one of"),
            (agents(x={"runtime": "claude-code", "provider": "anthropic"}), "needs exactly one"),
            (agents(Big=claude), "lowercase identifier"),
            (agents(x={"runtime": "aider", "provider": "anthropic"}), "agents.x.runtime"),
            (agents(x={"runtime": "claude-code", "provider": "acme"}), "agents.x.provider"),
            (agents(x={**claude, "endpoint": "https://a.b"}), "Unknown keys in agents.x"),
            ({"agent": "other", "model": "m"}, "agent must name an agent: claude"),
            ({"agent": "claude"}, "agent is set, so model must be set too"),
            (
                {"stages": {"plan": {"agent": "claude"}}},
                "stages.plan sets agent, so it must set model",
            ),
            ({"stages": {"plan": {"agent": "x", "model": "m"}}}, "stages.plan.agent must name"),
            ({"stages": {"plan": {"effort": "minimal"}}}, "stages.plan.effort must be one of low"),
            ({"effort": 3}, "effort must be an effort level"),
        ]
        for raw, message in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(config.ConfigError, message):
                validate(raw)

    def test_a_layered_configuration_validates_again(self):
        # route passes on the configuration with the run's profile applied; every later job
        # validates it again, without layering the other profiles over that profile.
        raw = {
            "agents": {
                "backup": {
                    "runtime": "claude-code",
                    "provider": "anthropic",
                    "api_key_secret": "ANTHROPIC_API_KEY",
                }
            },
            "profiles": {
                "a": {"when": "x", "stages": {"implement": {"model": "opus", "effort": "xhigh"}}},
                "b": {"when": "y", "stages": {"implement": {"agent": "backup", "model": "m"}}},
            },
            "default_profile": "a",
        }
        layered = config.with_profile(validate(raw), "a")
        runtime = config.RUNTIMES["claude-code"]
        with mock.patch.object(runtime, "validate", wraps=runtime.validate) as checked:
            again = validate(json.loads(json.dumps(layered)), layered=True)
        self.assertEqual(again, layered)
        where = [call.args[1] for call in checked.call_args_list]
        self.assertEqual(where, [f"stages.{name}" for name in again["stages"]])

    def test_push_token_secret(self):
        self.assertIsNone(validate({})["publish"]["push_token_secret"])
        named = validate({"publish": {"push_token_secret": "NEXKIT_PUSH_TOKEN"}})
        self.assertEqual(named["publish"]["push_token_secret"], "NEXKIT_PUSH_TOKEN")
        for value, message in (
            ("github_pat_0123456789abcdef", "the config holds only its name"),
            ("GITHUB_TOKEN", "the config holds only its name"),
            ("CLAUDE_CODE_OAUTH_TOKEN", "is an agent's secret"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(config.ConfigError, message):
                validate({"publish": {"push_token_secret": value}})
        with self.assertRaisesRegex(config.ConfigError, "Unknown keys in publish"):
            validate({"publish": {"token": "X"}})

    def test_agents_are_declared(self):
        # A configuration from before agents were declared says what to add.
        with self.assertRaisesRegex(config.ConfigError, "agents is required") as caught:
            config.validate({"model": "opus"})
        self.assertIn(
            '"agents": {"claude": {"runtime": "claude-code", "provider": "anthropic", '
            '"oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"}}',
            str(caught.exception),
        )
        agent = config.agent(config.validate({"agents": CLAUDE}), "claude")
        self.assertEqual(agent, config.CLAUDE_AGENT)

    def test_each_runtime_checks_the_stages_it_runs(self):
        raw = {
            "profiles": {"easy": {"when": "x", "stages": {"review": {"effort": "turbo"}}}},
            "default_profile": "easy",
        }
        with self.assertRaisesRegex(
            config.ConfigError,
            r"profiles\.easy\.stages\.review\.effort must be one of .* for Claude",
        ):
            validate(raw)

    def test_protected_paths(self):
        cfg = validate({})
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
            path.write_text(json.dumps({"agents": CLAUDE, "model": "opus"}))
            self.assertEqual(config.load(tmp)["model"], "opus")


if __name__ == "__main__":
    unittest.main()

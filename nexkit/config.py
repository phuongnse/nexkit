"""Load and validate `.nexkit/config.json`."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

CONFIG_PATH = ".nexkit/config.json"
STAGES = ("plan", "implement", "review")
# Triage chooses a profile before the plan, so only the top-level `stages` can set it.
TRIAGE = "triage"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")

DEFAULTS = {
    "version": 1,
    "model": "sonnet",
    "effort": None,
    "setup": [],
    "checks": [],
    "stages": {
        "plan": {"timeout_minutes": 15, "max_budget_usd": None},
        "implement": {"timeout_minutes": 45, "max_budget_usd": None},
        "review": {"timeout_minutes": 20, "max_budget_usd": None},
    },
    "profiles": {},
    "default_profile": None,
    "max_auto_fixes": 2,
    "auto_merge": False,
    "after_merge_workflows": [],
    "close_parent_issues": False,
    "resume_after_usage_limit": True,
    "protected_paths": [".github/", ".nexkit/"],
    "transcript": True,
    "log": {"tool_output": "truncated"},
}

TOP_KEYS = set(DEFAULTS)
STAGE_KEYS = {"model", "effort", "timeout_minutes", "max_budget_usd"}
PROFILE_KEYS = {"when", "stages"}
TRIAGE_DEFAULTS = {"timeout_minutes": 5, "max_budget_usd": None}
CHECK_KEYS = {"name", "run", "timeout_minutes"}
LOG_KEYS = {"tool_output"}
TOOL_OUTPUT = ("none", "truncated")


class ConfigError(ValueError):
    pass


def _fail(message):
    raise ConfigError(message)


def _positive_int(value, where, maximum):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        _fail(f"{where} must be an integer from 1 to {maximum}")


def _optional_budget(value, where):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        _fail(f"{where} must be a positive number or null")


def _model(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._\-\[\]]+", value):
        _fail(f"{where} must be a model alias such as 'sonnet' or a full model name")


def _effort(value, where):
    if value is not None and value not in EFFORTS:
        _fail(f"{where} must be one of {', '.join(EFFORTS)} or null")


def _stage_keys(settings, where):
    if not isinstance(settings, dict):
        _fail(f"{where} must be an object")
    extra = set(settings) - STAGE_KEYS
    if extra:
        _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")


def _stage_values(settings, where, max_minutes):
    if "model" in settings:
        _model(settings["model"], f"{where}.model")
    if "effort" in settings:
        _effort(settings["effort"], f"{where}.effort")
    _positive_int(settings.get("timeout_minutes"), f"{where}.timeout_minutes", max_minutes)
    _optional_budget(settings.get("max_budget_usd"), f"{where}.max_budget_usd")


def _profiles(cfg):
    """Check `profiles` and `default_profile`. A profile's stage settings are optional:
    what a profile leaves out comes from the top-level `stages`."""
    profiles, default = cfg["profiles"], cfg["default_profile"]
    if not isinstance(profiles, dict):
        _fail("profiles must be an object")
    for name, profile in profiles.items():
        where = f"profiles.{name}"
        if not NAME.match(name):
            _fail(f"Profile name '{name}' must be a short lowercase identifier")
        if not isinstance(profile, dict):
            _fail(f"{where} must be an object")
        extra = set(profile) - PROFILE_KEYS
        if extra:
            _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")
        if not isinstance(profile.get("when"), str) or not profile["when"].strip():
            _fail(f"{where}.when must say which issues belong in this profile")
        stages = profile.setdefault("stages", {})
        if not isinstance(stages, dict):
            _fail(f"{where}.stages must be an object")
        for stage, settings in stages.items():
            if stage == TRIAGE:
                _fail(
                    f"{where}.stages cannot set triage: triage runs before there is a profile, "
                    "so set it in the top-level stages"
                )
            if stage not in STAGES:
                _fail(f"Unknown stage '{stage}' in {where}; stages are {', '.join(STAGES)}")
            _stage_keys(settings, f"{where}.stages.{stage}")
            # Without its own timeout, the stage keeps the one from `stages`.
            _stage_values({"timeout_minutes": 1, **settings}, f"{where}.stages.{stage}", 340)
    if not profiles:
        if default is not None:
            _fail("default_profile needs profiles")
        if TRIAGE in cfg["stages"]:
            _fail("stages.triage needs profiles: triage chooses one of them")
        return
    if default is None:
        _fail("default_profile is required when profiles are set")
    if not isinstance(default, str) or default not in profiles:
        _fail(f"default_profile must name one of the profiles: {', '.join(profiles)}")
    cfg["stages"][TRIAGE] = {**TRIAGE_DEFAULTS, **cfg["stages"].get(TRIAGE, {})}


def validate(raw):
    """Return a complete configuration with defaults applied, or raise ConfigError."""
    if not isinstance(raw, dict):
        _fail("Configuration must be a JSON object")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        _fail(f"Unknown configuration keys: {', '.join(sorted(unknown))}")
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key == "stages":
            if not isinstance(value, dict):
                _fail("stages must be an object")
            for stage, settings in value.items():
                if stage not in (*STAGES, TRIAGE):
                    _fail(f"Unknown stage '{stage}'; stages are {', '.join((*STAGES, TRIAGE))}")
                _stage_keys(settings, f"stages.{stage}")
                cfg["stages"].setdefault(stage, {}).update(settings)
        elif key == "log":
            if not isinstance(value, dict):
                _fail("log must be an object")
            extra = set(value) - LOG_KEYS
            if extra:
                _fail(f"Unknown keys in log: {', '.join(sorted(extra))}")
            cfg["log"].update(value)
        else:
            cfg[key] = copy.deepcopy(value)

    if cfg["version"] != 1:
        _fail("version must be 1")
    _model(cfg["model"], "model")
    _effort(cfg["effort"], "effort")

    if not isinstance(cfg["setup"], list) or not all(
        isinstance(cmd, str) and cmd.strip() for cmd in cfg["setup"]
    ):
        _fail("setup must be a list of shell commands")

    if not isinstance(cfg["checks"], list):
        _fail("checks must be a list")
    names = set()
    for index, check in enumerate(cfg["checks"]):
        where = f"checks[{index}]"
        if not isinstance(check, dict):
            _fail(f"{where} must be an object")
        extra = set(check) - CHECK_KEYS
        if extra:
            _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")
        if not isinstance(check.get("name"), str) or not NAME.match(check["name"]):
            _fail(f"{where}.name must be a short lowercase identifier")
        if check["name"] in names:
            _fail(f"Duplicate check name '{check['name']}'")
        names.add(check["name"])
        if not isinstance(check.get("run"), str) or not check["run"].strip():
            _fail(f"{where}.run must be a shell command")
        check.setdefault("timeout_minutes", 15)
        _positive_int(check["timeout_minutes"], f"{where}.timeout_minutes", 360)

    _profiles(cfg)
    for stage, settings in cfg["stages"].items():
        _stage_values(settings, f"stages.{stage}", 30 if stage == TRIAGE else 340)

    if (
        not isinstance(cfg["max_auto_fixes"], int)
        or isinstance(cfg["max_auto_fixes"], bool)
        or not 0 <= cfg["max_auto_fixes"] <= 10
    ):
        _fail("max_auto_fixes must be an integer from 0 to 10")
    if not isinstance(cfg["auto_merge"], bool):
        _fail("auto_merge must be true or false")
    for key in ("close_parent_issues", "resume_after_usage_limit"):
        if not isinstance(cfg[key], bool):
            _fail(f"{key} must be true or false")
    workflows = cfg["after_merge_workflows"]
    if not isinstance(workflows, list) or not all(
        isinstance(w, str) and WORKFLOW_FILE.match(w) for w in workflows
    ):
        _fail("after_merge_workflows must be a list of workflow file names such as 'ci.yml'")
    if len(set(workflows)) != len(workflows):
        _fail("after_merge_workflows lists a workflow twice")
    paths = cfg["protected_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        _fail("protected_paths must be a list of path prefixes")
    for required in (".github/", ".nexkit/"):
        if required not in paths:
            _fail(f"protected_paths must include '{required}'")
    if not isinstance(cfg["transcript"], bool):
        _fail("transcript must be true or false")
    if cfg["log"]["tool_output"] not in TOOL_OUTPUT:
        _fail(f"log.tool_output must be one of {', '.join(TOOL_OUTPUT)}")
    return cfg


def load(root="."):
    path = Path(root) / CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Missing {CONFIG_PATH}; run 'nexkit init'") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CONFIG_PATH} is not valid JSON: {exc}") from None
    return validate(raw)


def with_profile(cfg, name):
    """The configuration with a profile's stage settings layered over `stages`."""
    cfg = copy.deepcopy(cfg)
    for stage, settings in cfg["profiles"][name]["stages"].items():
        cfg["stages"][stage].update(settings)
    return cfg


def stage(cfg, name):
    """Effective model and limits for a stage. Fix rounds use the implement settings."""
    settings = cfg["stages"]["implement" if name == "fix" else name]
    return {
        "model": settings.get("model") or cfg["model"],
        "effort": settings.get("effort", cfg["effort"]),
        "timeout_minutes": settings["timeout_minutes"],
        "max_budget_usd": settings.get("max_budget_usd"),
    }


def is_protected(path, cfg):
    path = path.removeprefix("./")
    return any(path == p.rstrip("/") or path.startswith(p) for p in cfg["protected_paths"])

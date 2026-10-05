"""Load and validate `.nexkit/config.json`."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

CONFIG_PATH = ".nexkit/config.json"
STAGES = ("plan", "implement", "review")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

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
    "max_auto_fixes": 2,
    "auto_merge": False,
    "protected_paths": [".github/", ".nexkit/"],
    "transcript": True,
    "log": {"tool_output": "truncated"},
}

TOP_KEYS = set(DEFAULTS)
STAGE_KEYS = {"model", "effort", "timeout_minutes", "max_budget_usd"}
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
                if stage not in STAGES:
                    _fail(f"Unknown stage '{stage}'; stages are {', '.join(STAGES)}")
                if not isinstance(settings, dict):
                    _fail(f"stages.{stage} must be an object")
                extra = set(settings) - STAGE_KEYS
                if extra:
                    _fail(f"Unknown keys in stages.{stage}: {', '.join(sorted(extra))}")
                cfg["stages"][stage].update(settings)
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

    for stage in STAGES:
        settings = cfg["stages"][stage]
        where = f"stages.{stage}"
        if "model" in settings:
            _model(settings["model"], f"{where}.model")
        if "effort" in settings:
            _effort(settings["effort"], f"{where}.effort")
        _positive_int(settings.get("timeout_minutes"), f"{where}.timeout_minutes", 340)
        _optional_budget(settings.get("max_budget_usd"), f"{where}.max_budget_usd")

    if (
        not isinstance(cfg["max_auto_fixes"], int)
        or isinstance(cfg["max_auto_fixes"], bool)
        or not 0 <= cfg["max_auto_fixes"] <= 10
    ):
        _fail("max_auto_fixes must be an integer from 0 to 10")
    if not isinstance(cfg["auto_merge"], bool):
        _fail("auto_merge must be true or false")
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

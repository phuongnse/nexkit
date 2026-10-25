"""Load and validate `.nexkit/config.json`."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .runtimes import PROVIDERS, REMOVED, RUNTIMES, SECRET_SLOTS
from .runtimes.base import SECRET_NAME, AgentConfigError

CONFIG_PATH = ".nexkit/config.json"
STAGES = ("plan", "implement", "review")
# Triage chooses a profile before the plan, so only the top-level `stages` can set it.
TRIAGE = "triage"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")

DEFAULTS = {
    "version": 1,
    "agents": {},
    "agent": "claude",
    "model": "sonnet",
    "effort": None,
    "setup": [],
    "checks": [],
    "cache": [],
    "stages": {
        "plan": {"timeout_minutes": 15, "max_budget_usd": None},
        "implement": {"timeout_minutes": 45, "max_budget_usd": None},
        "review": {"timeout_minutes": 20, "max_budget_usd": None},
    },
    "profiles": {},
    "default_profile": None,
    # What NexKit does by itself, grouped by feature.
    "fix": {"max_auto_rounds": 2},
    "merge": {"auto": False, "after_workflows": []},
    "conflicts": {"auto_resolve": False, "max_rounds": 3},
    "usage_limit": {"resume": True, "retry_minutes": 60, "wait_minutes": 330},
    "close_parent_issues": False,
    # Work through the open issues in scope without commands (nexkit/autopilot.py).
    "autopilot": {
        "enabled": False,
        "scope": "milestone",
        "max_parallel": None,
        "answer": "never",
        # A command autopilot started with no run and no run comment after this long
        # starts once more.
        "lost_after_minutes": 30,
    },
    "notify": [],
    "limits": {"setup_timeout_minutes": 30, "max_patch_mb": 5},
    "protected_paths": [".github/", ".nexkit/"],
    "transcript": True,
    "log": {"tool_output": "truncated"},
    # The secret with the token that pushes NexKit's branches, or the Actions token.
    "publish": {"push_token_secret": None},
    # The artifacts of the CI runs an issue names that the implement and fix agents get.
    "evidence": {"artifacts": []},
}

TOP_KEYS = set(DEFAULTS)
# The longest a run waits for a usage limit to reset: a GitHub-hosted job ends after six
# hours, and the wait needs time left to start the commands.
MAX_WAIT = 330
# The longest a stage may run, with all of its agents.
MAX_STAGE_MINUTES = 340
# Objects whose keys are merged over the defaults one by one.
GROUPS = (
    "fix",
    "merge",
    "conflicts",
    "usage_limit",
    "autopilot",
    "limits",
    "log",
    "publish",
    "evidence",
)
# The most artifact names `evidence.artifacts` may list.
MAX_ARTIFACTS = 10
# Keys of earlier releases, with where they live now.
RENAMED = {
    "max_auto_fixes": "fix.max_auto_rounds",
    "auto_merge": "merge.auto",
    "after_merge_workflows": "merge.after_workflows",
}
STAGE_KEYS = {"agent", "model", "effort", "timeout_minutes", "max_budget_usd"}
AGENT_KEYS = {"runtime", "provider", "fallback"}
PROFILE_KEYS = {"when", "stages"}
TRIAGE_DEFAULTS = {"timeout_minutes": 5, "max_budget_usd": None}
CHECK_KEYS = {"name", "run", "timeout_minutes", "slow", "results"}
# The most result paths one check may name.
MAX_RESULTS = 20
CACHE_KEYS = {"name", "paths", "key_files"}
# The restore action has one step per entry: .github/actions/restore-caches/action.yml.
MAX_CACHES = 8
TOOL_OUTPUT = ("none", "truncated")
# `null` takes every open issue.
AUTOPILOT_SCOPES = ("milestone", None)
AUTOPILOT_SCOPE_CHOICES = (
    'null for every open issue, or "milestone" for the open issues of the earliest open milestone'
)
AUTOPILOT_ANSWERS = ("never", "recommended")


class ConfigError(ValueError):
    pass


def _fail(message):
    raise ConfigError(message)


def _positive_int(value, where, maximum, minimum=1):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        _fail(f"{where} must be an integer from {minimum} to {maximum}")


def _bool(value, where):
    if not isinstance(value, bool):
        _fail(f"{where} must be true or false")


def _optional_budget(value, where):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        _fail(f"{where} must be a positive number or null")


def _model(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._\-\[\]]+", value):
        _fail(f"{where} must be a model name, such as 'sonnet' or a deployment name")


def _effort(value, where):
    # Each runtime checks the value against the efforts it supports.
    if value is not None and (not isinstance(value, str) or not NAME.match(value)):
        _fail(f"{where} must be an effort level such as 'high', or null")


def _stage_keys(settings, where):
    if not isinstance(settings, dict):
        _fail(f"{where} must be an object")
    extra = set(settings) - STAGE_KEYS
    if extra:
        _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")
    # A model name means something only to its agent, so the two are chosen together.
    if "agent" in settings and "model" not in settings:
        _fail(f"{where} sets agent, so it must set model too")


# What a configuration from before declared agents adds to keep running on Claude.
CLAUDE_AGENT = {
    "runtime": "claude-code",
    "provider": "anthropic",
    "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN",
}


def _agent_name(value, cfg, where):
    names = list(cfg["agents"])
    if not names:
        _fail(
            "agents is required: declare each agent with the secrets it uses. To keep running "
            f'on Claude, add "agents": {{"claude": {json.dumps(CLAUDE_AGENT)}}} to '
            f'{CONFIG_PATH}, with "api_key_secret": "ANTHROPIC_API_KEY" instead of '
            "oauth_token_secret if you use an API key"
        )
    if value not in names:
        _fail(f"{where} must name an agent: {', '.join(names)}")


def _stage_values(settings, where, max_minutes, cfg):
    if "agent" in settings:
        _agent_name(settings["agent"], cfg, f"{where}.agent")
    if "model" in settings:
        _model(settings["model"], f"{where}.model")
    if "effort" in settings:
        _effort(settings["effort"], f"{where}.effort")
    _positive_int(settings.get("timeout_minutes"), f"{where}.timeout_minutes", max_minutes)
    _optional_budget(settings.get("max_budget_usd"), f"{where}.max_budget_usd")


def _patterns(value, where, what):
    if not isinstance(value, list) or not value:
        _fail(f"{where} must be a non-empty list of {what}")
    for pattern in value:
        if not isinstance(pattern, str) or not pattern.strip() or "\n" in pattern:
            _fail(f"{where} must be a non-empty list of {what}")


def _results(paths, where):
    """Check a check's `results`: files or folders inside the repository that NexKit keeps
    when the check fails."""
    if not isinstance(paths, list) or len(paths) > MAX_RESULTS:
        _fail(f"{where} must be a list of at most {MAX_RESULTS} paths in the repository")
    for path in paths:
        if not isinstance(path, str) or not path.strip() or "\n" in path:
            _fail(f"{where} must be a list of at most {MAX_RESULTS} paths in the repository")
        if path.startswith(("/", "~")) or ".." in path.split("/"):
            _fail(f"{where}: '{path}' must be a path inside the repository")
    if len(set(paths)) != len(paths):
        _fail(f"{where} lists a path twice")


def _caches(entries):
    """Check `cache`. Paths are outside the checkout: NexKit checks the repository out in a
    subdirectory, so a path inside it would not match the cache the base branch saved."""
    if not isinstance(entries, list):
        _fail("cache must be a list")
    if len(entries) > MAX_CACHES:
        _fail(f"cache has at most {MAX_CACHES} entries")
    names = []
    for index, entry in enumerate(entries):
        where = f"cache[{index}]"
        if not isinstance(entry, dict):
            _fail(f"{where} must be an object")
        extra = set(entry) - CACHE_KEYS
        if extra:
            _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")
        name = entry.get("name")
        if not isinstance(name, str) or not NAME.match(name):
            _fail(f"{where}.name must be a short lowercase identifier")
        # Keys are `nexkit-<os>-<name>-<hash>`, and the prefix finds a partial match.
        for other in names:
            if name == other or name.startswith(f"{other}-") or other.startswith(f"{name}-"):
                _fail(f"Cache names '{other}' and '{name}' would share keys; rename one")
        names.append(name)
        _patterns(entry.get("paths"), f"{where}.paths", "paths")
        for path in entry["paths"]:
            if not path.removeprefix("!").startswith(("~/", "/")):
                _fail(
                    f"{where}.paths: '{path}' must start with '~/' or '/'. Cache what lives "
                    "outside the repository, such as ~/.npm or ~/.gradle/caches"
                )
        _patterns(entry.get("key_files"), f"{where}.key_files", "file patterns")
        for pattern in entry["key_files"]:
            path = pattern.removeprefix("!")
            if path.startswith(("/", "~")) or ".." in path.split("/"):
                _fail(f"{where}.key_files: '{pattern}' must be a path inside the repository")


def _agents(cfg):
    """Check `agents`: each pairs a runtime with a provider and the provider's settings."""
    agents = cfg["agents"]
    if not isinstance(agents, dict):
        _fail("agents must be an object")
    for name, agent in agents.items():
        where = f"agents.{name}"
        if not isinstance(name, str) or not NAME.match(name):
            _fail(f"Agent name '{name}' must be a short lowercase identifier")
        if not isinstance(agent, dict):
            _fail(f"{where} must be an object")
        runtime = RUNTIMES.get(agent.get("runtime"))
        if not runtime:
            removed = REMOVED.get(agent.get("runtime"))
            _fail(
                f"{where}.runtime must be one of {', '.join(RUNTIMES)}"
                + (f": {removed}" if removed else "")
            )
        provider = PROVIDERS.get(agent.get("provider"))
        if not provider or provider.name not in runtime.providers:
            removed = REMOVED.get(agent.get("provider"))
            if removed:
                _fail(f"{where}.provider: {removed}")
            _fail(
                f"{where}.provider must be one of {', '.join(runtime.providers)} for {runtime.name}"
            )
        extra = set(agent) - AGENT_KEYS - set(provider.fields)
        if extra:
            _fail(f"Unknown keys in {where}: {', '.join(sorted(extra))}")
        for key, field in provider.fields.items():
            if key in agent:
                try:
                    field.check(agent[key], f"{where}.{key}")
                except AgentConfigError as exc:
                    _fail(str(exc))
            elif field.required:
                _fail(f"{where}.{key} is required for the {provider.name} provider")
        try:
            provider.check(agent, where)
        except AgentConfigError as exc:
            _fail(str(exc))
    for name, agent in agents.items():
        if "fallback" in agent:
            _fallback(cfg, name, agent)


def _fallback(cfg, name, agent):
    """Check `agents.<name>.fallback`: the agents that run a stage in turn when this one
    stops at a usage limit. They share its runtime, which the stage's model is meant for."""
    where = f"agents.{name}.fallback"
    names = agent["fallback"]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        _fail(f"{where} must be a non-empty list of agent names")
    runtime = RUNTIMES[agent["runtime"]]
    if not runtime.reports_limits:
        _fail(
            f"{where}: {runtime.label} does not report usage limits, so a fallback would never "
            "run; remove it"
        )
    for other in names:
        if other == name:
            _fail(f"{where} cannot name the agent itself")
        if other not in cfg["agents"]:
            _fail(f"{where}: '{other}' is not an agent: {', '.join(cfg['agents'])}")
        if cfg["agents"][other]["runtime"] != agent["runtime"]:
            _fail(
                f"{where}: '{other}' must use the {runtime.name} runtime too, because a stage's "
                "model and effort are meant for one runtime"
            )
    if len(set(names)) != len(names):
        _fail(f"{where} names an agent twice")


def _runtime_settings(cfg, profiles=True):
    """Each runtime checks the settings of every stage it runs, in every profile."""
    variants = [("stages", cfg)]
    if profiles:
        variants += [(f"profiles.{p}.stages", with_profile(cfg, p)) for p in cfg["profiles"]]
    for prefix, variant in variants:
        for name in variant["stages"]:
            if prefix != "stages" and name == TRIAGE:
                continue
            settings = stage(variant, name)
            runtime = RUNTIMES[agent(variant, settings["agent"])["runtime"]]
            try:
                runtime.validate(settings, f"{prefix}.{name}")
            except AgentConfigError as exc:
                _fail(str(exc))


def _secret_slots(cfg):
    """No job may need more agent secrets than the pipeline has slots for."""
    variants = [cfg, *(with_profile(cfg, name) for name in cfg["profiles"])]
    jobs = [("plan", variants, ["plan", *([TRIAGE] if cfg["profiles"] else [])])]
    jobs += [(name, [variant], [name]) for variant in variants for name in ("implement", "review")]
    for job, configs, stages in jobs:
        names = secret_names(configs, stages)
        if len(names) > SECRET_SLOTS:
            _fail(
                f"The {job} stage can need {len(names)} agent secrets ({', '.join(names)}); "
                f"NexKit passes at most {SECRET_SLOTS} to one job"
            )


def _push_token(cfg):
    """`publish.push_token_secret`: null, or a secret that no agent uses, since the publish
    job may write to the repository and agents must never hold its token."""
    name = cfg["publish"]["push_token_secret"]
    if name is None:
        return
    if not isinstance(name, str) or not SECRET_NAME.match(name) or name.startswith("GITHUB_"):
        _fail(
            "publish.push_token_secret must be null or the name of a repository secret, such "
            "as NEXKIT_PUSH_TOKEN; the config holds only its name"
        )
    variants = [cfg, *(with_profile(cfg, profile) for profile in cfg["profiles"])]
    if name in secret_names(variants, list(cfg["stages"])):
        _fail(f"publish.push_token_secret: {name} is an agent's secret; use another secret")


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
            settings = {"timeout_minutes": 1, **settings}
            _stage_values(settings, f"{where}.stages.{stage}", MAX_STAGE_MINUTES, cfg)
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


def _autopilot(settings):
    _bool(settings["enabled"], "autopilot.enabled")
    if settings["scope"] == "label":
        _fail(f'autopilot.scope "label" is gone; use {AUTOPILOT_SCOPE_CHOICES}')
    if settings["scope"] not in AUTOPILOT_SCOPES:
        _fail(f"autopilot.scope must be {AUTOPILOT_SCOPE_CHOICES}")
    if settings["max_parallel"] is not None:
        _positive_int(settings["max_parallel"], "autopilot.max_parallel", 100)
    if settings["answer"] not in AUTOPILOT_ANSWERS:
        _fail(f"autopilot.answer must be one of {', '.join(AUTOPILOT_ANSWERS)}")
    _positive_int(settings["lost_after_minutes"], "autopilot.lost_after_minutes", 1440, minimum=5)


def validate(raw, *, layered=False):
    """Return a complete configuration with defaults applied, or raise ConfigError.

    `layered` is for the configuration that `route` passes to the other jobs: it may
    already hold one profile's settings in `stages`, so the other profiles are not checked
    on top of them again. `route` checked them on the configuration as written."""
    if not isinstance(raw, dict):
        _fail("Configuration must be a JSON object")
    renamed = [f"'{old}' is now '{RENAMED[old]}'" for old in sorted(set(raw) & set(RENAMED))]
    if renamed:
        _fail(
            "Configuration keys were renamed: " + "; ".join(renamed) + ". Move them in "
            f"{CONFIG_PATH} (see docs/configuration.md)"
        )
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
        elif key in GROUPS:
            if not isinstance(value, dict):
                _fail(f"{key} must be an object")
            if key == "autopilot" and "label" in value:
                if value["label"] is not None:
                    _fail(
                        'autopilot.label is gone with the "label" scope; remove it and set '
                        f"autopilot.scope to {AUTOPILOT_SCOPE_CHOICES}"
                    )
                # `"label": null`, as the full example of NexKit 4.x showed, set nothing.
                value = {k: v for k, v in value.items() if k != "label"}
            extra = set(value) - set(DEFAULTS[key])
            if extra:
                _fail(f"Unknown keys in {key}: {', '.join(sorted(extra))}")
            cfg[key].update(copy.deepcopy(value))
        else:
            cfg[key] = copy.deepcopy(value)

    if cfg["version"] != 1:
        _fail("version must be 1")
    _agents(cfg)
    _agent_name(cfg["agent"], cfg, "agent")
    if "agent" in raw and "model" not in raw:
        _fail(
            "agent is set, so model must be set too: a model name means something only to its agent"
        )
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
        check.setdefault("slow", False)
        _bool(check["slow"], f"{where}.slow")
        _results(check.setdefault("results", []), f"{where}.results")
    _caches(cfg["cache"])

    _profiles(cfg)
    for name, settings in cfg["stages"].items():
        _stage_values(settings, f"stages.{name}", 30 if name == TRIAGE else MAX_STAGE_MINUTES, cfg)
    _runtime_settings(cfg, profiles=not layered)
    if not layered:
        _secret_slots(cfg)

    _positive_int(cfg["fix"]["max_auto_rounds"], "fix.max_auto_rounds", 10, minimum=0)
    _bool(cfg["merge"]["auto"], "merge.auto")
    workflows = cfg["merge"]["after_workflows"]
    if not isinstance(workflows, list) or not all(
        isinstance(w, str) and WORKFLOW_FILE.match(w) for w in workflows
    ):
        _fail("merge.after_workflows must be a list of workflow file names such as 'ci.yml'")
    if len(set(workflows)) != len(workflows):
        _fail("merge.after_workflows lists a workflow twice")
    _bool(cfg["conflicts"]["auto_resolve"], "conflicts.auto_resolve")
    _positive_int(cfg["conflicts"]["max_rounds"], "conflicts.max_rounds", 10)
    _bool(cfg["usage_limit"]["resume"], "usage_limit.resume")
    _positive_int(cfg["usage_limit"]["retry_minutes"], "usage_limit.retry_minutes", 1440)
    _positive_int(
        cfg["usage_limit"]["wait_minutes"], "usage_limit.wait_minutes", MAX_WAIT, minimum=0
    )
    _bool(cfg["close_parent_issues"], "close_parent_issues")
    _autopilot(cfg["autopilot"])
    _positive_int(cfg["limits"]["setup_timeout_minutes"], "limits.setup_timeout_minutes", 120)
    _positive_int(cfg["limits"]["max_patch_mb"], "limits.max_patch_mb", 50)
    people = cfg["notify"]
    if not isinstance(people, list) or not all(
        isinstance(p, str) and LOGIN.match(p) for p in people
    ):
        _fail("notify must be a list of GitHub logins without '@', such as [\"octocat\"]")
    if len({p.lower() for p in people}) != len(people):
        _fail("notify lists a login twice")
    paths = cfg["protected_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        _fail("protected_paths must be a list of path prefixes")
    for required in (".github/", ".nexkit/"):
        if required not in paths:
            _fail(f"protected_paths must include '{required}'")
    _bool(cfg["transcript"], "transcript")
    if cfg["log"]["tool_output"] not in TOOL_OUTPUT:
        _fail(f"log.tool_output must be one of {', '.join(TOOL_OUTPUT)}")
    _push_token(cfg)
    _artifacts(cfg["evidence"]["artifacts"])
    return cfg


def _artifacts(names):
    """`evidence.artifacts`: names of artifacts that the repository's CI workflows upload."""
    where = "evidence.artifacts"
    if not isinstance(names, list) or len(names) > MAX_ARTIFACTS:
        _fail(f"{where} must be a list of at most {MAX_ARTIFACTS} artifact names")
    for name in names:
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or "\n" in name:
            _fail(f'{where} must be a list of artifact names, such as "test-results"')
    if len(set(names)) != len(names):
        _fail(f"{where} lists an artifact twice")


def setting(cfg, path):
    """The value at a dotted path such as `merge.auto`."""
    value = cfg
    for part in path.split("."):
        value = value[part]
    return value


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
    """Effective agent, model and limits for a stage. Fix rounds use the implement settings."""
    settings = cfg["stages"]["implement" if name == "fix" else name]
    return {
        "agent": settings.get("agent") or cfg["agent"],
        "model": settings.get("model") or cfg["model"],
        "effort": settings.get("effort", cfg["effort"]),
        "timeout_minutes": settings["timeout_minutes"],
        "max_budget_usd": settings.get("max_budget_usd"),
    }


def agent(cfg, name):
    """The definition of the agent `name`."""
    return cfg["agents"][name]


def chain(cfg, name):
    """The agents that run a stage of the agent `name`, in order: the agent, then its
    fallbacks. Only the agent's own `fallback` counts, not that of its fallbacks."""
    return [name, *cfg["agents"][name].get("fallback", [])]


def stage_minutes(cfg, name):
    """The longest a stage may run: its timeout for each agent that may run it in turn."""
    settings = stage(cfg, name)
    attempts = len(chain(cfg, settings["agent"]))
    return min(settings["timeout_minutes"] * attempts, MAX_STAGE_MINUTES)


def agents_used(cfg):
    """The names of the agents that some stage may run, in any profile, with fallbacks."""
    variants = [cfg, *(with_profile(cfg, name) for name in cfg["profiles"])]
    used = {
        member
        for variant in variants
        for name in variant["stages"]
        for member in chain(variant, stage(variant, name)["agent"])
    }
    return [name for name in cfg["agents"] if name in used]


def runtimes(cfg):
    """The runtimes that some stage uses, in any profile, in registry order."""
    used = {agent(cfg, name)["runtime"] for name in agents_used(cfg)}
    return [name for name in RUNTIMES if name in used]


def secret_names(variants, stages):
    """The agent secrets that `stages` need in any of the configurations `variants`."""
    names = set()
    for variant in variants:
        for name in stages:
            for member in chain(variant, stage(variant, name)["agent"]):
                chosen = agent(variant, member)
                for group in PROVIDERS[chosen["provider"]].secrets(chosen):
                    names.update(group)
    return sorted(names)


def credentials(cfg):
    """(agent name, secrets of which one must be set) for each secret that an agent a stage
    may run needs, fallbacks included."""
    return [
        (name, group)
        for name in agents_used(cfg)
        for group in PROVIDERS[agent(cfg, name)["provider"]].secrets(agent(cfg, name))
    ]


def is_protected(path, cfg):
    path = path.removeprefix("./")
    return any(path == p.rstrip("/") or path.startswith(p) for p in cfg["protected_paths"])

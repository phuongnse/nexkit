"""Load and validate `.nexkit/config.json`."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .globs import GlobError, compile_globs
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
    # Run the checks in parallel jobs: each `group`, and each check without one.
    "parallel_checks": False,
    "cache": [],
    "stages": {
        "plan": {"timeout_minutes": 15, "max_budget_usd": None},
        "implement": {"timeout_minutes": 45, "max_budget_usd": None},
        "review": {"timeout_minutes": 20, "max_budget_usd": None},
    },
    "profiles": {},
    "default_profile": None,
    # Short rules, in the repository's words, that each plan and review answers one by one.
    "review": {"checklist": []},
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
        # Workflows whose runs on the default branch sweeps watch, as they watch
        # `merge.after_workflows`, but which NexKit does not start after its merges.
        "watch_workflows": [],
        # Workflows of the default branch whose issues autopilot takes, by their marker.
        "trusted_workflows": [],
        # The label of an issue that runs only once the rest of its milestone is closed.
        "last_in_milestone_label": None,
        # The lowest severity of Dependabot alert that gets an issue, or None for none.
        "dependabot": None,
    },
    "notify": [],
    "limits": {"setup_timeout_minutes": 30, "max_patch_mb": 5},
    "protected_paths": [".github/", ".nexkit/"],
    "transcript": True,
    "log": {"tool_output": "truncated"},
    # The GitHub App that NexKit acts as: its slug, and the secrets with its id and key.
    "github_app": {"slug": None, "app_id_secret": None, "private_key_secret": None},
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
    "review",
    "fix",
    "merge",
    "conflicts",
    "usage_limit",
    "autopilot",
    "limits",
    "log",
    "github_app",
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
CHECK_KEYS = {"name", "run", "timeout_minutes", "slow", "results", "group", "unaffected_by"}
# The most result paths one check may name.
MAX_RESULTS = 20
# The most patterns one check's `unaffected_by` may list.
MAX_UNAFFECTED = 50
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
# GitHub's severities of an advisory, lowest first.
SEVERITIES = ("low", "medium", "high", "critical")
AFTER_WORKFLOW_KEYS = {"workflow", "every"}
# Each item costs every plan and review an answer, so a checklist stays short.
MAX_CHECKLIST = 20
MAX_CHECKLIST_ITEM = 300
# GitHub's limit on a label's name.
MAX_LABEL = 50
# GitHub's slug of an App, which `<slug>[bot]` makes its login.
APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# The secrets of `github_app`, with the names that `nexkit init` gives them.
APP_SECRETS = {"app_id_secret": "NEXKIT_APP_ID", "private_key_secret": "NEXKIT_APP_PRIVATE_KEY"}
APP_DOCS = "https://github.com/phuongnse/nexkit/blob/main/docs/configuration.md#the-github-app"
NO_TOKEN = (
    "NexKit could not get a token from its GitHub App: check that the App is installed on this "
    "repository and has NexKit's permissions, and that the secrets in github_app are set. Run "
    "'nexkit doctor --app-id ID --app-private-key FILE', or see "
    "https://github.com/phuongnse/nexkit/blob/main/docs/troubleshooting.md#the-github-app"
)
# The most merges an entry of `merge.after_workflows` may wait for.
MAX_EVERY = 1000


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


def _unaffected_by(patterns, where):
    """Check a check's `unaffected_by`: globs of the paths it never reads (nexkit/globs.py)."""
    if not isinstance(patterns, list) or len(patterns) > MAX_UNAFFECTED:
        _fail(f"{where} must be a list of at most {MAX_UNAFFECTED} path globs")
    if not all(isinstance(pattern, str) and len(pattern) <= 200 for pattern in patterns):
        _fail(f"{where} must be a list of path globs of at most 200 characters")
    try:
        compile_globs(patterns)
    except GlobError as exc:
        _fail(f"{where}: {exc}")
    if len(set(patterns)) != len(patterns):
        _fail(f"{where} lists a pattern twice")


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


def _checklist(items):
    """`review.checklist`: short items, one line each."""
    where = "review.checklist"
    if not isinstance(items, list) or len(items) > MAX_CHECKLIST:
        _fail(f"{where} must be a list of at most {MAX_CHECKLIST} items")
    for item in items:
        if (
            not isinstance(item, str)
            or not item.strip()
            or "\n" in item
            or len(item) > MAX_CHECKLIST_ITEM
        ):
            _fail(f"{where} items must be one line of at most {MAX_CHECKLIST_ITEM} characters")
    if len(set(items)) != len(items):
        _fail(f"{where} lists an item twice")


def _github_app(cfg):
    """`github_app`: unset, or a slug and two secrets that no agent uses, since an agent
    must never hold what mints a token that can write to the repository."""
    app = cfg["github_app"]
    if not any(value is not None for value in app.values()):
        return
    if not isinstance(app["slug"], str) or not APP_SLUG.match(app["slug"]):
        _fail("github_app.slug must be the App's slug, as in github.com/apps/<slug>")
    variants = [cfg, *(with_profile(cfg, profile) for profile in cfg["profiles"])]
    agents = secret_names(variants, list(cfg["stages"]))
    for key in APP_SECRETS:
        name = app[key]
        if not isinstance(name, str) or not SECRET_NAME.match(name) or name.startswith("GITHUB_"):
            _fail(
                f"github_app.{key} must be the name of a secret, such as {APP_SECRETS[key]}; "
                "the config holds only its name"
            )
        if name in agents:
            _fail(f"github_app.{key}: {name} is an agent's secret; use another secret")
    if app["app_id_secret"] == app["private_key_secret"]:
        _fail("github_app.app_id_secret and github_app.private_key_secret must differ")


def app_problem(cfg):
    """Why NexKit cannot act on GitHub with this configuration, or None. NexKit writes only
    as a GitHub App, so `nexkit doctor` and the start of each run refuse one without it."""
    if cfg["github_app"]["slug"]:
        return None
    return (
        "github_app is not set: NexKit acts on GitHub as a GitHub App. Run 'nexkit init', or "
        f"see {APP_DOCS}"
    )


def read_app(root="."):
    """The `github_app` of the configuration in `root`. A configuration wrong elsewhere
    still gives it, so `route` gets a token to say what is wrong; one without a usable App
    raises what is wrong, since nothing can be said on GitHub without a token."""
    try:
        cfg = load(root)
    except ConfigError as exc:
        try:
            raw = json.loads((Path(root) / CONFIG_PATH).read_text(encoding="utf-8"))
            found = {**DEFAULTS["github_app"], **raw["github_app"]}
            _github_app({"github_app": found, "profiles": {}, "stages": {}})
        except (OSError, ValueError, KeyError, TypeError):
            raise exc from None
        if not found["slug"]:
            raise exc
        return found
    problem = app_problem(cfg)
    if problem:
        raise ConfigError(problem)
    return cfg["github_app"]


def app_login(cfg):
    """The login that NexKit's comments, issues and commits carry, or None without an App."""
    slug = (cfg.get("github_app") or {}).get("slug")
    return f"{slug}[bot]" if slug else None


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
    label = settings["last_in_milestone_label"]
    if label is not None and (
        not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL
    ):
        _fail(f"autopilot.last_in_milestone_label must be null or a label of {MAX_LABEL} or fewer")
    if settings["dependabot"] is not None and settings["dependabot"] not in SEVERITIES:
        _fail(f"autopilot.dependabot must be null or one of {', '.join(SEVERITIES)}")


def _workflow_files(workflows, where):
    if not isinstance(workflows, list) or not all(
        isinstance(w, str) and WORKFLOW_FILE.match(w) for w in workflows
    ):
        _fail(f"{where} must be a list of workflow file names such as 'ci.yml'")
    if len(set(workflows)) != len(workflows):
        _fail(f"{where} lists a workflow twice")


def _after_workflows(entries):
    """`merge.after_workflows`: workflow files, each alone or as {"workflow", "every"}."""
    where = "merge.after_workflows"
    if not isinstance(entries, list):
        _fail(f"{where} must be a list of workflow file names such as 'ci.yml'")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        extra = set(entry) - AFTER_WORKFLOW_KEYS
        if extra:
            _fail(f"Unknown keys in {where}[{index}]: {', '.join(sorted(extra))}")
        if "workflow" not in entry:
            _fail(f"{where}[{index}] must name its workflow file, such as 'ci.yml'")
        _positive_int(entry.get("every", 1), f"{where}[{index}].every", MAX_EVERY)
    names = [entry["workflow"] if isinstance(entry, dict) else entry for entry in entries]
    _workflow_files(names, where)


def after_workflows(cfg):
    """The entries of `merge.after_workflows` as {"workflow", "every"}: NexKit starts each
    workflow once `every` commits reached the base branch since its latest run there. A
    file name alone starts after every merge."""
    return [
        {"workflow": entry["workflow"], "every": entry.get("every", 1)}
        if isinstance(entry, dict)
        else {"workflow": entry, "every": 1}
        for entry in cfg["merge"]["after_workflows"]
    ]


def after_workflow_names(cfg):
    """The workflow files of `merge.after_workflows`."""
    return [entry["workflow"] for entry in after_workflows(cfg)]


def _watch_workflows(cfg):
    """`autopilot.watch_workflows`: workflow files that no other setting lists."""
    watched = cfg["autopilot"]["watch_workflows"]
    _workflow_files(watched, "autopilot.watch_workflows")
    both = [name for name in watched if name in after_workflow_names(cfg)]
    if both:
        _fail(
            f"autopilot.watch_workflows lists {', '.join(both)}, which merge.after_workflows "
            "lists too; sweeps watch the workflows of merge.after_workflows already, so list "
            "each in one of them"
        )


def watched_workflows(cfg):
    """The workflows whose runs on the default branch autopilot's sweeps watch: those of
    `merge.after_workflows`, which NexKit starts after its merges, then those of
    `autopilot.watch_workflows`, which something else starts."""
    return [*after_workflow_names(cfg), *cfg["autopilot"]["watch_workflows"]]


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
    if "publish" in raw:
        _fail(
            "publish.push_token_secret is gone: NexKit now acts on GitHub as a GitHub App. "
            f"Remove `publish`, set `github_app`, and delete the old token; see {APP_DOCS}"
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
        _unaffected_by(check.setdefault("unaffected_by", []), f"{where}.unaffected_by")
        if "group" in check:
            if not isinstance(check["group"], str) or not NAME.match(check["group"]):
                _fail(f"{where}.group must be a short lowercase identifier")
            if cfg["parallel_checks"] is not True:
                _fail(f"{where}.group needs parallel_checks: true")
    _bool(cfg["parallel_checks"], "parallel_checks")
    _caches(cfg["cache"])

    _profiles(cfg)
    for name, settings in cfg["stages"].items():
        _stage_values(settings, f"stages.{name}", 30 if name == TRIAGE else MAX_STAGE_MINUTES, cfg)
    _runtime_settings(cfg, profiles=not layered)
    if not layered:
        _secret_slots(cfg)

    _positive_int(cfg["fix"]["max_auto_rounds"], "fix.max_auto_rounds", 10, minimum=0)
    _bool(cfg["merge"]["auto"], "merge.auto")
    _after_workflows(cfg["merge"]["after_workflows"])
    _bool(cfg["conflicts"]["auto_resolve"], "conflicts.auto_resolve")
    _positive_int(cfg["conflicts"]["max_rounds"], "conflicts.max_rounds", 10)
    _bool(cfg["usage_limit"]["resume"], "usage_limit.resume")
    _positive_int(cfg["usage_limit"]["retry_minutes"], "usage_limit.retry_minutes", 1440)
    _positive_int(
        cfg["usage_limit"]["wait_minutes"], "usage_limit.wait_minutes", MAX_WAIT, minimum=0
    )
    _bool(cfg["close_parent_issues"], "close_parent_issues")
    _autopilot(cfg["autopilot"])
    _watch_workflows(cfg)
    _workflow_files(cfg["autopilot"]["trusted_workflows"], "autopilot.trusted_workflows")
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
    _github_app(cfg)
    _checklist(cfg["review"]["checklist"])
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
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Missing {CONFIG_PATH}; run 'nexkit init'") from None
    return validate(parse(text))


def parse(text, where=CONFIG_PATH):
    """The JSON of a configuration file's `text`, not yet validated."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{where} is not valid JSON: {exc}") from None


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

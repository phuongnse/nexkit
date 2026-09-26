"""Consumer workflow bindings and settings, never a workflow graph or executor."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from copy import deepcopy

from .common import digest, safe_path
from .policy import HASH, require

IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
ENTRYPOINTS = {"intake", "clarify", "delivery", "release"}
IDENTITY = {"repository", "default_branch", "kit"}
SETTINGS = {
    "engine",
    "models",
    "reasoning_effort",
    "limits",
    "clarification",
    "environment",
    "decisions",
    "knowledge",
    "application",
    "merge_method",
    "release",
    "checks",
}


def workflow_path(name):
    safe_path(name)
    require(
        re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml", name),
        "Use an explicit .github/workflows/<name>.yml or .yaml path",
    )
    return name


def bundle_path(name):
    safe_path(name)
    if name.startswith(".github/workflows/"):
        return workflow_path(name)
    require(
        name.startswith(".nexkit/controls/"),
        "Setup bundles contain workflows and .nexkit/controls files",
    )
    return name


def merge_settings(defaults, overrides):
    """Maps inherit explicitly chosen defaults; lists replace, never append."""
    result = deepcopy(defaults)
    for key, value in overrides.items():
        result[key] = (
            merge_settings(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else deepcopy(value)
        )
    return result


def validate_project(value):
    from .policy import config

    require(
        set(value) == {"schema", *IDENTITY, "defaults", "pipelines", "files"},
        "Schema 2 declares identity, defaults, pipelines and accepted files",
    )
    defaults = value["defaults"]
    require(isinstance(defaults, dict) and set(defaults) <= SETTINGS, "Invalid pipeline defaults")
    files = value["files"]
    require(isinstance(files, dict) and files, "Declare the accepted workflow/control file hashes")
    for name, record in files.items():
        safe_path(name)
        require(
            name not in (".nexkit/project.json", ".nexkit/installation.json"),
            "Do not hash the configuration or installation ledger into itself",
        )
        require(
            isinstance(record, dict)
            and set(record) == {"sha256", "managed"}
            and type(record["managed"]) is bool,
            "Declare each accepted file's sha256 and explicit managed ownership",
        )
        if record["managed"]:
            bundle_path(name)
        checksum = record["sha256"]
        require(
            isinstance(checksum, str) and HASH.fullmatch(checksum),
            "Accepted files need SHA-256 hashes",
        )
    pipelines = value["pipelines"]
    require(isinstance(pipelines, dict) and pipelines, "Define at least one consumer pipeline")
    for name, pipeline in pipelines.items():
        require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "Invalid pipeline identifier")
        require(
            isinstance(pipeline, dict)
            and {"settings", "entrypoints", "agent_workflows"} <= set(pipeline)
            and set(pipeline)
            <= {"settings", "entrypoints", "agent_workflows", "invocations", "approvals"},
            "A pipeline declares settings, entrypoints and agent_workflows; ordering belongs to Actions YAML",
        )
        settings = pipeline["settings"]
        require(
            isinstance(settings, dict) and set(settings) <= SETTINGS, "Invalid pipeline settings"
        )
        entries = pipeline["entrypoints"]
        require(
            isinstance(entries, dict) and set(entries) <= ENTRYPOINTS,
            "Unknown NexKit integration entrypoint",
        )
        for path in entries.values():
            workflow_path(path)
            require(path in files, "Entrypoint workflow is not in accepted files")
        agent_files = pipeline["agent_workflows"]
        require(
            isinstance(agent_files, list), "Declare credential-bearing agent_workflows explicitly"
        )
        for path in agent_files:
            workflow_path(path)
            require(path in files, "Agent workflow is not in accepted files")
        require(len(set(agent_files)) == len(agent_files), "Duplicate agent workflow")
        effective = config(_effective(value, name))
        if "invocations" in pipeline:
            from .invocations import validate_definitions

            validate_definitions(effective)
        if "approvals" in pipeline:
            from .approvals import validate_definitions as validate_approvals

            validate_approvals(effective)
    from .approvals import pr_review_count

    counts = {
        pr_review_count(_effective(value, name))
        for name, pipeline in pipelines.items()
        if "delivery" in pipeline["entrypoints"]
    }
    require(
        len(counts) <= 1,
        "Delivery pipelines sharing the default branch must accept the same native PR approval count",
    )
    return value


def _effective(value, pipeline):
    settings = merge_settings(value["defaults"], value["pipelines"][pipeline]["settings"])
    result = {
        **settings,
        **{key: deepcopy(value[key]) for key in IDENTITY},
        "schema": 1,
        "binding": {
            "pipeline": pipeline,
            "project": digest(value),
            "files": deepcopy(value["files"]),
            "entrypoints": deepcopy(value["pipelines"][pipeline]["entrypoints"]),
            "agent_workflows": list(value["pipelines"][pipeline]["agent_workflows"]),
        },
    }
    if "invocations" in value["pipelines"][pipeline]:
        result["binding"]["invocations"] = deepcopy(value["pipelines"][pipeline]["invocations"])
    if "approvals" in value["pipelines"][pipeline]:
        result["binding"]["approvals"] = deepcopy(value["pipelines"][pipeline]["approvals"])
    return result


def composed_agents(cfg):
    return "invocations" in cfg.get("binding", {})


def effective_config(value, pipeline=None):
    from .policy import config

    config(value)
    if value["schema"] == 1:
        require(pipeline is None, "Schema 1 has no selectable pipeline")
        return deepcopy(value)
    require(pipeline in value["pipelines"], "Select an explicit configured pipeline")
    return _effective(value, pipeline)


def pipeline_id(cfg):
    return cfg.get("binding", {}).get("pipeline")


def entrypoint(cfg, operation):
    require(operation in ENTRYPOINTS, "Unknown NexKit integration entrypoint")
    if "binding" not in cfg:
        return f".github/workflows/nexkit-{operation}.yml"
    entries = cfg["binding"]["entrypoints"]
    require(operation in entries, f"Pipeline {pipeline_id(cfg)} has no {operation} entrypoint")
    return entries[operation]


def dispatch(gh, cfg, operation, inputs):
    payload = dict(inputs)
    if pipeline_id(cfg) is not None:
        payload["pipeline"] = pipeline_id(cfg)
    gh.dispatch(
        entrypoint(cfg, operation).removeprefix(".github/workflows/"),
        cfg["default_branch"],
        payload,
    )


def verify_controls(gh, cfg, ref):
    for name, record in cfg.get("binding", {}).get("files", {}).items():
        content = gh.content(name, ref)
        require(
            content.get("type") == "file" and content.get("encoding") == "base64",
            f"Invalid control file: {name}",
        )
        actual = hashlib.sha256(base64.b64decode(content["content"], validate=False)).hexdigest()
        require(actual == record["sha256"], f"Accepted workflow/control file changed: {name}")


def current_config(gh, ref, cfg):
    current = effective_config(gh.read_config(ref), pipeline_id(cfg))
    require(digest(current) == digest(cfg), "Pipeline configuration changed")
    verify_controls(gh, current, ref)
    return current


def check_caller(cfg, operation, workflow_ref):
    if "binding" not in cfg:
        return
    expected = (
        f"{cfg['repository']}/{entrypoint(cfg, operation)}@refs/heads/{cfg['default_branch']}"
    )
    require(workflow_ref == expected, "Workflow is not the pipeline's configured entrypoint")


def load_run_config(gh, ref, selected, operation, *, caller=None):
    cfg = effective_config(gh.read_config(ref), selected)
    if caller is None:
        check_caller(cfg, operation, os.environ.get("GITHUB_WORKFLOW_REF"))
    else:
        workflow_path(caller)
        require(caller in cfg["binding"]["files"], "Continuation is not an accepted workflow")
        require(
            os.environ.get("GITHUB_WORKFLOW_REF")
            == f"{cfg['repository']}/{caller}@refs/heads/{cfg['default_branch']}",
            "Approval continuation must run its accepted workflow on the default branch",
        )
    verify_controls(gh, cfg, ref)
    if "binding" in cfg:
        # GitHub's workflow SHA identifies the code actually running, including
        # a queued dispatch whose default branch moved before this job began.
        sha = os.environ.get("GITHUB_WORKFLOW_SHA", "")
        from .policy import SHA

        require(SHA.fullmatch(sha), "Executing workflow revision is required")
        require(
            digest(gh.read_config(sha)) == cfg["binding"]["project"],
            "Executing workflow uses a different project configuration",
        )
        verify_controls(gh, cfg, sha)
    return cfg


def issue_pipeline(issue):
    # The marker is part of the approved issue body. It cannot be inferred from
    # its title, labels, a default pipeline or the latest workflow to run.
    lines = (issue.get("body") or "").splitlines()
    if len(lines) > 1 and lines[1].startswith("<!-- nexkit:pipeline:"):
        match = re.fullmatch(r"<!-- nexkit:pipeline:([a-z][a-z0-9_-]{0,47}) -->", lines[1])
        require(match is not None, "Invalid work item pipeline binding")
        return match[1]
    return None


def bind_state(state, cfg):
    selected = pipeline_id(cfg)
    require(state.get("pipeline", selected) == selected, "Work item belongs to another pipeline")
    if selected is not None:
        state["pipeline"] = selected

"""Reserve and validate individual CLI calls selected by native Actions jobs."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from copy import deepcopy
from datetime import datetime
from pathlib import PurePosixPath

from .common import Blocked, digest
from .pipelines import IDENTIFIER, composed_agents, load_run_config
from .policy import (
    agent_result,
    config,
    delivery_budget_used,
    delivery_calls,
    now,
    positive,
    require,
)


def definitions(cfg):
    require(composed_agents(cfg), "This pipeline has no configured agent invocations")
    return cfg["binding"]["invocations"]


def runtime_guard(gh, context, kit_ref):
    require(
        context["repository"] == os.environ["GITHUB_REPOSITORY"] == gh.repository,
        "Invocation belongs to another repository",
    )
    require(
        context["run_key"]
        == os.environ["GITHUB_RUN_ID"] + "." + os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "Invocation belongs to another run attempt",
    )
    cfg = context["config"]
    require(kit_ref == cfg["kit"]["ref"], "Invocation kit revision changed")
    require(
        load_run_config(gh, context["base"], cfg["binding"]["pipeline"], "delivery") == cfg,
        "Invocation configuration changed",
    )


def execution_config(context):
    cfg = deepcopy(context["config"])
    if "invocation" not in context:
        return cfg
    definition = context["invocation"]["definition"]
    cfg["models"] = {role: definition["model"] for role in ("implement", "review")}
    cfg.pop("reasoning_effort", None)
    if "reasoning_effort" in definition:
        cfg["reasoning_effort"] = {
            role: definition["reasoning_effort"] for role in ("implement", "review")
        }
    for key in ("agent_runner", "setup"):
        if key in definition:
            cfg["environment"][key] = deepcopy(definition[key])
    return config(cfg)


def validate_definitions(cfg):
    values = definitions(cfg)
    require(isinstance(values, dict) and values, "Declare at least one agent invocation")
    require(
        "delivery" in cfg["binding"]["entrypoints"],
        "Agent invocations need an approved-work delivery entrypoint",
    )
    contracts = set()
    for name, value in values.items():
        require(
            isinstance(name, str) and IDENTIFIER.fullmatch(name), "Invalid invocation identifier"
        )
        require(
            isinstance(value, dict)
            and {"contract", "model", "minutes", "task"} <= set(value)
            and set(value)
            <= {
                "contract",
                "model",
                "minutes",
                "task",
                "reasoning_effort",
                "agent_runner",
                "setup",
                "skills",
            },
            "An invocation declares contract, model, minutes, task and optional reasoning, runner, setup and skills",
        )
        require(
            value["contract"] in ("deliver", "review", "task"),
            "Choose a source-editing, independent-review or read-only task contract",
        )
        contracts.add(value["contract"])
        positive(value["minutes"], "invocation.minutes", 60)
        require(
            value["minutes"] <= cfg["limits"]["minutes"],
            "Invocation time exceeds the delivery deadline",
        )
        require(
            isinstance(value["task"], str) and value["task"] in cfg["binding"]["files"],
            "Invocation task is not an accepted control file",
        )
        skills = value.get("skills", [])
        require(isinstance(skills, list), "Invocation skills must be accepted SKILL.md paths")
        names = set()
        for path in skills:
            require(
                isinstance(path, str) and path in cfg["binding"]["files"],
                "Invocation skill is not an accepted control file",
            )
            item = PurePosixPath(path)
            skill_name = item.parent.name
            require(
                item.name == "SKILL.md" and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", skill_name),
                "Use a named skill directory containing SKILL.md",
            )
            require(
                not skill_name.startswith("nexkit-") and skill_name not in names,
                "Consumer skills cannot replace kit methods or duplicate one another",
            )
            names.add(skill_name)
        execution_config({"config": cfg, "invocation": {"definition": value}})
    require(
        {"deliver", "review"} <= contracts,
        "Automatic delivery requires a source editor and an independent reviewer",
    )


def control_text(gh, cfg, ref, path):
    value = gh.content(path, ref)
    require(
        value.get("type") == "file" and value.get("encoding") == "base64",
        "Invalid task/skill control file",
    )
    content = base64.b64decode(value["content"])
    require(len(content) <= 64000, "Task/skill control file exceeds 64 KB")
    require(
        hashlib.sha256(content).hexdigest() == cfg["binding"]["files"][path]["sha256"],
        "Task/skill differs from the accepted source",
    )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Blocked("Task/skill control files must be UTF-8 text") from exc


def reservation_key(context):
    return context["run_key"] + "/" + context["invocation"]["id"]


def current_source(state, context):
    if state.get("candidate_run") == context["run_key"]:
        return state["candidate"]["head"]
    return state["round_source"]


def prepare(gh, context, name, input_reports=(), check_reports=()):
    from .delivery import revalidate

    state, _ = revalidate(gh, context)
    cfg = context["config"]
    values = definitions(cfg)
    require(name in values, "Unknown configured agent invocation")
    definition = deepcopy(values[name])
    if definition["contract"] == "review" or check_reports:
        require(
            state.get("candidate_run") == context["run_key"]
            and context.get("candidate") == state.get("candidate")
            and context.get("candidate"),
            "Independent review and check inputs need the current published candidate",
        )
    prepared = deepcopy(context)
    prepared.pop("invocation", None)
    prepared["invocation"] = {
        "id": name,
        "role": definition["contract"],
        "definition": definition,
        "started_at": now(),
        "task": control_text(gh, cfg, context["base"], definition["task"]),
        "skills": {},
    }
    for path in definition.get("skills", []):
        directory = str(PurePosixPath(path).parent)
        for control in cfg["binding"]["files"]:
            if control.startswith(directory + "/"):
                # Every copied support file is explicitly accepted; no directory
                # traversal or candidate-owned skill discovery occurs here.
                relative = PurePosixPath(directory).name + control[len(directory) :]
                prepared["invocation"]["skills"][relative] = control_text(
                    gh, cfg, context["base"], control
                )
    prepared["agent_minutes"] = definition["minutes"]
    prepared.pop("verification", None)
    if check_reports or definition["contract"] == "review":
        from .checks import combine_checks

        prepared["verification"] = combine_checks(context, check_reports)
    for attempt in range(3):
        state, revision = revalidate(gh, context)
        prepared["source"] = current_source(state, context)
        if state.get("candidate_run") == context["run_key"]:
            prepared.update(candidate=state["candidate"], pr=state["pr"])
        if definition["contract"] == "review":
            require(
                state.get("candidate_run") == context["run_key"]
                and context.get("candidate") == state.get("candidate"),
                "Independent review needs the current published candidate",
            )
        prepared["previous_outputs"] = []
        for report in input_reports:
            receipt = report.get("invocation", {})
            prior = state.get("invocations", {}).get(
                context["run_key"] + "/" + receipt.get("id", ""), {}
            )
            require(
                prior.get("result") == digest(report)
                and prior.get("status") in ("completed", "published"),
                "Input report is not a recorded invocation result",
            )
            prepared["previous_outputs"].append(report)
        key = reservation_key(prepared)
        records = state.setdefault("invocations", {})
        require(
            key not in records, "This invocation already reserved a CLI call in this run attempt"
        )
        require(
            delivery_budget_used(state, cfg) + 1 <= cfg["limits"]["agent_calls"],
            "Agent invocation budget exhausted",
        )
        if definition["contract"] == "deliver":
            require(not state.get("writer"), "Another source editor is awaiting publication")
            state["writer"] = key
        records[key] = {
            "context": digest(prepared),
            "status": "reserved",
            "role": definition["contract"],
        }
        if definition["contract"] == "review":
            records[key]["verification"] = digest(prepared["verification"])
        state.update(
            agent_calls=state.get("agent_calls", 0) + 1, delivery_calls=delivery_calls(state) + 1
        )
        try:
            gh.save_state(context["issue"]["number"], state, revision)
            return prepared
        except Blocked as exc:
            if attempt == 2 or "HTTP 409" not in str(exc):
                raise
    raise Blocked("Invocation reservation could not be persisted")


def guard(gh, context, *, completed=False):
    from .delivery import revalidate

    state, revision = revalidate(gh, context)
    value = context["invocation"]
    key = reservation_key(context)
    record = state.get("invocations", {}).get(key, {})
    require(
        record.get("context") == digest(context), "Invocation context differs from its reservation"
    )
    require(
        record.get("status") in ({"reserved", "completed"} if completed else {"reserved"}),
        "Invocation is no longer executable",
    )
    require(
        (
            datetime.fromisoformat(now()) - datetime.fromisoformat(value["started_at"])
        ).total_seconds()
        < value["definition"]["minutes"] * 60 + 300,
        "Invocation deadline exhausted",
    )
    require(
        context["source"] == current_source(state, context),
        "Source changed after invocation reservation",
    )
    if value["role"] == "deliver":
        require(state.get("writer") == key, "Source-editing reservation was superseded")
    return state, revision


def record(gh, context, report):
    for attempt in range(3):
        try:
            return _record(gh, context, report)
        except Blocked as exc:
            if attempt == 2 or "HTTP 409" not in str(exc):
                raise


def _record(gh, context, report):
    state, revision = guard(gh, context, completed=True)
    receipt = {"id": context["invocation"]["id"], "context": digest(context)}
    require(
        report.get("run_key") == context["run_key"] and report.get("invocation") == receipt,
        "Agent report has a different invocation provenance",
    )
    role = context["invocation"]["role"]
    agent_result(report.get("result"), role)
    expected_skills = {
        PurePosixPath(path).parent.name
        for path in context["invocation"]["definition"].get("skills", [])
    }
    require(
        expected_skills <= set(report["result"]["skills_used"]),
        "Agent did not report using the configured consumer skills",
    )
    if role == "deliver":
        from .delivery import validate_changes

        require(
            report.get("source") == context["source"] and report.get("base") == context["base"],
            "Source-editing report belongs to another source",
        )
        if report["result"]["status"] == "done":
            validate_changes(report.get("changes"), context["config"])
    else:
        require(report.get("unchanged") is True, "Read-only invocation modified source files")
        if role == "review":
            require(
                report.get("candidate") == context["candidate"]
                and report.get("independent") is True,
                "Independent review has a different candidate",
            )
    saved = state["invocations"][reservation_key(context)]
    if saved["status"] == "completed":
        require(saved.get("result") == digest(report), "Invocation result changed after recording")
        return {"recorded": True, "duplicate": True}
    saved.update(status="completed", result=digest(report))
    gh.save_state(context["issue"]["number"], state, revision)
    return {"recorded": True, "duplicate": False}

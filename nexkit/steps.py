"""Project commands and native jobs with recorded inputs, outcomes and approvals."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from .common import Blocked, canonical, digest, short_summary
from .pipelines import IDENTIFIER, composed_work, task_pipeline
from .policy import command, now, positive, require
from .step_io import transfer


def definitions(cfg):
    return cfg.get("binding", {}).get("steps", {})


def validate_definitions(cfg):
    values = definitions(cfg)
    require(isinstance(values, dict) and values, "Declare at least one project step")
    require(composed_work(cfg), "Project steps need composed delivery or a tasks entrypoint")
    job_names = set()
    for name, value in values.items():
        require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "Invalid project step name")
        require(
            isinstance(value, dict)
            and {"kind", "subject", "timeout_seconds", "retry"} <= set(value)
            and set(value)
            <= {"kind", "subject", "timeout_seconds", "retry", "required", "argv", "job_name"},
            "A step declares kind, subject, timeout_seconds, retry, optional required and its command or job",
        )
        require(value["kind"] in ("command", "workflow"), "Use a command or native workflow step")
        require(value["subject"] in ("stage", "candidate"), "Bind a step to a stage or candidate")
        require(
            not task_pipeline(cfg) or value["subject"] == "stage",
            "Standalone project steps use a stage subject",
        )
        require(type(value.get("required", True)) is bool, "step.required must be boolean")
        require(value["retry"] in ("safe", "never"), "Declare whether replaying this step is safe")
        positive(
            value["timeout_seconds"],
            "step.timeout_seconds",
            min(cfg["limits"]["command_seconds"], cfg["limits"]["minutes"] * 60),
        )
        if value["kind"] == "command":
            require("job_name" not in value, "Command steps do not bind a native job name")
            command(value.get("argv"))
        else:
            name = value.get("job_name")
            require(
                "argv" not in value
                and isinstance(name, str)
                and 0 < len(name) <= 160
                and all(char.isprintable() for char in name)
                and "${{" not in name
                and name not in job_names,
                "Workflow steps bind unique, literal GitHub job display names",
            )
            job_names.add(name)


def execution_key(context):
    return context.get("continuation", {}).get("run_key", context["run_key"])


def reservation_key(context):
    return context["run_key"] + "/" + context["step"]["id"]


def recorded_inputs(state, context, reports):
    """Accept exact outputs of recorded agents or project steps in this logical round."""
    seen = set()
    for report in reports:
        require(isinstance(report, dict), "A step input must be a recorded JSON report")
        kinds = [key for key in ("invocation", "step") if key in report]
        require(len(kinds) == 1, "Input must identify exactly one agent invocation or project step")
        kind = kinds[0]
        receipt = report[kind]
        require(isinstance(receipt, dict), "Invalid input receipt")
        key = context["run_key"] + "/" + str(receipt.get("id", ""))
        identity = (kind, key)
        require(identity not in seen, "Duplicate step input")
        seen.add(identity)
        record = state.get("invocations" if kind == "invocation" else "steps", {}).get(key, {})
        require(
            report.get("run_key") == context["run_key"]
            and record.get("status") in {"completed", "published"}
            and record.get("result") == digest(report),
            "Input is not a recorded invocation or project-step result from this work round",
        )


def no_active_steps(state, context):
    require(
        not any(
            record.get("status") == "reserved"
            for key, record in state.get("steps", {}).items()
            if key.startswith(context["run_key"] + "/")
        ),
        "Complete started project steps before waiting or finishing",
    )


def replay_allowed(state, cfg):
    return not any(record.get("retry") == "never" for record in state.get("steps", {}).values())


def prepare(gh, context, name, reports=()):
    from .approvals import ApprovalRequired, record_denial, require_capability, restored_data
    from .delivery import revalidate
    from .invocations import current_source

    cfg = context["config"]
    require(name in definitions(cfg), "Unknown configured project step")
    definition = definitions(cfg)[name]
    for attempt in range(3):
        state, revision = revalidate(gh, context)
        try:
            require_capability(context, state, "step:" + name)
        except ApprovalRequired as exc:
            record_denial(gh, context, exc)
            raise
        selected = list(reports or restored_data(state, context).get("inputs", []))
        recorded_inputs(state, context, selected)
        require(not state.get("writer"), "Publish the active editor before starting a project step")
        if definition["subject"] == "candidate":
            require(
                state.get("candidate_run") == context["run_key"]
                and context.get("candidate") == state.get("candidate")
                and context.get("candidate"),
                "This project step requires the current published candidate",
            )
        prepared = deepcopy(context)
        prepared.pop("invocation", None)
        prepared["source"] = current_source(state, context)
        prepared["step"] = {
            "id": name,
            "definition": deepcopy(definition),
            "started_at": now(),
        }
        prepared["previous_outputs"] = selected
        key = reservation_key(prepared)
        records = state.setdefault("steps", {})
        require(key not in records, "This project step already has a reservation in this round")
        records[key] = {
            "context": digest(prepared),
            "status": "reserved",
            "source": prepared["source"],
            "candidate": prepared.get("candidate")
            if definition["subject"] == "candidate"
            else None,
            "retry": definition["retry"],
            "execution": execution_key(prepared),
        }
        state["activity"] = short_summary(f"Running {name}: {context['issue']['title']}")
        try:
            gh.save_state(context["issue"]["number"], state, revision)
            return prepared
        except Blocked as exc:
            if attempt == 2 or "HTTP 409" not in str(exc):
                raise
    raise Blocked("Project step reservation could not be persisted")


def guard(gh, context, *, completed=False):
    from .delivery import revalidate
    from .invocations import current_source

    state, revision = revalidate(gh, context)
    value = context["step"]
    require(
        definitions(context["config"]).get(value["id"]) == value["definition"],
        "Project step definition changed",
    )
    record = state.get("steps", {}).get(reservation_key(context), {})
    require(record.get("context") == digest(context), "Project step differs from its reservation")
    require(
        record.get("status") in ({"reserved", "completed"} if completed else {"reserved"}),
        "Project step is no longer executable",
    )
    require(
        datetime.fromisoformat(now()) - datetime.fromisoformat(value["started_at"])
        < timedelta(seconds=value["definition"]["timeout_seconds"] + 300),
        "Project step deadline exhausted",
    )
    require(context["source"] == current_source(state, context), "Project step source changed")
    if value["definition"]["subject"] == "candidate":
        require(
            context.get("candidate") == state.get("candidate"), "Project step candidate changed"
        )
    return state, revision


def result_data(value):
    require(
        isinstance(value, dict)
        and {"status", "summary"} <= set(value)
        and set(value) <= {"status", "summary", "data"},
        "A project result declares status, summary and optional JSON data",
    )
    require(value["status"] in ("done", "blocked"), "Project result status is done or blocked")
    require(
        isinstance(value["summary"], str)
        and value["summary"].strip()
        and len(value["summary"]) <= 8000,
        "Project result needs a summary of at most 8000 characters",
    )
    require(len(canonical(value).encode()) <= 48000, "Project result exceeds 48 KB")
    return value


def envelope(context, result, evidence):
    return {
        "run_key": context["run_key"],
        "step": {"id": context["step"]["id"], "context": digest(context)},
        "source": context["source"],
        "base": context["base"],
        "result": result_data(result),
        "evidence": evidence,
    }


def execute(context, workspace):
    """Run the accepted command in the separate, unprivileged command job."""
    from .checks import execute as execute_command

    definition = context["step"]["definition"]
    require(definition["kind"] == "command", "Only command steps use the isolated command runner")
    root = Path(workspace).absolute()
    transfer(
        "prepare",
        root,
        {
            "issue": context["issue"],
            "source": context["source"],
            "feedback": context.get("feedback"),
            "inputs": context.get("previous_outputs", []),
        },
    )
    deadline = monotonic() + definition["timeout_seconds"]
    commands = [*context["config"]["environment"]["setup"], definition["argv"]]
    logs = []
    with TemporaryDirectory(prefix="nexkit-step-home-") as command_home:
        for index, argv in enumerate(commands):
            seconds = min(context["config"]["limits"]["command_seconds"], deadline - monotonic())
            if seconds <= 0:
                logs.append(
                    {"exit_code": 124, "timed_out": True, "log": "Project step time limit reached"}
                )
                break
            outcome = execute_command(argv, root, seconds, command_home=command_home)
            logs.append(outcome)
            if index < len(commands) - 1:
                require(
                    not transfer("result-exists", root),
                    "Environment setup cannot supply the project step result",
                )
            if outcome["exit_code"] != 0:
                break
    passed = len(logs) == len(commands) and all(item["exit_code"] == 0 for item in logs)
    result = {
        "status": "done" if passed else "blocked",
        "summary": "Command completed." if passed else "Command failed; inspect its run logs.",
    }
    collected = transfer("read-result", root)
    if collected["exists"]:
        result = result_data(collected["value"])
        require(passed or result["status"] == "blocked", "A failed command cannot report done")
    return envelope(context, result, {"kind": "command", "commands": logs})


def workflow_report(gh, context, result=None):
    """Confirm a configured native job actually finished in the reserved execution."""
    definition = context["step"]["definition"]
    require(definition["kind"] == "workflow", "Native job receipts require a workflow step")
    run, attempt = execution_key(context).split(".")
    jobs = gh.api(
        f"{gh.root}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100",
        pages=True,
        collection="jobs",
    )
    matches = [job for job in jobs if job.get("name") == definition["job_name"]]
    require(len(matches) == 1, "Expected exactly one native job matching step.job_name")
    job = matches[0]
    require(job.get("status") == "completed", "Native project job has not completed")
    require(job.get("started_at") and job.get("completed_at"), "Native job timing is missing")
    started = datetime.fromisoformat(job["started_at"])
    finished = datetime.fromisoformat(job["completed_at"])
    reserved = datetime.fromisoformat(context["step"]["started_at"])
    require(started >= reserved - timedelta(seconds=1), "Native job started before its reservation")
    require(
        timedelta(0) <= finished - started <= timedelta(seconds=definition["timeout_seconds"]),
        "Native project job exceeded its configured time limit",
    )
    passed = job.get("conclusion") == "success"
    result = (
        result_data(result)
        if result is not None
        else {
            "status": "done" if passed else "blocked",
            "summary": f"{definition['job_name']}: {job.get('conclusion') or 'incomplete'}.",
        }
    )
    require(
        passed or result["status"] == "blocked", "An unsuccessful native job cannot report done"
    )
    return envelope(
        context,
        result,
        {
            "kind": "workflow",
            "job_id": job["id"],
            "execution": execution_key(context),
            "url": job["html_url"],
        },
    )


def record(gh, context, report):
    require(isinstance(report, dict), "A project report must be a JSON object")
    for attempt in range(3):
        state, revision = guard(gh, context, completed=True)
        require(
            report.get("step") == {"id": context["step"]["id"], "context": digest(context)}
            and report.get("run_key") == context["run_key"]
            and report.get("source") == context["source"]
            and report.get("base") == context["base"],
            "Project report belongs to a different reservation or source",
        )
        result_data(report.get("result"))
        definition = context["step"]["definition"]
        if definition["kind"] == "workflow":
            require(
                report == workflow_report(gh, context, report["result"]),
                "Native job evidence differs from GitHub",
            )
        else:
            evidence = report.get("evidence", {})
            require(
                isinstance(evidence, dict) and evidence.get("kind") == "command",
                "Missing isolated command evidence",
            )
            commands = evidence.get("commands")
            require(
                isinstance(commands, list)
                and commands
                and all(isinstance(item, dict) for item in commands),
                "No valid command evidence was recorded",
            )
            if report["result"]["status"] == "done":
                require(
                    len(commands) == len(context["config"]["environment"]["setup"]) + 1
                    and all(
                        type(item.get("exit_code")) is int
                        and item["exit_code"] == 0
                        and item.get("timed_out") is False
                        for item in commands
                    ),
                    "Successful project steps require successful command execution",
                )
        saved = state["steps"][reservation_key(context)]
        if saved["status"] == "completed":
            require(saved.get("result") == digest(report), "Project result changed after recording")
            return {"recorded": True, "duplicate": True}
        saved.update(
            status="completed",
            result=digest(report),
            outcome=report["result"]["status"],
            summary=(
                report["result"]["summary"]
                if task_pipeline(context["config"])
                else short_summary(report["result"]["summary"], 2000)
            ),
        )
        state["activity"] = short_summary(f"{context['step']['id']}: {saved['summary']}")
        try:
            gh.save_state(context["issue"]["number"], state, revision)
            return {"recorded": True, "duplicate": False}
        except Blocked as exc:
            if attempt == 2 or "HTTP 409" not in str(exc):
                raise
    raise Blocked("Project step result could not be persisted")


def require_results(state, context):
    """A native job's success cannot hide an omitted or blocked managed step."""
    cfg = context["config"]
    prefix = context["run_key"] + "/"
    no_active_steps(state, context)
    for name, definition in definitions(cfg).items():
        record = state.get("steps", {}).get(prefix + name)
        if not record and not definition.get("required", True):
            continue
        require(
            record and record.get("status") == "completed", f"Project step {name} has not completed"
        )
        require(record.get("outcome") == "done", f"Project step {name} is blocked")
        if definition["subject"] == "candidate":
            require(
                record.get("candidate") == context.get("candidate"), f"Project step {name} is stale"
            )
    for name, definition in cfg["binding"].get("invocations", {}).items():
        record = state.get("invocations", {}).get(prefix + name)
        required = definition.get("required", task_pipeline(cfg))
        if not record and not required:
            continue
        require(
            record and record.get("status") in {"completed", "published"},
            f"Agent invocation {name} has not completed",
        )
        require(record.get("outcome") == "done", f"Agent invocation {name} is blocked")

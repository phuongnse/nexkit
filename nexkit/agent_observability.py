"""Prepare, finish and summarize an accepted session's diagnostic artifact."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from .adapters import adapter
from .agent_session import minutes, prepared
from .command_workspace import export
from .common import (
    Blocked,
    canonical,
    is_link,
    read_json,
    read_regular_bytes,
    read_regular_json,
    write_json,
)
from .observability import (
    Redactor,
    RunLog,
    action_run,
    diagnostic_json,
    markdown_text,
    read_trace,
    session_identity,
)
from .policy import require


def begin(context, role, data):
    cfg, _, _, _, _ = prepared(context, role, data)
    identity = session_identity(context, role)
    identity["timeout_seconds"] = minutes(context, cfg, role) * 60
    write_json(Path(data) / "observation.json", identity)
    adapter(cfg["engine"]).prepare_observation(cfg["engine"], data=Path(data))
    locations(context, role, data)
    return identity


def locations(context, role, data):
    identity = session_identity(context, role)
    invocation = re.sub("[^a-zA-Z0-9_-]", "-", str(identity["invocation"]))[:60]
    attempt = str(identity["run_key"]).split(".")[-1]
    attempt = attempt if attempt.isdigit() else "1"
    name = f"agent-diagnostics-{role}-{invocation}-{attempt}"
    export(
        {
            "diagnostic_name": name,
            "diagnostic_directory": str(Path(data).absolute() / "diagnostics"),
        }
    )
    return {"diagnostic_name": name}


def report(identity, result, events):
    text = "# NexKit agent report\n\n"
    text += f"Execution: **{markdown_text(identity.get('status', 'unavailable'))}**"
    if identity.get("seconds") is not None:
        text += f" · {identity['seconds']} seconds"
    text += "\n\n"
    run = action_run(identity["repository"], identity.get("run_key"))
    if run:
        text += f"[Workflow run]({run})"
        if identity.get("issue"):
            text += f" · [Issue #{identity['issue']}](https://github.com/{identity['repository']}/issues/{identity['issue']})"
        text += "\n\n"
    text += "## Agent-reported result\n\n"
    if result:
        text += Redactor().text(result["summary"]) + "\n\n"
        commands = result.get("commands", [])
        if commands:
            text += "Agent-reported commands:\n\n"
            text += "\n".join(
                "- " + markdown_text(Redactor().text(command)) for command in commands
            )
            text += "\n\n"
        if result.get("limitations"):
            text += "Limitations:\n\n"
            text += "\n".join(
                "- " + markdown_text(Redactor().text(item)) for item in result["limitations"]
            )
            text += "\n\n"
        for key, title in (("reply", "Reply"), ("specification", "Specification")):
            if result.get(key):
                text += f"## {title}\n\n" + Redactor().text(result[key]) + "\n\n"
        if result.get("questions"):
            text += "## Questions\n\n"
            text += (
                "\n".join("- " + markdown_text(question) for question in result["questions"])
                + "\n\n"
            )
        if "ready_for_approval" in result:
            text += f"Agent reports ready for approval: {result['ready_for_approval']}.\n\n"
        if result.get("verdict"):
            text += "## Review\n\nVerdict: " + markdown_text(result["verdict"]) + "\n\n"
            for finding in result.get("findings", []):
                text += f"- **{markdown_text(finding['severity'])}**: {markdown_text(finding['detail'])}\n"
            text += "\n"
        if result.get("acceptance"):
            text += "## Acceptance evidence\n\n"
            for criterion in result["acceptance"]:
                text += f"- **{markdown_text(criterion['criterion'])}** · passed: {criterion['passed']}\n"
                text += "  " + markdown_text(criterion["evidence"]).replace("\n", "\n  ") + "\n"
            text += "\n"
        text += "The full sanitized structured result is in result.json.\n\n"
    else:
        text += (
            "No valid final agent result was available. Read the activity and diagnostic logs.\n\n"
        )
    commands = [event for event in events if event["kind"] == "command"]
    if commands:
        text += "## Observed command activity\n\n"
        for event in commands[:100]:
            command = markdown_text(event.get("command", ""))
            status = markdown_text(event.get("status", ""))
            text += f"- {command} · {status}"
            if "exit_code" in event:
                text += f" · exit {event['exit_code']}"
            text += "\n"
        text += "\nFull recorded events are in events.jsonl and activity.log.\n\n"
    if identity.get("usage"):
        text += "## Reported model usage\n\n"
        text += "\n".join(f"- {name}: {count}" for name, count in identity["usage"].items())
        text += "\n\n"
    if identity.get("omitted_events"):
        text += f"{identity['omitted_events']} events or unsupported lines were omitted.\n\n"
    text += (
        "These are activity diagnostics and an agent-reported result. "
        "The pipeline's recorded checks, approvals and final status remain authoritative.\n"
    )
    return text


def result_fields(value, schema):
    """Project only fields declared in the trusted result schema."""
    kind = schema.get("type")
    if kind == "object":
        require(isinstance(value, dict), "Invalid structured diagnostic result")
        return {
            key: result_fields(value[key], definition)
            for key, definition in schema.get("properties", {}).items()
            if key in value
        }
    if kind == "array":
        require(isinstance(value, list), "Invalid structured diagnostic result")
        return [result_fields(item, schema["items"]) for item in value]
    require(
        (kind == "string" and isinstance(value, str))
        or (kind == "boolean" and type(value) is bool),
        "Invalid structured diagnostic field",
    )
    return value


def finish(context, role, data, outcome):
    from .ci import agent_result
    from .invocations import execution_config

    data = Path(data).absolute()
    cfg = execution_config(context)
    adapter(cfg["engine"]).stop_session(cfg["engine"])
    from .checkpoints import stop_monitor

    checkpoint_failure = None
    try:
        stop_monitor(data)
    except (Blocked, OSError) as exc:
        checkpoint_failure = str(exc)
    try:
        _, _, _, _, output = prepared(context, role, data)
    except (Blocked, OSError):
        output = None
    accepted = session_identity(context, role)
    expected = (
        read_regular_json(data / "observation.json", 48000)
        if (data / "observation.json").exists()
        else accepted
    )
    require(
        all(expected.get(key) == value for key, value in accepted.items() if key != "run_key"),
        "Diagnostic preparation differs from the accepted session",
    )
    folder = data / "diagnostics"
    require(not any(is_link(p) for p in (folder, *folder.parents)), "Linked diagnostic directory")
    folder.mkdir(exist_ok=True)
    if not (folder / "run.json").exists():
        log = RunLog(folder, expected)
        log.record(
            {
                "kind": "diagnostic",
                "message": "CLI activity was unavailable or execution did not start",
            }
        )
        log.finish(0 if outcome == "success" else 1, status="unavailable")
    identity = read_regular_json(folder / "run.json", 48000)
    require(
        # The context digest binds all configuration and source inputs. Display
        # settings may have been redacted with credentials available only to
        # the provider step, so they are not compared as raw strings here.
        all(
            identity.get(key) == expected.get(key)
            for key in (
                "schema",
                "repository",
                "issue",
                "pipeline",
                "role",
                "invocation",
                "context",
                "reservation_run_key",
                "run_key",
            )
        ),
        "Agent diagnostics have a different accepted session",
    )
    events = read_trace(folder / "events.jsonl")
    result = None
    try:
        if output is not None:
            result = agent_result(read_regular_json(output), role)
            from .ci import kit_root

            result = Redactor().data(
                result_fields(result, read_json(kit_root() / f"schemas/{role}.json"))
            )
    except (Blocked, OSError):
        pass
    identity["job_step_outcome"] = outcome
    if checkpoint_failure:
        identity["checkpoint_failure"] = checkpoint_failure
    identity["result_available"] = bool(result)
    if identity.get("status") == "running":
        identity["status"] = "interrupted"
    if result:
        identity["agent_reported_status"] = result.get("verdict", result["status"])
        diagnostic_json(folder / "result.json", result)
    diagnostic_json(folder / "run.json", identity)
    target = folder / "report.md"
    require(not is_link(target), "Linked diagnostic report")
    target.write_text(report(identity, result, events), encoding="utf-8")
    target.chmod(0o644)
    return identity


def summary(data, artifact_url=""):
    folder = Path(data).absolute() / "diagnostics"
    identity = read_regular_json(folder / "run.json", 48000)
    text = "### NexKit agent activity\n\n"
    text += f"**{markdown_text(identity['invocation'])}** · {markdown_text(identity['role'])}"
    text += f" · execution {markdown_text(identity.get('status', 'unavailable'))}\n\n"
    if identity.get("seconds") is not None:
        text += f"Elapsed: {identity['seconds']}s. Observed commands: {identity.get('commands', 0)}.\n\n"
    run = action_run(identity["repository"], identity.get("run_key"))
    if run:
        text += f"[Workflow run]({run})"
        if identity.get("issue"):
            text += f" · [Issue #{identity['issue']}](https://github.com/{identity['repository']}/issues/{identity['issue']})"
        text += "\n\n"
    run_id = str(identity.get("run_key", "")).split(".")[0]
    allowed = f"https://github.com/{identity['repository']}/actions/runs/{run_id}/artifacts/"
    if artifact_url.startswith(allowed) and re.fullmatch(
        re.escape(allowed) + r"[0-9]+", artifact_url
    ):
        text += f"[Download report and activity]({artifact_url})\n\n"
    # The downloadable report retains its full text. A job summary has a native
    # size limit and is intentionally a preview.
    text += Redactor().text(
        read_regular_bytes(folder / "report.md", 2_000_000).decode("utf-8"), 24000
    )
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(text + "\n")
    return {"summary_written": bool(path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("begin", "finish", "summary", "locations"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--role", required=True, choices=("request", "deliver", "review", "task"))
    parser.add_argument(
        "--outcome", choices=("success", "failure", "cancelled", "skipped"), default="failure"
    )
    parser.add_argument("--artifact-url", default="")
    args = parser.parse_args()
    path = Path(args.context).absolute()
    context = read_json(path)
    if args.operation == "begin":
        value = begin(context, args.role, path.parent)
    elif args.operation == "locations":
        value = locations(context, args.role, path.parent)
    elif args.operation == "finish":
        value = finish(context, args.role, path.parent, args.outcome)
    else:
        value = summary(path.parent, args.artifact_url)
    if args.operation in ("begin", "finish"):
        value = {
            "role": args.role,
            "diagnostics": args.operation,
            "status": value.get("status", "prepared"),
            "result_available": value.get("result_available", False),
        }
    print(canonical(value))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, ValueError) as exc:
        print(
            "Agent diagnostics could not be finalized: " + str(type(exc).__name__), file=sys.stderr
        )
        raise SystemExit(2) from exc

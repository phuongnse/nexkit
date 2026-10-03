"""Bounded public checkpoints, independent of successful agent result collection."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .common import (
    Blocked,
    canonical,
    digest,
    is_link,
    read_json,
    read_regular_bytes,
    read_regular_json,
    write_json,
)
from .policy import now, protected_path, require, spec_hash

MAX_BYTES = 3_000_000
RUNTIME = (
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_RUNTIME_URL",
    "ACTIONS_RESULTS_URL",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
)


def settings(context):
    from .invocations import execution_config

    value = {
        "enabled": True,
        "checkpoint_seconds": 60,
        "max_checkpoints": 6,
        "retention_days": 7,
        **execution_config(context).get("recovery", {}),
    }
    require(
        type(value["enabled"]) is bool
        and set(value) == {"enabled", "checkpoint_seconds", "max_checkpoints", "retention_days"},
        "Invalid recovery settings",
    )
    for name, minimum, maximum in (
        ("checkpoint_seconds", 10, 3600),
        ("max_checkpoints", 2, 8),
        ("retention_days", 1, 90),
    ):
        require(
            type(value[name]) is int and minimum <= value[name] <= maximum,
            "Invalid recovery." + name,
        )
    return value


def binding(context, role):
    from .invocations import execution_config
    from .pipelines import pipeline_id

    answers = [
        answer
        for answer in context.get("answers", [])
        if answer.get("body", "").strip() != "/nexkit resume"
    ]
    outputs = []
    for report in context.get("previous_outputs", []):
        value = {key: item for key, item in report.items() if key not in {"run_key", "producer"}}
        for kind in ("invocation", "step"):
            if kind in value:
                value[kind] = {"id": value[kind]["id"]}
        outputs.append(value)
    return {
        "repository": context["config"]["repository"],
        "issue": context["issue"]["number"],
        "pipeline": pipeline_id(context["config"]),
        "role": role,
        "invocation": context.get("invocation", {}).get("id", role),
        "spec": spec_hash(context["issue"]),
        "edited_at": context["issue"].get("last_edited_at"),
        "config": digest(context["config"]),
        "execution": digest(execution_config(context)),
        "kit": context["config"]["kit"]["ref"],
        "base": context["base"],
        "source": context["source"],
        "inputs": digest({"answers": answers, "previous_outputs": outputs}),
    }


def artifact_prefix(context, role, *, run_key=None):
    from .observability import session_identity

    identity = session_identity(context, role)
    if run_key is not None:
        identity["run_key"] = run_key
    invocation = re.sub(r"[^a-zA-Z0-9_-]", "-", str(identity["invocation"]))
    return f"agent-checkpoint-{identity['issue']}-{role}-{invocation}-{identity['run_key'].replace('.', '-')}-"


def secret_free(value):
    from .observability import TOKEN, Redactor

    text = canonical(value)
    require(
        not TOKEN.search(text) and not any(secret in text for secret in Redactor().secrets),
        "Checkpoint contains a recognized credential",
    )


def public_handover(data, output):
    from .observability import Redactor, read_trace

    explicit = Path(output).parent / "handover.json"
    if explicit.exists() or is_link(explicit):
        value = read_regular_json(explicit, 24000)
        require(
            isinstance(value, dict)
            and set(value) <= {"completed", "remaining", "blockers", "draft", "verification"}
            and value,
            "Invalid public handover",
        )
        require(
            all(isinstance(text, str) for text in value.values()),
            "Public handover fields must be text",
        )
        secret_free(value)
        return value
    # Only a bounded, complete public message is carried forward. Command
    # outputs, stderr, private logs and reasoning are never checkpoint inputs.
    events = (
        read_trace(Path(data) / "diagnostics/events.jsonl")
        if (Path(data) / "diagnostics/events.jsonl").exists()
        else []
    )
    messages = [
        event["message"]
        for event in events
        if event.get("kind") in {"message", "plan"}
        and isinstance(event.get("message"), str)
        and 0 < len(event["message"].encode()) <= 12000
    ]
    return (
        {
            "draft": Redactor().text(messages[-1]),
            "remaining": "Interrupted public activity; a fresh session must determine remaining work.",
        }
        if messages
        else {}
    )


def capture(source, workspace, context, role, data, sequence, *, capture_point="interval"):
    from .agent_session import minutes, prepared
    from .ci import file_snapshot
    from .delivery import validate_changes

    source, workspace, data = Path(source), Path(workspace), Path(data)
    cfg, _, _, accepted_work, _ = prepared(context, role, data)
    require(workspace == accepted_work, "Checkpoint workspace differs from its sealed session")
    initial = read_json(data / "initial.json")
    origin = read_json(data / "recovery-baseline.json")
    baseline = file_snapshot(source, source)
    current = file_snapshot(source, workspace)
    require(
        current == file_snapshot(source, workspace), "Workspace changed during checkpoint capture"
    )
    handover = public_handover(data, read_regular_json(data / "agent-runtime.json")["output"])
    changes = []
    if role == "deliver":
        for name in sorted(set(current) | set(baseline) | set(origin)):
            if current.get(name) == baseline.get(name) == origin.get(name):
                continue
            require(
                not protected_path(name, context["config"]),
                "Checkpoint contains an administrative control change",
            )
            require(
                not re.search(
                    r"(?:^|/)(?:\.env(?:\..*)?|auth\.json|id_(?:rsa|ed25519)|[^/]*\.pem)$", name
                ),
                "Private credential files cannot be checkpointed",
            )
            item = {
                "path": name,
                "before": digest(origin.get(name)),
                **(
                    current[name]
                    if name in current
                    else {"mode": (origin.get(name) or baseline[name])["mode"], "deleted": True}
                ),
            }
            secret_free(item)
            changes.append(item)
        if changes:
            validate_changes(changes, context["config"])
    else:
        require(current == initial, "Readonly role modified source before checkpoint capture")
    require(changes or handover, "No recoverable public work yet")
    payload = {"changes": changes, "handover": handover}
    encoded = (canonical(payload) + "\n").encode()
    require(len(encoded) <= MAX_BYTES - 24000, "Checkpoint exceeds transfer bound")
    require(type(sequence) is int and sequence > 0, "Invalid checkpoint sequence")
    manifest = {
        "schema": 1,
        "binding": binding(context, role),
        "context": digest(context),
        "context_snapshot": context,
        "reservation": context["run_key"],
        "run_key": os.environ.get("GITHUB_RUN_ID", context["run_key"].split(".")[0])
        + "."
        + os.environ.get("GITHUB_RUN_ATTEMPT", context["run_key"].split(".")[-1]),
        "sequence": sequence,
        "captured_at": now(),
        "capture_point": capture_point,
        "cadence_seconds": max(
            settings(context)["checkpoint_seconds"],
            minutes(context, cfg, role) * 60 / (settings(context)["max_checkpoints"] - 1),
        ),
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "untrusted_partial_work": True,
    }
    require(
        len(canonical(manifest).encode()) <= 450000, "Checkpoint context exceeds its transfer bound"
    )
    secret_free(manifest)
    parent = data / "checkpoints"
    parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=parent) as temporary:
        folder = Path(temporary)
        (folder / "payload.json").write_bytes(encoded)
        write_json(folder / "manifest.json", manifest)
        target = parent / str(sequence)
        require(not target.exists(), "Checkpoint sequence already exists")
        folder.rename(target)
    return target, manifest


def validate_payload(manifest, payload, context, role, *, exact_context=False):
    from .delivery import validate_changes

    require(isinstance(manifest, dict) and isinstance(payload, dict), "Invalid checkpoint document")
    require(
        manifest.get("schema") == 1
        and manifest.get("untrusted_partial_work") is True
        and manifest.get("binding") == binding(context, role),
        "Checkpoint specification/configuration/source/role changed",
    )
    require(
        manifest.get("context") == digest(manifest.get("context_snapshot"))
        and isinstance(manifest.get("context_snapshot"), dict),
        "Checkpoint context snapshot differs from its reservation",
    )
    require(
        type(manifest.get("sequence")) is int
        and manifest["sequence"] > 0
        and manifest.get("capture_point") in {"interval", "final"}
        and isinstance(manifest.get("captured_at"), str)
        and type(manifest.get("cadence_seconds")) in (int, float)
        and 10 <= manifest["cadence_seconds"] <= 3600
        and re.fullmatch(r"[0-9]+\.[0-9]+", str(manifest.get("run_key")))
        and re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("payload_sha256"))),
        "Invalid checkpoint manifest",
    )
    if exact_context:
        require(
            manifest.get("context") == digest(context)
            and manifest.get("reservation") == context["run_key"],
            "Checkpoint has another invocation reservation",
        )
    require(
        set(payload) == {"changes", "handover"}
        and isinstance(payload["changes"], list)
        and isinstance(payload["handover"], dict),
        "Invalid checkpoint payload",
    )
    require(
        role == "deliver" or not payload["changes"],
        "Readonly checkpoint cannot restore source changes",
    )
    if payload["changes"]:
        validate_changes(payload["changes"], context["config"])
        require(
            all(
                re.fullmatch(r"[0-9a-f]{64}", str(item.get("before")))
                for item in payload["changes"]
            ),
            "Checkpoint lacks source conflict hashes",
        )
    require(
        set(payload["handover"]) <= {"completed", "remaining", "blockers", "draft", "verification"}
        and all(isinstance(text, str) for text in payload["handover"].values())
        and len(canonical(payload["handover"]).encode()) <= 24000
        and len(canonical(payload).encode()) <= MAX_BYTES,
        "Invalid checkpoint handover",
    )
    secret_free(payload)


def archive_data(archive):
    require(len(archive) <= MAX_BYTES, "Checkpoint archive exceeds transfer bound")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            require(
                {item.filename for item in entries} == {"manifest.json", "payload.json"}
                and len(entries) == 2
                and sum(item.file_size for item in entries) <= MAX_BYTES
                and all((item.external_attr >> 16) & 0o170000 != 0o120000 for item in entries),
                "Unexpected, linked or oversized checkpoint archive",
            )
            raw = zipped.read("payload.json")
            manifest = json.loads(zipped.read("manifest.json"))
            payload = json.loads(raw)
            require(
                isinstance(manifest, dict) and isinstance(payload, dict),
                "Invalid checkpoint document",
            )
            require(
                hashlib.sha256(raw).hexdigest() == manifest.get("payload_sha256")
                and raw == (canonical(payload) + "\n").encode(),
                "Checkpoint payload hash or encoding mismatch",
            )
            return manifest, payload
    except (ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, Blocked):
            raise
        raise Blocked("Cannot read a complete checkpoint archive") from exc


def download(gh, artifact):
    require(
        not artifact.get("expired") and artifact.get("size_in_bytes", MAX_BYTES + 1) <= MAX_BYTES,
        "Checkpoint is missing, expired or oversized",
    )
    # gh follows GitHub's signed download redirect. No archive path is extracted.
    process = subprocess.Popen(
        ["gh", "api", f"{gh.root}/actions/artifacts/{int(artifact['id'])}/zip"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Read at most the transfer bound, irrespective of server metadata.
        import threading

        timer = threading.Timer(55, process.kill)
        timer.start()
        archive = process.stdout.read(MAX_BYTES + 1)
        require(len(archive) <= MAX_BYTES, "Checkpoint archive exceeds transfer bound")
        require(process.wait(timeout=5) == 0, "Checkpoint artifact download failed")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if artifact.get("digest"):
        require(
            artifact["digest"] == "sha256:" + hashlib.sha256(archive).hexdigest(),
            "Checkpoint archive digest mismatch",
        )
    return archive_data(archive)


def record(
    gh, context, role, *, state=None, revision=None, persist=True, artifacts=None, execution=None
):
    """Select a complete artifact after interrupted execution; no model calls."""
    from .github import run_attempt

    number = context["issue"]["number"]
    if state is None:
        state, revision = gh.get_state(number)
    expected = context.get("invocation", {})
    if expected:
        reservation = state.get("invocations", {}).get(
            context["run_key"] + "/" + expected["id"], {}
        )
        require(
            reservation.get("context") == digest(context), "Checkpoint reservation was superseded"
        )
    else:
        require(
            (
                state.get("clarification", {}).get("run_key")
                if role == "request"
                else state.get("run_key")
            )
            == context["run_key"],
            "Checkpoint invocation was superseded",
        )
        if role in {"request", "deliver"}:
            owner = state.get("clarification", {}) if role == "request" else state
            require(
                owner.get("checkpoint_context") == digest(context),
                "Checkpoint context differs from its owned reservation",
            )
        elif role == "review":
            require(
                state.get("candidate") == context.get("candidate")
                and context.get("source") == state.get("candidate", {}).get("head"),
                "Review checkpoint has another owned candidate",
            )
    actual = execution or (
        os.environ.get("GITHUB_RUN_ID", context["run_key"].split(".")[0])
        + "."
        + os.environ.get("GITHUB_RUN_ATTEMPT", context["run_key"].split(".")[-1])
    )
    run = run_attempt(gh, actual)
    require(run.get("head_sha") == context["base"], "Checkpoint Actions source changed")
    from .pipelines import verify_controls

    accepted = context["config"]["binding"]["files"]
    require(
        run.get("path", "").split("@", 1)[0] in accepted
        and run.get("head_branch") == context["config"]["default_branch"]
        and run.get("repository", {}).get("full_name") == gh.repository,
        "Checkpoint Actions caller or repository changed",
    )
    referenced = run.get("referenced_workflows", [])
    kit = context["config"]["kit"]
    require(
        any(
            item.get("sha") == kit["ref"]
            and item.get("path", "").startswith(kit["repository"] + "/.github/workflows/")
            for item in referenced
        ),
        "Checkpoint Actions run did not use the accepted toolkit pin",
    )
    verify_controls(gh, context["config"], context["base"])
    artifacts = (
        artifacts
        if artifacts is not None
        else gh.api(
            f"{gh.root}/actions/runs/{actual.split('.')[0]}/artifacts?per_page=100",
            pages=True,
            collection="artifacts",
        )
    )
    prefix = artifact_prefix(context, role, run_key=actual)
    artifacts = [
        artifact
        for artifact in artifacts
        if artifact["name"].startswith(prefix) and artifact["name"][len(prefix) :].isdigit()
    ]
    failures = []
    for artifact in sorted(
        artifacts, key=lambda item: int(item["name"][len(prefix) :]), reverse=True
    ):
        try:
            manifest, payload = download(gh, artifact)
            validate_payload(manifest, payload, context, role, exact_context=True)
            require(
                manifest["run_key"] == actual
                and artifact["name"] == prefix + str(manifest["sequence"]),
                "Checkpoint artifact identity changed",
            )
            native = artifact.get("workflow_run", {})
            require(
                native.get("id") == int(actual.split(".")[0])
                and native.get("head_sha") == context["base"],
                "Checkpoint artifact belongs to another native Actions run",
            )
            key = role + "/" + context.get("invocation", {}).get("id", role)
            scope = {name: value for name, value in manifest.items() if name != "context_snapshot"}
            receipt = {
                "artifact": artifact["id"],
                "run_key": actual,
                "manifest": scope,
                "manifest_scope_sha256": digest(scope),
                "manifest_sha256": digest(manifest),
                "expires_at": artifact.get("expires_at"),
                "restore_state": "available",
                "capture_failures": failures,
            }
            retired = {
                item["artifact"]
                for entry in state.get("recovery_history", [])
                for item in entry["checkpoints"].values()
            }
            if artifact["id"] in retired:
                return {"restore_state": "retired", "artifact": artifact["id"]}
            old = state.setdefault("recovery", {}).get(key)
            if (
                old
                and old["run_key"] == actual
                and old["manifest"]["sequence"] >= manifest["sequence"]
            ):
                return old
            state["recovery"][key] = receipt
            if persist:
                gh.save_state(number, state, revision)
            return receipt
        except (Blocked, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
            if "HTTP 409" in str(exc):
                raise
            failures.append(
                {"artifact": artifact["id"], "reason": "Checkpoint could not be validated"}
            )
    if failures:
        state["recovery_capture_failures"] = failures[:8]
        if persist:
            gh.save_state(number, state, revision)
    return {"restore_state": "unavailable", "capture_failures": failures}


def discover(gh, number, *, persist=True):
    """Recover completed native attempts even when their finalizer never ran."""
    from .github import run_attempt

    state, revision = gh.get_state(number)
    if state.get("status") in {"completed", "merged", "released"}:
        return state, revision
    keys = {
        record.get("execution")
        for record in state.get("invocations", {}).values()
        if record.get("status") == "reserved"
    }
    if state.get("checkpoint_context"):
        keys.add(state.get("run_key"))
    if state.get("clarification", {}).get("checkpoint_context"):
        keys.add(state["clarification"].get("run_key"))
    for run_key in sorted(key for key in keys if key):
        if run_attempt(gh, run_key).get("status") != "completed":
            continue
        artifacts = gh.api(
            f"{gh.root}/actions/runs/{run_key.split('.')[0]}/artifacts?per_page=100",
            pages=True,
            collection="artifacts",
        )
        artifacts = [
            item
            for item in artifacts
            if item.get("name", "").startswith(f"agent-checkpoint-{number}-")
        ]
        attempted = set()
        for artifact in sorted(artifacts, key=lambda item: item["id"], reverse=True):
            try:
                manifest, _ = download(gh, artifact)
                context = manifest.get("context_snapshot")
                require(isinstance(context, dict), "Checkpoint has no owned context")
                role = manifest["binding"]["role"]
                invocation = context.get("invocation", {}).get("id", role)
                key = role + "/" + invocation
                if (
                    key in attempted
                    or manifest.get("run_key") != run_key
                    or context.get("issue", {}).get("number") != number
                ):
                    continue
                if "invocation" in context:
                    reservation = state.get("invocations", {}).get(
                        context["run_key"] + "/" + invocation, {}
                    )
                    if reservation.get("status") != "reserved" or reservation.get(
                        "context"
                    ) != digest(context):
                        continue
                elif role == "deliver" and state.get("candidate_run") == run_key:
                    continue  # Complete source is already durable in its PR.
                elif role == "request" and state.get("clarification", {}).get(
                    "completed_input"
                ) == context.get("input"):
                    continue
                elif role == "review" and state.get("feedback", {}).get("review"):
                    continue
                record(
                    gh,
                    context,
                    role,
                    state=state,
                    revision=revision,
                    persist=persist,
                    artifacts=artifacts,
                    execution=run_key,
                )
                attempted.add(key)
                if persist:
                    state, revision = gh.get_state(number)
            except (Blocked, OSError, ValueError, TypeError, KeyError) as exc:
                if "HTTP 409" in str(exc):
                    raise
                # A malformed/newer artifact cannot hide an older complete one.
                continue
    return state, revision


def select(state, context, role):
    key = role + "/" + context.get("invocation", {}).get("id", role)
    receipt = state.get("recovery", {}).get(key)
    if receipt:
        require(
            isinstance(receipt, dict) and isinstance(receipt.get("manifest"), dict),
            "Invalid recovery receipt",
        )
        require(
            receipt.get("manifest_scope_sha256") == digest(receipt["manifest"])
            and receipt["manifest"].get("binding") == binding(context, role),
            "Recovery checkpoint has stale inputs; inspect it before continuing from published source",
        )
    return receipt


def inspect(gh, issue, cfg, state):
    """Preview live scope/retention; full invocation inputs are rechecked at reservation."""
    from .pipelines import pipeline_id

    base = gh.ref(cfg["default_branch"])
    source = gh.pull(state["pr"])["head"]["sha"] if state.get("pr") else base
    common = {
        "repository": gh.repository,
        "issue": issue["number"],
        "spec": spec_hash(issue),
        "edited_at": issue.get("last_edited_at"),
        "config": digest(cfg),
        "pipeline": pipeline_id(cfg),
        "kit": cfg["kit"]["ref"],
        "base": base,
    }
    results = {}
    for key, receipt in state.get("recovery", {}).items():
        require(
            isinstance(receipt, dict)
            and isinstance(receipt.get("manifest"), dict)
            and type(receipt.get("artifact")) is int,
            "Invalid recovery receipt",
        )
        manifest = receipt["manifest"]
        scope = manifest.get("binding", {})
        current = (
            isinstance(scope, dict)
            and all(scope.get(name) == value for name, value in common.items())
            and scope.get("source")
            == (base if scope.get("role") in {"task", "request"} else source)
            and receipt.get("manifest_scope_sha256") == digest(manifest)
        )
        try:
            artifact = (
                gh.api(f"{gh.root}/actions/artifacts/{receipt['artifact']}") if current else None
            )
        except Blocked:
            artifact = None
        results[key] = {
            "artifact": receipt.get("artifact"),
            "scope_matches": bool(current),
            "available": bool(artifact and not artifact.get("expired")),
            "expires_at": (artifact or {}).get("expires_at", receipt.get("expires_at")),
            "input_validation": "Full invocation inputs and payload are checked before execution; earlier verification is not reused",
        }
    return results


def consume(state, context, role, report):
    """Retire restored input only after a validated complete result owns it."""
    key = role + "/" + context.get("invocation", {}).get("id", role)
    receipt = state.get("recovery", {}).get(key)
    if not receipt:
        require(not report.get("recovered_checkpoint"), "Recovered result has no owned checkpoint")
        return
    require(
        report.get("recovered_checkpoint") == receipt["manifest_sha256"],
        "Completed result did not consume its owned recovery checkpoint",
    )
    state.setdefault("recovery_history", []).append(
        {
            "checkpoints": {key: state["recovery"].pop(key)},
            "consumed_at": now(),
            "run_key": context["run_key"],
        }
    )


def discard_subject(issue, cfg, state):
    require(state.get("recovery"), "No recorded checkpoint to discard")
    return {
        "repository": cfg["repository"],
        "issue": issue["number"],
        "spec": spec_hash(issue),
        "edited_at": issue.get("last_edited_at"),
        "config": digest(cfg),
        "recovery": digest(state["recovery"]),
    }


def discard_proposal(issue, cfg, state):
    subject = discard_subject(issue, cfg, state)
    return {
        "subject": subject,
        "human_comment": "/nexkit discard-checkpoint " + digest(subject),
        "note": "An administrator posts this after inspecting the checkpoint. Continuation then starts from published source; consumption and checkpoint history are retained.",
    }


def reconcile_discard(gh, issue, cfg, state):
    from .policy import human

    if not state.get("recovery"):
        return False
    subject = discard_subject(issue, cfg, state)
    command = "/nexkit discard-checkpoint " + digest(subject)
    for comment in gh.comments(issue["number"]):
        if (
            comment.get("body", "").strip() == command
            and comment.get("created_at") == comment.get("updated_at")
            and human(comment, gh.permission)
            and gh.permission(comment["user"]["login"]) == "admin"
        ):
            state.setdefault("recovery_history", []).append(
                {
                    "checkpoints": state.pop("recovery"),
                    "subject": subject,
                    "comment": comment["id"],
                    "actor": comment["user"]["login"],
                    "discarded_at": now(),
                }
            )
            return True
    return False


def fetch(gh, context, role, data):
    if not settings(context)["enabled"]:
        return None
    if role == "request":
        from .clarify import revalidate
    elif "invocation" in context:
        from .invocations import guard as revalidate
    else:
        from .delivery import revalidate

    revalidate(gh, context)
    state, _ = gh.get_state(context["issue"]["number"])
    receipt = select(state, context, role)
    if not receipt:
        return None
    artifact = gh.api(f"{gh.root}/actions/artifacts/{int(receipt['artifact'])}")
    manifest, payload = download(gh, artifact)
    require(
        digest(manifest) == receipt["manifest_sha256"]
        and manifest["run_key"] == receipt["run_key"],
        "Recovery artifact differs from its recorded checkpoint",
    )
    validate_payload(manifest, payload, context, role)
    folder = Path(data) / "recovery-input"
    require(not folder.exists(), "Use a fresh recovery input directory")
    folder.mkdir(mode=0o700)
    write_json(folder / "manifest.json", manifest)
    (folder / "payload.json").write_text(canonical(payload) + "\n", encoding="utf-8")
    return receipt


def restore(source, workspace, context, role, data):
    from .ci import file_snapshot

    folder = Path(data) / "recovery-input"
    if not folder.exists():
        return None
    manifest, payload = (
        read_regular_json(folder / "manifest.json"),
        read_regular_json(folder / "payload.json", MAX_BYTES),
    )
    require(
        hashlib.sha256(read_regular_bytes(folder / "payload.json", MAX_BYTES)).hexdigest()
        == manifest.get("payload_sha256"),
        "Recovery payload differs from its recorded hash",
    )
    validate_payload(manifest, payload, context, role)
    current = file_snapshot(source, workspace)
    changes = {item["path"]: item for item in payload["changes"]}
    deleted = {name for name, item in changes.items() if item.get("deleted")}
    written = set(changes) - deleted
    require(
        not any(other.startswith(name + "/") for name in written for other in written),
        "Recovery writes conflicting file and directory paths",
    )
    for item in payload["changes"]:
        after = None if item.get("deleted") else {"mode": item["mode"], "content": item["content"]}
        require(
            digest(current.get(item["path"])) == item["before"]
            or current.get(item["path"]) == after,
            "Recovery conflicts with the current source: " + item["path"],
        )
        target = Path(workspace, item["path"])
        require(
            not any(is_link(path) for path in (target, *target.parents)),
            "Recovery destination contains a link",
        )
        for parent in target.parents:
            if parent == Path(workspace):
                break
            if parent.is_file():
                require(
                    parent.relative_to(workspace).as_posix() in deleted,
                    "Recovery parent is an undeleted file",
                )
        if target.is_dir() and not item.get("deleted"):
            for descendant in target.rglob("*"):
                require(not is_link(descendant), "Recovery directory contains a link")
                if not descendant.is_dir():
                    require(
                        descendant.relative_to(workspace).as_posix() in deleted,
                        "Recovery directory contains an undeleted file",
                    )
    # Delete before writing so ordinary file/directory transitions are valid.
    for name in sorted(deleted, key=lambda name: name.count("/"), reverse=True):
        target = Path(workspace, name)
        if not target.is_dir():
            target.unlink(missing_ok=True)
    for name in sorted(written):
        item = changes[name]
        target = Path(workspace, item["path"])
        require(
            not any(is_link(path) for path in (target, *target.parents)),
            "Recovery destination contains a link",
        )
        if target.is_dir():
            for directory in sorted(
                (path for path in target.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.rmdir()
            target.rmdir()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8", newline="")
        target.chmod(0o755 if item["mode"] == "100755" else 0o644)
    result = {
        "manifest": {name: value for name, value in manifest.items() if name != "context_snapshot"},
        "manifest_sha256": digest(manifest),
        "restored_paths": [item["path"] for item in payload["changes"]],
        "handover": payload["handover"],
        "untrusted_partial_work": True,
    }
    write_json(Path(data) / "recovery-restored.json", result)
    return result


def stop_monitor(data):
    data = Path(data)
    metadata = data / "checkpoint-monitor.json"
    if not metadata.exists():
        return
    write_json(data / "checkpoint-stop.json", {"stop": True, "deadline": time.time() + 45})
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline and read_regular_json(metadata).get("status") in {
        "starting",
        "running",
    }:
        time.sleep(0.1)
    require(
        read_regular_json(metadata).get("status") not in {"starting", "running"},
        "Final checkpoint upload did not finish before cleanup",
    )


def monitor(context, role, source, data, node, sdk, credentials):
    """Trusted detached controller, with only artifact credentials in memory."""
    from .agent_session import minutes, prepared
    from .common import kit_root

    cfg, _, _, workspace, _ = prepared(context, role, data)
    options = settings(context)
    if not options["enabled"]:
        return
    require(
        os.name == "posix" and os.geteuid() == 0,
        "Checkpoint monitoring requires the trusted Linux controller",
    )
    data = Path(data)
    require(not (data / "checkpoint-monitor.json").exists(), "Checkpoint monitor already started")
    timeout = minutes(context, cfg, role) * 60
    cadence = max(options["checkpoint_seconds"], timeout / (options["max_checkpoints"] - 1))
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        **{key: credentials[key] for key in RUNTIME if key in credentials},
    }
    require(
        environment.get("ACTIONS_RUNTIME_TOKEN") and environment.get("ACTIONS_RESULTS_URL"),
        "Actions checkpoint transport credentials are unavailable",
    )
    write_json(
        data / "checkpoint-monitor.json",
        {
            "status": "starting",
            "cadence_seconds": cadence,
            "max_checkpoints": options["max_checkpoints"],
        },
    )
    child = os.fork()
    if child:
        write_json(
            data / "checkpoint-monitor.json",
            {
                "status": "running",
                "pid": child,
                "cadence_seconds": cadence,
                "max_checkpoints": options["max_checkpoints"],
            },
        )
        return
    os.setsid()
    stream = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(stream, descriptor)
    os.environ.clear()
    os.environ.update(environment)
    sequence, uploads, failure, last = 0, 0, None, None
    deadline, next_capture = time.monotonic() + timeout + 60, time.monotonic() + cadence
    try:
        while time.monotonic() < deadline:
            owner = read_regular_json(data / "checkpoint-monitor.json")
            if owner.get("status") == "starting":
                time.sleep(0.02)
                continue
            if owner.get("pid") != os.getpid() or owner.get("status") != "running":
                break
            final = (data / "checkpoint-stop.json").exists()
            if final or (
                time.monotonic() >= next_capture and uploads < options["max_checkpoints"] - 1
            ):
                sequence += 1
                try:
                    folder, manifest = capture(
                        source,
                        workspace,
                        context,
                        role,
                        data,
                        sequence,
                        capture_point="final" if final else "interval",
                    )
                    # A failed upload never replaces the previous complete receipt.
                    remaining = 45
                    if final:
                        remaining = min(
                            45,
                            read_regular_json(data / "checkpoint-stop.json").get(
                                "deadline", time.time() + 45
                            )
                            - time.time(),
                        )
                        require(remaining > 0, "Final checkpoint cleanup deadline elapsed")
                    uploads += 1
                    process = subprocess.run(
                        [
                            node,
                            str(kit_root() / "actions/checkpoint-artifact/upload.cjs"),
                            sdk,
                            str(folder),
                            artifact_prefix(context, role) + str(sequence),
                            str(options["retention_days"]),
                        ],
                        env=environment,
                        capture_output=True,
                        timeout=remaining,
                        check=False,
                    )
                    require(process.returncode == 0, "Actions checkpoint upload failed")
                    last = {
                        "sequence": sequence,
                        "captured_at": manifest["captured_at"],
                        "capture_point": manifest["capture_point"],
                    }
                    failure = None
                except (Blocked, OSError, subprocess.SubprocessError) as exc:
                    failure = type(exc).__name__
                next_capture = time.monotonic() + cadence
                if final:
                    break
            time.sleep(0.2)
    finally:
        try:
            owner = read_regular_json(data / "checkpoint-monitor.json")
            if owner.get("pid") == os.getpid():
                write_json(
                    data / "checkpoint-monitor.json",
                    {
                        "status": "completed",
                        "pid": os.getpid(),
                        "cadence_seconds": cadence,
                        "last_complete": last,
                        "upload_attempts": uploads,
                        "capture_failure": failure,
                    },
                )
        finally:
            os._exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("monitor", "stop", "record", "fetch"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--role", required=True, choices=("request", "deliver", "review", "task"))
    parser.add_argument("--source")
    parser.add_argument("--data")
    parser.add_argument("--node")
    parser.add_argument("--sdk")
    args = parser.parse_args()
    context = read_json(args.context)
    data = args.data or str(Path(args.context).absolute().parent)
    if args.operation == "monitor":
        monitor(context, args.role, args.source, data, args.node, args.sdk, json.load(sys.stdin))
    elif args.operation == "stop":
        stop_monitor(data)
    else:
        from .github import GitHub

        gh = GitHub(context["config"]["repository"])
        result = (
            record(gh, context, args.role)
            if args.operation == "record"
            else fetch(gh, context, args.role, data)
        )
        print(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, ValueError) as exc:
        raise SystemExit("Checkpoint operation blocked: " + str(exc)) from exc

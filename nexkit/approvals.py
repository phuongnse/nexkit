"""Human decisions over persisted stage results; native YAML owns continuation jobs."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import datetime

from .common import Blocked, canonical, digest
from .github import run_attempt
from .pipelines import IDENTIFIER, composed_agents, load_run_config, workflow_path
from .policy import human, merge_gate, now, positive, require


class ApprovalRequired(Blocked):
    def __init__(self, gate, capability):
        self.gate, self.capability = gate, capability
        super().__init__(f"Human approval {gate} is required before {capability}")


def record_denial(gh, context, denial):
    state, revision = gh.get_state(context["issue"]["number"])
    require(
        state["run_key"] == context["run_key"]
        and state.get("approval_execution") == context.get("continuation"),
        "Approval denial belongs to a superseded execution",
    )
    state["approval_denial"] = {"gate": denial.gate, "capability": denial.capability}
    gh.save_state(context["issue"]["number"], state, revision)


def check_denials(gh, context, reports):
    for report in reports:
        for item in report.get("checks", []):
            denial = item.get("approval_denial")
            if not denial:
                continue
            gate, capability = denial.get("gate"), denial.get("capability")
            require(
                report.get("candidate") == context.get("candidate")
                and report.get("producer")
                == {"run_key": context["run_key"], "check": item.get("name")}
                and gate in enabled(context["config"])
                and capability == "check:" + item["name"]
                and capability in definitions(context["config"])[gate]["protects"],
                "Invalid approval denial provenance",
            )
            record_denial(gh, context, ApprovalRequired(gate, capability))


def definitions(cfg):
    return cfg.get("binding", {}).get("approvals", {})


def enabled(cfg):
    return {name: value for name, value in definitions(cfg).items() if value["enabled"]}


def recovery_gate(state):
    """Select an existing checkpoint that can resume without reserving new work."""
    if state.get("status") == "waiting_for_approval":
        return state["approval_wait"]["gate"]
    repair = state.get("approval_repair", {})
    if repair.get("status") == "pending" and state.get("status") not in {"blocked", "merged"}:
        return repair["gate"]
    execution = state.get("approval_execution")
    if (
        execution
        and state.get("status") in {"implementing", "verifying"}
        and set(state.get("invocations", {})) == set(execution["invocations"])
    ):
        return execution["gate"]
    return None


def validate_definitions(cfg):
    values = definitions(cfg)
    require(isinstance(values, dict), "Approvals must be a map of consumer-chosen gate identifiers")
    require(composed_agents(cfg), "Stage approvals require individually composed delivery")
    for name, value in values.items():
        require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "Invalid approval identifier")
        require(
            isinstance(value, dict)
            and set(value)
            == {
                "enabled",
                "mode",
                "subject",
                "reviewers",
                "minimum",
                "wait_minutes",
                "on_rejection",
                "continuation",
                "protects",
            },
            "An approval declares enabled, mode, subject, reviewers, minimum, wait_minutes, on_rejection, continuation and protects",
        )
        require(type(value["enabled"]) is bool, "Approval enabled must be a boolean")
        require(
            value["mode"] in {"issue", "pull_request"},
            "Use issue decisions or native pull-request reviews",
        )
        require(
            value["subject"] in {"stage", "candidate"},
            "Approve a stage result or a published candidate",
        )
        require(
            value["mode"] != "pull_request" or value["subject"] == "candidate",
            "PR review approves a published candidate",
        )
        reviewers = value["reviewers"]
        require(
            isinstance(reviewers, list)
            and reviewers
            and all(
                isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", x)
                for x in reviewers
            )
            and len({x.lower() for x in reviewers}) == len(reviewers),
            "Declare unique authorized human GitHub logins for each approval",
        )
        positive(value["minimum"], "approval.minimum", len(reviewers))
        positive(value["wait_minutes"], "approval.wait_minutes", 43200)
        require(
            value["on_rejection"] in {"retry", "block"},
            "Choose bounded repair or a blocked outcome on rejection",
        )
        workflow_path(value["continuation"])
        require(
            value["continuation"] in cfg["binding"]["files"],
            "Approval continuation is not an accepted workflow",
        )
        capabilities = (
            {"publish", "merge"}
            | {"invocation:" + key for key in cfg["binding"]["invocations"]}
            | {"check:" + item["name"] for item in cfg["checks"]}
        )
        protected = value["protects"]
        require(
            isinstance(protected, list)
            and protected
            and all(isinstance(x, str) and x in capabilities for x in protected)
            and len(set(protected)) == len(protected),
            "Declare the existing capabilities this approval protects",
        )
        if value["enabled"] and value["subject"] == "candidate":
            editors = {
                "invocation:" + key
                for key, item in cfg["binding"]["invocations"].items()
                if item["contract"] == "deliver"
            }
            require(
                "publish" not in protected and not editors <= set(protected),
                "A candidate approval cannot prevent creation/publication of its own candidate",
            )
        if value["enabled"] and value["mode"] == "pull_request":
            reviewers = {
                "invocation:" + key
                for key, item in cfg["binding"]["invocations"].items()
                if item["contract"] == "review"
            }
            require(
                not any(x.startswith("check:") for x in protected)
                and not reviewers <= set(protected),
                "A PR approval follows its required checks and at least one independent AI review",
            )


def pr_review_count(cfg):
    return max(
        (x["minimum"] for x in enabled(cfg).values() if x["mode"] == "pull_request"), default=0
    )


def _eligible(value, definition, gh):
    return value.get("user", {}).get("login", "").lower() in {
        name.lower() for name in definition["reviewers"]
    } and human(value, gh.permission)


def decision(gh, record):
    """Read real human authority again; event payloads and model prose grant nothing."""
    require(
        record["digest"]
        == digest(
            {key: record[key] for key in ("gate", "origin", "definition", "data", "opened_at")}
        ),
        "Approval checkpoint content changed",
    )
    definition = record["definition"]
    checkpoint = record["data"]
    context = checkpoint["context"]
    latest = {}
    if definition["mode"] == "issue":
        approve = f"/nexkit approve-stage {record['gate']} {record['digest']}"
        changes = f"/nexkit request-changes {record['gate']} {record['digest']}"
        for comment in gh.comments(context["issue"]["number"]):
            if not _eligible(comment, definition, gh):
                continue
            if comment["created_at"] != comment["updated_at"] or datetime.fromisoformat(
                comment["created_at"]
            ) < datetime.fromisoformat(record["opened_at"]):
                continue
            body = (comment.get("body") or "").replace("\r\n", "\n").strip()
            if body == approve:
                status, feedback = "approved", ""
            elif body.startswith(changes + "\n") and body[len(changes) :].strip():
                status, feedback = "changes_requested", body[len(changes) :].strip()[:6000]
            else:
                continue
            item = {
                "id": comment["id"],
                "user": comment["user"]["login"],
                "status": status,
                "feedback": feedback,
            }
            login = item["user"].lower()
            if comment["id"] > latest.get(login, {}).get("id", 0):
                latest[login] = item
    else:
        reviews = gh.api(f"{gh.root}/pulls/{context['pr']}/reviews?per_page=100", pages=True)
        external = {}
        for review in reviews:
            if not review.get("submitted_at"):
                continue
            login = review["user"]["login"].lower()
            # A dismissed review replaces its previous verdict. Ordinary review
            # comments do not erase an existing Approve or Request changes.
            if review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                continue
            if not _eligible(review, definition, gh):
                if human(review, gh.permission) and review["id"] > external.get(login, {}).get(
                    "id", 0
                ):
                    external[login] = review
                continue
            if review["id"] > latest.get(login, {}).get("id", 0):
                status = {
                    "APPROVED": "approved",
                    "CHANGES_REQUESTED": "changes_requested",
                    "DISMISSED": "waiting",
                }[review["state"]]
                if (
                    datetime.fromisoformat(review["submitted_at"])
                    < datetime.fromisoformat(record["opened_at"])
                    or review.get("commit_id") != context["candidate"]["head"]
                ):
                    status = "waiting"
                latest[login] = {
                    "id": review["id"],
                    "user": review["user"]["login"],
                    "status": status,
                    "feedback": (review.get("body") or "")[:6000],
                }
        if any(review["state"] == "CHANGES_REQUESTED" for review in external.values()):
            return {
                "status": "waiting",
                "reviews": [],
                "reason": "A native blocking review from another authorized collaborator must be resolved",
            }
    items = sorted(latest.values(), key=lambda item: item["id"])
    changes = [item for item in items if item["status"] == "changes_requested"]
    if changes:
        return {"status": "changes_requested", "reviews": changes}
    approvals = [item for item in items if item["status"] == "approved"]
    return {
        "status": "approved" if len(approvals) >= definition["minimum"] else "waiting",
        "reviews": approvals,
    }


def _recorded_inputs(state, context, reports):
    for report in reports:
        receipt = report.get("invocation", {})
        value = state.get("invocations", {}).get(
            context["run_key"] + "/" + receipt.get("id", ""), {}
        )
        require(
            value.get("status") in {"completed", "published"}
            and value.get("result") == digest(report),
            "Checkpoint input is not a recorded invocation result",
        )


def _current_subject(gh, state, record):
    context = record["data"]["context"]
    require(
        record["definition"] == definitions(context["config"])[record["gate"]],
        "Approval policy changed",
    )
    require(record["origin"] == state["run_key"], "Approval belongs to another delivery round")
    if record["definition"]["subject"] == "candidate":
        candidate = context.get("candidate")
        require(
            candidate and state.get("candidate") == candidate, "Human approval candidate is stale"
        )
        pr = gh.pull(context["pr"])
        require(
            pr["head"]["sha"] == candidate["head"]
            and pr["base"]["sha"] == candidate["base"]
            and pr["head"]["repo"]["full_name"] == gh.repository
            and pr["base"]["repo"]["full_name"] == gh.repository
            and pr["base"]["ref"] == context["config"]["default_branch"]
            and pr["state"] == "open"
            and not pr.get("merged_at"),
            "Human approval PR changed or is no longer open",
        )


def restored_data(state, context):
    execution = context.get("continuation")
    if not execution:
        return {}
    require(execution == state.get("approval_execution"), "Unrecognized approval continuation")
    record = state["stage_approvals"][execution["gate"]]
    require(
        record["status"] == "approved" and record["digest"] == execution["checkpoint"],
        "Continuation checkpoint changed",
    )
    return record["data"]


def recheck_approved(gh, context, state):
    for record in state.get("stage_approvals", {}).values():
        if record["status"] != "approved":
            continue
        _current_subject(gh, state, record)
        require(
            decision(gh, record)["status"] == "approved",
            "A stage approval was revoked or changes were requested",
        )


def require_complete(gh, context, state):
    recheck_approved(gh, context, state)
    for name in enabled(context["config"]):
        if state.get("stage_approvals", {}).get(name, {}).get("status") != "approved":
            raise ApprovalRequired(name, "merge")


def require_capability(context, state, capability):
    for name, definition in enabled(context["config"]).items():
        if capability in definition["protects"]:
            if state.get("stage_approvals", {}).get(name, {}).get("status") != "approved":
                raise ApprovalRequired(name, capability)


def request(gh, context, gate, *, inputs=(), checks=(), review=None):
    from .checks import combine_checks
    from .delivery import revalidate
    from .invocations import current_source

    state, revision = revalidate(gh, context)
    values = definitions(context["config"])
    require(gate in values, "Unknown configured human approval")
    definition = values[gate]
    if not definition["enabled"]:
        return {"status": "disabled", "ready": True, "context": context}
    require(not state.get("writer"), "Publish source changes before requesting human approval")
    require(
        not any(
            x.get("status") == "reserved"
            for key, x in state.get("invocations", {}).items()
            if key.startswith(context["run_key"] + "/")
        ),
        "Complete active agent invocations before entering a human wait",
    )
    require(
        gate not in state.get("stage_approvals", {}),
        "This gate already has a checkpoint in this delivery round",
    )
    restored = restored_data(state, context)
    inputs, checks, review = (
        inputs or restored.get("inputs", []),
        checks or restored.get("checks", []),
        review or restored.get("review"),
    )
    _recorded_inputs(state, context, inputs)
    verification = combine_checks(context, checks) if checks else None
    if review:
        _recorded_inputs(state, context, [review])
    if definition["subject"] == "candidate":
        require(
            context.get("candidate") == state.get("candidate") and context.get("candidate"),
            "Candidate approval needs the current published candidate",
        )
    if definition["mode"] == "pull_request":
        require(
            verification and review,
            "Human PR review follows current checks and independent AI review",
        )
        merge_gate(context["candidate"], verification, review, context["config"])
        receipt = state["invocations"][context["run_key"] + "/" + review["invocation"]["id"]]
        require(
            receipt.get("role") == "review" and receipt.get("verification") == digest(verification),
            "Checkpoint review did not consume these exact checks",
        )
    frozen = deepcopy(context)
    frozen.pop("invocation", None)
    frozen["source"] = current_source(state, context)
    data = {"context": frozen, "inputs": list(inputs), "checks": list(checks), "review": review}
    record = {
        "gate": gate,
        "origin": context["run_key"],
        "definition": deepcopy(definition),
        "data": data,
        "opened_at": now(),
        "status": "waiting",
    }
    record["digest"] = digest({key: value for key, value in record.items() if key != "status"})
    _current_subject(gh, state, record)
    state.setdefault("stage_approvals", {})[gate] = record
    state["approval_wait"] = {
        "gate": gate,
        "opened_at": record["opened_at"],
        "previous_status": state["status"],
        "origin_execution": state.get("approval_execution", {}).get("run_key", context["run_key"]),
    }
    state.update(status="waiting_for_approval", updated_at=now())
    require(
        len(canonical(state).encode()) <= 750000,
        "Approval checkpoint exceeds the bounded GitHub state size",
    )
    gh.save_state(context["issue"]["number"], state, revision)
    if definition["mode"] == "pull_request":
        gh.check("NexKit verification", context["candidate"]["head"], True, canonical(verification))
        gh.check("NexKit review", context["candidate"]["head"], True, canonical(review))
    notice(gh, state)
    return {
        "status": "waiting_for_approval",
        "ready": False,
        "gate": gate,
        "checkpoint": record["digest"],
    }


def notice(gh, state):
    record = state["stage_approvals"][state["approval_wait"]["gate"]]
    context = record["data"]["context"]
    marker = f"<!-- nexkit:stage-approval:{record['digest']} -->"
    if any(
        marker in (comment.get("body") or "") for comment in gh.comments(context["issue"]["number"])
    ):
        return
    subject = (
        f"Candidate `{context['candidate']['head']}`"
        if record["definition"]["subject"] == "candidate"
        else f"Stage result from source `{context['source']}`"
    )
    body = f"{marker}\nNexKit is waiting for human approval of **{record['gate']}**.\n\n{subject}. Checkpoint: `{record['digest']}`.\n\n"
    body += f"[Originating Actions run](https://github.com/{gh.repository}/actions/runs/{record['origin'].split('.')[0]}).\n\n"
    remaining = 18000
    for report in record["data"]["inputs"]:
        summary = report.get("result", {}).get("summary", "")[: min(6000, remaining)]
        if not summary:
            break
        remaining -= len(summary)
        body += (
            "Stage result:\n\n" + "\n".join("> " + line for line in summary.splitlines()) + "\n\n"
        )
    body += f"[Full persisted checkpoint](https://github.com/{gh.repository}/blob/nexkit/state/issues/{context['issue']['number']}.json).\n\n"
    if record["definition"]["mode"] == "pull_request":
        body += f"Review PR #{context['pr']} using GitHub **Approve** or **Request changes**.\n"
    else:
        body += f"Approve this exact result with:\n\n`/nexkit approve-stage {record['gate']} {record['digest']}`\n\nTo request changes, post `/nexkit request-changes {record['gate']} {record['digest']}` followed by a newline and your feedback.\n"
    body += f"\nRequired approvals: {record['definition']['minimum']} from {', '.join(record['definition']['reviewers'])}. Wait limit: {record['definition']['wait_minutes']} minutes. No runner or model remains active while awaiting this decision."
    gh.comment(context["issue"]["number"], body)


def _end_wait(state):
    waited = max(
        0,
        (
            datetime.fromisoformat(now())
            - datetime.fromisoformat(state["approval_wait"]["opened_at"])
        ).total_seconds(),
    )
    state["human_wait_seconds"] = state.get("human_wait_seconds", 0) + waited
    state["status"] = state["approval_wait"]["previous_status"]
    state.pop("approval_wait")


def stop(gh, number, state, revision, reason):
    if state.get("approval_wait"):
        _end_wait(state)
    state.update(status="blocked", reason=reason, human_stop=now(), updated_at=now())
    gh.save_state(number, state, revision)
    return {"ready": False, "reason": reason, "status": "blocked"}


def human_feedback(gh, record, verdict):
    feedback = "Human requested changes at " + record["gate"] + ":\n"
    feedback += "\n".join(x["user"] + ": " + x["feedback"] for x in verdict["reviews"])
    if record["definition"]["mode"] == "pull_request":
        for item in verdict["reviews"]:
            comments = gh.api(
                f"{gh.root}/pulls/{record['data']['context']['pr']}/reviews/{item['id']}/comments?per_page=100",
                pages=True,
            )
            feedback += (
                "\n"
                + "\n".join(
                    f"{x.get('path', '')}: {(x.get('body') or '')[:3000]}" for x in comments
                )[:18000]
            )
    return feedback.encode()[:24000].decode(errors="ignore")


def decision_receipt(verdict):
    """Keep identities; bounded feedback is stored once for delivery recovery."""
    return {
        **verdict,
        "reviews": [
            {k: v for k, v in item.items() if k != "feedback"} for item in verdict["reviews"]
        ],
    }


def admit_continuation(gh, record):
    cfg = record["data"]["context"]["config"]
    expected = (
        gh.repository
        + "/"
        + record["definition"]["continuation"]
        + "@refs/heads/"
        + cfg["default_branch"]
    )
    require(
        os.environ.get("GITHUB_WORKFLOW_REF") == expected,
        "Caller is not the recorded approval continuation",
    )


def resume(gh, number, pipeline, gate, kit_ref, run_key):
    from .delivery import revalidate

    state, revision = gh.get_state(number)
    if state.get("pr") and state.get("candidate") and state.get("status") != "merged":
        pr = gh.pull(state["pr"])
        if pr.get("merged_at"):
            require(
                pr["head"]["sha"] == state["candidate"]["head"],
                "Unexpected merged approval candidate",
            )
            state.update(status="merged", merge_sha=pr["merge_commit_sha"], updated_at=now())
            gh.save_state(number, state, revision)
            return {"ready": False, "status": "merged"}
    if state.get("approval_repair", {}).get("gate") == gate:
        return repair(gh, number, pipeline, gate, kit_ref)
    execution = state.get("approval_execution")
    if (
        execution
        and execution["gate"] == gate
        and state.get("status") in {"implementing", "verifying"}
    ):
        record = state["stage_approvals"][gate]
        context = deepcopy(record["data"]["context"])
        context["continuation"] = execution
        cfg = context["config"]
        require(
            pipeline == cfg["binding"]["pipeline"] and kit_ref == cfg["kit"]["ref"],
            "Approval continuation identity changed",
        )
        admit_continuation(gh, record)
        if execution["run_key"] != run_key:
            # Recover a claim whose workflow stopped before any subsequent CLI
            # reservation. Completed agent calls are never replayed as free work.
            if set(state.get("invocations", {})) != set(execution["invocations"]):
                return {
                    "ready": False,
                    "reason": "Continuation already started agent work; use bounded delivery recovery",
                }
            previous = run_attempt(gh, execution["run_key"])
            if previous["status"] != "completed":
                return {"ready": False, "reason": "Approval continuation is already active"}
        try:
            require(
                load_run_config(
                    gh, context["base"], pipeline, "delivery", caller=execution["workflow"]
                )
                == cfg,
                "Approval continuation configuration changed",
            )
            # Handle changed human decisions explicitly below, after validating
            # the immutable subject and current requirement/configuration.
            state, revision = revalidate(gh, context, check_approvals=False)
            for approved in state.get("stage_approvals", {}).values():
                if approved["status"] == "approved":
                    _current_subject(gh, state, approved)
        except Blocked as exc:
            return stop(gh, number, state, revision, str(exc))
        try:
            recheck_approved(gh, context, state)
        except Blocked as exc:
            state["approval_repair"] = {
                "gate": gate,
                "checkpoint": record["digest"],
                "feedback": str(exc),
                "status": "pending",
            }
            gh.save_state(number, state, revision)
            return repair(gh, number, pipeline, gate, kit_ref)
        if execution["run_key"] != run_key:
            execution = {**execution, "run_key": run_key}
            state["approval_execution"] = execution
            gh.save_state(number, state, revision)
            context["continuation"] = execution
        return {"ready": True, "status": "approved", "context": context}
    if state.get("status") != "waiting_for_approval" or state["approval_wait"]["gate"] != gate:
        return {"ready": False, "reason": "No matching pending human approval"}
    record = state["stage_approvals"][gate]
    context = record["data"]["context"]
    cfg = context["config"]
    require(
        pipeline == cfg["binding"]["pipeline"] and kit_ref == cfg["kit"]["ref"],
        "Approval belongs to a different pipeline or kit",
    )
    admit_continuation(gh, record)
    require(
        run_attempt(gh, state["approval_wait"]["origin_execution"])["status"] == "completed",
        "The originating workflow must finish before approval continuation",
    )
    try:
        require(
            load_run_config(
                gh,
                context["base"],
                pipeline,
                "delivery",
                caller=record["definition"]["continuation"],
            )
            == cfg,
            "Approval continuation configuration changed",
        )
        state, revision = revalidate(gh, context, waiting=True)
        _current_subject(gh, state, record)
        waited = (
            datetime.fromisoformat(now()) - datetime.fromisoformat(record["opened_at"])
        ).total_seconds()
        require(waited < record["definition"]["wait_minutes"] * 60, "Human approval wait expired")
    except Blocked as exc:
        return stop(gh, number, state, revision, str(exc))
    verdict = decision(gh, record)
    if verdict["status"] == "waiting":
        notice(gh, state)
        return {
            "ready": False,
            "status": "waiting_for_approval",
            "reason": verdict.get("reason", "Awaiting the configured human reviewers"),
        }
    _end_wait(state)
    record = state["stage_approvals"][gate]
    record.update(status=verdict["status"], decision=decision_receipt(verdict), decided_at=now())
    if verdict["status"] == "changes_requested":
        feedback = human_feedback(gh, record, verdict)
        if record["definition"]["on_rejection"] == "block":
            state.update(
                status="blocked",
                reason=feedback,
                feedback={"reason": feedback, "verification": None, "review": None},
                human_stop=now(),
                updated_at=now(),
            )
            gh.save_state(number, state, revision)
            return {"ready": False, "status": "blocked", "reason": feedback}
        state["approval_repair"] = {
            "gate": gate,
            "checkpoint": record["digest"],
            "feedback": feedback,
            "status": "pending",
        }
        gh.save_state(number, state, revision)
        return repair(gh, number, pipeline, gate, kit_ref)
    execution = {
        "gate": gate,
        "checkpoint": record["digest"],
        "origin": context["run_key"],
        "run_key": run_key,
        "workflow": record["definition"]["continuation"],
        "invocations": list(state.get("invocations", {})),
    }
    if state.get("approval_denial", {}).get("gate") == gate:
        state.pop("approval_denial")
    state.update(approval_execution=execution, updated_at=now())
    gh.save_state(number, state, revision)
    resumed = deepcopy(context)
    resumed["continuation"] = execution
    return {"ready": True, "status": "approved", "context": resumed}


def repair(gh, number, pipeline, gate, kit_ref):
    """Recover feedback persistence and a possibly interrupted native dispatch."""
    from .delivery import failed
    from .pipelines import dispatch

    state, revision = gh.get_state(number)
    intent = state["approval_repair"]
    record = state["stage_approvals"][gate]
    context = deepcopy(record["data"]["context"])
    if state.get("approval_execution"):
        context["continuation"] = state["approval_execution"]
    cfg = context["config"]
    require(
        intent["checkpoint"] == record["digest"] and record["origin"] == state["run_key"],
        "Repair intent belongs to a different checkpoint",
    )
    require(
        pipeline == cfg["binding"]["pipeline"] and kit_ref == cfg["kit"]["ref"],
        "Repair intent configuration changed",
    )
    admit_continuation(gh, record)
    try:
        require(
            load_run_config(
                gh,
                context["base"],
                pipeline,
                "delivery",
                caller=record["definition"]["continuation"],
            )
            == cfg,
            "Repair continuation configuration changed",
        )
    except Blocked as exc:
        return stop(gh, number, state, revision, str(exc))
    if state["status"] not in {"retry", "blocked"}:
        failed(
            gh,
            context,
            intent["feedback"],
            dispatch_retry=False,
        )
        state, revision = gh.get_state(number)
    if state["status"] == "retry" and intent["status"] != "dispatched":
        dispatch(gh, cfg, "delivery", {"issue": number})
        state["approval_repair"]["status"] = "dispatched"
        gh.save_state(number, state, revision)
    return {"ready": False, "status": state["status"], "reason": intent["feedback"]}


def human_stop_released(gh, state, number):
    return any(
        (c.get("body") or "").strip() == "/nexkit resume"
        and c["created_at"] == c["updated_at"]
        and datetime.fromisoformat(c["created_at"]) > datetime.fromisoformat(state["human_stop"])
        and human(c, gh.permission)
        for c in gh.comments(number)
    )


def relay_review(gh, event, kit_ref):
    """A PR-context event may only wake accepted default-branch continuations."""
    from .pipelines import effective_config, verify_controls

    pr = event.get("pull_request", {})
    if pr.get("head", {}).get("repo", {}).get("full_name") != gh.repository:
        return {"dispatched": False, "reason": "Not a same-repository delivery PR"}
    match = re.fullmatch(
        r"nexkit/([a-z][a-z0-9_-]{0,47})/issue-([1-9][0-9]*)", pr.get("head", {}).get("ref", "")
    )
    if not match:
        return {"dispatched": False, "reason": "Not a composed NexKit delivery branch"}
    pipeline, number = match[1], int(match[2])
    state, _ = gh.get_state(number)
    gate = recovery_gate(state)
    if not gate or state.get("pr") != pr.get("number"):
        return {"dispatched": False, "reason": "No matching pending PR approval"}
    record = state["stage_approvals"][gate]
    if record["definition"]["mode"] != "pull_request":
        return {"dispatched": False, "reason": "Current gate uses issue decisions"}
    base = gh.ref(gh.repo()["default_branch"])
    cfg = effective_config(gh.read_config(base), pipeline)
    require(
        cfg["kit"]["ref"] == kit_ref and cfg == record["data"]["context"]["config"],
        "PR event does not match the accepted approval configuration",
    )
    verify_controls(gh, cfg, base)
    if not human({"user": event.get("sender", {})}, gh.permission):
        return {
            "dispatched": False,
            "reason": "Review event actor is not an authorized gate reviewer",
        }
    gh.dispatch(
        record["definition"]["continuation"].removeprefix(".github/workflows/"),
        cfg["default_branch"],
        {"issue": number},
    )
    return {"dispatched": True, "issue": number, "gate": record["gate"]}

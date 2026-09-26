"""Concrete GitHub delivery steps. Actions supplies scheduling and job isolation."""

from __future__ import annotations

from .common import Blocked, canonical, digest
from .pipelines import (
    bind_state,
    composed_agents,
    current_config,
    dispatch,
    issue_pipeline,
    load_run_config,
)
from .policy import (
    agent_result,
    approval,
    candidate_key,
    control_command,
    delivery_budget_used,
    merge_gate,
    now,
    protected_path,
    require,
    reserve,
    spec_hash,
)

RELEASE_MARKER = "<!-- nexkit:release -->"


def authorized(gh, number, *, release=False):
    issue = gh.issue(number)
    comments = gh.comments(number)
    require(
        control_command(comments, gh.permission) != "cancel",
        "Delivery cancelled by an authorized human",
    )
    approved = approval(issue, comments, gh.permission, release=release)
    return issue, approved


def prepare(gh, number, run_key, kit_ref, *, pipeline=None, individual_agents=False):
    """Read live authority and reserve a bounded run before any model invocation."""
    issue = gh.issue(number)
    if (issue.get("body") or "").startswith(RELEASE_MARKER) or issue.get("pull_request"):
        return {"ready": False, "reason": "Not a delivery work item"}
    if issue_pipeline(issue) != pipeline:
        return {"ready": False, "reason": "Work item belongs to another pipeline"}
    state, revision = gh.get_state(number)
    if state.get("status") == "merged":
        return {"ready": False, "reason": "Already merged", "state": state}
    if state.get("pr") and state.get("candidate"):
        prior_pr = gh.pull(state["pr"])
        if prior_pr.get("merged_at"):
            require(
                prior_pr["head"]["sha"] == state["candidate"]["head"], "Unexpected merged candidate"
            )
            state.update(status="merged", merge_sha=prior_pr["merge_commit_sha"], updated_at=now())
            gh.save_state(number, state, revision)
            return {"ready": False, "reason": "Recovered completed merge", "state": state}
    try:
        issue, approved = authorized(gh, number)
        repository = gh.repo()
        base = gh.ref(repository["default_branch"])
        cfg = load_run_config(gh, base, pipeline, "delivery")
        require(
            composed_agents(cfg) == individual_agents,
            "Select individual agent capabilities for a pipeline with invocations",
        )
        bind_state(state, cfg)
        require(cfg["repository"] == gh.repository, "Configuration belongs to another repository")
        require(cfg["default_branch"] == repository["default_branch"], "Default branch changed")
        require(cfg["kit"]["ref"] == kit_ref, "Caller and configured kit revisions differ")
        gh.strict_protection(cfg["default_branch"])
        require(
            {"test", "e2e"} <= {c["kind"] for c in cfg["checks"]},
            "Setup must declare the intended test and E2E commands before bootstrap delivery",
        )
        if state.get("run_key") == run_key:
            return {"ready": False, "reason": "Duplicate run reservation"}
        if state.get("status") in ("implementing", "verifying"):
            old_id = str(state.get("run_key", "")).split(".")[0]
            require(old_id.isdigit(), "Unrecognized prior run identity")
            prior = gh.api(f"{gh.root}/actions/runs/{old_id}")
            require(prior["status"] == "completed", "Previous delivery run is still active")
        state = reserve(state, cfg, run_key)
        state.update(
            status="implementing",
            writer=None,
            run_key=run_key,
            issue=int(number),
            repository=gh.repository,
            approval=approved,
            config=digest(cfg),
            base=base,
            kit=kit_ref,
            updated_at=now(),
        )
        revision = gh.save_state(number, state, revision)
        source = base
        prefix = f"nexkit/{pipeline}/" if pipeline else "nexkit/"
        branch = f"{prefix}issue-{int(number)}"
        pr = gh.pull_for_branch(branch)
        if pr:
            require(pr["state"] == "open" and not pr.get("merged_at"), "Prior PR is no longer open")
            source = pr["head"]["sha"]
        else:
            try:
                source = gh.ref(branch)
            except Blocked as exc:
                if "HTTP 404" not in str(exc):
                    raise
        if individual_agents:
            state["round_source"] = source
            revision = gh.save_state(number, state, revision)
        return {
            "ready": True,
            "issue": issue,
            "approval": approved,
            "config": cfg,
            "base": base,
            "source": source,
            "branch": branch,
            "run_key": run_key,
            "feedback": state.get("feedback"),
            "attempt": state["attempts"],
            "repository": gh.repository,
        }
    except Blocked as exc:
        # Approval has not happened yet: no model call, no runner waiting.
        state.update(status="blocked", reason=str(exc), updated_at=now())
        gh.save_state(number, state, revision)
        return {"ready": False, "reason": str(exc)}


def revalidate(gh, context):
    number = context["issue"]["number"]
    issue, approved = authorized(gh, number)
    cfg = context["config"]
    require(approved == context["approval"], "Requirement approval changed or was revoked")
    require(spec_hash(issue) == spec_hash(context["issue"]), "Requirement changed")
    require(
        gh.ref(cfg["default_branch"]) == context["base"],
        "Base changed; rebuild and reverify integration",
    )
    current_config(gh, context["base"], cfg)
    state, revision = gh.get_state(number)
    bind_state(state, cfg)
    require(state.get("run_key") == context["run_key"], "Superseded delivery run")
    require(
        state.get("config") == digest(cfg)
        and state.get("base") == context["base"]
        and state.get("approval") == context["approval"],
        "Delivery context differs from its accepted reservation",
    )
    require(state.get("status") in ("implementing", "verifying"), "Delivery is no longer active")
    from datetime import datetime

    require(
        (
            datetime.fromisoformat(now()) - datetime.fromisoformat(state["started_at"])
        ).total_seconds()
        < cfg["limits"]["minutes"] * 60,
        "Total delivery time budget exhausted",
    )
    return state, revision


def validate_changes(changes, cfg=None):
    require(
        isinstance(changes, list) and 0 < len(changes) <= 200,
        "No changes, or too many changed files",
    )
    total, names = 0, set()
    tree = []
    for item in changes:
        require(isinstance(item, dict), "Invalid change record")
        path = item.get("path")
        require(
            not protected_path(path, cfg), f"Administrative policy change requires setup: {path}"
        )
        require(path not in names, "Duplicate changed path")
        names.add(path)
        require(
            item.get("mode") in ("100644", "100755"),
            "Symlinks and submodules cannot cross the publication boundary",
        )
        entry = {"path": path, "mode": item["mode"], "type": "blob"}
        if item.get("deleted") is True:
            entry["sha"] = None
        else:
            content = item.get("content")
            require(isinstance(content, str), "Missing file contents")
            total += len(content.encode())
            require(
                total <= 2_000_000, "Change bundle exceeds the configured transfer bound (2 MB)"
            )
            entry["content"] = content
        tree.append(entry)
    return tree


def publish(gh, context, bundle):
    state, revision = revalidate(gh, context)
    if composed_agents(context["config"]):
        from .invocations import guard, reservation_key

        record = state.get("invocations", {}).get(reservation_key(context), {})
        require(
            record.get("context") == digest(context)
            and record.get("role") == "deliver"
            and record.get("result") == digest(bundle),
            "Publication requires the recorded source-editing result",
        )
        if record.get("status") == "published":
            require(record.get("candidate") == state.get("candidate"), "Publication was superseded")
            require(
                gh.ref(context["branch"]) == state["candidate"]["head"], "Published branch changed"
            )
            return publication_context(context, state["candidate"], state["pr"], bundle["result"])
        state, revision = guard(gh, context, completed=True)
        require(record.get("status") == "completed", "Source editing has not completed")
    require(bundle.get("run_key") == context["run_key"], "Bundle belongs to another run")
    require(
        bundle.get("source") == context["source"] and bundle.get("base") == context["base"],
        "Bundle belongs to another source or base",
    )
    result = agent_result(bundle.get("result"), "deliver")
    require(result["status"] == "done", "Implementer is blocked: " + result["summary"])
    entries = validate_changes(bundle.get("changes"), context["config"])
    base_commit = gh.api(f"{gh.root}/git/commits/{context['base']}")
    tree = gh.api(
        f"{gh.root}/git/trees", "POST", {"base_tree": base_commit["tree"]["sha"], "tree": entries}
    )
    require(tree["sha"] != base_commit["tree"]["sha"], "Empty changes cannot trigger a delivery")
    # A retry can rediscover a previously published candidate after interruption.
    pr = gh.pull_for_branch(context["branch"])
    parents = [context["base"]]
    commit = None
    if pr:
        require(
            pr["head"]["sha"] == context["source"], "PR candidate changed during implementation"
        )
    existing_branch = context["source"] != context["base"]
    if existing_branch:
        previous = gh.api(f"{gh.root}/git/commits/{context['source']}")
        integration = gh.api(f"{gh.root}/compare/{context['base']}...{context['source']}")
        if previous["tree"]["sha"] == tree["sha"] and integration["status"] in (
            "ahead",
            "identical",
        ):
            # Recover publication interruptions and allow reasoned rebuttals to
            # findings without manufacturing an empty commit to retrigger checks.
            commit = previous
        parents = [context["source"], context["base"]]
    fingerprint = digest({"tree": tree["sha"], "base": context["base"], "result": result})
    require(
        not (state.get("last_bundle") == fingerprint and state.get("feedback")),
        "No progress: the same candidate and explanation were returned again",
    )
    if commit is None:
        commit = gh.api(
            f"{gh.root}/git/commits",
            "POST",
            {
                "message": f"NexKit #{context['issue']['number']}: {context['issue']['title'][:160]}",
                "tree": tree["sha"],
                "parents": parents,
            },
        )
    revalidate(gh, context)
    if existing_branch:
        if commit["sha"] != context["source"]:
            gh.api(
                f"{gh.root}/git/refs/heads/{context['branch']}",
                "PATCH",
                {"sha": commit["sha"], "force": False},
            )
    else:
        try:
            gh.api(
                f"{gh.root}/git/refs",
                "POST",
                {"ref": "refs/heads/" + context["branch"], "sha": commit["sha"]},
            )
        except Blocked as exc:
            if "HTTP 422" not in str(exc):
                raise
            require(gh.ref(context["branch"]) == commit["sha"], "Existing delivery branch differs")
    if not pr:
        pr = gh.api(
            f"{gh.root}/pulls",
            "POST",
            {
                "title": context["issue"]["title"],
                "head": context["branch"],
                "base": context["config"]["default_branch"],
                "body": f"Closes #{context['issue']['number']}\n\nRequirement `{context['approval']['spec']}`.\n"
                "NexKit will merge only after current independent review and verification.",
            },
        )
    key = candidate_key(context["issue"], context["config"], context["base"], commit["sha"])
    for attempt in range(3):
        # Independent read-only invocations may record their result while this
        # job publishes. Keep their reservations and reports on CAS retries.
        if composed_agents(context["config"]):
            state, revision = guard(gh, context, completed=True)
            state["invocations"][reservation_key(context)].update(status="published", candidate=key)
            state.update(candidate_run=context["run_key"], writer=None)
        state.update(
            status="verifying",
            candidate=key,
            pr=pr["number"],
            last_bundle=fingerprint,
            updated_at=now(),
        )
        try:
            gh.save_state(context["issue"]["number"], state, revision)
            break
        except Blocked as exc:
            if not composed_agents(context["config"]) or attempt == 2 or "HTTP 409" not in str(exc):
                raise
    return publication_context(context, key, pr["number"], result)


def publication_context(context, key, pr, result):
    value = {**context, "candidate": key, "pr": pr, "implementation": result}
    value.pop("invocation", None)
    return value


def finish(gh, context, verification, review):
    number = context["issue"]["number"]
    try:
        state, revision = revalidate(gh, context)
        key = context["candidate"]
        pr = gh.pull(context["pr"])
        require(
            pr["head"]["sha"] == key["head"] and pr["base"]["sha"] == key["base"],
            "PR head/base changed; review and checks are stale",
        )
        require(
            pr["base"]["repo"]["full_name"] == gh.repository
            and pr["head"]["repo"]["full_name"] == gh.repository,
            "PR repository identity mismatch",
        )
        require(pr["base"]["ref"] == context["config"]["default_branch"], "PR target changed")
        require(
            state.get("candidate") == key, "Candidate differs from the published work item state"
        )
        if composed_agents(context["config"]):
            receipt = review.get("invocation", {})
            record = state.get("invocations", {}).get(
                context["run_key"] + "/" + receipt.get("id", ""), {}
            )
            require(
                record.get("role") == "review"
                and record.get("status") == "completed"
                and record.get("result") == digest(review)
                and record.get("verification") == digest(verification),
                "Merge requires a recorded independent review from this run",
            )
            require(
                not state.get("writer")
                and all(
                    value["status"] != "reserved"
                    for name, value in state.get("invocations", {}).items()
                    if name.startswith(context["run_key"] + "/")
                ),
                "A started agent invocation has not completed",
            )
        merge_gate(key, verification, review, context["config"])
        gh.strict_protection(context["config"]["default_branch"])
        # Check runs are emitted only by this trusted controller, after validated
        # reports from the current workflow's isolated jobs.
        gh.check("NexKit verification", key["head"], True, canonical(verification))
        gh.check("NexKit review", key["head"], True, canonical(review))
        revalidate(gh, context)
        merged = gh.api(
            f"{gh.root}/pulls/{context['pr']}/merge",
            "PUT",
            {"sha": key["head"], "merge_method": context["config"]["merge_method"]},
        )
        require(merged.get("merged") is True, "GitHub refused merge; protections remain enforced")
        state.update(status="merged", merge_sha=merged["sha"], updated_at=now(), feedback=None)
        gh.save_state(number, state, revision)
        gh.comment(
            number,
            f"NexKit merged PR #{context['pr']} at `{merged['sha']}`. "
            f"Attempts: {state['attempts']}; reserved agent calls: {state['agent_calls']}. "
            "No release was created. Knowledge changes are included in the reviewed PR.",
        )
        return state
    except Blocked as exc:
        return failed(gh, context, str(exc), verification=verification, review=review)


def failed(gh, context, reason, *, verification=None, review=None):
    number = context["issue"]["number"]
    state, revision = gh.get_state(number)
    require(state.get("run_key") == context["run_key"], "Cannot mutate another delivery run")
    if state.get("status") == "merged":
        return state
    # A successful merge followed by an interrupted state update is recoverable.
    if state.get("pr"):
        pr = gh.pull(state["pr"])
        if pr.get("merged_at"):
            require(
                pr["head"]["sha"] == state.get("candidate", {}).get("head"),
                "Unexpected merged candidate",
            )
            state.update(status="merged", merge_sha=pr["merge_commit_sha"], updated_at=now())
            gh.save_state(number, state, revision)
            return state
    cfg = context["config"]
    retry = (
        state.get("attempts", 0) < cfg["limits"]["attempts"]
        and delivery_budget_used(state, cfg) + 2 <= cfg["limits"]["agent_calls"]
    )
    try:
        issue, approved = authorized(gh, number)
        require(approved == context["approval"], "Approval changed")
    except Blocked:
        retry = False
    state.update(
        status="retry" if retry else "blocked",
        reason=reason,
        feedback={"reason": reason, "verification": verification, "review": review},
        updated_at=now(),
    )
    gh.save_state(number, state, revision)
    gh.comment(
        number,
        f"NexKit {state['status']}: {reason[:1500]}\n\n"
        f"Attempt {state.get('attempts', 0)}/{cfg['limits']['attempts']}. "
        "See Actions logs and `nexkit status` for details.",
    )
    if retry:
        dispatch(gh, cfg, "delivery", {"issue": number})
    return state

"""Protected setup proposals, isolated review and exact native administrator approval."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import tempfile
from copy import deepcopy
from pathlib import Path

from .administrative_publisher import KEY_ENV, publisher, validate_publication
from .common import Blocked, canonical, consumer_path, digest, kit_root, read_json, run, write_json
from .criteria import coverage
from .github import run_attempt
from .pipelines import effective_config, verify_controls
from .policy import HASH, SHA, agent_result, agent_runner, config, human, require

PROPOSAL_PATH = ".nexkit/administration/proposal.json"
WORKFLOW_PATH = ".github/workflows/nexkit-administration.yml"
CRITERIA = [
    {
        "id": "ADM1",
        "text": "The proposed configuration, ownership ledger and control hashes agree.",
    },
    {
        "id": "ADM2",
        "text": "Changed workflows preserve authority, credentials and the consumer's intended checks and approvals.",
    },
    {
        "id": "ADM3",
        "text": "The change's impact on in-flight work, budgets and runner admission is explicit and acceptable.",
    },
]


def bootstrap(proposal):
    """A push runs this exact template before any default-branch installation exists."""
    identifier = digest(proposal)
    pin = proposal["project"]["kit"]
    return f"""name: NexKit administrative setup
on:
  push:
    branches: [nexkit/setup-{identifier[:24]}]
permissions: {{}}
concurrency:
  group: nexkit-administration-{identifier}
  cancel-in-progress: false
jobs:
  setup:
    permissions:
      contents: write
      actions: read
      issues: read
      pull-requests: write
      checks: write
    uses: {pin["repository"]}/.github/workflows/administration.yml@{pin["ref"]}
    with:
      kit_repository: {pin["repository"]}
      kit_ref: {pin["ref"]}
      proposal: {identifier}
      proposal_sha: ${{{{ github.sha }}}}
    secrets:
      OPENAI_API_KEY: ${{{{ secrets.OPENAI_API_KEY }}}}
      NEXKIT_ADMIN_APP_PRIVATE_KEY: ${{{{ secrets.NEXKIT_ADMIN_APP_PRIVATE_KEY }}}}
"""


def plan(
    root,
    project,
    pipeline,
    *,
    bundle=None,
    hosts=("codex",),
    minutes=10,
    calls=2,
    documents=(),
    publication=None,
):
    """Prepare reviewable files; do not spend work budgets or write GitHub state."""
    import shutil

    from .project import install

    root = Path(root).resolve()
    validate_publication(publication)
    project = config(deepcopy(project))
    require(
        type(minutes) is int and 1 <= minutes <= 60 and type(calls) is int and 1 <= calls <= 5,
        "Administrative review limits are bounded",
    )
    base = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    require(SHA.fullmatch(base), "Use a committed base checkout")
    old = (
        read_json(consumer_path(root, ".nexkit/project.json"))
        if (root / ".nexkit/project.json").exists()
        else None
    )
    if old:
        config(old)
        accepted = run(["git", "show", base + ":.nexkit/project.json"], cwd=root, check=False)
        require(
            accepted.returncode == 0 and read_value(accepted.stdout) == old,
            "Start administrative planning from the unchanged committed accepted project",
        )
        require(
            old["repository"] == project["repository"]
            and old["default_branch"] == project["default_branch"],
            "Administrative updates preserve repository identity",
        )
    review = review_settings(effective_config(old or project, pipeline))
    require(
        review.get("engine") and "review" in review.get("models", {}),
        "Administrative setup needs an accepted independent reviewer integration",
    )
    with tempfile.TemporaryDirectory(prefix="nexkit-setup-plan-") as temporary:
        staging = Path(temporary) / "consumer"
        shutil.copytree(
            root,
            staging,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "node_modules", ".venv"),
        )
        updates = {}
        if bundle and old:
            for name, record in project["files"].items():
                previous = old["files"].get(name, {})
                if (
                    name.startswith(".github/workflows/")
                    and record["managed"] is False
                    and previous.get("managed") is False
                    and record["sha256"] != previous.get("sha256")
                ):
                    updates[name] = consumer_path(bundle, name).read_bytes()
        result = install(
            staging,
            project,
            list(hosts),
            apply=True,
            bundle=bundle,
            kit=kit_root(),
            consumer_updates=updates,
        )
        names = {
            item["path"]
            for item in result["changes"]
            if item["operation"] in {"add", "update", "remove"}
        }
        names.update((".nexkit/project.json", ".nexkit/installation.json"))
        for name in documents:
            require(
                name.endswith(".md") and name.startswith("docs/"),
                "Additional setup explanations must be docs/*.md files",
            )
            consumer_path(root, name)
            names.add(name)
        changes = []
        for name in sorted(names):
            path = consumer_path(root if name in documents else staging, name)
            content = path.read_text(encoding="utf-8") if path.exists() else None
            changes.append({"path": name, "content": content})
        ledger = read_json(staging / ".nexkit/installation.json")
    proposal = {
        "schema": 1,
        "repository": project["repository"],
        "base": base,
        "previous_project": digest(old) if old else None,
        "project": project,
        "ledger": ledger,
        "pipeline": pipeline,
        "review": {"minutes": minutes, "calls": calls},
        "changes": changes,
        "publication": deepcopy(publication),
    }
    validate(proposal)
    return {
        "proposal": proposal,
        "id": digest(proposal),
        "branch": "nexkit/setup-" + digest(proposal)[:24],
        "bootstrap": bootstrap(proposal),
        "changes": result["changes"],
        "impact": {
            "old_project": proposal["previous_project"],
            "new_project": digest(project),
            "changed_paths": sorted(names),
            "usage": "Existing issue identity and consumption are retained; old evidence must be revalidated.",
            "runner_admission": "Refresh each affected subscription runner's accepted configuration and control hashes before running the updated pipelines.",
        },
    }


def validate(proposal):
    if isinstance(proposal, dict):
        validate_publication(proposal.get("publication"))
    require(
        isinstance(proposal, dict)
        and set(proposal)
        == {
            "schema",
            "repository",
            "base",
            "previous_project",
            "project",
            "ledger",
            "pipeline",
            "review",
            "changes",
            "publication",
        }
        and proposal["schema"] == 1,
        "Invalid administrative proposal",
    )
    project = config(proposal["project"])
    require(
        proposal["repository"] == project["repository"]
        and SHA.fullmatch(proposal["base"])
        and (proposal["previous_project"] is None or HASH.fullmatch(proposal["previous_project"])),
        "Invalid administrative identity",
    )
    effective_config(project, proposal["pipeline"])
    review = proposal["review"]
    require(
        isinstance(review, dict)
        and set(review) == {"minutes", "calls"}
        and type(review["minutes"]) is int
        and 1 <= review["minutes"] <= 60
        and type(review["calls"]) is int
        and 1 <= review["calls"] <= 5,
        "Invalid administrative review budget",
    )
    changes = proposal["changes"]
    require(
        isinstance(changes, list)
        and 2 <= len(changes) <= 200
        and len(canonical(proposal).encode()) <= 450000,
        "Administrative proposal exceeds its transfer bound",
    )
    names = []
    for item in changes:
        require(
            isinstance(item, dict) and set(item) == {"path", "content"},
            "Invalid administrative file",
        )
        name = item["path"]
        consumer_path(Path.cwd(), name)
        require(
            name in {".nexkit/project.json", ".nexkit/installation.json"}
            or name.startswith(
                (".github/workflows/", ".nexkit/controls/", ".agents/skills/nexkit-")
            )
            or (name.startswith("docs/") and name.endswith(".md"))
            or name in project["files"],
            "Administrative publication cannot deliver ordinary source",
        )
        require(
            item["content"] is None or isinstance(item["content"], str),
            "Administrative files must be regular text",
        )
        names.append(name)
    require(
        len(names) == len(set(names))
        and {".nexkit/project.json", ".nexkit/installation.json"} <= set(names),
        "Duplicate or missing administrative files",
    )
    values = {item["path"]: item["content"] for item in changes}
    require(
        read_value(values[".nexkit/project.json"]) == project
        and read_value(values[".nexkit/installation.json"]) == proposal["ledger"],
        "Proposal configuration/ledger differ from publication",
    )
    ledger = proposal["ledger"]
    require(
        ledger.get("project") == digest(project)
        and ledger.get("version") == project["kit"]["version"]
        and set(ledger.get("bundle_files", []))
        == {name for name, record in project["files"].items() if record["managed"]},
        "Installation ledger differs from the accepted project",
    )
    for name, checksum in ledger.get("files", {}).items():
        if name in values:
            require(
                values[name] is not None
                and hashlib.sha256(values[name].encode()).hexdigest() == checksum,
                "Installation ledger file hash differs: " + name,
            )
    for name, record in project["files"].items():
        if record["managed"]:
            require(
                ledger.get("files", {}).get(name) == record["sha256"],
                "Managed ownership hash is missing: " + name,
            )


def read_value(value):
    import json

    require(isinstance(value, str), "Missing administrative JSON")
    try:
        return json.loads(value)
    except ValueError as exc:
        raise Blocked("Invalid administrative JSON") from exc


def remote_bytes(gh, name, ref):
    item = gh.content(name, ref)
    require(
        item.get("type") == "file"
        and item.get("encoding") == "base64"
        and item.get("size", 0) <= 450000,
        "Administrative input must be a bounded regular file",
    )
    return base64.b64decode(item["content"])


def admitted(gh, identifier, proposal_sha, kit_ref):
    require(
        HASH.fullmatch(identifier) and SHA.fullmatch(proposal_sha),
        "Invalid administrative proposal reference",
    )
    proposal = read_value(remote_bytes(gh, PROPOSAL_PATH, proposal_sha).decode())
    validate(proposal)
    require(
        digest(proposal) == identifier
        and proposal["repository"] == gh.repository
        and proposal["project"]["kit"]["ref"] == kit_ref,
        "Administrative proposal or controller kit changed",
    )
    branch = "nexkit/setup-" + identifier[:24]
    require(
        os.environ.get("GITHUB_WORKFLOW_REF")
        == f"{gh.repository}/{WORKFLOW_PATH}@refs/heads/{branch}"
        and os.environ.get("GITHUB_WORKFLOW_SHA") == proposal_sha
        and os.environ.get("GITHUB_REF") == "refs/heads/" + branch,
        "Administrative caller is not the exact bootstrap proposal",
    )
    require(
        remote_bytes(gh, WORKFLOW_PATH, proposal_sha).decode() == bootstrap(proposal),
        "Bootstrap workflow differs from the trusted template",
    )
    key = os.environ["GITHUB_RUN_ID"] + "." + os.environ["GITHUB_RUN_ATTEMPT"]
    attempt = run_attempt(gh, key)
    actor = attempt.get("actor", {})
    trigger = attempt.get("triggering_actor", actor)
    require(
        attempt.get("event") == "push"
        and attempt.get("head_sha") == proposal_sha
        and attempt.get("path") == WORKFLOW_PATH
        and actor.get("type") == "User"
        and trigger.get("type") == "User"
        and gh.permission(actor.get("login")) == "admin"
        and gh.permission(trigger.get("login")) == "admin",
        "An actual current repository administrator must own the setup push/rerun",
    )
    return proposal, {"run_key": key, "actor": actor["login"], "proposal_sha": proposal_sha}


def current(gh, proposal, *, merged=False):
    branch = proposal["project"]["default_branch"]
    require(gh.repo()["default_branch"] == branch, "Administrative default branch changed")
    if not merged:
        require(
            gh.ref(branch) == proposal["base"],
            "Default branch changed; prepare a new setup proposal",
        )
    old = None
    if proposal["previous_project"] is not None:
        old = config(gh.read_config(proposal["base"]))
        require(
            digest(old) == proposal["previous_project"],
            "Accepted administrative configuration changed",
        )
        verify_controls(gh, effective_config(old, proposal["pipeline"]), proposal["base"])
    cfg = effective_config(old or proposal["project"], proposal["pipeline"])
    from .approvals import pr_review_count

    require(
        pr_review_count(cfg)
        == pr_review_count(effective_config(proposal["project"], proposal["pipeline"])),
        "Changing native PR approval counts requires an explicit ruleset migration; administrative publication preserves the current count",
    )
    gh.strict_protection(branch, cfg)
    return cfg


def review_settings(cfg):
    if cfg.get("engine") and cfg.get("models", {}).get("review"):
        return deepcopy(cfg)
    from .invocations import definitions, execution_config

    reviewers = [item for item in definitions(cfg).values() if item["contract"] == "review"]
    require(
        len(reviewers) == 1,
        "Administrative review needs explicit models.review or one accepted review invocation in the selected pipeline",
    )
    return execution_config({"config": cfg, "invocation": {"definition": reviewers[0]}})


def preflight(gh, proposal):
    actor = gh.api("user")
    require(
        actor.get("type") == "User" and gh.permission(actor.get("login")) == "admin",
        "Administrative preflight requires the repository administrator's actual account",
    )
    cfg = current(gh, proposal)
    audit = gh.audit_settings(proposal["project"]["default_branch"], cfg)
    permission = gh.api(f"{gh.root}/actions/permissions/workflow")
    require(
        permission.get("can_approve_pull_request_reviews") is True,
        "Allow Actions to create pull requests before administrative publication",
    )
    try:
        secret = gh.api(f"{gh.root}/actions/secrets/{KEY_ENV}")
    except Blocked as exc:
        if "HTTP 404" not in str(exc):
            raise
        raise Blocked(
            f"Configure repository Actions secret {KEY_ENV} before staging administrative work"
        ) from None
    require(secret.get("name") == KEY_ENV, "Administrative Actions publication secret unavailable")
    review = review_settings(cfg)
    labels = agent_runner(review)
    if isinstance(labels, list):
        runners = gh.api(
            f"{gh.root}/actions/runners?per_page=100", pages=True, collection="runners"
        )
        require(
            any(
                item.get("status") == "online"
                and set(labels) <= {label["name"].lower() for label in item.get("labels", [])}
                for item in runners
            ),
            "No online runner matches the accepted administrator reviewer settings",
        )
    with publisher(gh, proposal) as publication:
        binding = deepcopy(publication.binding)
    return {
        "default_branch_and_controls_verified": True,
        "rules": audit,
        "actions_can_create_pr": True,
        "administrator_inspection_verified": True,
        "publication": {
            **binding,
            "actions_secret_configured": True,
            "actions_key_identity_verified": False,
            "effective_token_verified": True,
            "live_candidate_publication_verified": False,
        },
        "live_model_access_verified": False,
        "subscription_bootstrap_admission": "An exact temporary host admission is required when the reviewer uses a subscription runner.",
    }


def verify(gh, proposal, head):
    """Inspect metadata/content only; never execute the candidate's commands."""
    validate(proposal)
    commit = gh.api(f"{gh.root}/git/commits/{head}")
    require(
        [item["sha"] if isinstance(item, dict) else item for item in commit["parents"]]
        == [proposal["base"]],
        "Administrative candidate has another base",
    )

    def leaves(ref):
        tree = gh.api(f"{gh.root}/git/trees/{ref}?recursive=1")
        require(
            tree.get("truncated") is False, "Administrative tree cannot be inspected completely"
        )
        return {
            item["path"]: {key: item[key] for key in ("mode", "type", "sha")}
            for item in tree["tree"]
            if item["type"] != "tree"
        }

    expected = leaves(proposal["base"])
    for item in proposal["changes"]:
        if item["content"] is None:
            expected.pop(item["path"], None)
        else:
            content = item["content"].encode()
            checksum = hashlib.sha1(
                b"blob " + str(len(content)).encode() + b"\0" + content
            ).hexdigest()
            expected[item["path"]] = {"mode": "100644", "type": "blob", "sha": checksum}
    require(leaves(head) == expected, "Administrative candidate contains unapproved changes")
    cfg = effective_config(proposal["project"], proposal["pipeline"])
    verify_controls(gh, cfg, head)
    ledger = proposal["ledger"]
    for name, checksum in ledger["files"].items():
        actual = remote_bytes(gh, name, head)
        require(
            hashlib.sha256(actual).hexdigest() == checksum,
            "Administrative installed file differs: " + name,
        )
        if name.startswith(".agents/skills/"):
            trusted = kit_root() / "plugins/nexkit/skills" / name.removeprefix(".agents/skills/")
            require(
                trusted.is_file() and trusted.read_bytes() == actual,
                "Installed skill differs from the selected immutable kit",
            )
    return {
        "scope": "administrative setup; application commands were not executed",
        "candidate": head,
        "proposal": digest(proposal),
        "passed": True,
        "checks": [
            {"name": name, "passed": True}
            for name in (
                "exact candidate tree and base",
                "configuration contract",
                "accepted control hashes",
                "installation ledger ownership",
                "trusted installed skills",
            )
        ],
    }


def pull_subject(gh, state, proposal):
    pr = gh.pull(state["pr"])
    require(
        pr["head"]["sha"] == state["head"]
        and pr["head"]["ref"] == state["branch"]
        and pr["head"]["repo"]["full_name"] == gh.repository
        and pr["base"]["ref"] == proposal["project"]["default_branch"]
        and (pr.get("merged_at") or pr["base"]["sha"] == proposal["base"])
        and pr.get("user", {}).get("login") == "github-actions[bot]"
        and pr["user"].get("type") == "Bot",
        "Administrative PR author, candidate or base changed",
    )
    return pr


def prepare(gh, identifier, proposal_sha, kit_ref):
    proposal, authority = admitted(gh, identifier, proposal_sha, kit_ref)
    state, revision = gh.get_administration(identifier)
    if state.get("proposal"):
        require(
            state["proposal"] == proposal
            and state["authority"]["proposal_sha"] == proposal_sha
            and state["authority"]["actor"] == authority["actor"],
            "Administrative ownership changed",
        )
    if state.get("status") == "merged":
        return {"ready": False, "completed": True, "state": state}
    if state.get("pr"):
        pr = pull_subject(gh, state, proposal)
        if pr.get("merged_at"):
            verify(gh, proposal, state["head"])
            require(
                state.get("review")
                and state["review"]["candidate"]["head"] == state["head"]
                and administrator_review(gh, state),
                "Merged administrative candidate lacks recorded independent review and administrator approval",
            )
            verify_installation(gh, proposal, pr["merge_commit_sha"])
            state.update(status="merged", merge_sha=pr["merge_commit_sha"])
            gh.save_administration(identifier, state, revision)
            return {"ready": False, "completed": True, "state": state}
        require(pr["state"] == "open", "Administrative PR was closed; do not recreate it")
    cfg = current(gh, proposal)
    branch = "nexkit/administration-" + identifier[:24]
    # Credential inspection precedes any state/publication write or model
    # reservation. Only Git object/ref writes use the scoped App credential.
    with publisher(gh, proposal) as publication:
        require(
            not state.get("publication") or state["publication"] == publication.binding,
            "Administrative App installation or publication authority changed; prepare a new proposal",
        )
        state.update(
            proposal=proposal,
            authority=state.get("authority", authority),
            publication=deepcopy(publication.binding),
            branch=branch,
        )
        revision = gh.save_administration(identifier, state, revision)
        try:
            existing = gh.ref(branch)
        except Blocked as exc:
            if "HTTP 404" not in str(exc):
                raise
            existing = None
        if existing:
            require(
                not state.get("head") or state["head"] == existing,
                "Administrative branch candidate changed",
            )
            verify(gh, proposal, existing)
            state["head"] = existing
        elif not state.get("head"):
            base = gh.api(f"{gh.root}/git/commits/{proposal['base']}")
            tree = publication.api(
                f"{gh.root}/git/trees",
                "POST",
                {
                    "base_tree": base["tree"]["sha"],
                    "tree": [
                        {
                            "path": item["path"],
                            "mode": "100644",
                            "type": "blob",
                            **(
                                {"sha": None}
                                if item["content"] is None
                                else {"content": item["content"]}
                            ),
                        }
                        for item in proposal["changes"]
                    ],
                },
            )
            # Stable metadata makes an interrupted commit creation reproducible.
            # Identity grants no authority; GitHub authenticates the App token.
            author = {
                "name": proposal["publication"]["app_slug"] + "[bot]",
                "email": proposal["publication"]["app_slug"] + "[bot]@users.noreply.github.com",
                "date": base["committer"]["date"],
            }
            commit = publication.api(
                f"{gh.root}/git/commits",
                "POST",
                {
                    "message": "Apply accepted NexKit administrative setup " + identifier,
                    "tree": tree["sha"],
                    "parents": [proposal["base"]],
                    "author": author,
                    "committer": author,
                },
            )
            state["head"] = commit["sha"]
        revision = gh.save_administration(identifier, state, revision)
        if not existing:
            verify(gh, proposal, state["head"])
            publication.api(
                f"{gh.root}/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": state["head"]}
            )
        if not state.get("pr"):
            pr = gh.pull_for_branch(branch)
            if not pr:
                pr = gh.api(
                    f"{gh.root}/pulls",
                    "POST",
                    {
                        "head": branch,
                        "base": proposal["project"]["default_branch"],
                        "title": "Apply accepted NexKit setup",
                        "body": f"Administrative proposal `{identifier}` against `{proposal['base']}`.\n\nIndependent administrative review and an actual administrator PR approval are required. Existing work consumption is preserved. Changed inputs invalidate earlier work evidence; update affected runner admission before resuming pipelines.",
                    },
                )
            state.update(pr=pr["number"], status="reviewing")
            revision = gh.save_administration(identifier, state, revision)
    pr = pull_subject(gh, state, proposal)
    require(
        pr["state"] == "open" and not pr.get("merged_at"), "Administrative PR is no longer open"
    )
    verification = verify(gh, proposal, state["head"])
    gh.check("NexKit verification", state["head"], True, canonical(verification))
    cfg = review_settings(cfg)
    cfg["kit"] = deepcopy(proposal["project"]["kit"])
    cfg["environment"]["setup"] = []
    cfg["recovery"] = {"enabled": False}
    candidate = {
        "administration": identifier,
        "base": proposal["base"],
        "head": state["head"],
        "config": digest(cfg),
        "criteria": CRITERIA,
    }
    ready = not state.get("review")
    if ready:
        require(
            state.get("review_calls", 0) < proposal["review"]["calls"],
            "Administrative review budget exhausted; prepare a new explicitly approved proposal",
        )
        if state.get("review_run") == authority["run_key"]:
            raise Blocked("Duplicate administrative review reservation")
        if state.get("review_run"):
            require(
                run_attempt(gh, state["review_run"])["status"] == "completed",
                "Previous administrative review is still active",
            )
        state.update(review_calls=state.get("review_calls", 0) + 1, review_run=authority["run_key"])
        gh.check("NexKit review", state["head"], False, "Independent administrative review pending")
    context = {
        "administration": {"id": identifier, "proposal_sha": proposal_sha, "proposal": proposal},
        "repository": gh.repository,
        "issue": {
            "number": state["pr"],
            "title": "Administrative setup",
            "body": "Review the exact approved administrative proposal and its impact.",
        },
        "config": cfg,
        "base": proposal["base"],
        "source": state["head"],
        "run_key": authority["run_key"],
        "candidate": candidate,
        "verification": verification,
        "publication": deepcopy(state["publication"]),
        "agent_minutes": proposal["review"]["minutes"],
        "reused_review": not ready,
    }
    state.update(
        context=digest(context),
        verification=verification,
        status="reviewing" if ready else "waiting_for_administrator",
    )
    gh.save_administration(identifier, state, revision)
    return {
        "ready": ready,
        "completed": False,
        "context": context,
        "runner": agent_runner(cfg),
        "head": state["head"],
        "pr": state["pr"],
    }


def guard(gh, context):
    info = context["administration"]
    proposal, _ = admitted(gh, info["id"], info["proposal_sha"], context["config"]["kit"]["ref"])
    require(
        proposal == info["proposal"], "Administrative context differs from the approved proposal"
    )
    state, revision = gh.get_administration(info["id"])
    require(
        state.get("context") == digest(context)
        and state["head"] == context["source"]
        and state.get("publication") == context.get("publication"),
        "Administrative session was superseded",
    )
    current(gh, proposal)
    pr = pull_subject(gh, state, proposal)
    require(
        pr["state"] == "open" and not pr.get("merged_at"), "Administrative PR is no longer open"
    )
    require(
        verify(gh, proposal, state["head"]) == context["verification"],
        "Administrative verification changed",
    )
    return state, revision


def review_valid(context, report):
    require(
        isinstance(report, dict)
        and report.get("candidate") == context["candidate"]
        and report.get("unchanged") is True
        and report.get("independent") is True,
        "Administrative independent review evidence is missing or stale",
    )
    result = agent_result(report.get("result"), "review")
    coverage(context["candidate"], result)
    require(
        result["status"] == "done" and result["verdict"] == "approve",
        "Administrative reviewer did not approve",
    )


def administrator_review(gh, state):
    latest = {}
    for review in gh.api(f"{gh.root}/pulls/{state['pr']}/reviews?per_page=100", pages=True):
        if (
            review.get("submitted_at")
            and review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
            and human(review, gh.permission)
        ):
            login = review["user"]["login"]
            if review["id"] > latest.get(login, {}).get("id", 0):
                latest[login] = review
    require(
        not any(value["state"] == "CHANGES_REQUESTED" for value in latest.values()),
        "An authorized native review requests changes",
    )
    return [
        {"id": item["id"], "actor": item["user"]["login"], "commit": item["commit_id"]}
        for item in latest.values()
        if item["state"] == "APPROVED"
        and item.get("commit_id") == state["head"]
        and gh.permission(item["user"]["login"]) == "admin"
    ]


def finish(gh, context, report=None):
    state, revision = guard(gh, context)
    if report is not None:
        require(
            not context["reused_review"] and report.get("run_key") == context["run_key"],
            "Administrative review belongs to another reservation",
        )
        review_valid(context, report)
        state["review"] = report
        revision = gh.save_administration(context["administration"]["id"], state, revision)
    review_valid(context, state.get("review"))
    gh.check("NexKit review", state["head"], True, canonical(state["review"]))
    approvals = administrator_review(gh, state)
    if not approvals:
        state.update(status="waiting_for_administrator")
        gh.save_administration(context["administration"]["id"], state, revision)
        return {
            "merged": False,
            "pr": state["pr"],
            "reason": "An actual current repository administrator must approve this exact bot-authored PR; rerun the bootstrap after approval",
        }
    # Revalidate immediately before publication. Native protection also retains
    # its required review count and all GitHub approval restrictions.
    with publisher(gh, context["administration"]["proposal"]) as publication:
        require(
            publication.binding == state["publication"],
            "Administrative publication authority changed before merge",
        )
        guard(gh, context)
        require(
            administrator_review(gh, state) == approvals,
            "Administrative approval changed before merge",
        )
        result = publication.api(
            f"{gh.root}/pulls/{state['pr']}/merge",
            "PUT",
            {"sha": state["head"], "merge_method": "squash"},
        )
    require(
        result.get("merged") is True,
        "Native protection did not accept the administrative candidate",
    )
    verify_installation(gh, context["administration"]["proposal"], result["sha"])
    state.update(status="merged", merge_sha=result["sha"], administrator_reviews=approvals)
    gh.save_administration(context["administration"]["id"], state, revision)
    return {
        "merged": True,
        "pr": state["pr"],
        "sha": result["sha"],
        "runner_admission": "Apply the new accepted configuration to affected subscription runner admission before continuing work",
    }


def verify_installation(gh, proposal, ref):
    require(
        digest(gh.read_config(ref)) == digest(proposal["project"]),
        "Merged project differs from the approved installation",
    )
    require(
        read_value(remote_bytes(gh, ".nexkit/installation.json", ref).decode())
        == proposal["ledger"],
        "Merged installation ledger differs from its proposal",
    )
    verify_controls(gh, effective_config(proposal["project"], proposal["pipeline"]), ref)


def main():
    from .ci import output
    from .github import GitHub

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "guard", "finish"))
    parser.add_argument("--proposal")
    parser.add_argument("--proposal-sha")
    parser.add_argument("--kit-ref")
    parser.add_argument("--context")
    parser.add_argument("--report")
    parser.add_argument("--out")
    args = parser.parse_args()
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    if args.operation == "prepare":
        result = prepare(gh, args.proposal, args.proposal_sha, args.kit_ref)
        if "context" in result:
            write_json(args.out, result["context"])
        output(
            ready=str(result["ready"]).lower(),
            completed=str(result["completed"]).lower(),
            runner=canonical(result.get("runner", "ubuntu-24.04")),
            head=result.get("head", ""),
        )
    else:
        context = read_json(args.context)
        result = (
            guard(gh, context)[0]
            if args.operation == "guard"
            else finish(
                gh,
                context,
                read_json(args.report) if args.report and Path(args.report).exists() else None,
            )
        )
    print(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (Blocked, OSError, ValueError, KeyError) as exc:
        raise SystemExit("Administrative setup blocked: " + str(exc)) from exc

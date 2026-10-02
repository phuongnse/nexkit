"""Close explicitly completed issues without repeating delivery or publication."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime

from .common import Blocked, canonical, digest
from .policy import now, require, spec_hash

HEADING = "## Issues to close"
DEFAULTS = {"close_after_merge": True, "close_after_release": True}
PR_START = "<!-- nexkit:delivery-summary -->"
PR_END = "<!-- nexkit:delivery-summary:end -->"


def settings(cfg):
    return {**DEFAULTS, **cfg.get("issue_completion", {})}


def selected_issues(issue):
    """Only a dedicated section grants scope; mentions and code examples do not."""
    sections, selected, fence, comment = [], None, None, False
    body = (issue.get("body") or "").split("\n<!-- nexkit:spec -->\n", 1)[-1]
    for line in body.splitlines():
        stripped = line.strip()
        if line.startswith(("    ", "\t")):
            if selected is not None and stripped:
                selected.append(line)
            continue
        if not fence and (comment or stripped.startswith("<!--")):
            comment = "-->" not in stripped
            continue
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            if selected is not None:
                selected.append(line)
            continue
        if fence:
            if selected is not None:
                selected.append(line)
            continue
        if stripped == HEADING:
            selected = []
            sections.append(selected)
        elif re.match(r"^#{1,2}\s", stripped):
            selected = None
        elif selected is not None and stripped:
            selected.append(stripped)
    if not sections:
        return [issue["number"]]
    require(len(sections) == 1, "Use only one 'Issues to close' section")
    lines = sections[0]
    if lines == ["None."]:
        return []
    require(lines and len(lines) <= 20, "List 1–20 issues to close, or write None.")
    numbers = []
    for line in lines:
        match = re.fullmatch(r"- #([1-9][0-9]*)", line)
        require(match is not None, "Issues to close must use one '- #123' line per issue")
        numbers.append(int(match[1]))
    require(len(set(numbers)) == len(numbers), "Duplicate issue in completion scope")
    return numbers


def snapshot(issue):
    require(not issue.get("pull_request"), "Completion targets must be issues, not PRs")
    return {
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "spec": spec_hash(issue),
        "edited_at": issue.get("last_edited_at"),
    }


def unchanged(issue, saved):
    require(
        not issue.get("pull_request")
        and issue["number"] == saved["number"]
        and spec_hash(issue) == saved["spec"]
        and issue.get("last_edited_at") == saved["edited_at"],
        f"Issue #{saved['number']} changed; leave it open for review",
    )


def delivery_plan(gh, issue, approved, cfg):
    targets = []
    for number in selected_issues(issue):
        target = issue if number == issue["number"] else gh.issue(number)
        if number != issue["number"]:
            require(
                not (target.get("body") or "").startswith("<!-- nexkit:release -->"),
                "A delivery cannot close a release candidate",
            )
            other, _ = gh.get_state(number)
            require(not other, f"Issue #{number} has its own NexKit work; complete it separately")
            stamps = [target.get("created_at"), target.get("last_edited_at")]
            require(
                target.get("created_at")
                and all(
                    datetime.fromisoformat(stamp)
                    < datetime.fromisoformat(approved["comment_updated_at"])
                    for stamp in stamps
                    if stamp
                ),
                f"Issue #{number} was created or edited after approval; approve the current scope",
            )
        targets.append(snapshot(target))
    plan = {
        "kind": "delivery",
        "repository": gh.repository,
        "enabled": settings(cfg)["close_after_merge"],
        "work": snapshot(issue),
        "targets": targets,
    }
    require(len(canonical(plan).encode()) <= 200000, "Issue completion scope exceeds 200 KB")
    return plan


def release_plan(gh, issue, cfg):
    return {
        "kind": "release",
        "repository": gh.repository,
        "enabled": settings(cfg)["close_after_release"],
        "work": snapshot(issue),
        "targets": [snapshot(issue)],
    }


def revalidate(gh, context, state):
    plan = context.get("completion_plan")
    if plan is None:
        require(not state.get("completion"), "Delivery is missing its recorded completion scope")
        return
    require(state.get("completion", {}).get("plan") == plan, "Issue completion scope changed")
    for saved in plan["targets"]:
        unchanged(gh.issue(saved["number"]), saved)
        if saved["number"] != context["issue"]["number"]:
            other, _ = gh.get_state(saved["number"])
            require(not other, "A completion target now has its own NexKit work")


def pull_body(context, previous=None):
    # Explicit controller closure also respects changed/reopened issues and opt-outs.
    # Native closing keywords would act before these post-merge checks can run.
    body = (
        f"Refs #{context['issue']['number']}\n\nRequirement `{context['approval']['spec']}`.\n"
        "NexKit will merge only after current independent review and verification."
    )
    plan = context.get("completion_plan")
    if plan:
        body += "\n\n## Issue completion\n\n"
        if plan["enabled"] and plan["targets"]:
            body += "After merge, NexKit will check and close these completed issues:\n\n"
            body += "\n".join(f"- #{item['number']}" for item in plan["targets"])
        else:
            body += "Automatic issue closure is disabled for this work."
    block = PR_START + "\n" + body + "\n" + PR_END
    if not previous:
        return block
    if PR_START in previous or PR_END in previous:
        require(
            previous.count(PR_START) == previous.count(PR_END) == 1,
            "PR contains an incomplete or duplicate NexKit summary",
        )
        prefix, rest = previous.split(PR_START, 1)
        require(PR_END in rest, "PR summary markers are out of order")
        _, suffix = rest.split(PR_END, 1)
        return prefix + block + suffix
    return block + "\n\n" + previous.strip()


def review_receipt(context, review):
    plan = context.get("completion_plan", {})
    acceptance = review["result"]["acceptance"]
    for target in plan.get("targets", []):
        if target["number"] == context["issue"]["number"]:
            continue
        criterion = f"Issue #{target['number']} completion"
        require(
            any(item["criterion"] == criterion and item["passed"] for item in acceptance),
            f"Independent review must include evidence for '{criterion}'",
        )
    return {"candidate": context["candidate"], "acceptance": acceptance}


def evidence(gh, state):
    branch = gh.repo()["default_branch"]
    base = gh.ref(branch)
    if state.get("status") == "merged":
        pr = gh.pull(state["pr"])
        require(
            pr.get("merged_at")
            and pr["head"]["sha"] == state["candidate"]["head"]
            and pr["base"]["ref"] == branch
            and pr["base"]["repo"]["full_name"] == gh.repository
            and pr["head"]["repo"]["full_name"] == gh.repository
            and pr["merge_commit_sha"] == state["merge_sha"],
            "The recorded candidate is not the merged PR on the default branch",
        )
        source = pr["merge_commit_sha"]
        result = {
            "kind": "delivery",
            "at": pr["merged_at"],
            "url": f"https://github.com/{gh.repository}/pull/{pr['number']}",
            "summary": f"Merged PR #{pr['number']}",
        }
    else:
        require(state.get("status") == "released", "Work has not merged or been released")
        value = state["release"]
        release = gh.api(f"{gh.root}/releases/{state['release_id']}")
        require(
            value["repository"] == gh.repository
            and release.get("draft") is False
            and release.get("published_at")
            and release["tag_name"] == state["tag"]
            and (release.get("body") or "").startswith(
                f"<!-- nexkit-candidate:{digest(value)} -->\n"
            ),
            "The approved release is not published",
        )
        ref = gh.api(f"{gh.root}/git/ref/tags/{state['tag']}")["object"]
        require(
            ref["type"] == "commit" and ref["sha"] == value["commit"],
            "Published release tag differs from the approved source",
        )
        assets = gh.api(f"{gh.root}/releases/{release['id']}/assets?per_page=100", pages=True)
        expected = {
            a["name"]: (a["size"], "sha256:" + a["sha256"]) for a in state["manifest"]["artifacts"]
        }
        require(
            len(assets) == len(expected)
            and {a["name"]: (a["size"], a.get("digest")) for a in assets} == expected,
            "Published release assets differ from the verified build",
        )
        source = value["commit"]
        result = {
            "kind": "release",
            "at": release["published_at"],
            "url": release["html_url"],
            "summary": f"Published release {value['version']}",
        }
    require(
        gh.api(f"{gh.root}/compare/{source}...{base}")["status"] in ("ahead", "identical"),
        "Completed source is no longer on the default branch",
    )
    return result


def reconcile(gh, number, *, apply=True):
    """Retry only completion; keep successful work successful on a GitHub failure."""
    state, revision = gh.get_state(number)
    require(state.get("status") in {"merged", "released"}, "Work is not complete")
    before = deepcopy(state)
    record = state.get("completion")
    require(isinstance(record, dict), "Completed work is missing its recorded completion scope")
    if record.get("status") in {"complete", "disabled"}:
        return state

    def save():
        nonlocal revision, before
        if apply and state != before:
            revision = gh.save_state(number, state, revision)
            before = deepcopy(state)

    try:
        plan = record["plan"]
        require(plan["repository"] == gh.repository, "Completion repository changed")
        require(plan["work"]["number"] == number, "Completion work item changed")
        if plan["kind"] == "delivery" and any(item["number"] != number for item in plan["targets"]):
            receipt = record.get("review", {})
            require(receipt.get("candidate") == state["candidate"], "Completion review is missing")
            review_receipt(
                {
                    "completion_plan": plan,
                    "issue": {"number": number},
                    "candidate": state["candidate"],
                },
                {"result": receipt},
            )
        result = evidence(gh, state)
        require(result["kind"] == plan["kind"], "Completion kind changed")
        unchanged(gh.issue(number), plan["work"])
        record["evidence"] = result
        record["status"] = "pending"
        record.pop("error", None)
        save()
        for target in plan["targets"] if plan["enabled"] else []:
            key = str(target["number"])
            if key in record["issues"]:
                continue
            issue = gh.issue(target["number"])
            outcome = {"status": "would_close"}
            if issue["state"] == "closed":
                outcome = {"status": "already_closed"}
            else:
                try:
                    unchanged(issue, target)
                except Blocked as exc:
                    outcome = {"status": "kept_open", "reason": str(exc)}
                events = gh.api(
                    f"{gh.root}/issues/{target['number']}/timeline?per_page=100", pages=True
                )
                if any(
                    event.get("event") == "reopened"
                    and datetime.fromisoformat(event["created_at"])
                    >= datetime.fromisoformat(result["at"])
                    for event in events
                ):
                    outcome = {"status": "kept_open", "reason": "Reopened after completion"}
                if target["number"] != number:
                    other, _ = gh.get_state(target["number"])
                    if other:
                        outcome = {"status": "kept_open", "reason": "Has its own NexKit work"}
                if apply and outcome["status"] == "would_close":
                    # Re-read immediately before the write. GitHub does not offer an
                    # atomic issue-body-and-state comparison through this endpoint.
                    unchanged(gh.issue(number), plan["work"])
                    latest = gh.issue(target["number"])
                    unchanged(latest, target)
                    if latest["state"] == "closed":
                        outcome = {"status": "already_closed"}
                    else:
                        gh.api(
                            f"{gh.root}/issues/{target['number']}",
                            "PATCH",
                            {"state": "closed", "state_reason": "completed"},
                        )
                        closed = gh.issue(target["number"])
                        require(
                            closed["state"] == "closed"
                            and closed.get("state_reason") == "completed",
                            f"GitHub did not confirm completion of issue #{target['number']}",
                        )
                        outcome = {"status": "closed", "at": now()}
            record["issues"][key] = outcome
            save()
        if apply:
            marker = f"<!-- nexkit:completion:{digest(plan)} -->"
            body = marker + f"\n## Work completed\n\n[{result['summary']}]({result['url']}).\n\n"
            if not plan["enabled"]:
                body += "Automatic issue closure is disabled by this pipeline's configuration.\n"
            elif record["issues"]:
                for key, item in record["issues"].items():
                    label = item["status"].replace("_", " ").capitalize()
                    body += f"- #{key}: {label}."
                    if item.get("reason"):
                        body += " " + item["reason"] + "."
                    body += "\n"
            else:
                body += "No issues were selected for automatic closure.\n"
            if result["kind"] == "delivery":
                body += "\nThis merge does not publish a release.\n"
            if not any(c.get("body") == body for c in gh.comments(number)):
                gh.comment(number, body)
            record.update(status="complete" if plan["enabled"] else "disabled", completed_at=now())
            save()
    except Blocked as exc:
        record.update(status="pending", error=str(exc)[:2000])
        save()
        if apply:
            body = (
                "## Issue completion needs attention\n\n"
                f"The work is recorded as **{state['status']}**. "
                f"Issue completion could not finish: {record['error']}\n\n"
                f"Inspect `nexkit status {number}`. After resolving the cause, "
                f"preview `nexkit complete {number}` and use `--apply` to retry closure. "
                "This does not repeat implementation or publication."
            )
            try:
                if not any(c.get("body") == body for c in gh.comments(number)):
                    gh.comment(number, body)
            except Blocked:
                # The original failure may be missing issue-write permission.
                # Keep the saved pending result available through status.
                pass
    return state

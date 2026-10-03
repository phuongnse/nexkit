"""Live publication checks in the toolkit repository, with zero model calls."""

import argparse
import base64
import os
import re
from urllib.parse import quote

from nexkit.administrative_publisher import publisher, validate_publication
from nexkit.common import Blocked, canonical, write_json
from nexkit.github import GitHub, run_attempt
from nexkit.policy import require

WORKFLOW = (
    "name: Administrative publication fixture\n"
    "on: workflow_dispatch\n"
    "permissions:\n  contents: read\n"
    "jobs:\n  fixture:\n    runs-on: ubuntu-24.04\n"
    "    steps:\n      - run: echo 'Publication fixture; no application acceptance claimed.'\n"
)


def missing(gh, branch):
    try:
        gh.ref(branch)
    except Blocked as exc:
        if "HTTP 404" in str(exc):
            return True
        raise
    return False


def cleanup(gh, writer, owned, body):
    """Only close/delete exact resources created by this run attempt."""
    if not owned:
        return
    base, head = owned
    for branch, sha in owned.items():
        if not missing(gh, branch):
            require(gh.ref(branch) == sha, "Probe branch changed; preserve it for inspection")
    pr = gh.pull_for_branch(head)
    if pr:
        require(
            pr["head"]["sha"] == owned[head]
            and pr["head"]["ref"] == head
            and pr["base"]["ref"] == base
            and pr.get("body") == body
            and pr.get("user", {}).get("login") == "github-actions[bot]"
            and not pr.get("merged_at"),
            "Probe PR changed; preserve it for inspection",
        )
        if pr["state"] == "open":
            require(pr.get("draft"), "Probe PR was promoted; preserve it for inspection")
            gh.api(f"{gh.root}/pulls/{pr['number']}", "PATCH", {"state": "closed"})
            require(gh.pull(pr["number"])["state"] == "closed", "Probe PR did not close")
    for branch in reversed(owned):
        if not missing(gh, branch):
            writer.api(f"{gh.root}/git/refs/heads/{quote(branch, safe='/')}", "DELETE")
        require(missing(gh, branch), "Probe branch cleanup failed")


def verify(gh, publication, environment, *, record=None):
    result = {
        "repository": gh.repository,
        "source_commit": environment.get("GITHUB_SHA"),
        "run_key": environment.get("GITHUB_RUN_ID", "")
        + "."
        + environment.get("GITHUB_RUN_ATTEMPT", ""),
        "passed": False,
        "live_github_publication": False,
        "cleaned": False,
        "token_revoked": False,
        "model_calls": 0,
        "consumer_delivery_verified": False,
        "native_merge_or_human_approval_verified": False,
    }
    owned = {}
    if record:
        record(result)
    try:
        validate_publication(publication)
        require(
            environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and re.fullmatch(r"[0-9a-f]{40}", result["source_commit"] or ""),
            "Run this probe through the toolkit's manually dispatched CI",
        )
        run = run_attempt(gh, result["run_key"])
        require(
            run["head_sha"] == result["source_commit"]
            and run["event"] == "workflow_dispatch"
            and run["path"].split("@", 1)[0] == ".github/workflows/ci.yml"
            and gh.permission(run.get("actor", {}).get("login")) == "admin"
            and gh.permission(run.get("triggering_actor", {}).get("login")) == "admin",
            "Probe must belong to this exact CI revision and an actual administrator",
        )
        repo = gh.repo()
        default_branch = repo["default_branch"]
        default_head = gh.ref(default_branch)
        prefix = "nexkit/publication-probe/" + result["run_key"]
        base, head = prefix + "/base", prefix + "/head"
        require(all(missing(gh, branch) for branch in (base, head)), "Probe branches already exist")
        result["branches"] = [base, head]
        if record:
            record(result)
        commit = gh.api(f"{gh.root}/git/commits/{result['source_commit']}")
        path = ".github/workflows/nexkit-publication-probe.yml"
        source_tree = gh.api(f"{gh.root}/git/trees/{commit['tree']['sha']}?recursive=1")
        require(
            not source_tree.get("truncated")
            and not any(item["path"] == path for item in source_tree["tree"]),
            "Probe fixture path already exists in the selected source",
        )
        ordinary = {
            "base_tree": commit["tree"]["sha"],
            "tree": [
                {
                    "path": prefix + ".txt",
                    "mode": "100644",
                    "type": "blob",
                    "content": result["run_key"],
                }
            ],
        }
        workflow = {
            "base_tree": commit["tree"]["sha"],
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": WORKFLOW}],
        }
        body = (
            "NexKit publication probe "
            + result["run_key"]
            + " at "
            + result["source_commit"]
            + ".\nNo model, application checks, approval or merge is requested."
        )
        with publisher(gh, {"publication": publication}) as writer:
            result["publication"] = dict(writer.binding)
            if record:
                record(result)
            try:
                gh.api(f"{gh.root}/git/trees", "POST", ordinary)
                result["workflow_token_ordinary_write"] = True
                try:
                    gh.api(f"{gh.root}/git/trees", "POST", workflow)
                except Blocked as exc:
                    require("HTTP 403" in str(exc), "Expected native workflow-token HTTP 403")
                    result["workflow_token_workflow_denied"] = True
                else:
                    raise Blocked(
                        "Workflow-token permission boundary changed; inspect before release"
                    )
                tree = writer.api(f"{gh.root}/git/trees", "POST", workflow)
                candidate = writer.api(
                    f"{gh.root}/git/commits",
                    "POST",
                    {"message": body, "tree": tree["sha"], "parents": [result["source_commit"]]},
                )
                result["candidate_head"] = candidate["sha"]
                if record:
                    record(result)
                owned.update({base: result["source_commit"], head: candidate["sha"]})
                for branch, sha in owned.items():
                    writer.api(
                        f"{gh.root}/git/refs", "POST", {"ref": "refs/heads/" + branch, "sha": sha}
                    )
                    require(gh.ref(branch) == sha, "Published probe ref does not match its source")
                content = gh.content(path, candidate["sha"])
                require(
                    base64.b64decode(content["content"]).decode() == WORKFLOW,
                    "Published workflow differs",
                )
                pr = gh.api(
                    f"{gh.root}/pulls",
                    "POST",
                    {
                        "title": "NexKit administrative publication probe",
                        "body": body,
                        "head": head,
                        "base": base,
                        "draft": True,
                    },
                )
                result["pr"] = pr["html_url"]
                if record:
                    record(result)
                pr = gh.pull(pr["number"])
                require(
                    pr["user"]["login"] == "github-actions[bot]"
                    and pr["head"]["sha"] == candidate["sha"],
                    "Probe PR does not have the expected Actions author and head",
                )
                check = gh.check(
                    "NexKit publication probe",
                    candidate["sha"],
                    True,
                    "Live App workflow write, exact refs/content and Actions PR identity verified. No application/review/merge acceptance is claimed.",
                )
                require(
                    check["head_sha"] == candidate["sha"]
                    and check["app"]["id"] == gh.api("apps/github-actions")["id"]
                    and check["conclusion"] == "success",
                    "Probe check is not bound to the exact head and Actions App",
                )
                result["check"] = check["html_url"]
                result["live_github_publication"] = True
            finally:
                cleanup(gh, writer, owned, body)
                result["cleaned"] = True
        try:
            writer.api("installation/repositories")
        except Blocked as exc:
            require("HTTP 401" in str(exc), "Cannot prove native App token revocation")
            result["token_revoked"] = True
        else:
            raise Blocked("Administrative publication token was not revoked")
        require(gh.ref(default_branch) == default_head, "Default branch changed during the probe")
        result["default_branch_preserved"] = True
        result["passed"] = True
    except Blocked as exc:
        result["reason"] = str(exc)
    if record:
        record(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        publication = {
            "app_id": int(os.environ.get("NEXKIT_ADMIN_APP_ID", "")),
            "app_slug": os.environ.get("NEXKIT_ADMIN_APP_SLUG", ""),
            "installation_id": int(os.environ.get("NEXKIT_ADMIN_APP_INSTALLATION_ID", "")),
        }
    except ValueError:
        result = {
            "passed": False,
            "reason": "Configure the App ID, slug and installation ID repository variables",
            "model_calls": 0,
        }
    else:
        result = verify(
            GitHub(os.environ["GITHUB_REPOSITORY"]),
            publication,
            os.environ,
            record=lambda report: write_json(args.out, report),
        )
    write_json(args.out, result)
    print(canonical(result))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

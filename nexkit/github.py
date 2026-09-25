"""GitHub operations via its official CLI; no token storage or custom auth loop."""

from __future__ import annotations

import base64
import json
from urllib.parse import quote

from .common import Blocked, canonical, run
from .policy import REPO, STATE_BRANCH, require


class GitHub:
    def __init__(self, repository):
        require(REPO.fullmatch(repository), "Invalid repository")
        self.repository = repository
        self.root = f"repos/{repository}"

    def api(self, path, method="GET", data=None, *, pages=False):
        args = [
            "gh",
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            path,
        ]
        if data is not None:
            args += ["--input", "-"]
        if pages:
            args += ["--paginate", "--slurp"]
        result = run(args, data=canonical(data) if data is not None else None)
        try:
            value = json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise Blocked("GitHub returned invalid JSON") from exc
        if pages:
            return [item for page in value for item in page]
        return value

    def repo(self):
        return self.api(self.root)

    def issue(self, number):
        value = self.api(f"{self.root}/issues/{int(number)}")
        owner, name = self.repository.split("/")
        query = (
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
            "{issue(number:$number){lastEditedAt timelineItems(last:1,itemTypes:[RENAMED_TITLE_EVENT])"
            "{nodes{... on RenamedTitleEvent{createdAt}}}}}}"
        )
        edit = self.api(
            "graphql",
            "POST",
            {"query": query, "variables": {"owner": owner, "name": name, "number": int(number)}},
        )
        require(not edit.get("errors"), "Cannot verify requirement edit history")
        detail = edit["data"]["repository"]["issue"]
        stamps = [
            detail["lastEditedAt"],
            *[x.get("createdAt") for x in detail["timelineItems"]["nodes"]],
        ]
        value["last_edited_at"] = max((s for s in stamps if s), default=None)
        return value

    def comments(self, number):
        return self.api(f"{self.root}/issues/{int(number)}/comments?per_page=100", pages=True)

    def permission(self, login):
        if not login:
            return "none"
        return self.api(f"{self.root}/collaborators/{quote(login, safe='')}/permission")[
            "permission"
        ]

    def comment(self, number, body):
        return self.api(f"{self.root}/issues/{int(number)}/comments", "POST", {"body": body})

    def content(self, path, ref):
        return self.api(f"{self.root}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")

    def read_config(self, ref):
        value = self.content(".nexkit/project.json", ref)
        return json.loads(base64.b64decode(value["content"]))

    def ref(self, branch):
        return self.api(f"{self.root}/git/ref/heads/{quote(branch, safe='/')}")["object"]["sha"]

    def get_state(self, number):
        # A missing state is different from insufficient permissions/network failure.
        path = f"{self.root}/contents/issues/{int(number)}.json?ref={quote(STATE_BRANCH, safe='')}"
        try:
            value = self.api(path)
        except Blocked as exc:
            if "HTTP 404" in str(exc):
                return {}, None
            raise
        return json.loads(base64.b64decode(value["content"])), value["sha"]

    def ensure_state_branch(self):
        try:
            self.ref(STATE_BRANCH)
            return
        except Blocked as exc:
            if "HTTP 404" not in str(exc):
                raise
        tree = self.api(
            f"{self.root}/git/trees",
            "POST",
            {
                "tree": [
                    {
                        "path": "README.md",
                        "mode": "100644",
                        "type": "blob",
                        "content": "NexKit bounded delivery state. No application source or secrets.\n",
                    }
                ]
            },
        )
        commit = self.api(
            f"{self.root}/git/commits",
            "POST",
            {"message": "Initialize NexKit state", "tree": tree["sha"], "parents": []},
        )
        try:
            self.api(
                f"{self.root}/git/refs",
                "POST",
                {"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"]},
            )
        except Blocked as exc:
            if "HTTP 422" not in str(exc):
                raise
            self.ref(STATE_BRANCH)

    def save_state(self, number, value, previous):
        self.ensure_state_branch()
        data = {
            "message": f"NexKit #{int(number)}: {value.get('status', 'updated')}",
            "content": base64.b64encode((canonical(value) + "\n").encode()).decode(),
            "branch": STATE_BRANCH,
        }
        if previous:
            data["sha"] = previous
        result = self.api(f"{self.root}/contents/issues/{int(number)}.json", "PUT", data)
        return result["content"]["sha"]

    def dispatch(self, workflow, branch, inputs):
        self.api(
            f"{self.root}/actions/workflows/{quote(workflow, safe='')}/dispatches",
            "POST",
            {"ref": branch, "inputs": {k: str(v) for k, v in inputs.items()}},
        )

    def pull(self, number):
        return self.api(f"{self.root}/pulls/{int(number)}")

    def pull_for_branch(self, branch):
        owner = self.repository.split("/")[0]
        result = self.api(
            f"{self.root}/pulls?state=all&head={quote(owner + ':' + branch, safe='')}"
        )
        require(len(result) <= 1, "Multiple pull requests found for the delivery branch")
        return result[0] if result else None

    def check(self, name, sha, passed, summary):
        return self.api(
            f"{self.root}/check-runs",
            "POST",
            {
                "name": name,
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success" if passed else "failure",
                "output": {"title": name, "summary": summary[:60000]},
            },
        )

    def strict_protection(self, branch):
        # This endpoint needs Metadata:read. The classic branch-protection
        # endpoint needs Administration:read, which GITHUB_TOKEN cannot have.
        # NexKit uses an active native ruleset, audited for bypasses at setup.
        rules = self.api(
            f"{self.root}/rules/branches/{quote(branch, safe='')}?per_page=100", pages=True
        )
        names, strict = set(), False
        actions_id = self.api("apps/github-actions")["id"]
        for rule in rules:
            params = rule.get("parameters", {})
            if rule.get("type") == "required_status_checks":
                strict = strict or params.get("strict_required_status_checks_policy") is True
                for check in params.get("required_status_checks", []):
                    names.add(check["context"])
                    require(
                        check.get("integration_id") == actions_id,
                        "Required NexKit checks must be bound to the GitHub Actions App",
                    )
            if rule.get("type") == "pull_request":
                require(
                    not params.get("required_approving_review_count")
                    and not params.get("require_code_owner_review")
                    and not params.get("require_last_push_approval"),
                    "A ruleset adds mandatory human PR approval",
                )
            require(
                rule.get("type")
                not in ("required_deployments", "merge_queue", "required_signatures", "workflows"),
                "Repository rules need an explicit compatible setup integration",
            )
        require(
            strict and names == {"NexKit verification", "NexKit review"},
            "An active strict ruleset requiring exactly NexKit verification/review is needed; migrate other checks into declared commands during setup",
        )
        return rules

    def audit_settings(self, branch):
        """Administrator-only setup inspection. Never called in delivery jobs."""
        self.strict_protection(branch)
        entries = self.api(f"{self.root}/rulesets?includes_parents=true&per_page=100", pages=True)
        for entry in entries:
            if entry.get("enforcement") != "active":
                continue
            url = entry.get("_links", {}).get("self", {}).get("href")
            require(
                url and url.startswith("https://api.github.com/"),
                "Cannot inspect inherited ruleset",
            )
            rule = self.api(url)
            require("bypass_actors" in rule, "Setup account cannot audit ruleset bypass actors")
            require(
                not rule["bypass_actors"],
                "Remove blanket bypasses only through an approved administrative setup decision",
            )
        try:
            classic = self.api(f"{self.root}/branches/{quote(branch, safe='')}/protection")
        except Blocked as exc:
            if "HTTP 404" not in str(exc):
                raise
            classic = {}
        reviews = classic.get("required_pull_request_reviews") or {}
        require(
            not reviews.get("required_approving_review_count")
            and not reviews.get("require_code_owner_reviews"),
            "Classic branch protection adds a human review gate",
        )
        extra = (classic.get("required_status_checks") or {}).get("contexts", [])
        require(
            set(extra) <= {"NexKit verification", "NexKit review"},
            "Classic protection has additional required checks without an automatic dispatch integration",
        )
        return {"ruleset_bypasses_audited": True, "classic_protection_audited": True}

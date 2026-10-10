"""Minimal GitHub REST client for the calls NexKit makes."""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

WRITE_ROLES = {"admin", "maintain", "write"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, repository=None, token=None, api_url=None):
        self.repository = repository or os.environ["GITHUB_REPOSITORY"]
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.api_url = api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        self.api_url = self.api_url.rstrip("/")
        self._roles = {}

    # -- transport -----------------------------------------------------------------

    def request(self, method, path, body=None, params=None):
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nexkit",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            try:
                with urlopen(Request(url, data, headers, method=method), timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code >= 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GitHubError(exc.code, detail) from None
            except URLError as exc:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise GitHubError(0, str(exc.reason)) from None
        raise AssertionError("unreachable")

    def paginate(self, path, params=None, limit=1000):
        items, page = [], 1
        while len(items) < limit:
            batch = self.request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    @property
    def _repo(self):
        return f"/repos/{self.repository}"

    # -- repository and people -----------------------------------------------------

    def default_branch(self):
        return self.request("GET", self._repo)["default_branch"]

    def role(self, user):
        """Repository role of a user: admin, maintain, write, triage, read or none."""
        if user not in self._roles:
            try:
                data = self.request("GET", f"{self._repo}/collaborators/{quote(user)}/permission")
                self._roles[user] = data.get("role_name") or data.get("permission") or "none"
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                self._roles[user] = "none"
        return self._roles[user]

    def can_write(self, user):
        return self.role(user) in WRITE_ROLES

    # -- issues and comments -------------------------------------------------------

    def issue(self, number):
        return self.request("GET", f"{self._repo}/issues/{number}")

    def comments(self, number):
        return self.paginate(f"{self._repo}/issues/{number}/comments")

    def parent_issue(self, number):
        """The parent of a sub-issue, or None for an issue without a parent."""
        try:
            return self.request("GET", f"{self._repo}/issues/{number}/parent")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            return None

    def sub_issues(self, number):
        return self.paginate(f"{self._repo}/issues/{number}/sub_issues")

    def close_issue(self, number, reason="completed"):
        return self.request(
            "PATCH", f"{self._repo}/issues/{number}", {"state": "closed", "state_reason": reason}
        )

    def recent_comments(self, since, limit=2000):
        """Comments on any issue or pull request, edited or posted since `since` (ISO 8601),
        newest first."""
        params = {"since": since, "sort": "updated", "direction": "desc"}
        return self.paginate(f"{self._repo}/issues/comments", params, limit)

    def comment(self, number, body):
        return self.request("POST", f"{self._repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, comment_id, body):
        return self.request("PATCH", f"{self._repo}/issues/comments/{comment_id}", {"body": body})

    def react(self, comment_id, content="eyes"):
        try:
            self.request(
                "POST", f"{self._repo}/issues/comments/{comment_id}/reactions", {"content": content}
            )
        except GitHubError as exc:
            # A missing reaction is cosmetic, but say why so permission problems show up.
            print(f"warning: could not add the {content} reaction: {exc}", file=sys.stderr)

    # -- pull requests -------------------------------------------------------------

    def pull(self, number):
        return self.request("GET", f"{self._repo}/pulls/{number}")

    def open_pulls(self, branch):
        owner = self.repository.split("/")[0]
        return self.request(
            "GET", f"{self._repo}/pulls", params={"state": "open", "head": f"{owner}:{branch}"}
        )

    def pulls_into(self, base):
        """Open pull requests whose base branch is `base`."""
        return self.paginate(f"{self._repo}/pulls", {"state": "open", "base": base})

    def branch_sha(self, branch):
        return self.request("GET", f"{self._repo}/commits/{quote(branch)}")["sha"]

    def create_pull(self, title, head, base, body):
        return self.request(
            "POST",
            f"{self._repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def reviews(self, number):
        return self.paginate(f"{self._repo}/pulls/{number}/reviews")

    def review_comments(self, number):
        return self.paginate(f"{self._repo}/pulls/{number}/comments")

    def create_review(self, number, commit_id, body):
        return self.request(
            "POST",
            f"{self._repo}/pulls/{number}/reviews",
            {"commit_id": commit_id, "body": body, "event": "COMMENT"},
        )

    def merge(self, number, sha, method="squash"):
        return self.request(
            "PUT", f"{self._repo}/pulls/{number}/merge", {"sha": sha, "merge_method": method}
        )

    # -- checks and workflows ------------------------------------------------------

    def set_status(self, sha, context, state, description, target_url=None):
        body = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        return self.request("POST", f"{self._repo}/statuses/{sha}", body)

    def dispatch(self, workflow, ref, inputs, run_details=False):
        """Start a workflow. With run_details, returns the run's `html_url` among others."""
        body = {"ref": ref, "inputs": inputs}
        if run_details:
            body["return_run_details"] = True
        return self.request(
            "POST", f"{self._repo}/actions/workflows/{quote(workflow)}/dispatches", body
        )


def _actions_url():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions"


def run_url():
    run_id = os.environ.get("GITHUB_RUN_ID")
    return f"{_actions_url()}/runs/{run_id}" if run_id else None


def dispatched_runs_url(workflow, branch):
    """The page listing a workflow's runs that were dispatched on a branch, for a dispatch
    that returned no run (GitHub Enterprise Server versions without run details)."""
    query = urlencode({"query": f"branch:{branch} event:workflow_dispatch"})
    return f"{_actions_url()}/workflows/{quote(workflow)}?{query}"

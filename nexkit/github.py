"""Minimal GitHub REST client for the calls NexKit makes."""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

WRITE_ROLES = {"admin", "maintain", "write"}
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


# The most a download from GitHub reads: a job's log or an artifact's zip.
DOWNLOAD_LIMIT = 64_000_000


class _NoRedirect(HTTPRedirectHandler):
    """Leave redirects to the caller, so the token never goes to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


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

    def download(self, path, limit=DOWNLOAD_LIMIT):
        """The bytes behind an API path that redirects to storage, such as a job's log or an
        artifact's zip, at most `limit + 1` of them. GitHub gets the token; the storage it
        redirects to never does."""
        headers = {"User-Agent": "nexkit", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with build_opener(_NoRedirect).open(
                Request(f"{self.api_url}{path}", headers=headers), timeout=120
            ) as resp:
                return resp.read(limit + 1)
        except HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get("Location"):
                raise GitHubError(exc.code, exc.read().decode(errors="replace")[:500]) from None
            location = exc.headers["Location"]
        except URLError as exc:
            raise GitHubError(0, str(exc.reason)) from None
        try:
            with urlopen(Request(location, headers={"User-Agent": "nexkit"}), timeout=120) as resp:
                return resp.read(limit + 1)
        except HTTPError as exc:
            raise GitHubError(exc.code, "the download failed") from None
        except URLError as exc:
            raise GitHubError(0, str(exc.reason)) from None

    def job_log(self, job_id):
        return self.download(f"{self._repo}/actions/jobs/{job_id}/logs")

    def artifact_zip(self, artifact_id, limit=DOWNLOAD_LIMIT):
        return self.download(f"{self._repo}/actions/artifacts/{artifact_id}/zip", limit)

    def run_artifacts(self, run_id):
        path = f"{self._repo}/actions/runs/{run_id}/artifacts"
        return self.request("GET", path, params={"per_page": 100}).get("artifacts") or []

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

    def blocked_by(self, number):
        """The issues that block this one (GitHub issue dependencies), or [] where the API
        does not exist, such as on older GitHub Enterprise Server versions."""
        try:
            return self.paginate(f"{self._repo}/issues/{number}/dependencies/blocked_by")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            return []

    def add_blocked_by(self, number, blocker_id):
        """Record that issue `number` is blocked by the issue with database id `blocker_id`."""
        path = f"{self._repo}/issues/{number}/dependencies/blocked_by"
        return self.request("POST", path, {"issue_id": blocker_id})

    def milestones(self):
        """Open milestones."""
        return self.paginate(f"{self._repo}/milestones", {"state": "open"})

    def scoped_issues(self, milestone=None):
        """Open issues and pull requests, oldest first: those in a milestone (its number), or
        all of them."""
        params = {"state": "open", "sort": "created", "direction": "asc"}
        if milestone is not None:
            params["milestone"] = str(milestone)
        return self.paginate(f"{self._repo}/issues", params)

    def latest_closed_issue(self, milestone):
        """The issue in a milestone (its number) that closed last, or None. Issues sort by
        their last update, which closing is, so the one that closed last is on the first
        page unless more than a page of closed issues changed since."""
        params = {
            "state": "closed",
            "milestone": str(milestone),
            "sort": "updated",
            "direction": "desc",
            "per_page": "50",
        }
        found = self.request("GET", f"{self._repo}/issues", params=params) or []
        issues = [i for i in found if "pull_request" not in i and i.get("closed_at")]
        return max(issues, key=lambda i: (i["closed_at"], i["number"]), default=None)

    def create_issue(self, title, body, milestone=None, labels=()):
        data = {"title": title, "body": body}
        if milestone is not None:
            data["milestone"] = milestone
        if labels:
            data["labels"] = list(labels)
        return self.request("POST", f"{self._repo}/issues", data)

    def update_issue(self, number, milestone=None, labels=()):
        """Set the milestone and replace the labels of an issue or pull request."""
        data = {}
        if milestone is not None:
            data["milestone"] = milestone
        if labels:
            data["labels"] = list(labels)
        return self.request("PATCH", f"{self._repo}/issues/{number}", data)

    def add_sub_issue(self, number, child_id):
        """Add an issue, by its database id (not its number), as the last sub-issue."""
        return self.request(
            "POST", f"{self._repo}/issues/{number}/sub_issues", {"sub_issue_id": child_id}
        )

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

    def open_issues(self):
        """Open issues and pull requests; pull requests have a `pull_request` key."""
        return self.paginate(f"{self._repo}/issues", {"state": "open"})

    def list_pulls(self, state="open", limit=1000):
        params = {"state": state, "sort": "updated", "direction": "desc"}
        return self.paginate(f"{self._repo}/pulls", params, limit)

    def workflow_run(self, run_id):
        return self.request("GET", f"{self._repo}/actions/runs/{run_id}")

    def workflow_runs(self, workflow, branch, limit=5):
        """The latest runs of a workflow on a branch, newest first: one page, at most 100."""
        params = {"branch": branch, "per_page": min(limit, 100)}
        path = f"{self._repo}/actions/workflows/{quote(workflow)}/runs"
        return self.request("GET", path, params=params).get("workflow_runs") or []

    def run_attempt(self, run_id, attempt):
        """One attempt of a run, with that attempt's status and conclusion."""
        return self.request("GET", f"{self._repo}/actions/runs/{run_id}/attempts/{attempt}")

    def attempt_jobs(self, run_id, attempt):
        """The jobs of one attempt of a run: the first 100."""
        path = f"{self._repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
        return self.request("GET", path, params={"per_page": 100}).get("jobs") or []

    def check_runs(self, sha):
        """The latest check run of each name on a commit, such as the jobs of the workflow
        runs that tested it: the first 100."""
        path = f"{self._repo}/commits/{sha}/check-runs"
        params = {"filter": "latest", "per_page": 100}
        return self.request("GET", path, params=params).get("check_runs") or []

    def check_run_annotations(self, check_run_id):
        """The annotations of a check run, such as a job of a workflow run: the first 100."""
        return self.paginate(f"{self._repo}/check-runs/{check_run_id}/annotations", limit=100)

    def rerun_failed_jobs(self, run_id):
        return self.request("POST", f"{self._repo}/actions/runs/{run_id}/rerun-failed-jobs")

    def issues_by(self, creator, limit=100, state="all", since=None):
        """Issues that `creator` opened, newest first, without pull requests: in `state`,
        and with `since`, an ISO 8601 time, only those updated since then. GitHub lists
        pull requests among issues, so `limit` counts them too."""
        params = {"creator": creator, "state": state, "sort": "created", "direction": "desc"}
        if since:
            params["since"] = since
        found = self.paginate(f"{self._repo}/issues", params, limit)
        return [item for item in found if "pull_request" not in item]

    def pulls_into(self, base):
        """Open pull requests whose base branch is `base`."""
        return self.paginate(f"{self._repo}/pulls", {"state": "open", "base": base})

    def branch_sha(self, branch):
        return self.request("GET", f"{self._repo}/git/ref/heads/{quote(branch)}")["object"]["sha"]

    def close_pull(self, number):
        return self.request("PATCH", f"{self._repo}/pulls/{number}", {"state": "closed"})

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

    def update_branch(self, number, sha):
        """Merge the base branch into a pull request's branch on GitHub, when its head is
        still `sha`."""
        return self.request(
            "PUT", f"{self._repo}/pulls/{number}/update-branch", {"expected_head_sha": sha}
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

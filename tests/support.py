"""Shared fixtures: an in-memory GitHub, temporary git repositories and a fake Claude CLI."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import tempfile
import textwrap
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from nexkit.agent import interpret
from nexkit.config import validate
from nexkit.github import GitHubError
from nexkit.runtimes import RUNTIMES
from nexkit.runtimes.base import Launch, Runtime
from nexkit.runtimes.claude_code import ClaudeReader
from nexkit.state import BOT_LOGIN, RUN_PATTERN, render_run

# The Claude agent that `nexkit init` declares.
CLAUDE = {
    "claude": {
        "runtime": "claude-code",
        "provider": "anthropic",
        "oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN",
    }
}


def with_claude(raw):
    """`raw` with the Claude agent declared next to the agents it has."""
    return {**raw, "agents": {**CLAUDE, **raw.get("agents", {})}}


def make_config(**overrides):
    raw = {"checks": [{"name": "test", "run": "true"}], **overrides}
    return validate(with_claude(raw))


PROFILES = {
    "standard": {
        "when": "Clear spec, one layer.",
        "stages": {"plan": {"model": "opus"}, "implement": {"model": "sonnet"}},
    },
    "hard": {
        "when": "Touches security or storage.",
        "stages": {
            "plan": {"model": "fable", "timeout_minutes": 40},
            "implement": {"model": "opus", "max_budget_usd": 20},
            "review": {"model": "fable", "effort": "max"},
        },
    },
}


def profile_config(**overrides):
    """A configuration with the `standard` and `hard` profiles."""
    return make_config(profiles=PROFILES, default_profile="standard", **overrides)


def interpret_claude(
    stage,
    event=None,
    *,
    minutes=5,
    timed_out=False,
    stderr="",
    last_text="",
    returncode=0,
    stream=(),
    **kw,
):
    """Interpret a Claude Code run that ended with the result `event` (None: no result) and
    whose last message from Claude Code itself was `last_text`. `stream` holds the events
    that came before."""
    reader = ClaudeReader()
    for raw in stream:
        reader.events(raw)
    if last_text:
        content = [{"type": "text", "text": last_text}]
        reader.events(
            {"type": "assistant", "message": {"model": "<synthetic>", "content": content}}
        )
    if event is not None:
        reader.events({"type": "result", **event})
    run = {"seconds": 1.0, "timed_out": timed_out}
    outcome = reader.outcome(returncode, stderr)
    return interpret(stage, run, outcome, minutes, label="Claude", **kw)


class FakeGitHub:
    def __init__(self, repository="acme/app"):
        self.repository = repository
        self.roles = {"alice": "write", "olivia": "admin", "bob": "read"}
        self.issues = {}
        self.issue_comments = defaultdict(list)
        self.pulls = {}
        self.pr_reviews = defaultdict(list)
        self.pr_review_comments = defaultdict(list)
        self.created_reviews = []
        self.statuses = []
        self.dispatches = []
        self.merged = []
        self.updated = []  # (number, head) for each update of a branch
        self.reactions = []
        self.merge_error = None
        self.dispatch_errors = {}
        self.close_errors = {}
        self.update_errors = {}
        self.updates = []  # (number, milestone, labels) for each update_issue
        self.parents = {}  # sub-issue number -> parent number
        self.blockers = defaultdict(list)  # issue number -> numbers of issues that block it
        self.open_milestones = []
        self.workflow_history = {}  # workflow file -> its runs, oldest first
        self.jobs = {}  # run id, or (run id, attempt) -> jobs
        self.attempts = {}  # (run id, attempt) -> conclusion; others: the run's own
        self.annotations = {}  # job id -> its annotations
        self.commit_checks = {}  # commit sha -> its check runs
        self.logs = {}  # job id -> its log, as bytes
        self.run_artifact_list = {}  # run id -> its artifacts
        self.zips = {}  # artifact id -> its zip, as bytes
        self.calls = []  # ("attempt" or "jobs", run id, attempt) for each attempt read
        self.reruns = []
        self.branches = {}
        self.delete_on_merge = False  # the repository's `delete_branch_on_merge`
        self.deleted = []  # each branch deleted
        self.delete_error = None
        self.runs = {}
        self.dispatch_runs = {}
        self._next = 1000

    def _id(self):
        self._next += 1
        return self._next

    # fixtures -------------------------------------------------------------------

    def add_issue(
        self,
        number,
        title="Add a feature",
        body="Please add it.",
        state="open",
        reason=None,
        user="alice",
        milestone=None,
        labels=(),
    ):
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "state_reason": reason,
            "user": {"login": user},
            "milestone": {"number": milestone} if milestone else None,
            "labels": [{"name": label} for label in labels],
        }
        return self.issues[number]

    def round_comment(self, number, round, head, checks, text=""):
        """A NexKit round comment, as `report` leaves it."""
        run = {
            "round": round,
            "trigger": "fix",
            "status": "success",
            "url": f"https://github.com/acme/app/actions/runs/{round}",
            "head": head,
            "checks": checks,
            "verdict": "approve",
        }
        return self.comment(number, render_run(run, text))

    def human_comment(self, number, body, login="alice", association="COLLABORATOR"):
        comment = {
            "id": self._id(),
            "body": body,
            "user": {"login": login, "type": "User"},
            "author_association": association,
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.issue_comments[number].append(comment)
        return comment

    def add_pull(self, number, issue, head_sha="a" * 40, branch=None, state="open", repo=None):
        branch = branch or f"nexkit/issue-{issue}"
        self.pulls[number] = {
            "number": number,
            "state": state,
            "head": {
                "ref": branch,
                "sha": head_sha,
                "repo": {"full_name": repo or self.repository},
            },
            "base": {"ref": "main"},
            "mergeable": True,
        }
        self.issues[number] = {
            "number": number,
            "title": "PR",
            "body": "",
            "state": state,
            "pull_request": {},
        }
        return self.pulls[number]

    # API surface used by nexkit -------------------------------------------------------

    def role(self, user):
        return self.roles.get(user, "none")

    def can_write(self, user):
        return self.role(user) in {"admin", "maintain", "write"}

    def default_branch(self):
        return "main"

    def issue(self, number):
        return self.issues[number]

    def comments(self, number):
        return list(self.issue_comments[number])

    def parent_issue(self, number):
        parent = self.parents.get(number)
        return self.issues[parent] if parent else None

    def sub_issues(self, number):
        return [self.issues[n] for n, parent in self.parents.items() if parent == number]

    def blocked_by(self, number):
        return [self.issues[n] for n in self.blockers[number]]

    def add_blocked_by(self, number, blocker_id):
        self.blockers[number].append(blocker_id - 90000)

    def milestones(self):
        found = []
        for milestone in self.open_milestones:
            issues = [
                i
                for i in self.issues.values()
                if (i.get("milestone") or {}).get("number") == milestone["number"]
            ]
            open_issues = sum(i["state"] == "open" for i in issues)
            closed = len(issues) - open_issues
            found.append({**milestone, "open_issues": open_issues, "closed_issues": closed})
        return found

    def add_milestone(self, number, title=None, due_on=None):
        self.open_milestones.append(
            {"number": number, "title": title or f"M{number}", "due_on": due_on}
        )

    def latest_closed_issue(self, milestone):
        closed = [
            i
            for i in self.issues.values()
            if i["state"] == "closed" and (i.get("milestone") or {}).get("number") == milestone
        ]
        return max(closed, key=lambda i: (i.get("closed_at") or "", i["number"]), default=None)

    def scoped_issues(self, milestone=None):
        found = []
        for issue in self.issues.values():
            if issue["state"] != "open":
                continue
            if milestone is not None and (issue.get("milestone") or {}).get("number") != milestone:
                continue
            found.append(issue)
        return sorted(found, key=lambda i: i["number"])

    def create_issue(self, title, body, milestone=None, labels=()):
        number = max([*self.issues, *self.pulls, 0]) + 1
        issue = self.add_issue(
            number, title, body, user=BOT_LOGIN, milestone=milestone, labels=labels
        )
        issue["id"] = 90000 + number
        self._touch(number)
        return issue

    def update_issue(self, number, milestone=None, labels=()):
        if number in self.update_errors:
            raise self.update_errors[number]
        self.updates.append((number, milestone, list(labels)))
        issue = self.issues[number]
        if milestone is not None:
            issue["milestone"] = {"number": milestone}
        if labels:
            issue["labels"] = [{"name": label} for label in labels]
        return issue

    def add_sub_issue(self, number, child_id):
        self.parents[child_id - 90000] = number

    def close_pull(self, number):
        self.pulls[number]["state"] = "closed"
        self.issues[number]["state"] = "closed"

    def close_issue(self, number, reason="completed"):
        if number in self.close_errors:
            raise self.close_errors[number]
        closed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.issues[number].update(state="closed", state_reason=reason, closed_at=closed_at)
        self._touch(number)
        return self.issues[number]

    def recent_comments(self, since, limit=2000):
        found = []
        for number, comments in self.issue_comments.items():
            for comment in comments:
                url = f"https://api.github.com/repos/{self.repository}/issues/{number}"
                found.append({**comment, "issue_url": url})
        return found[::-1]

    def comment(self, number, body):
        comment = {
            "id": self._id(),
            "body": body,
            "user": {"login": BOT_LOGIN, "type": "Bot"},
            "author_association": "NONE",
            "created_at": "2026-01-01T00:00:00Z",
            "html_url": f"https://github.com/{self.repository}/issues/{number}#c",
        }
        self.issue_comments[number].append(comment)
        self._touch(number)
        return comment

    def update_comment(self, comment_id, body):
        for comments in self.issue_comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    # GitHub moves `updated_at` with each edit.
                    comment["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    return comment
        raise KeyError(comment_id)

    def react(self, comment_id, content="eyes"):
        self.reactions.append((comment_id, content))

    def pull(self, number):
        return self.pulls[number]

    def open_pulls(self, branch):
        return [
            p for p in self.pulls.values() if p["head"]["ref"] == branch and p["state"] == "open"
        ]

    def open_issues(self):
        return [i for i in self.issues.values() if i["state"] == "open"]

    def list_pulls(self, state="open", limit=1000):
        return [p for p in self.pulls.values() if state == "all" or p["state"] == state]

    def workflow_run(self, run_id):
        if run_id not in self.runs:
            raise GitHubError(404, "Not Found")
        return self.runs[run_id]

    def workflow_runs(self, workflow, branch, limit=5):
        found = [r for r in self.workflow_history.get(workflow, []) if r["head_branch"] == branch]
        return found[::-1][:limit]

    def _run(self, run_id):
        for runs in self.workflow_history.values():
            for run in runs:
                if run["id"] == run_id:
                    return run
        return self.runs[run_id]

    def run_attempt(self, run_id, attempt):
        self.calls.append(("attempt", run_id, attempt))
        run = self._run(run_id)
        conclusion = self.attempts.get((run_id, attempt), run.get("conclusion"))
        return {**run, "run_attempt": attempt, "status": "completed", "conclusion": conclusion}

    def attempt_jobs(self, run_id, attempt):
        self.calls.append(("jobs", run_id, attempt))
        return list(self.jobs.get((run_id, attempt), self.jobs.get(run_id, [])))

    def check_runs(self, sha):
        return list(self.commit_checks.get(sha, []))

    def check_run_annotations(self, check_run_id):
        self.calls.append(("annotations", check_run_id))
        return list(self.annotations.get(check_run_id, []))

    def job_log(self, job_id):
        if job_id not in self.logs:
            raise GitHubError(410, "Gone")
        return self.logs[job_id]

    def run_artifacts(self, run_id):
        return list(self.run_artifact_list.get(run_id, []))

    def artifact_zip(self, artifact_id, limit=64_000_000):
        return self.zips[artifact_id][: limit + 1]

    def rerun_failed_jobs(self, run_id):
        self.reruns.append(run_id)

    def issues_by(self, creator, limit=100, state="all", since=None):
        # As GitHub does, the newest `limit` items, pull requests among them, then without
        # them. An issue without `updated_at`, as tests add them, counts as updated now.
        found = [
            i
            for i in self.issues.values()
            if (i.get("user") or {}).get("login") == creator
            and state in ("all", i["state"])
            and (not since or i.get("updated_at", since) >= since)
        ]
        found = sorted(found, key=lambda i: -i["number"])[:limit]
        return [i for i in found if "pull_request" not in i]

    def _touch(self, number):
        """GitHub moves an issue's `updated_at` with each comment, edit and close."""
        if number in self.issues:
            self.issues[number]["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def pulls_into(self, base):
        return [p for p in self.pulls.values() if p["state"] == "open" and p["base"]["ref"] == base]

    def branch_sha(self, branch):
        return self.branches.get(branch, "f" * 40)

    def deletes_merged_branches(self):
        return self.delete_on_merge

    def delete_branch(self, branch):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(branch)

    def create_pull(self, title, head, base, body):
        number = max([*self.issues, *self.pulls, 0]) + 1
        pull = self.add_pull(number, int(head.rsplit("-", 1)[1]), branch=head)
        pull.update(title=title, body=body, base={"ref": base})
        # GitHub lists a pull request among its author's issues.
        self.issues[number]["user"] = {"login": BOT_LOGIN}
        return pull

    def reviews(self, number):
        return list(self.pr_reviews[number])

    def review_comments(self, number):
        return list(self.pr_review_comments[number])

    def create_review(self, number, commit_id, body):
        self.created_reviews.append({"pr": number, "commit_id": commit_id, "body": body})
        return {}

    def merge(self, number, sha, method="squash"):
        if isinstance(self.merge_error, list):
            # A refusal per try, in order; once none are left, the merge goes through.
            if self.merge_error:
                raise self.merge_error.pop(0)
        elif self.merge_error:
            raise self.merge_error
        self.merged.append((number, sha, method))
        return {"merged": True, "sha": "d" * 40}

    def update_branch(self, number, sha):
        self.updated.append((number, sha))
        self.pulls[number]["head"]["sha"] = "e" * 40

    def set_status(self, sha, context, state, description, target_url=None):
        self.statuses.append(
            {
                "sha": sha,
                "context": context,
                "state": state,
                "description": description,
                "target_url": target_url,
            }
        )

    def dispatch(self, workflow, ref, inputs, run_details=False):
        if workflow in self.dispatch_errors:
            raise self.dispatch_errors[workflow]
        self.dispatches.append({"workflow": workflow, "ref": ref, "inputs": inputs})
        if run_details and workflow in self.dispatch_runs:
            return {"workflow_run_id": 1, "html_url": self.dispatch_runs[workflow]}
        return None

    def comments_matching(self, number, text):
        return [c for c in self.issue_comments[number] if text in c["body"]]

    def run_comments(self, number):
        """NexKit run comments, oldest first, each with its decoded marker as `run`."""
        found = []
        for comment in self.issue_comments[number]:
            match = RUN_PATTERN.search(comment["body"])
            if match and comment["user"]["login"] == BOT_LOGIN:
                found.append({**comment, "run": json.loads(base64.b64decode(match.group(1)))})
        return found


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class GitRepos:
    """A bare 'origin' with a main branch, and helpers to make clones."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.clone("seed")
        (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (seed / ".gitignore").write_text("ignored/\n")
        git(seed, "add", "-A")
        self.commit(seed, "initial")
        git(seed, "push", "-q", "origin", "main")

    def clone(self, name):
        path = self.root / name
        git(self.root, "clone", "-q", str(self.origin), str(path))
        return path

    @staticmethod
    def commit(path, message):
        git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
        return git(path, "rev-parse", "HEAD")

    def push_commit(self, name, branch, files, message):
        """Commit `files` (None deletes) on `branch` of origin. Return the commit."""
        work = self.root / name
        if not work.exists():
            self.clone(name)
        git(work, "fetch", "-q", "origin")
        exists = git(work, "ls-remote", "--heads", "origin", branch)
        git(work, "checkout", "-q", "-B", branch, f"origin/{branch}" if exists else "origin/main")
        for path, content in files.items():
            target = work / path
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(work, "add", "-A")
        commit = self.commit(work, message)
        git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return commit

    def diverge(self, base_files=None, branch="nexkit/issue-5"):
        """A NexKit branch and a later base commit that both append a function to calc.py,
        so they conflict there. Return (branch head, base commit)."""
        head = self.push_commit("dev", branch, {"calc.py": CALC + SUB}, "Add sub")
        base = self.push_commit(
            "dev", "main", {"calc.py": CALC + MUL, **(base_files or {})}, "Add mul"
        )
        return head, base

    def cleanup(self):
        self._tmp.cleanup()


CALC = "def add(a, b):\n    return a - b\n"
SUB = "\n\ndef sub(a, b):\n    return a - b\n"
MUL = "\n\ndef mul(a, b):\n    return a * b\n"


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
spec = json.loads(os.environ["FAKE_CLAUDE"])
# A spec per credential, for agents with fallbacks: {"by_token": {token: spec}}.
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
spec = spec.get("by_token", {}).get(token, spec)
call = {"args": args, "prompt": prompt, "cwd": os.getcwd(),
        "has_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")), "token": token,
        "base_sha": os.environ.get("NEXKIT_BASE_SHA"),
        "verified_sha": os.environ.get("NEXKIT_VERIFIED_SHA"),
        "files": sorted(os.listdir(".")),
        "merging": os.path.exists(os.path.join(".git", "MERGE_HEAD"))}
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump(call, log)
with open(os.environ["FAKE_CLAUDE_LOG"] + "l", "a") as log:
    log.write(json.dumps(call) + "\\n")
time.sleep(spec.get("sleep", 0))
for path, content in spec.get("write", {}).items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
print(json.dumps({"type": "system", "subtype": "init"}))
for event in spec.get("events", [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Working on it"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "make test"}}]}}]):
    print(event if isinstance(event, str) else json.dumps(event))
if "result" in spec:
    event = {"type": "result", "subtype": spec.get("subtype", "success"),
             "is_error": spec.get("is_error", False), "num_turns": 3,
             "total_cost_usd": 0.5, "result": spec.get("text", ""),
             "modelUsage": {"m": {"inputTokens": 100, "cacheCreationInputTokens": 50,
                                  "cacheReadInputTokens": 850, "outputTokens": 120}}}
    if spec["result"] is not None:
        event["structured_output"] = spec["result"]
    print(json.dumps(event))
sys.exit(spec.get("exit", 0))
"""


class FakeClaude:
    """Install a fake `claude` executable; configure its behaviour per test."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.path = self.dir / "claude"
        self.path.write_text(textwrap.dedent(FAKE_CLAUDE))
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "claude-call.json"
        self._saved = {}

    def configure(self, **spec):
        self._set("FAKE_CLAUDE", json.dumps(spec))
        self._set("FAKE_CLAUDE_LOG", str(self.log))
        self._set("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    def _set(self, key, value):
        self._saved.setdefault(key, os.environ.get(key))
        os.environ[key] = value

    def call(self):
        return json.loads(self.log.read_text())

    def calls(self):
        """Every call since the fake was installed, oldest first."""
        path = self.log.with_name(self.log.name + "l")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class OtherRuntime(Runtime):
    """A second runtime for tests of what holds across runtimes. It never reports usage
    limits and takes the anthropic provider's fields."""

    name = "other"
    label = "Other"
    providers = {"anthropic": lambda agent, spec, secrets: Launch([], {}, {})}


def with_other_runtime():
    """Register `OtherRuntime` for the duration of a test: `with with_other_runtime(): …`."""
    return mock.patch.dict(RUNTIMES, {OtherRuntime.name: OtherRuntime()})


# A Claude agent on an API key: a second agent with its own secret.
TEAM = {"runtime": "claude-code", "provider": "anthropic", "api_key_secret": "TEAM_KEY"}
# An agent on the test-only runtime.
OTHER = {"runtime": "other", "provider": "anthropic", "api_key_secret": "OTHER_KEY"}

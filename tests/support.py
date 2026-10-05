"""Shared fixtures: an in-memory GitHub, temporary git repositories and a fake Claude CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
from collections import defaultdict
from pathlib import Path

from nexkit.config import validate
from nexkit.state import BOT_LOGIN


def make_config(**overrides):
    raw = {"checks": [{"name": "test", "run": "true"}], **overrides}
    return validate(raw)


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
        self.reactions = []
        self.merge_error = None
        self._next = 1000

    def _id(self):
        self._next += 1
        return self._next

    # fixtures -------------------------------------------------------------------

    def add_issue(self, number, title="Add a feature", body="Please add it.", state="open"):
        self.issues[number] = {"number": number, "title": title, "body": body, "state": state}
        return self.issues[number]

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
        return comment

    def update_comment(self, comment_id, body):
        for comments in self.issue_comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
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

    def create_pull(self, title, head, base, body):
        number = max([*self.issues, *self.pulls, 0]) + 1
        pull = self.add_pull(number, int(head.rsplit("-", 1)[1]), branch=head)
        pull.update(title=title, body=body, base={"ref": base})
        return pull

    def reviews(self, number):
        return list(self.pr_reviews[number])

    def review_comments(self, number):
        return list(self.pr_review_comments[number])

    def create_review(self, number, commit_id, body):
        self.created_reviews.append({"pr": number, "commit_id": commit_id, "body": body})
        return {}

    def merge(self, number, sha, method="squash"):
        if self.merge_error:
            raise self.merge_error
        self.merged.append((number, sha, method))
        return {"merged": True}

    def set_status(self, sha, context, state, description, target_url=None):
        self.statuses.append(
            {"sha": sha, "context": context, "state": state, "target_url": target_url}
        )

    def dispatch(self, workflow, ref, inputs):
        self.dispatches.append({"workflow": workflow, "ref": ref, "inputs": inputs})

    def comments_matching(self, number, text):
        return [c for c in self.issue_comments[number] if text in c["body"]]


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

    def cleanup(self):
        self._tmp.cleanup()


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
spec = json.loads(os.environ["FAKE_CLAUDE"])
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump({"args": args, "prompt": prompt, "cwd": os.getcwd(),
               "has_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}, log)
time.sleep(spec.get("sleep", 0))
for path, content in spec.get("write", {}).items():
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Working on it"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "make test"}}]}}))
if "result" in spec:
    event = {"type": "result", "subtype": spec.get("subtype", "success"),
             "is_error": spec.get("is_error", False), "num_turns": 3,
             "total_cost_usd": 0.5, "result": spec.get("text", "")}
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

    def restore(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

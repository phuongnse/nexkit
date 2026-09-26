from copy import deepcopy

from nexkit.common import Blocked, digest
from nexkit.policy import now, spec_hash


def project(repository="owner/project"):
    return {
        "schema": 1,
        "repository": repository,
        "default_branch": "main",
        "kit": {"repository": "phuongnse/nexkit", "ref": "a" * 40, "version": "0.1.0-rc.1"},
        "engine": {"name": "codex", "version": "0.156.1"},
        "models": {"implement": "test-model", "review": "test-model"},
        "limits": {"attempts": 3, "agent_calls": 6, "minutes": 60, "command_seconds": 30},
        "environment": {"runner": "ubuntu-24.04", "setup": []},
        "decisions": ["Use the existing project conventions"],
        "knowledge": ["README.md"],
        "application": "present",
        "merge_method": "squash",
        "release": {"enabled": False},
        "checks": [
            {
                "name": kind,
                "kind": kind,
                "argv": ["python3", "-m", "unittest"],
                "timeout_seconds": 30,
                "report": {"format": "unittest"},
            }
            for kind in ("test", "e2e")
        ],
    }


def issue():
    return {
        "number": 1,
        "title": "Handle negative integers",
        "body": "CLI must sum negative and positive integers.",
        "state": "open",
        "html_url": "https://github.com/owner/project/issues/1",
        "last_edited_at": None,
    }


def approve(work, *, login="owner", user_type="User", verb="approve", number=10):
    stamp = now()
    return {
        "id": number,
        "body": f"/nexkit {verb} {spec_hash(work)}",
        "created_at": stamp,
        "updated_at": stamp,
        "user": {"login": login, "type": user_type},
    }


def agent(role="deliver"):
    value = {
        "status": "done",
        "summary": "Implemented and checked signed integer sum",
        "skills_used": [f"nexkit-{role}"],
        "commands": ["python3 -m unittest"],
        "limitations": [],
    }
    if role == "review":
        value.update(
            verdict="approve",
            findings=[],
            acceptance=[
                {
                    "criterion": "Signed integers are summed",
                    "evidence": "CLI -2 5 returns 3",
                    "passed": True,
                }
            ],
        )
    return value


def verified(key):
    return {
        "candidate": deepcopy(key),
        "passed": True,
        "checks": [{"name": x, "kind": x, "tests": 2, "passed": True} for x in ("test", "e2e")],
    }


def reviewed(key):
    return {
        "candidate": deepcopy(key),
        "result": agent("review"),
        "unchanged": True,
        "independent": True,
    }


class FakeGitHub:
    """Deterministic failure injection only; never represented as live GitHub."""

    def __init__(self):
        self.repository = "owner/project"
        self.root = "repos/owner/project"
        self.cfg = project()
        self.work = issue()
        self.discussion = [approve(self.work)]
        self.state = {}
        self.revision = 0
        self.branches = {"main": "b" * 40}
        self.commits = {"b" * 40: {"sha": "b" * 40, "tree": {"sha": "c" * 40}}}
        self.pr = None
        self.merges = []
        self.dispatches = []
        self.checks = []
        self.messages = []
        self.commit_sequence = 0

    def issue(self, number):
        return deepcopy(self.work)

    def comments(self, number):
        return deepcopy(self.discussion)

    def permission(self, login):
        return "admin" if login == "owner" else "read"

    def repo(self):
        return {"default_branch": "main"}

    def ref(self, branch):
        if branch not in self.branches:
            raise Blocked("HTTP 404")
        return self.branches[branch]

    def read_config(self, ref):
        return deepcopy(self.cfg)

    def get_state(self, number):
        return deepcopy(self.state), str(self.revision) if self.revision else None

    def save_state(self, number, value, previous):
        expected = str(self.revision) if self.revision else None
        if previous != expected:
            raise Blocked("HTTP 409: state conflict")
        self.state = deepcopy(value)
        self.revision += 1
        return str(self.revision)

    def strict_protection(self, branch, cfg=None):
        return {"required_status_checks": {"strict": True}}

    def pull_for_branch(self, branch):
        return deepcopy(self.pr)

    def pull(self, number):
        return deepcopy(self.pr)

    def comment(self, number, body):
        self.messages.append(body)

    def check(self, name, sha, passed, summary):
        self.checks.append((name, sha, passed))

    def dispatch(self, workflow, branch, inputs):
        self.dispatches.append((workflow, branch, inputs))

    def api(self, path, method="GET", data=None, **kwargs):
        if "/compare/" in path:
            base, head = path.split("/compare/")[1].split("...")

            def ancestors(sha):
                found, pending = set(), [sha]
                while pending:
                    item = pending.pop()
                    if item in found:
                        continue
                    found.add(item)
                    pending.extend(self.commits[item].get("parents", []))
                return found

            return {
                "status": "identical"
                if base == head
                else "ahead"
                if base in ancestors(head)
                else "behind"
                if head in ancestors(base)
                else "diverged"
            }
        if "/git/commits/" in path:
            return deepcopy(self.commits[path.split("/")[-1]])
        if path.endswith("/git/trees"):
            return {"sha": digest(data)[:40]}
        if path.endswith("/git/commits"):
            self.commit_sequence += 1
            sha = digest({**data, "simulated_commit_timestamp": self.commit_sequence})[:40]
            value = {"sha": sha, "tree": {"sha": data["tree"]}, "parents": data["parents"]}
            self.commits[sha] = value
            return deepcopy(value)
        if path.endswith("/git/refs"):
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.branches:
                raise Blocked("HTTP 422: Reference already exists")
            self.branches[branch] = data["sha"]
            return {}
        if "/git/refs/heads/" in path:
            branch = path.split("/git/refs/heads/")[1]
            self.branches[branch] = data["sha"]
            if self.pr:
                self.pr["head"]["sha"] = data["sha"]
            return {}
        if path.endswith("/pulls") and method == "POST":
            self.pr = {
                "number": 2,
                "state": "open",
                "merged_at": None,
                "head": {
                    "sha": self.branches[data["head"]],
                    "ref": data["head"],
                    "repo": {"full_name": self.repository},
                },
                "base": {
                    "sha": self.branches[data["base"]],
                    "ref": data["base"],
                    "repo": {"full_name": self.repository},
                },
            }
            return deepcopy(self.pr)
        if path.endswith("/merge"):
            self.merges.append(deepcopy(data))
            self.pr.update(merged_at=now(), merge_commit_sha="d" * 40)
            return {"merged": True, "sha": "d" * 40}
        if "/actions/runs/" in path:
            return {"status": "completed"}
        raise AssertionError(f"Unhandled mock API: {method} {path}")

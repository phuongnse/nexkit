import base64
import hashlib
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from nexkit.common import Blocked, digest
from nexkit.pipelines import _effective, effective_config
from nexkit.policy import now, spec_hash


def project(repository="owner/project"):
    """Resolved settings fixture; on-disk projects use project_document()."""
    value = {
        "schema": 1,
        "repository": repository,
        "default_branch": "main",
        "kit": {"repository": "phuongnse/nexkit", "ref": "a" * 40, "version": "1.0.0"},
        "engine": {"name": "codex"},
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
                "argv": [sys.executable, "-m", "unittest"],
                "timeout_seconds": 30,
                "report": {"format": "unittest"},
            }
            for kind in ("test", "e2e")
        ],
    }

    return effective_config(project_document(value), "maintenance")


def workflow_files():
    """Small immutable workflow files for mocked controller boundaries."""
    return {
        f".github/workflows/nexkit-{name}.yml": f"name: {name}\non: workflow_dispatch\njobs: {{}}\n".encode()
        for name in ("intake", "clarify", "delivery", "release")
    }


def project_document(settings):
    if "pipelines" in settings:
        return deepcopy(settings)
    identity = {"repository", "default_branch", "kit"}
    return {
        "schema": 1,
        **{key: deepcopy(settings[key]) for key in identity},
        "defaults": {
            key: deepcopy(value)
            for key, value in settings.items()
            if key not in identity | {"schema", "binding"}
        },
        "pipelines": {
            "maintenance": {
                "settings": {},
                "entrypoints": {
                    name: f".github/workflows/nexkit-{name}.yml"
                    for name in ("intake", "clarify", "delivery", "release")
                },
                "agent_workflows": [
                    f".github/workflows/nexkit-{name}.yml" for name in ("clarify", "delivery")
                ],
            }
        },
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "managed": True}
            for name, content in workflow_files().items()
        },
    }


def workflow_environment(operation="delivery"):
    return {
        "NEXKIT_PIPELINE": "maintenance",
        "GITHUB_WORKFLOW_REF": f"owner/project/.github/workflows/nexkit-{operation}.yml@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": "b" * 40,
    }


def install_fixture(root, cfg, hosts, *, apply=False):
    from nexkit.project import install

    with tempfile.TemporaryDirectory() as bundle:
        for name, content in workflow_files().items():
            target = Path(bundle, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return install(root, project_document(cfg), hosts, apply=apply, bundle=bundle)


def doctor_fixture(root, cfg, **kwargs):
    from nexkit.project import doctor

    return doctor(root, project_document(cfg), **kwargs)["pipelines"]["maintenance"]


def issue():
    return {
        "number": 1,
        "title": "Handle negative integers",
        "body": "<!-- nexkit:request:maintenance:change -->\n<!-- nexkit:pipeline:maintenance -->\nCLI must sum negative and positive integers.\n\n## Acceptance criteria\n- [AC1] Signed integers are summed.\n",
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
                    "criterion": "AC1",
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
    result = agent("review")
    if isinstance(key.get("criteria"), list):
        result["acceptance"] = [
            {
                "criterion": item["id"],
                "evidence": "Simulated evidence: " + item["text"],
                "passed": True,
            }
            for item in key["criteria"]
        ]
    return {
        "candidate": deepcopy(key),
        "result": result,
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

    @property
    def cfg(self):
        if "pipelines" not in self._cfg:
            document = project_document(self._cfg)
            self._cfg["binding"] = _effective(document, "maintenance")["binding"]
        return self._cfg

    @cfg.setter
    def cfg(self, value):
        self._cfg = value

    def content(self, path, ref):
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(workflow_files()[path]).decode(),
        }

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
        return project_document(self.cfg)

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

    def observe_merge(self, sha="d" * 40):
        """Record a real-looking external merge without issuing a second merge request."""
        self.pr.update(merged_at=now(), merge_commit_sha=sha)
        self.commits[sha] = {
            "sha": sha,
            "tree": self.commits[self.pr["head"]["sha"]]["tree"],
            "parents": [self.pr["base"]["sha"], self.pr["head"]["sha"]],
        }
        self.branches[self.pr["base"]["ref"]] = sha

    def api(self, path, method="GET", data=None, **kwargs):
        if "/actions/runs/" in path and "/artifacts?" in path:
            return []
        if path.endswith("/timeline?per_page=100"):
            return []
        if path == f"{self.root}/issues/{self.work['number']}" and method == "PATCH":
            self.work.update(deepcopy(data))
            if data.get("state") == "closed":
                self.work["closed_at"] = now()
            return deepcopy(self.work)
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
                "title": data["title"],
                "body": data["body"],
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
        if self.pr and path == f"{self.root}/pulls/{self.pr['number']}" and method == "PATCH":
            self.pr.update(deepcopy(data))
            return deepcopy(self.pr)
        if path.endswith("/merge"):
            self.merges.append(deepcopy(data))
            self.observe_merge()
            return {"merged": True, "sha": "d" * 40}
        if "/actions/runs/" in path:
            return {"status": "completed"}
        raise AssertionError(f"Unhandled mock API: {method} {path}")

"""Actual setup planning with simulated protected GitHub publication and approvals."""

import base64
import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nexkit.administration import (
    PROPOSAL_PATH,
    WORKFLOW_PATH,
    bootstrap,
    finish,
    plan,
    preflight,
    prepare,
    validate,
    verify,
)
from nexkit.administrative_publisher import PERMISSIONS
from nexkit.clarify import prepare as prepare_clarification
from nexkit.common import Blocked, canonical, digest, run
from nexkit.pipelines import effective_config
from nexkit.policy import now
from nexkit.runner_administration import admission
from nexkit.runner_administration import operate as runner_operation
from runner.job_hook import admitted as runner_admitted
from tests.support import FakeGitHub, install_fixture, project_document, reviewed, workflow_files
from tests.test_clarify import RequirementGitHub

PUBLICATION = {"app_id": 12345, "app_slug": "nexkit-test-publisher", "installation_id": 90}


class PublicationGitHub:
    def __init__(self, gh):
        self.gh = gh
        self.binding = {
            **PUBLICATION,
            "installation_id": 90,
            "repository_id": 50,
            "permissions": dict(PERMISSIONS),
        }
        self.requests = []

    def api(self, path, method="GET", data=None):
        self.requests.append((path, method))
        with patch.object(self.gh, "publication_active", True):
            return self.gh.api(path, method, data)


@contextmanager
def simulated_publisher(client):
    yield client


class AdministrativeGitHub(FakeGitHub):
    def __init__(self, root, proposal):
        super().__init__()
        self.proposal = deepcopy(proposal)
        self.base_files = {
            name: Path(root, name).read_text()
            for name in run(["git", "ls-files"], cwd=root).stdout.splitlines()
        }
        self.base = proposal["base"]
        self.branches = {"main": self.base}
        self.files = {self.base: self.base_files}
        self.trees = {}
        self.commits = {
            self.base: {
                "sha": self.base,
                "tree": {"sha": self.base},
                "parents": [],
                "committer": {"date": "2026-01-01T00:00:00Z"},
            }
        }
        self.stage = "e" * 40
        self.files[self.stage] = {
            PROPOSAL_PATH: canonical(proposal),
            WORKFLOW_PATH: bootstrap(proposal),
        }
        self.administrative_state, self.admin_revision = {}, None
        self.reviews, self.requests = [], []
        self.publication_active = False

    def get_administration(self, identifier):
        return deepcopy(self.administrative_state), self.admin_revision

    def save_administration(self, identifier, value, previous):
        if previous != self.admin_revision:
            raise Blocked("HTTP 409: administrative state conflict")
        self.administrative_state = deepcopy(value)
        self.admin_revision = str(int(self.admin_revision or "0") + 1)
        return self.admin_revision

    def read_config(self, ref):
        import json

        return json.loads(self.files[self.branches.get(ref, ref)][".nexkit/project.json"])

    def content(self, name, ref):
        ref = self.branches.get(ref, ref)
        if name not in self.files[ref]:
            raise Blocked("HTTP 404")
        return {
            "type": "file",
            "size": len(self.files[ref][name].encode()),
            "encoding": "base64",
            "content": base64.b64encode(self.files[ref][name].encode()).decode(),
        }

    def api(self, path, method="GET", data=None, **kwargs):
        if ("/git/" in path and method != "GET") or (path.endswith("/merge") and method == "PUT"):
            if not self.publication_active:
                raise AssertionError("Workflow Git writes must use the App credential")
        self.requests.append((path, method))
        if "/actions/runs/" in path:
            return {
                "status": "completed",
                "event": "push",
                "head_sha": self.stage,
                "path": WORKFLOW_PATH,
                "actor": {"login": "owner", "type": "User"},
                "triggering_actor": {"login": "owner", "type": "User"},
            }
        if path.endswith("/git/trees") and method == "POST":
            contents = deepcopy(self.files[data["base_tree"]])
            for item in data["tree"]:
                if "content" in item:
                    contents[item["path"]] = item["content"]
                else:
                    contents.pop(item["path"], None)
            sha = digest(contents)[:40]
            self.trees[sha] = contents
            return {"sha": sha}
        if "/git/trees/" in path:
            ref = path.split("/git/trees/")[1].split("?")[0]
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": name,
                        "mode": "100644",
                        "type": "blob",
                        "sha": hashlib.sha1(
                            b"blob " + str(len(text.encode())).encode() + b"\0" + text.encode()
                        ).hexdigest(),
                    }
                    for name, text in self.files[ref].items()
                ],
            }
        if path.endswith("/git/commits") and method == "POST":
            sha = digest(data)[:40]
            self.files[sha] = deepcopy(self.trees[data["tree"]])
            self.commits[sha] = {
                "sha": sha,
                "tree": {"sha": data["tree"]},
                "parents": [{"sha": parent} for parent in data["parents"]],
            }
            return deepcopy(self.commits[sha])
        if path.endswith("/pulls") and method == "POST":
            result = super().api(path, method, data, **kwargs)
            self.pr["user"] = {"login": "github-actions[bot]", "type": "Bot"}
            return {**result, "user": deepcopy(self.pr["user"])}
        if path.endswith("/reviews?per_page=100"):
            return deepcopy(self.reviews)
        if path.endswith("/merge"):
            self.files["d" * 40] = deepcopy(self.files[self.pr["head"]["sha"]])
        return super().api(path, method, data, **kwargs)

    def approve_candidate(self, *, actor="owner", state="APPROVED", commit=None):
        self.reviews.append(
            {
                "id": len(self.reviews) + 10,
                "user": {"login": actor, "type": "User"},
                "state": state,
                "commit_id": commit or self.pr["head"]["sha"],
                "submitted_at": now(),
                "body": "Simulated native approval of the exact candidate",
            }
        )


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nexkit-administration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        old = project_document(FakeGitHub().cfg)
        old["defaults"]["clarification"] = {"agent_minutes": 10, "max_calls": 3}
        install_fixture(self.root, old, ["codex"], apply=True)
        (self.root / "app.py").write_text("print('existing source')\n")
        run(["git", "init", "-b", "main"], cwd=self.root)
        run(["git", "add", "."], cwd=self.root)
        run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "base",
            ],
            cwd=self.root,
        )
        proposed = deepcopy(old)
        proposed["defaults"]["clarification"]["agent_minutes"] = 30
        self.bundle = self.root / "bundle-input"
        for name, content in workflow_files().items():
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.result = plan(
            self.root, proposed, "maintenance", bundle=self.bundle, publication=PUBLICATION
        )
        self.gh = AdministrativeGitHub(self.root, self.result["proposal"])
        self.publication = PublicationGitHub(self.gh)
        self.publisher = self.enterContext(
            patch(
                "nexkit.administration.publisher",
                side_effect=lambda *_: simulated_publisher(self.publication),
            )
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "GITHUB_WORKFLOW_REF": f"owner/project/{WORKFLOW_PATH}@refs/heads/{self.result['branch']}",
                    "GITHUB_WORKFLOW_SHA": self.gh.stage,
                    "GITHUB_REF": "refs/heads/" + self.result["branch"],
                    "GITHUB_RUN_ID": "100",
                    "GITHUB_RUN_ATTEMPT": "1",
                },
            )
        )

    def prepare(self):
        return prepare(self.gh, self.result["id"], self.gh.stage, "a" * 40)

    def review(self, context):
        return {**reviewed(context["candidate"]), "run_key": context["run_key"]}

    def test_solo_administrator_approves_bot_pr_after_real_contract_validation_and_separate_review(
        self,
    ):
        source = (self.root / ".nexkit/project.json").read_bytes()
        self.gh.state = {"agent_calls": 8, "delivery_calls": 4, "attempts": 2}
        old_work = deepcopy(self.gh.state)
        result = self.prepare()
        context = result["context"]
        self.assertEqual((self.root / ".nexkit/project.json").read_bytes(), source)
        self.assertEqual(self.gh.pr["user"]["type"], "Bot")
        self.assertEqual(context["config"]["environment"]["setup"], [])
        self.assertFalse(finish(self.gh, context, self.review(context))["merged"])
        self.assertEqual(self.gh.merges, [])
        self.gh.approve_candidate()
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        resumed = self.prepare()
        self.assertFalse(resumed["ready"])
        self.assertTrue(finish(self.gh, resumed["context"])["merged"])
        self.assertEqual(self.gh.administrative_state["review_calls"], 1)
        self.assertEqual(self.gh.state, old_work)
        self.assertEqual(
            self.gh.read_config("main")["defaults"]["clarification"]["agent_minutes"], 30
        )
        self.assertEqual(len(self.gh.merges), 1)
        self.assertTrue(self.prepare()["completed"])
        self.assertEqual(len(self.gh.merges), 1)

    def test_updated_clarification_reserves_thirty_minutes_without_resetting_prior_usage(self):
        first = self.prepare()["context"]
        self.gh.approve_candidate()
        finish(self.gh, first, self.review(first))
        self.gh.work = RequirementGitHub().work
        stamp = now()
        self.gh.discussion = [
            {
                "id": 40,
                "body": "/nexkit resume",
                "created_at": stamp,
                "updated_at": stamp,
                "user": {"login": "owner", "type": "User"},
            }
        ]
        self.gh.state = {
            "agent_calls": 1,
            "clarification": {
                "calls": 1,
                "run_key": "90.1",
                "reserved_minutes": 10,
                "status": "blocked",
            },
        }
        os.environ.update(
            {
                "GITHUB_WORKFLOW_REF": "owner/project/.github/workflows/nexkit-clarify.yml@refs/heads/main",
                "GITHUB_WORKFLOW_SHA": self.gh.branches["main"],
            }
        )
        context = prepare_clarification(self.gh, 1, "101.1", "a" * 40, {}, pipeline="maintenance")
        self.assertTrue(context["ready"], context)
        self.assertEqual(context["agent_minutes"], 30)
        self.assertEqual(self.gh.state["clarification"]["calls"], 2)
        self.assertEqual(self.gh.state["clarification"]["reserved_minutes"], 40)

    def test_guard_rejects_unapproved_source_extra_controls_wrong_kit_and_forged_bootstrap(self):
        self.gh.files[self.gh.stage][WORKFLOW_PATH] += "# modified\n"
        with self.assertRaisesRegex(Blocked, "template"):
            self.prepare()
        self.gh.files[self.gh.stage][WORKFLOW_PATH] = bootstrap(self.result["proposal"])
        with self.assertRaisesRegex(Blocked, "kit"):
            prepare(self.gh, self.result["id"], self.gh.stage, "f" * 40)
        context = self.prepare()["context"]
        self.gh.files[context["source"]]["app.py"] = "print('unapproved source')\n"
        with self.assertRaisesRegex(Blocked, "unapproved"):
            finish(self.gh, context, self.review(context))
        self.assertEqual(self.gh.merges, [])

    def test_changed_base_author_actor_or_closed_pr_cannot_reuse_the_proposal(self):
        context = self.prepare()["context"]
        original = deepcopy(self.gh.pr)
        for mutation in ("base", "author", "closed", "actor"):
            self.gh.pr = deepcopy(original)
            self.gh.branches["main"] = self.gh.base
            self.gh.permission = lambda login: "admin" if login == "owner" else "read"
            if mutation == "base":
                self.gh.branches["main"] = "f" * 40
            elif mutation == "author":
                self.gh.pr["user"] = {"login": "owner", "type": "User"}
            elif mutation == "closed":
                self.gh.pr["state"] = "closed"
            else:
                self.gh.permission = lambda _: "read"
            os.environ["GITHUB_RUN_ATTEMPT"] = "2"
            with self.subTest(mutation=mutation), self.assertRaises(Blocked):
                self.prepare()
            self.assertEqual(self.gh.merges, [])
            self.assertEqual(self.gh.administrative_state["review_calls"], 1)
        self.gh.pr = original
        self.gh.permission = lambda login: "admin" if login == "owner" else "read"
        self.gh.branches["main"] = self.gh.base
        self.assertEqual(self.gh.administrative_state["head"], context["source"])

    def test_temporary_runner_admission_accepts_only_the_exact_proposal_and_commit(self):
        proposal = self.result["proposal"]
        cfg = effective_config(proposal["project"], proposal["pipeline"])
        bound = {
            "repository": cfg["repository"],
            "default_branch": "main",
            "isolation": "container-v1",
            "workflows": ["nexkit-delivery.yml"],
            "agent": "codex",
            "auth": "chatgpt",
            "image": "sha256:" + "a" * 64,
        }
        admitted = admission(bound, cfg, proposal, self.gh.stage, 120)
        ref = "refs/heads/" + self.result["branch"]
        env = {
            "GITHUB_REPOSITORY": cfg["repository"],
            "GITHUB_REF": ref,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_SHA": self.gh.stage,
            "GITHUB_WORKFLOW_SHA": self.gh.stage,
            "GITHUB_WORKFLOW_REF": f"{cfg['repository']}/{WORKFLOW_PATH}@{ref}",
        }
        self.assertTrue(runner_admitted(admitted, env))
        self.assertEqual(bound["workflows"], admitted["workflows"])
        self.assertNotIn("administration", bound)
        for name, value in (
            ("GITHUB_REPOSITORY", "other/repo"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_EVENT_NAME", "pull_request_target"),
            ("GITHUB_SHA", "f" * 40),
            ("GITHUB_WORKFLOW_SHA", "f" * 40),
            ("GITHUB_WORKFLOW_REF", "owner/project/.github/workflows/other.yml@" + ref),
        ):
            with self.subTest(name=name):
                self.assertFalse(runner_admitted(admitted, {**env, name: value}))
        admitted["administration"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
        self.assertFalse(runner_admitted(admitted, env))
        for minutes in (0, True, 1441):
            with self.subTest(minutes=minutes), self.assertRaises(Blocked):
                admission(bound, cfg, proposal, self.gh.stage, minutes)

    def test_runner_binding_preview_and_apply_preserve_registration_and_require_capable_image(self):
        proposal = self.result["proposal"]
        cfg = effective_config(proposal["project"], proposal["pipeline"])
        host = self.root / "host"
        host.mkdir()
        image = "sha256:" + "a" * 64
        bound = {
            "repository": cfg["repository"],
            "default_branch": "main",
            "isolation": "container-v1",
            "workflows": ["nexkit-delivery.yml"],
            "agent": "codex",
            "auth": "chatgpt",
            "image": image,
            "registration": "existing-runner",
            "login_directory": "/private/existing-login",
        }
        from nexkit.common import write_json

        write_json(host / "runner.json", bound)
        with (
            patch(
                "nexkit.runner_administration.runner_identity",
                return_value=("test", str(host), "label"),
            ),
            patch("nexkit.runner_administration.run") as command,
        ):
            preview = runner_operation(
                "admit-administration", cfg, proposal=proposal, sha=self.gh.stage
            )
            command.assert_not_called()
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["binding"]["registration"], "existing-runner")
            self.assertEqual(preview["binding"]["login_directory"], bound["login_directory"])
            command.return_value = SimpleNamespace(stdout="active\n", returncode=0)
            with self.assertRaisesRegex(Blocked, "Stop"):
                runner_operation(
                    "admit-administration", cfg, proposal=proposal, sha=self.gh.stage, apply=True
                )
            for capable in (False, True):
                command.reset_mock()
                command.side_effect = [
                    SimpleNamespace(stdout="inactive\n", returncode=3),
                    SimpleNamespace(stdout=image + "\n", returncode=0),
                    SimpleNamespace(stdout="", returncode=0 if capable else 1),
                    SimpleNamespace(stdout="", returncode=0),
                ]
                if capable:
                    result = runner_operation(
                        "admit-administration",
                        cfg,
                        proposal=proposal,
                        sha=self.gh.stage,
                        apply=True,
                    )
                    self.assertTrue(result["login_preserved"])
                    self.assertTrue(result["registration_preserved"])
                    self.assertIn("install", command.call_args.args[0])
                else:
                    with self.assertRaisesRegex(Blocked, "image lacks"):
                        runner_operation(
                            "admit-administration",
                            cfg,
                            proposal=proposal,
                            sha=self.gh.stage,
                            apply=True,
                        )
                    self.assertEqual(command.call_count, 3)

    def test_missing_partial_failed_or_modified_review_never_satisfies_the_gate(self):
        context = self.prepare()["context"]
        self.gh.approve_candidate()
        for mutation in ("missing", "partial", "failed", "modified", "candidate", "reservation"):
            report = self.review(context)
            if mutation == "partial":
                report["result"]["acceptance"].pop()
            elif mutation == "failed":
                report["result"]["acceptance"][0]["passed"] = False
            elif mutation == "modified":
                report["unchanged"] = False
            elif mutation == "candidate":
                report["candidate"]["head"] = "f" * 40
            elif mutation == "reservation":
                report["run_key"] = "90.1"
            with self.subTest(mutation=mutation), self.assertRaises(Blocked):
                finish(self.gh, context, None if mutation == "missing" else report)
        self.assertEqual(self.gh.merges, [])
        self.assertNotIn(("NexKit review", context["source"], True), self.gh.checks)

    def test_stale_revoked_and_unauthorized_native_reviews_cannot_merge(self):
        context = self.prepare()["context"]
        for actor, verdict, commit in (
            ("reader", "APPROVED", None),
            ("owner", "APPROVED", "f" * 40),
            ("owner", "DISMISSED", None),
        ):
            self.gh.reviews = []
            self.gh.approve_candidate(actor=actor, state=verdict, commit=commit)
            self.assertFalse(finish(self.gh, context, self.review(context))["merged"])
        self.gh.approve_candidate(state="CHANGES_REQUESTED")
        with self.assertRaisesRegex(Blocked, "requests changes"):
            finish(self.gh, context, self.review(context))
        self.assertEqual(self.gh.merges, [])

    def test_duplicate_and_interrupted_publication_reuse_the_exact_owned_pr(self):
        context = self.prepare()["context"]
        with self.assertRaisesRegex(Blocked, "Duplicate"):
            self.prepare()
        self.assertEqual(self.gh.administrative_state["review_calls"], 1)
        # Simulate loss of the state write after branch/PR creation. The same
        # proposal recovers those native objects rather than recreating them.
        self.gh.administrative_state, self.gh.admin_revision = {}, None
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        recovered = self.prepare()["context"]
        self.assertEqual(recovered["source"], context["source"])
        self.assertEqual(self.gh.commit_sequence, 0)
        self.assertEqual(
            sum(path.endswith("/pulls") and method == "POST" for path, method in self.gh.requests),
            1,
        )

    def test_candidate_verification_needs_no_write_token_or_consumer_execution(self):
        context = self.prepare()["context"]
        self.gh.requests.clear()
        report = verify(self.gh, self.result["proposal"], context["source"])
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 5)
        self.assertTrue(all(method == "GET" for _, method in self.gh.requests))
        self.assertNotIn("application tests passed", canonical(report))

    def test_publication_credential_denial_cannot_create_state_pr_or_consume_review_budget(self):
        self.publisher.side_effect = Blocked("publication credential unavailable")
        with self.assertRaisesRegex(Blocked, "credential unavailable"):
            self.prepare()
        self.assertEqual(self.gh.administrative_state, {})
        self.assertEqual(self.publication.requests, [])
        self.assertFalse(any(method != "GET" for _, method in self.gh.requests))

    def test_local_preflight_requires_actions_secret_and_reports_hosted_identity_as_unverified(
        self,
    ):
        missing = True

        def inspect(path, *args, **kwargs):
            if path == "user":
                return {"login": "owner", "type": "User"}
            if path.endswith("/actions/permissions/workflow"):
                return {"can_approve_pull_request_reviews": True}
            if "/actions/secrets/" in path:
                if missing:
                    raise Blocked("HTTP 404")
                return {"name": "NEXKIT_ADMIN_APP_PRIVATE_KEY"}
            raise AssertionError(path)

        with (
            patch.object(self.gh, "api", side_effect=inspect),
            patch.object(self.gh, "audit_settings", create=True, return_value={}),
        ):
            with self.assertRaisesRegex(Blocked, "before staging"):
                preflight(self.gh, self.result["proposal"])
            self.publisher.assert_not_called()
            missing = False
            result = preflight(self.gh, self.result["proposal"])
        self.assertTrue(result["publication"]["effective_token_verified"])
        self.assertTrue(result["publication"]["actions_secret_configured"])
        self.assertFalse(result["publication"]["actions_key_identity_verified"])
        self.assertFalse(result["publication"]["live_candidate_publication_verified"])
        self.assertEqual(self.gh.administrative_state, {})

    def test_ref_publication_failure_reuses_recorded_commit_without_reserving_a_review(self):
        original = self.publication.api

        def fail_ref(path, method="GET", data=None):
            if path.endswith("/git/refs"):
                raise Blocked("HTTP 403: publication denied")
            return original(path, method, data)

        with patch.object(self.publication, "api", side_effect=fail_ref):
            with self.assertRaisesRegex(Blocked, "HTTP 403"):
                self.prepare()
        head = self.gh.administrative_state["head"]
        self.assertNotIn("pr", self.gh.administrative_state)
        self.assertNotIn("review_calls", self.gh.administrative_state)
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        context = self.prepare()["context"]
        self.assertEqual(context["source"], head)
        self.assertEqual(
            sum(
                path.endswith("/git/commits") and method == "POST"
                for path, method in self.publication.requests
            ),
            1,
        )
        self.assertEqual(self.gh.administrative_state["review_calls"], 1)

    def test_lost_commit_response_reuses_the_same_reproducible_git_object(self):
        original = self.publication.api
        created = []

        def interrupt(path, method="GET", data=None):
            result = original(path, method, data)
            if path.endswith("/git/commits") and method == "POST":
                created.append(result["sha"])
                raise Blocked("Transport interrupted after commit creation")
            return result

        with patch.object(self.publication, "api", side_effect=interrupt):
            with self.assertRaisesRegex(Blocked, "Transport interrupted"):
                self.prepare()
        self.assertNotIn("review_calls", self.gh.administrative_state)
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        self.assertEqual(self.prepare()["context"]["source"], created[0])

    def test_lost_pr_response_reuses_the_owned_bot_pr(self):
        original = self.gh.api

        def interrupt(path, method="GET", data=None, **kwargs):
            result = original(path, method, data, **kwargs)
            if path.endswith("/pulls") and method == "POST":
                raise Blocked("Transport interrupted after PR creation")
            return result

        with patch.object(self.gh, "api", side_effect=interrupt):
            with self.assertRaisesRegex(Blocked, "Transport interrupted"):
                self.prepare()
        self.assertNotIn("review_calls", self.gh.administrative_state)
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        self.prepare()
        self.assertEqual(
            sum(path.endswith("/pulls") and method == "POST" for path, method in self.gh.requests),
            1,
        )

    def test_changed_app_installation_cannot_reuse_review_or_merge(self):
        context = self.prepare()["context"]
        self.gh.approve_candidate()
        self.publication.binding["installation_id"] = 91
        with self.assertRaisesRegex(Blocked, "authority changed"):
            finish(self.gh, context, self.review(context))
        self.assertEqual(self.gh.merges, [])
        self.assertEqual(self.gh.administrative_state["review_calls"], 1)

    def test_proposal_contract_rejects_source_delivery_and_ledger_drift(self):
        for mutation in ("source", "ledger", "control", "duplicate", "unbounded"):
            value = deepcopy(self.result["proposal"])
            if mutation == "source":
                value["changes"].append({"path": "app.py", "content": "changed"})
            elif mutation == "ledger":
                value["ledger"]["project"] = "f" * 64
            elif mutation == "control":
                value["project"]["files"][".github/workflows/nexkit-delivery.yml"]["sha256"] = (
                    "f" * 64
                )
            elif mutation == "duplicate":
                value["changes"].append(value["changes"][0])
            else:
                value["review"]["calls"] = 6
            with self.subTest(mutation=mutation), self.assertRaises(Blocked):
                validate(value)

    def test_new_consumer_bootstraps_without_a_default_branch_administration_workflow(self):
        for name in (".nexkit/project.json", ".nexkit/installation.json"):
            (self.root / name).unlink()
        run(["git", "add", ".nexkit"], cwd=self.root)
        run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "uninstalled base",
            ],
            cwd=self.root,
        )
        result = plan(
            self.root,
            self.result["proposal"]["project"],
            "maintenance",
            bundle=self.bundle,
            publication=PUBLICATION,
        )
        self.assertIsNone(result["proposal"]["previous_project"])
        self.assertIn("push:", result["bootstrap"])
        self.assertNotIn("workflow_dispatch:", result["bootstrap"])
        self.assertNotIn(WORKFLOW_PATH, result["proposal"]["project"]["files"])

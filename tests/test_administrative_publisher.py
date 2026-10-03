"""Credential routing, safe API failures and scoped App tokens (simulated HTTP)."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from nexkit.administrative_publisher import PERMISSIONS, jwt, publisher
from nexkit.common import Blocked
from nexkit.github import AppRedirectHandler, GitHub

PUBLICATION = {"app_id": 12345, "app_slug": "nexkit-test-publisher", "installation_id": 90}


class ApiDiagnosticsTests(unittest.TestCase):
    def test_app_jwt_uses_bearer_in_memory_without_a_cli_authorization_argument(self):
        @contextmanager
        def response():
            yield BytesIO(b'{"id":12345}')

        opener = SimpleNamespace(open=lambda *_args, **_kwargs: response())
        with (
            patch("nexkit.github.build_opener", return_value=opener),
            patch("nexkit.github.run") as cli,
        ):
            gh = GitHub("owner/project", token="jwt-secret", bearer=True)
            self.assertEqual(gh.api("app"), {"id": 12345})
            cli.assert_not_called()
        with patch("nexkit.github.build_opener") as builder:
            builder.return_value.open.return_value = response()
            gh.api("app")
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("Authorization"), "Bearer jwt-secret")
            self.assertEqual(request.full_url, "https://api.github.com/app")

    def test_app_bearer_http_error_is_sanitized_and_external_hosts_are_rejected(self):
        error = HTTPError(
            "https://api.github.com/app?secret=query-secret",
            403,
            "response-secret",
            {},
            BytesIO(b"body-secret"),
        )
        with patch("nexkit.github.build_opener") as builder:
            builder.return_value.open.side_effect = error
            with self.assertRaises(Blocked) as failed:
                GitHub("owner/project", token="jwt-secret", bearer=True).api("app")
            self.assertNotIn("secret", str(failed.exception))
            self.assertIn("GET /app", str(failed.exception))
            self.assertIn("HTTP 403", str(failed.exception))
        with self.assertRaisesRegex(Blocked, "Invalid App API host"):
            GitHub("owner/project", token="jwt-secret", bearer=True).api(
                "https://other.invalid/app"
            )

    def test_failed_request_reports_endpoint_status_and_permission_hint_without_secrets(self):
        response = SimpleNamespace(
            returncode=1,
            stdout='{"token":"response-secret"}',
            stderr="gh: authorization-secret payload-secret (HTTP 403)",
        )
        with patch("nexkit.github.run", return_value=response) as command:
            gh = GitHub("owner/project", token="credential-secret")
            with self.assertRaises(Blocked) as failed:
                gh.api(
                    "repos/owner/project/git/trees?secret=query-secret",
                    "POST",
                    {"content": "payload-secret"},
                )
        message = str(failed.exception)
        self.assertIn("POST /repos/owner/project/git/trees", message)
        self.assertIn("HTTP 403", message)
        self.assertIn("Workflows:write", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("credential-secret", str(command.call_args.args))
        self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], "credential-secret")
        self.assertNotIn("GITHUB_TOKEN", command.call_args.kwargs["env"])

    def test_not_found_remains_distinct_from_permission_denial_and_transport_failure(self):
        for stderr, expected in (
            ("gh: Not Found (HTTP 404)", "HTTP 404"),
            ("gh: denied (HTTP 403)", "HTTP 403"),
            ("transport-secret", "gh exit 1"),
        ):
            with (
                self.subTest(stderr=stderr),
                patch(
                    "nexkit.github.run",
                    return_value=SimpleNamespace(returncode=1, stdout="", stderr=stderr),
                ),
            ):
                with self.assertRaisesRegex(Blocked, expected):
                    GitHub("owner/project").api("repos/owner/project/contents/state.json")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(api=self.app_api)
        self.client = SimpleNamespace(api=self.token_api)
        self.gh = SimpleNamespace(
            repository="owner/project", root="repos/owner/project", repo=lambda: {"id": 50}
        )
        self.identity = dict(PUBLICATION, id=PUBLICATION["app_id"], slug=PUBLICATION["app_slug"])
        self.installation = {
            "id": 90,
            "app_id": 12345,
            "account": {"login": "owner"},
            "permissions": dict(PERMISSIONS),
            "suspended_at": None,
        }
        self.issued = {"token": "installation-secret", "permissions": dict(PERMISSIONS)}
        self.repositories = {
            "total_count": 1,
            "repositories": [{"id": 50, "full_name": "owner/project"}],
        }
        self.requests = []
        self.enterContext(patch("nexkit.administrative_publisher.jwt", return_value="jwt-secret"))
        self.enterContext(
            patch("nexkit.administrative_publisher.GitHub", side_effect=[self.app, self.client])
        )

    def app_api(self, path, method="GET", data=None):
        self.requests.append((path, method, deepcopy(data)))
        if path == "app":
            return self.identity
        if path.endswith("/installation"):
            return self.installation
        if path.endswith("/access_tokens"):
            return self.issued
        raise AssertionError(path)

    def token_api(self, path, method="GET", data=None):
        self.requests.append((path, method, deepcopy(data)))
        if path.startswith("installation/repositories"):
            return self.repositories
        self.assertEqual((path, method), ("installation/token", "DELETE"))

    def test_app_identity_and_actual_token_are_bound_to_one_repository_and_minimal_permissions(
        self,
    ):
        with publisher(self.gh, {"publication": PUBLICATION}) as client:
            self.assertEqual(client.binding["installation_id"], 90)
            self.assertEqual(client.binding["permissions"], PERMISSIONS)
            self.assertNotIn("secret", json.dumps(client.binding))
        self.assertEqual(
            self.requests[2],
            (
                "app/installations/90/access_tokens",
                "POST",
                {
                    "repository_ids": [50],
                    "permissions": {"contents": "write", "workflows": "write"},
                },
            ),
        )
        self.assertEqual(self.requests[-1][:2], ("installation/token", "DELETE"))

    def test_wrong_app_and_unavailable_installation_permissions_stop_before_token_issuance(self):
        self.identity["id"] = 54321
        with self.assertRaisesRegex(Blocked, "another GitHub App"):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("wrong App accepted")
        self.assertFalse(any(method == "POST" for _, method, _ in self.requests))

    def test_missing_workflow_permission_stops_before_token_issuance(self):
        self.installation["permissions"].pop("workflows")
        with self.assertRaisesRegex(Blocked, "Workflows:write"):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("missing permission accepted")
        self.assertFalse(any(method == "POST" for _, method, _ in self.requests))

    def test_suspended_or_different_owner_installation_cannot_issue_token(self):
        self.installation["account"]["login"] = "other"
        with self.assertRaises(Blocked):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("different installation accepted")

    def test_suspended_installation_is_rejected_before_token_issuance(self):
        self.installation["suspended_at"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(Blocked):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("suspended installation accepted")
        self.assertFalse(any(method == "POST" for _, method, _ in self.requests))

    def test_reinstalled_app_requires_a_fresh_installation_binding(self):
        self.installation["id"] = 91
        with self.assertRaises(Blocked):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("replacement installation accepted")
        self.assertFalse(any(method == "POST" for _, method, _ in self.requests))

    def test_excess_token_permissions_are_rejected_and_revoked(self):
        self.issued["permissions"]["administration"] = "write"
        with self.assertRaisesRegex(Blocked, "unexpected permissions"):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("excess token permissions accepted")
        self.assertEqual(self.requests[-1][:2], ("installation/token", "DELETE"))

    def test_implicit_metadata_read_need_not_appear_in_the_token_response(self):
        self.issued["permissions"].pop("metadata")
        self.installation["permissions"].pop("metadata")
        with publisher(self.gh, {"publication": PUBLICATION}) as client:
            self.assertEqual(client.binding["permissions"], PERMISSIONS)

    def test_another_repository_or_multiple_repositories_are_rejected_and_revoked(self):
        self.repositories["total_count"] = 2
        with self.assertRaisesRegex(Blocked, "one repository"):
            with publisher(self.gh, {"publication": PUBLICATION}):
                self.fail("broad token accepted")
        self.assertEqual(self.requests[-1][:2], ("installation/token", "DELETE"))

    def test_publication_failure_still_revokes_the_ephemeral_token(self):
        with self.assertRaisesRegex(Blocked, "publication denied"):
            with publisher(self.gh, {"publication": PUBLICATION}):
                raise Blocked("publication denied")
        self.assertEqual(self.requests[-1][:2], ("installation/token", "DELETE"))

    def test_missing_key_does_not_fall_back_to_the_administrators_login(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(Blocked, "credential unavailable"),
        ):
            jwt(PUBLICATION)


class NativeSigningTests(unittest.TestCase):
    def test_actual_http_redirect_does_not_forward_an_app_credential(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302 if self.path == "/app" else 200)
                if self.path == "/app":
                    self.send_header("Location", "/redirect-target")
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/app",
                headers={"Authorization": "Bearer test-only-credential"},
            )
            with self.assertRaises(HTTPError) as error:
                build_opener(AppRedirectHandler()).open(request, timeout=5)
            self.assertEqual(error.exception.code, 302)
            error.exception.close()
            self.assertEqual(paths, ["/app"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL native signing unavailable")
    def test_actual_openssl_signs_a_bounded_jwt_without_persisting_the_private_key(self):
        with tempfile.TemporaryDirectory(prefix="nexkit-native-app-signing-") as temporary:
            root = Path(temporary)
            key, public, signature, data = (
                root / name for name in ("key.pem", "public.pem", "signature", "data")
            )
            subprocess.run(
                ["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True
            )
            subprocess.run(
                ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)],
                check=True,
                capture_output=True,
            )
            with patch.dict(
                os.environ,
                {"NEXKIT_ADMIN_APP_PRIVATE_KEY_FILE": str(key), "NEXKIT_ADMIN_APP_PRIVATE_KEY": ""},
            ):
                token = jwt(PUBLICATION)
            head, payload, signed = token.split(".")
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            self.assertEqual(claims["iss"], 12345)
            self.assertEqual(claims["exp"] - claims["iat"], 600)
            data.write_bytes((head + "." + payload).encode())
            signature.write_bytes(base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4)))
            verified = subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public),
                    "-signature",
                    str(signature),
                    str(data),
                ],
                capture_output=True,
            )
            self.assertEqual(verified.returncode, 0)

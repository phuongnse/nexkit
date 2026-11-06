import json
import subprocess
import tempfile
import threading
import unittest
from base64 import urlsafe_b64decode
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

from nexkit import app, scaffold
from nexkit.github import GitHubError
from tests.support import APP


def decoded(part):
    return urlsafe_b64decode(part + "=" * (-len(part) % 4))


class Client:
    """Answers what GitHub answers an App, by path."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, repository, token):
        self.token = token
        return self

    def request(self, method, path):
        found = self.answers[path]
        if isinstance(found, Exception):
            raise found
        return found


class ManifestTests(unittest.TestCase):
    def test_the_app_is_private_with_exactly_what_nexkit_needs(self):
        found = app.manifest("acme", "http://127.0.0.1:9/done")
        self.assertFalse(found["public"])
        self.assertEqual(found["redirect_url"], "http://127.0.0.1:9/done")
        self.assertEqual(found["default_events"], [])
        self.assertFalse(found["hook_attributes"]["active"])
        self.assertEqual(
            found["default_permissions"],
            {
                "actions": "write",
                "checks": "read",
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
                "statuses": "write",
                "workflows": "write",
            },
        )
        self.assertEqual(len(app.manifest("a" * 60, "x")["name"]), app.NAME_LIMIT)

    def test_the_form_goes_to_the_owner_of_the_repository(self):
        page = app.form("acme", False, "s1", "http://x/done")
        self.assertIn("action='https://github.com/settings/apps/new?state=s1'", page)
        page = app.form("acme", True, "s1", "http://x/done")
        self.assertIn(
            "action='https://github.com/organizations/acme/settings/apps/new?state=s1'", page
        )
        self.assertNotIn('"public"', page)  # escaped for the attribute

    def test_the_flow_takes_the_code_only_with_its_state(self):
        answers, threads = [], []

        def browser(address):
            def visit():
                page = urlopen(address, timeout=5).read().decode()
                state = page.split("state=")[1].split("'")[0]
                with self.assertRaises(OSError) as refused:
                    urlopen(f"{address}done?code=forged&state=wrong", timeout=5)
                refused.exception.close()
                answers.append(urlopen(f"{address}done?code=c0de&state={state}", timeout=5).read())

            threads.append(threading.Thread(target=visit))
            threads[0].start()

        self.assertEqual(app.receive_code("acme", False, opener=browser, seconds=10), "c0de")
        threads[0].join()
        self.assertIn(b"Go back to the terminal", answers[0])

    def test_a_flow_that_nobody_finishes_ends(self):
        with self.assertRaisesRegex(app.AppError, "did not answer in time"):
            app.receive_code("acme", False, opener=lambda url: None, seconds=0)


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.set = mock.patch.object(app, "set_secret").start()
        self.addCleanup(mock.patch.stopall)
        self.request = mock.patch.object(
            app.GitHub,
            "request",
            return_value={"id": 7, "slug": "nexkit-acme", "pem": "-----KEY-----"},
        ).start()

    def test_it_exchanges_the_code_and_sets_both_secrets(self):
        slug = app.create("acme/app", False, "t", flow=lambda owner, org: "c0de")
        self.assertEqual(slug, "nexkit-acme")
        self.request.assert_called_once_with("POST", "/app-manifests/c0de/conversions")
        self.assertEqual(
            self.set.call_args_list,
            [
                mock.call("acme/app", "NEXKIT_APP_ID", "7"),
                mock.call("acme/app", "NEXKIT_APP_PRIVATE_KEY", "-----KEY-----"),
            ],
        )

    def test_a_key_it_could_not_store_is_lost_and_it_says_how_to_go_on(self):
        self.set.side_effect = app.AppError("Could not set the secret")
        with self.assertRaisesRegex(
            app.AppError,
            r"its key is lost: generate a new one at https://github.com/settings/apps/nexkit-acme,"
            r" then run 'nexkit init --app-id 7 --app-private-key FILE'",
        ):
            app.create("acme/app", False, "t", flow=lambda owner, org: "c0de")

    def test_github_refusing_the_code_is_said(self):
        self.request.side_effect = GitHubError(404, "Not Found")
        with self.assertRaisesRegex(app.AppError, "GitHub did not create the App"):
            app.create("acme/app", False, "t", flow=lambda owner, org: "c0de")


class ExistingAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key = Path(cls.tmp.name) / "key.pem"
        subprocess.run(
            ["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_token_proves_the_key(self):
        token = app.jwt(7, self.key, now=lambda: 1000)
        header, claims, signature = token.split(".")
        self.assertEqual(json.loads(decoded(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(decoded(claims)), {"iat": 940, "exp": 1540, "iss": "7"})
        public = Path(self.tmp.name) / "key.pub"
        subprocess.run(
            ["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(public)],
            check=True,
            capture_output=True,
        )
        sig = Path(self.tmp.name) / "sig"
        sig.write_bytes(decoded(signature))
        check = ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(sig)]
        done = subprocess.run(check, input=f"{header}.{claims}".encode(), capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_file_that_is_no_key_is_said(self):
        other = Path(self.tmp.name) / "other"
        other.write_text("hello")
        with self.assertRaisesRegex(app.AppError, "is not an App's private key"):
            app.jwt(7, other)

    def test_an_installed_app_gets_its_secrets(self):
        client = Client(
            {
                "/app": {"slug": "nexkit-acme"},
                "/repos/acme/app/installation": {"permissions": app.PERMISSIONS},
            }
        )
        with mock.patch.object(app, "set_secret") as secret:
            found = app.use_existing("acme/app", "7", self.key, client)
        self.assertEqual(found, ("nexkit-acme", []))
        self.assertEqual(
            [call.args[1] for call in secret.call_args_list],
            ["NEXKIT_APP_ID", "NEXKIT_APP_PRIVATE_KEY"],
        )
        self.assertIn("PRIVATE KEY", secret.call_args_list[1].args[2])

    def test_an_app_that_is_not_installed_gets_no_secrets(self):
        client = Client(
            {
                "/app": {"slug": "nexkit-acme"},
                "/repos/acme/app/installation": GitHubError(404, "Not Found"),
            }
        )
        with mock.patch.object(app, "set_secret") as secret:
            with self.assertRaisesRegex(
                app.AppError,
                "not installed on acme/app: install it at "
                "https://github.com/apps/nexkit-acme/installations/new",
            ):
                app.use_existing("acme/app", "7", self.key, client)
        secret.assert_not_called()

    def test_an_id_that_does_not_fit_the_key_is_said(self):
        client = Client({"/app": GitHubError(401, "bad credentials")})
        with self.assertRaisesRegex(app.AppError, "does not accept this App id and key"):
            app.inspect("acme/app", "7", self.key, client)

    def test_it_says_which_permissions_the_installation_lacks(self):
        client = Client(
            {
                "/app": {"slug": "nexkit-acme"},
                "/repos/acme/app/installation": {"permissions": {"contents": "write"}},
            }
        )
        with mock.patch.object(app, "set_secret"):
            _, lacking = app.use_existing("acme/app", "7", self.key, client)
        self.assertIn("workflows: write", lacking)
        self.assertNotIn("contents: write", lacking)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.root = Path(self.tmp.name)
        self.repository = mock.patch.object(
            scaffold, "repository_of", return_value="acme/app"
        ).start()
        mock.patch.object(scaffold, "_gh", return_value="User\n").start()
        mock.patch.object(scaffold, "gh_token", return_value="t").start()

    def test_no_app_yet_creates_one_and_says_to_install_it(self):
        with mock.patch.object(app, "create", return_value="nexkit-acme") as create:
            found, lines = scaffold.github_app(self.root)
        create.assert_called_once_with("acme/app", False, "t")
        self.assertEqual(found, APP)
        self.assertIn("https://github.com/apps/nexkit-acme/installations/new", lines[0])

    def test_an_existing_app_is_checked(self):
        with mock.patch.object(app, "use_existing", return_value=("nexkit-acme", ["x: write"])):
            found, lines = scaffold.github_app(self.root, app_id="7", key_file="k.pem")
        self.assertEqual(found, APP)
        self.assertIn("It lacks x: write", lines[1])

    def test_an_app_with_secrets_set_already_needs_only_its_slug(self):
        with mock.patch.object(app, "create") as create:
            self.assertEqual(scaffold.github_app(self.root, slug="nexkit-acme"), (APP, []))
        create.assert_not_called()

    def test_a_repository_with_an_app_keeps_it(self):
        path = self.root / ".nexkit/config.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"github_app": {**APP, "slug": "kept"}}))
        with mock.patch.object(app, "create") as create:
            found, _ = scaffold.github_app(self.root)
        create.assert_not_called()
        self.assertEqual(found["slug"], "kept")

    def test_an_id_needs_its_key(self):
        with self.assertRaisesRegex(app.AppError, "go together"):
            scaffold.github_app(self.root, app_id="7")
        with self.assertRaisesRegex(app.AppError, "go together"):
            scaffold.github_app(self.root, key_file="k.pem")

    def test_without_a_github_remote_only_a_named_app_works(self):
        self.repository.return_value = None
        with self.assertRaisesRegex(app.AppError, "Cannot tell the GitHub repository"):
            scaffold.github_app(self.root)

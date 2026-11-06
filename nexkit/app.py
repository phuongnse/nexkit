"""The GitHub App that NexKit acts as: what it may do, how `nexkit init` creates one with
GitHub's App manifest flow, and how `nexkit init` and `nexkit doctor` check one.

NexKit has no server, so it publishes no App of its own: the owner of a repository creates
one under their account and installs it on every repository that uses NexKit. The jobs of
the pipeline mint short-lived tokens from its private key; nothing here runs in them.
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import subprocess
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import APP_DOCS, APP_SECRETS
from .github import GitHub, GitHubError

HOME = "https://github.com/phuongnse/nexkit"
# What the pipeline's jobs ask for, at most: each job mints a token with only what it needs.
PERMISSIONS = {
    "actions": "write",  # start rounds and sweeps, stop rounds, read runs
    "checks": "read",  # the failures of required checks and of runs on the default branch
    "contents": "write",  # push branches, merge pull requests
    "issues": "write",  # comments, labels, reactions, closing and opening issues
    "pull_requests": "write",  # open, update and review pull requests
    "statuses": "write",  # nexkit/checks and nexkit/review
    "workflows": "write",  # push and merge changes to .github/workflows/
}
LEVELS = {"read": 1, "write": 2}
# GitHub's limit on an App's name.
NAME_LIMIT = 34
# How long `nexkit init` waits for the person to confirm the new App in the browser.
FLOW_SECONDS = 600
# GitHub refuses a JWT that lives longer than ten minutes, or that starts in its future.
JWT_SECONDS = 540
JWT_SKEW = 60


class AppError(RuntimeError):
    pass


def settings(slug):
    """The `github_app` of the configuration, with the secrets' usual names."""
    return {"slug": slug, **APP_SECRETS}


def install_url(slug):
    return f"https://github.com/apps/{slug}/installations/new"


def app_url(slug, owner=None, organization=False):
    """The App's settings page, with its id and its private keys. It is not the page of the
    App's installation, which GitHub links from a repository and from the list of Apps."""
    where = f"organizations/{owner}/settings" if organization else "settings"
    return f"https://github.com/{where}/apps/{slug}"


def settings_url(slug, owner=None, organization=False):
    """Where the owner changes the App's permissions."""
    return f"{app_url(slug, owner, organization)}/permissions"


def key_steps(slug, owner, organization=False):
    """Where a repository gets the App's id and a new private key, and the command that
    sets them. GitHub shows a key once and no one can read a secret back."""
    return (
        f"The App ID is under *About* at {app_url(slug, owner, organization)}, and *Generate a "
        "private key* is under *Private keys* near the bottom of that page, not on the "
        f"installation's page. Install the App at {install_url(slug)}, then run 'nexkit init "
        "--app-id ID --app-private-key FILE', which sets both secrets"
    )


def missing(granted):
    """The permissions NexKit needs that `granted` lacks, as `name: level`."""
    return [
        f"{name}: {level}"
        for name, level in PERMISSIONS.items()
        if LEVELS.get((granted or {}).get(name), 0) < LEVELS[level]
    ]


def default_name(owner):
    """A name for the owner's App. Names are unique across GitHub, so a plain "NexKit" is
    taken; the person can change it on GitHub's page, or with `nexkit init --app-name`."""
    return f"NexKit {owner}"


def manifest(owner, redirect_url, name=None):
    """The App that the manifest flow creates: private, so only its owner can install it,
    with exactly NexKit's permissions and no webhook."""
    return {
        "name": (name or default_name(owner))[:NAME_LIMIT],
        "url": HOME,
        "description": "Acts for NexKit in the repositories of this account.",
        "hook_attributes": {"url": HOME, "active": False},
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def form(owner, organization, state, redirect_url, name=None):
    """The page that posts the manifest to GitHub, which accepts it only from a form."""
    where = f"organizations/{owner}/settings" if organization else "settings"
    action = f"https://github.com/{where}/apps/new?state={state}"
    value = html.escape(json.dumps(manifest(owner, redirect_url, name)), quote=True)
    return (
        "<!doctype html><title>NexKit</title><body onload='document.forms[0].submit()'>"
        f"<form action='{html.escape(action, quote=True)}' method='post'>"
        f"<input type='hidden' name='manifest' value='{value}'>"
        "<button type='submit'>Create the GitHub App on GitHub</button></form>"
    )


class _Flow(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/":
            self._page(200, self.server.page)
        elif url.path == "/done" and query.get("state") == [self.server.state]:
            self.server.code = (query.get("code") or [None])[0]
            self._page(200, "The App exists. Go back to the terminal.")
        else:
            self._page(404, "Not found")

    def _page(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def receive_code(owner, organization, opener=webbrowser.open, seconds=FLOW_SECONDS, name=None):
    """Send the person through GitHub's page that creates the App, with a page on this
    machine that posts the manifest and takes the code GitHub answers with. The state
    keeps out an answer that did not come from this flow."""
    server = HTTPServer(("127.0.0.1", 0), _Flow)
    try:
        address = f"http://127.0.0.1:{server.server_port}"
        server.state = secrets.token_urlsafe(16)
        server.code = None
        server.page = form(owner, organization, server.state, f"{address}/done", name)
        server.timeout = 1
        print(f"Create the GitHub App in your browser: {address}/", flush=True)
        print(
            "GitHub's page lets you change its name, which must be unique across GitHub. "
            f"This waits {seconds // 60} minutes.",
            flush=True,
        )
        opener(f"{address}/")
        deadline = time.monotonic() + seconds
        while server.code is None and time.monotonic() < deadline:
            server.handle_request()
        if server.code is None:
            raise AppError("GitHub did not answer in time; run 'nexkit init' again")
        return server.code
    finally:
        server.server_close()


def set_secret(repository, name, value):
    """Set a repository secret through the GitHub CLI, which encrypts it. The value goes
    through stdin, so it is on no command line."""
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", name, "--repo", repository],
            input=value,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise AppError("The GitHub CLI is missing; install it to set secrets") from None
    if proc.returncode:
        raise AppError(f"Could not set the secret {name}: {proc.stderr.strip()[-300:]}")


def set_secrets(repository, app_id, key):
    set_secret(repository, APP_SECRETS["app_id_secret"], str(app_id))
    set_secret(repository, APP_SECRETS["private_key_secret"], key)


def create(repository, organization, token, flow=receive_code, name=None):
    """Create an App with the manifest flow and set its secrets. GitHub shows the private
    key only here, so it goes straight into the secret and nowhere else."""
    owner = repository.split("/")[0]
    code = flow(owner, organization, name=name) if name else flow(owner, organization)
    try:
        found = GitHub(repository=repository, token=token).request(
            "POST", f"/app-manifests/{code}/conversions"
        )
    except GitHubError as exc:
        raise AppError(f"GitHub did not create the App: {exc}") from None
    try:
        set_secrets(repository, found["id"], found["pem"])
    except AppError as exc:
        where = app_url(found["slug"], owner, organization)
        raise AppError(
            f"{exc}. The App {found['slug']} exists, but its key is lost: generate a new one at "
            f"{where}, then run 'nexkit init --app-id {found['id']} --app-private-key FILE'"
        ) from None
    return found["slug"]


def _encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt(app_id, key_file, now=time.time):
    """A token that proves to hold the App's key. The standard library cannot sign with
    RSA, so `openssl` does; the key stays in its file."""
    issued = int(now())
    claims = {"iat": issued - JWT_SKEW, "exp": issued + JWT_SECONDS, "iss": str(app_id)}
    header = _encoded(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    signed = f"{header}.{_encoded(json.dumps(claims).encode())}"
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(key_file)],
            input=signed.encode(),
            capture_output=True,
        )
    except FileNotFoundError:
        raise AppError("openssl is missing; it signs the request that checks the App") from None
    if proc.returncode:
        raise AppError(f"{key_file} is not an App's private key")
    return f"{signed}.{_encoded(proc.stdout)}"


def inspect(repository, app_id, key_file, client=GitHub):
    """What GitHub says about the App with this key: {slug, granted, installed,
    permissions}, where `granted` are the App's own permissions and `permissions` those of
    its installation on `repository`, which lag until the owner accepts new ones. Only the
    App itself may read a private App or ask where it is installed: GitHub refuses the
    GitHub CLI's token."""
    gh = client(repository=repository, token=jwt(app_id, key_file))
    try:
        found = gh.request("GET", "/app")
    except GitHubError as exc:
        raise AppError(f"GitHub does not accept this App id and key: {exc}") from None
    state = {"slug": found["slug"], "granted": found.get("permissions") or {}}
    try:
        installed = gh.request("GET", f"/repos/{repository}/installation")
    except GitHubError as exc:
        if exc.status != 404:
            raise AppError(f"Could not read the App's installation: {exc}") from None
        return {**state, "installed": False, "permissions": {}}
    return {**state, "installed": True, "permissions": installed.get("permissions") or {}}


def use_existing(repository, app_id, key_file, client=GitHub):
    """Check that the App is installed on `repository`, then set its secrets. Returns its
    slug and what it lacks."""
    found = inspect(repository, app_id, key_file, client)
    if not found["installed"]:
        raise AppError(
            f"The App {found['slug']} is not installed on {repository}: install it at "
            f"{install_url(found['slug'])}, then run this again (see {APP_DOCS})"
        )
    set_secrets(repository, app_id, Path(key_file).read_text())
    return found["slug"], missing(found["permissions"])

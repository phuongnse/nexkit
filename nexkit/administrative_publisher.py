"""Short-lived, repository-scoped GitHub App authority for administrative Git writes."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .common import Blocked, canonical
from .github import GitHub
from .policy import require

PERMISSIONS = {"contents": "write", "workflows": "write", "metadata": "read"}
KEY_ENV = "NEXKIT_ADMIN_APP_PRIVATE_KEY"
KEY_FILE_ENV = "NEXKIT_ADMIN_APP_PRIVATE_KEY_FILE"


def token_permissions(value):
    # Metadata read is automatic and may be omitted from GitHub's response.
    return (
        isinstance(value, dict)
        and {"contents", "workflows"} <= set(value) <= set(PERMISSIONS)
        and all(value.get(name) == "write" for name in ("contents", "workflows"))
        and value.get("metadata", "read") == "read"
    )


def validate_publication(value):
    require(
        isinstance(value, dict)
        and set(value) == {"app_id", "app_slug", "installation_id"}
        and type(value["app_id"]) is int
        and value["app_id"] > 0
        and type(value["installation_id"]) is int
        and value["installation_id"] > 0
        and isinstance(value["app_slug"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value["app_slug"]),
        "Administrative publication needs an exact GitHub App ID, slug and installation ID; prepare a fresh proposal with --publisher-app-id, --publisher-app-slug and --publisher-installation-id",
    )


def jwt(publication):
    """OpenSSL signs the private key locally; no key or JWT is a command argument."""
    key = os.environ.get(KEY_ENV)
    if not key and os.environ.get(KEY_FILE_ENV):
        try:
            key = Path(os.environ[KEY_FILE_ENV]).read_text(encoding="utf-8")
        except OSError as exc:
            raise Blocked("Cannot read the administrative App private key file") from exc
    require(
        key and len(key.encode()) <= 20000,
        f"Administrative publication credential unavailable. Configure {KEY_ENV} as the repository secret; for local --online preflight set {KEY_FILE_ENV} to a protected App private key file. An administrator's gh login or a personal token cannot replace this credential.",
    )

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    stamp = int(time.time())
    data = b".".join(
        encode(canonical(value).encode())
        for value in (
            {"alg": "RS256", "typ": "JWT"},
            {"iat": stamp - 60, "exp": stamp + 540, "iss": publication["app_id"]},
        )
    )
    with tempfile.TemporaryDirectory(prefix="nexkit-admin-key-") as temporary:
        path = Path(temporary) / "key.pem"
        with open(
            path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)
        ) as target:
            target.write(key)
        try:
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", str(path)],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked("Administrative App signing requires a working OpenSSL command") from exc
        require(
            result.returncode == 0 and result.stdout,
            "Administrative App private key signing failed",
        )
    return (data + b"." + encode(result.stdout)).decode()


@contextmanager
def publisher(gh, proposal):
    """Prove App identity and effective token scope, then revoke it after use."""
    publication = proposal.get("publication")
    validate_publication(publication)
    app = GitHub(gh.repository, token=jwt(publication), bearer=True)
    identity = app.api("app")
    require(
        identity.get("id") == publication["app_id"]
        and identity.get("slug") == publication["app_slug"],
        "Administrative private key belongs to another GitHub App",
    )
    installation = app.api(f"{gh.root}/installation")
    require(
        type(installation.get("id")) is int
        and installation["id"] == publication["installation_id"]
        and installation.get("app_id") == publication["app_id"]
        and installation.get("account", {}).get("login", "").lower()
        == gh.repository.split("/")[0].lower()
        and installation.get("suspended_at") is None
        and all(
            installation.get("permissions", {}).get(name) == "write"
            for name in ("contents", "workflows")
        ),
        "Install the declared administrative App on this repository with Contents:write and Workflows:write, without a ruleset bypass",
    )
    repository_id = gh.repo()["id"]
    issued = app.api(
        f"app/installations/{installation['id']}/access_tokens",
        "POST",
        {
            "repository_ids": [repository_id],
            "permissions": {"contents": "write", "workflows": "write"},
        },
    )
    require(
        isinstance(issued.get("token"), str) and issued["token"],
        "GitHub did not issue an administrative installation token",
    )
    client = GitHub(gh.repository, token=issued["token"])
    try:
        require(
            token_permissions(issued.get("permissions")),
            "Administrative token has unexpected permissions",
        )
        repositories = client.api("installation/repositories?per_page=100")
        require(
            repositories.get("total_count") == 1
            and [
                (item.get("id"), item.get("full_name", "").lower())
                for item in repositories.get("repositories", [])
            ]
            == [(repository_id, gh.repository.lower())],
            "Administrative token must be restricted to this one repository",
        )
        client.binding = {
            **publication,
            "installation_id": installation["id"],
            "repository_id": repository_id,
            "permissions": dict(PERMISSIONS),
        }
        yield client
    finally:
        try:
            client.api("installation/token", "DELETE")
        except Blocked as exc:
            # Preserve publication/usage state and the original failure. Tokens
            # also expire natively; expose only the sanitized API diagnostic.
            print("Administrative token revocation failed: " + str(exc), file=sys.stderr)

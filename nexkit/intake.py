"""GitHub's FIFO workflow concurrency serializes work item creation and retries."""

import json
import os

from .cli import create_request
from .common import canonical, read_json
from .github import GitHub
from .policy import HUMAN_PERMISSIONS, config, require
from .release import candidate


def main():
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    event = read_json(os.environ["GITHUB_EVENT_PATH"])
    require(
        os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch",
        "Intake only accepts dispatch events",
    )
    require(
        gh.permission(os.environ["GITHUB_ACTOR"]) in HUMAN_PERMISSIONS,
        "Only an authorized project collaborator can submit work",
    )
    branch = gh.repo()["default_branch"]
    require(
        os.environ["GITHUB_REF"] == "refs/heads/" + branch, "Intake must run on the default branch"
    )
    cfg = config(gh.read_config(gh.ref(branch)))
    inputs = event["inputs"]
    payload = json.loads(inputs["payload"])
    require(isinstance(payload, dict), "Invalid intake payload")
    if inputs["operation"] == "request":
        require(set(payload) == {"title", "request", "key"}, "Invalid request fields")
        require(all(isinstance(v, str) and v for v in payload.values()), "Invalid request values")
        result = create_request(gh, payload["title"], payload["request"], payload["key"])
        gh.dispatch("nexkit-clarify.yml", branch, {"issue": result["issue"]["number"]})
    else:
        require(
            inputs["operation"] == "release" and set(payload) == {"commit", "version", "notes"},
            "Invalid release submission",
        )
        result = candidate(gh, cfg, payload["commit"], payload["version"], payload["notes"])
    print(canonical(result))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        issue = result["issue"]
        url = issue["html_url"] if isinstance(issue, dict) else issue
        with open(summary, "a") as out:
            out.write(f"NexKit work item: {url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

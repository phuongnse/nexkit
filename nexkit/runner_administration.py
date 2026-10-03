"""Explicit host decisions for a stopped consumer runner; no registration or login reset."""

from __future__ import annotations

import json
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .administration import WORKFLOW_PATH, validate
from .common import digest, read_regular_json, run
from .policy import SHA, require
from .runner_host import runner_identity


def admission(binding, cfg, proposal, sha, minutes):
    require(
        binding.get("repository") == cfg["repository"]
        and binding.get("default_branch") == cfg["default_branch"]
        and binding.get("isolation") == "container-v1",
        "Runner admission belongs to another consumer",
    )
    validate(proposal)
    require(
        proposal["repository"] == cfg["repository"]
        and proposal["pipeline"] == cfg["binding"]["pipeline"]
        and isinstance(sha, str)
        and SHA.fullmatch(sha),
        "Administrative runner proposal/revision differs",
    )
    require(
        type(minutes) is int and 1 <= minutes <= 1440,
        "Administrative runner admission lasts 1..1440 minutes",
    )
    value = deepcopy(binding)
    value["administration"] = [
        {
            "proposal": digest(proposal),
            "workflow_sha": sha,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
        }
    ]
    return value


def operate(operation, cfg, *, proposal=None, sha=None, minutes=120, apply=False):
    require(operation in {"admit-administration", "rebind"}, "Unknown runner admission operation")
    name, directory, _ = runner_identity(cfg)
    root = Path(directory)
    bound = read_regular_json(root / "runner.json")
    require(
        bound.get("repository") == cfg["repository"]
        and bound.get("default_branch") == cfg["default_branch"]
        and bound.get("isolation") == "container-v1"
        and bound.get("agent") == cfg["engine"]["name"],
        "Runner binding belongs to another consumer",
    )
    value = (
        admission(bound, cfg, proposal, sha, minutes)
        if operation == "admit-administration"
        else {
            **bound,
            "workflows": [Path(path).name for path in cfg["binding"]["agent_workflows"]],
            "administration": [],
        }
    )
    result = {
        "applied": apply,
        "runner": name,
        "binding": value,
        "registration_preserved": True,
        "login_preserved": True,
        "next": "Start the runner after applying; remove temporary admission with rebind after the administrative merge.",
    }
    if not apply:
        return result
    require(
        run(["systemctl", "is-active", name + ".service"], check=False).stdout.strip()
        not in {"active", "activating"},
        "Stop the consumer runner before changing its immutable admission",
    )
    image = run(["docker", "container", "inspect", "--format", "{{.Image}}", name]).stdout.strip()
    require(
        image == bound.get("image") and image.startswith("sha256:"),
        "Runner image differs from its accepted binding",
    )
    if operation == "admit-administration":
        # Probe behavior in the existing immutable image without any credentials.
        # Older hooks cannot admit the new branch; an image update is a distinct
        # host maintenance operation, never an implicit broad workflow bypass.
        reference = "refs/heads/nexkit/setup-" + digest(proposal)[:24]
        env = {
            "GITHUB_REPOSITORY": cfg["repository"],
            "GITHUB_REF": reference,
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_WORKFLOW_REF": f"{cfg['repository']}/{WORKFLOW_PATH}@{reference}",
            "GITHUB_WORKFLOW_SHA": sha,
            "GITHUB_SHA": sha,
        }
        program = "import importlib.util,json,sys; spec=importlib.util.spec_from_file_location('hook','/opt/nexkit/job_hook.py'); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); assert module.admitted(json.loads(sys.argv[1]),json.loads(sys.argv[2]))"
        probe = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python3",
                image,
                "-I",
                "-c",
                program,
                json.dumps(value),
                json.dumps(env),
            ],
            check=False,
        )
        require(
            probe.returncode == 0,
            "The installed image lacks exact administrative admission; update the reviewed runner image before applying this binding",
        )
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as staged:
        json.dump(value, staged, indent=2)
        staged.write("\n")
        staged.flush()
        run(
            [
                "sudo",
                "-n",
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "644",
                staged.name,
                str(root / "runner.json"),
            ]
        )
    return result

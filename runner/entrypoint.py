"""Bootstrap the official repo-scoped runner once, then execute its own service."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

os.chdir("/opt/actions-runner")
if not Path(".runner").exists():
    bootstrap = json.loads(sys.stdin.readline(16000))
    binding = json.loads(Path("/etc/nexkit/runner.json").read_text())
    label = bootstrap["label"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", label):
        raise SystemExit("Invalid runner label")
    env = dict(os.environ)
    env["ACTIONS_RUNNER_INPUT_TOKEN"] = bootstrap.pop("registration_token")
    result = subprocess.run(
        [
            "./config.sh",
            "--unattended",
            "--url",
            "https://github.com/" + binding["repository"],
            "--name",
            label,
            "--labels",
            label,
            "--work",
            "_work",
            "--disableupdate",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    env.clear()
    if result.returncode:
        raise SystemExit(
            "Runner registration failed; request a fresh repository registration token"
        )
    print("Repository-scoped runner registered", flush=True)
    del result
# Registration credentials are neither retained in this process nor inherited
# by the official listener. No host GitHub token enters the container.
with open(os.devnull, "rb") as empty:
    os.dup2(empty.fileno(), 0)
os.execv("/bin/bash", ["/bin/bash", "./run.sh"])

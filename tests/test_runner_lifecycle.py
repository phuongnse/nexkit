"""Real Windows parent, WSL pipes and systemd; the Actions listener is a fixture."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from nexkit.common import run
from nexkit.runner_host import windows_addresses, wsl_path, wsl_prefix

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("NEXKIT_TEST_WSL_LIFECYCLE") == "1",
    "Physical Windows/WSL service lifetime is checked in the container CI job",
)
class WslLifecycleTests(unittest.TestCase):
    def exercise(self, *, missing_heartbeat):
        name = "nexkit-lifecycle-" + uuid.uuid4().hex[:16]
        prefix = wsl_prefix("Ubuntu-24.04")
        fixture = ROOT / "tests/runner_lifecycle_probe.py"
        if missing_heartbeat:
            command = [*prefix, "python3", wsl_path(prefix, fixture), "--name", name, "--worker"]
        else:
            command = [
                sys.executable,
                "-I",
                str(fixture),
                "--name",
                name,
                "--distribution",
                "Ubuntu-24.04",
            ]

        def active():
            return (
                run(
                    [*prefix, "systemctl", "is-active", name + ".service"], check=False
                ).stdout.strip()
                == "active"
            )

        with tempfile.TemporaryFile() as log:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log)
            try:
                if missing_heartbeat:
                    child.stdin.write(
                        (json.dumps({"windows_host_ipv4": windows_addresses()}) + "\n").encode()
                    )
                    child.stdin.flush()
                deadline = time.monotonic() + 60
                while not active():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        self.fail(log.read().decode("utf-8", errors="replace")[-3000:])
                    time.sleep(0.25)
                if missing_heartbeat:
                    self.assertEqual(child.wait(timeout=30), 2)
                else:
                    # Killing the actual Windows parent closes its WSL pipe.
                    child.terminate()
                    child.wait(timeout=10)
                deadline = time.monotonic() + 40
                while active() and time.monotonic() < deadline:
                    time.sleep(0.25)
                self.assertFalse(active(), "The fixture service survived its Windows supervisor")
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                child.stdin.close()
                run([*prefix, "systemctl", "stop", name + ".service"], check=False)

    def test_windows_parent_termination_stops_the_real_wsl_service(self):
        self.exercise(missing_heartbeat=False)

    def test_missing_heartbeat_stops_the_real_wsl_service(self):
        self.exercise(missing_heartbeat=True)

"""Real Actions artifact transport with a timed fixture process and zero model calls."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from nexkit.checkpoints import artifact_prefix, download, stop_monitor, validate_payload
from nexkit.ci import collect, materialize
from nexkit.common import Blocked, canonical, digest, read_json, write_json
from nexkit.github import GitHub
from nexkit.policy import require
from tests.support import issue
from tests.test_agent_session import AcceptedSessionTests

ROOT = Path("/tmp/nexkit-transport-fixture")
DATA = Path("/tmp/nexkit")


def prepare():
    ROOT.mkdir()
    context, home, data = AcceptedSessionTests().fixture(ROOT, "deliver")
    shutil.move(data, DATA)
    context.update(
        issue=issue(),
        run_key=os.environ["GITHUB_RUN_ID"] + "." + os.environ["GITHUB_RUN_ATTEMPT"],
        fixture=True,
    )
    context["config"]["repository"] = os.environ["GITHUB_REPOSITORY"]
    context["config"]["limits"]["minutes"] = (
        2  # A one-minute fixture session, twelve-second cadence.
    )
    context["config"]["recovery"] = {"checkpoint_seconds": 10}
    runtime = read_json(DATA / "agent-runtime.json")
    runtime["context"] = digest(context)
    write_json(DATA / "agent-runtime.json", runtime)
    write_json(DATA / "context.json", context)
    write_json(
        home / "output/handover.json",
        {
            "draft": "Controlled fixture interrupted before final JSON",
            "remaining": "Recheck recovered source; no application or model acceptance is claimed.",
        },
    )


def interrupt():
    context = read_json(DATA / "context.json")
    runtime = read_json(DATA / "agent-runtime.json")
    program = "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text(\"print('checkpoint-transport-edit')\\n\"); time.sleep(60)"
    try:
        subprocess.run(
            [sys.executable, "-c", program, str(Path(runtime["workspace"]) / "app.py")],
            timeout=16,
            check=True,
        )
    except subprocess.TimeoutExpired:
        pass
    else:
        raise Blocked("Fixture process did not time out")
    try:
        collect(
            ROOT / "source",
            runtime["workspace"],
            context,
            runtime["output"],
            DATA / "report.json",
            "deliver",
            DATA / "initial.json",
        )
    except Blocked:
        pass
    else:
        raise Blocked("Missing final JSON incorrectly produced a complete report")
    stop_monitor(DATA)
    require(not (DATA / "report.json").exists(), "Interrupted execution produced passing evidence")


def finish():
    context = read_json(DATA / "context.json")
    gh = GitHub(os.environ["GITHUB_REPOSITORY"])
    artifacts = gh.api(
        f"{gh.root}/actions/runs/{os.environ['GITHUB_RUN_ID']}/artifacts?per_page=100",
        pages=True,
        collection="artifacts",
    )
    artifacts = [
        item for item in artifacts if item["name"].startswith(artifact_prefix(context, "deliver"))
    ]
    require(artifacts, "No complete checkpoint reached Actions artifact storage")
    checkpoints = [download(gh, item) for item in artifacts]
    for manifest, payload in checkpoints:
        validate_payload(manifest, payload, context, "deliver", exact_context=True)
    require(
        any(manifest["capture_point"] == "interval" for manifest, _ in checkpoints),
        "No durable interval checkpoint was uploaded before the forced timeout",
    )
    manifest, payload = max(checkpoints, key=lambda item: item[0]["sequence"])
    shutil.rmtree(ROOT / "home")  # Disk loss must not be the recovery mechanism.
    data, workspace = ROOT / "fresh-data", ROOT / "fresh-work"
    write_json(data / "recovery-input/manifest.json", manifest)
    (data / "recovery-input/payload.json").write_text(canonical(payload) + "\n", encoding="utf-8")
    materialize(ROOT / "source", workspace, context, "deliver", data)
    require(
        "checkpoint-transport-edit" in (workspace / "app.py").read_text(),
        "Durable source recovery failed in a fresh workspace",
    )
    result = {
        "actual_actions_artifact_transport": True,
        "forced_fixture_timeout": True,
        "source_restored_after_disk_loss": True,
        "complete_checkpoints": len(checkpoints),
        "model_calls": 0,
        "native_requirement_or_merge_approval": False,
    }
    print(canonical(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "interrupt", "finish"))
    arguments = parser.parse_args()
    {"prepare": prepare, "interrupt": interrupt, "finish": finish}[arguments.operation]()

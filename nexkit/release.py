"""A release decision is separate from delivery and bound to immutable source."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from .checks import execute, verify
from .common import (
    Blocked,
    canonical,
    consumer_path,
    digest,
    file_hash,
    read_json,
    run,
    safe_path,
    write_json,
)
from .delivery import RELEASE_MARKER, authorized
from .pipelines import bind_state, current_config, load_run_config, pipeline_id
from .policy import SHA, VERSION, now, require, spec_hash


def parse_candidate(issue):
    body = issue.get("body") or ""
    require(
        body.startswith(RELEASE_MARKER + "\n```json\n") and body.endswith("\n```\n"),
        "Malformed release candidate work item",
    )
    try:
        value = json.loads(body[len(RELEASE_MARKER + "\n```json\n") : -len("\n```\n")])
    except ValueError as exc:
        raise Blocked("Invalid release candidate JSON") from exc
    require(
        set(value)
        == {"schema", "commit", "version", "notes", "config", "repository"}
        | ({"pipeline"} if value.get("schema") == 2 else set()),
        "Invalid release candidate fields",
    )
    require(
        value["schema"] in (1, 2)
        and SHA.fullmatch(value["commit"])
        and VERSION.fullmatch(value["version"]),
        "Invalid release identity",
    )
    if value["schema"] == 2:
        from .pipelines import IDENTIFIER

        require(
            isinstance(value["pipeline"], str) and IDENTIFIER.fullmatch(value["pipeline"]),
            "Invalid release pipeline",
        )
    require(
        isinstance(value["notes"], str) and value["notes"].strip(), "Release notes are required"
    )
    return value


def candidate(gh, cfg, commit, version, notes):
    require(cfg["release"]["enabled"], "Release is not configured for this project")
    require(
        SHA.fullmatch(commit) and VERSION.fullmatch(version), "Use an exact commit SHA and version"
    )
    require(notes.strip(), "Release notes are required")
    base = gh.ref(cfg["default_branch"])
    comparison = gh.api(f"{gh.root}/compare/{commit}...{base}")
    require(
        comparison["status"] in ("ahead", "identical"),
        "Candidate is not merged into the default branch",
    )
    current_config(gh, base, cfg)
    value = {
        "schema": 1,
        "commit": commit,
        "version": version,
        "notes": notes,
        "config": digest(cfg),
        "repository": gh.repository,
    }
    if pipeline_id(cfg) is not None:
        value.update(schema=2, pipeline=pipeline_id(cfg))
    body = (
        RELEASE_MARKER + "\n```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```\n"
    )
    matches = [
        i
        for i in gh.api(f"{gh.root}/issues?state=all&per_page=100", pages=True)
        if not i.get("pull_request") and i.get("body") == body
    ]
    require(len(matches) <= 1, "Duplicate release candidate work items")
    issue = (
        matches[0]
        if matches
        else gh.api(
            f"{gh.root}/issues", "POST", {"title": f"Release candidate {version}", "body": body}
        )
    )
    return {
        "issue": issue["html_url"],
        "candidate": value,
        "human_comment": "/nexkit release " + spec_hash(issue),
    }


def prepare_release(gh, number, run_key, kit_ref, *, pipeline=None):
    issue = gh.issue(number)
    if not (issue.get("body") or "").startswith(RELEASE_MARKER):
        return {"ready": False, "reason": "Not a release candidate"}
    if parse_candidate(issue).get("pipeline") != pipeline:
        return {"ready": False, "reason": "Candidate belongs to another pipeline"}
    state, revision = gh.get_state(number)
    if state.get("status") == "released":
        return {"ready": False, "reason": "Already released", "state": state}
    try:
        issue, approved = authorized(gh, number, release=True)
        value = parse_candidate(issue)
        base = gh.ref(gh.repo()["default_branch"])
        cfg = load_run_config(gh, base, pipeline, "release")
        bind_state(state, cfg)
        require(cfg["release"]["enabled"], "Release is disabled")
        require(cfg["kit"]["ref"] == kit_ref, "Release kit revision changed")
        require(
            value["config"] == digest(cfg), "Release configuration drift; prepare a new candidate"
        )
        require(value["repository"] == gh.repository, "Candidate belongs to another repository")
        comparison = gh.api(f"{gh.root}/compare/{value['commit']}...{base}")
        require(
            comparison["status"] in ("ahead", "identical"),
            "Release source is no longer on the default branch",
        )
        manifest = state.get("manifest")
        if manifest:
            require(
                manifest.get("candidate") == digest(value),
                "Partial release belongs to another candidate; use a new work item",
            )
        require(state.get("run_key") != run_key, "Duplicate release run")
        # Retries of a partial release are bounded just like delivery; never
        # silently reset the budget or change a previously selected candidate.
        require(state.get("attempts", 0) < cfg["limits"]["attempts"], "Release attempts exhausted")
        state.setdefault("started_at", now())
        release_deadline(state, cfg)
        state.update(
            status="release-building",
            run_key=run_key,
            release=value,
            attempts=state.get("attempts", 0) + 1,
            approval=approved,
            updated_at=now(),
        )
        gh.save_state(number, state, revision)
        return {
            "ready": True,
            "repository": gh.repository,
            "issue": issue,
            "approval": approved,
            "release": value,
            "config": cfg,
            "run_key": run_key,
            "build_run_key": manifest["run_key"] if manifest else run_key,
            "reuse_build": bool(manifest),
        }
    except Blocked as exc:
        state.update(status="blocked", reason=str(exc), updated_at=now())
        gh.save_state(number, state, revision)
        return {"ready": False, "reason": str(exc)}


def release_deadline(state, cfg):
    require(
        (
            datetime.fromisoformat(now()) - datetime.fromisoformat(state["started_at"])
        ).total_seconds()
        < cfg["limits"]["minutes"] * 60,
        "Total release time budget exhausted",
    )


def revalidate_release(gh, context):
    value, cfg = context["release"], context["config"]
    issue, approved = authorized(gh, context["issue"]["number"], release=True)
    require(
        approved == context["approval"] and parse_candidate(issue) == value,
        "Release approval/candidate changed",
    )
    base = gh.ref(cfg["default_branch"])
    current_config(gh, base, cfg)
    require(digest(cfg) == value["config"], "Release policy drift")
    comparison = gh.api(f"{gh.root}/compare/{value['commit']}...{base}")
    require(
        comparison["status"] in ("ahead", "identical"), "Release source left the default branch"
    )
    state, revision = gh.get_state(issue["number"])
    require(state.get("run_key") == context["run_key"], "Superseded release run")
    require(
        state.get("status") in ("release-building", "release-uploading", "released"),
        "Release is no longer active",
    )
    if state.get("status") != "released":
        release_deadline(state, cfg)
    return state, revision


def failed_release(gh, context, reason):
    state, revision = gh.get_state(context["issue"]["number"])
    if state.get("run_key") == context["run_key"] and state.get("status") != "released":
        state.update(status="blocked", reason=reason[:4000], updated_at=now())
        gh.save_state(context["issue"]["number"], state, revision)
    return state


def build_release(context, workspace, destination, *, command_home=None):
    if command_home is None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="nexkit-release-home-") as home:
            return build_release(context, workspace, destination, command_home=home)
    cfg, value = context["config"], context["release"]
    verification = verify(
        cfg,
        workspace,
        {"head": value["commit"], "config": value["config"]},
        command_home=command_home,
    )
    require(verification["passed"], "Approved release source failed verification")
    command = [
        part.replace("{version}", value["version"]).replace("{commit}", value["commit"])
        for part in cfg["release"]["build"]
    ]
    result = execute(
        command, workspace, cfg["limits"]["command_seconds"], command_home=command_home
    )
    require(result["exit_code"] == 0, "Release build command failed: " + result["log"][-3000:])
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    artifacts = []
    import shutil

    names = set()
    for name in cfg["release"]["artifacts"]:
        name = name.replace("{version}", value["version"])
        safe_path(name)
        path = consumer_path(workspace, name)
        require(path.is_file() and not path.is_symlink(), f"Release artifact missing: {name}")
        require(path.stat().st_size <= 100_000_000, "Release artifact exceeds 100 MB")
        require(path.name not in names, "Release artifacts have duplicate filenames")
        names.add(path.name)
        target = destination / path.name
        shutil.copyfile(path, target)
        artifacts.append(
            {"name": path.name, "sha256": file_hash(target), "size": target.stat().st_size}
        )
    manifest = {
        "source": value["commit"],
        "candidate": digest(value),
        "run_key": context["run_key"],
        "artifacts": artifacts,
        "verification": verification,
    }
    write_json(destination / "nexkit-release-manifest.json", manifest)
    return manifest


def publish_release(gh, context, directory):
    directory = Path(directory)
    value, cfg = context["release"], context["config"]
    manifest = read_json(directory / "nexkit-release-manifest.json")
    require(
        manifest.get("source") == value["commit"]
        and manifest.get("candidate") == digest(value)
        and manifest.get("run_key") == context.get("build_run_key", context["run_key"]),
        "Release artifact provenance mismatch",
    )
    require(manifest.get("verification", {}).get("passed") is True, "Release verification missing")
    expected_names = {
        Path(x.replace("{version}", value["version"])).name for x in cfg["release"]["artifacts"]
    }
    assets = manifest.get("artifacts", [])
    require(
        {a.get("name") for a in assets} == expected_names and len(assets) == len(expected_names),
        "Unexpected release artifact set",
    )
    for asset in assets:
        safe_path(asset["name"])
        require("/" not in asset["name"], "Asset must be a filename")
        path = directory / asset["name"]
        require(
            path.is_file()
            and not path.is_symlink()
            and file_hash(path) == asset["sha256"]
            and path.stat().st_size == asset["size"],
            "Release artifact bytes changed",
        )

    def authority():
        return revalidate_release(gh, context)

    state, revision = authority()
    if state.get("status") == "released":
        return state
    tag = cfg["release"]["tag_prefix"] + value["version"]
    try:
        ref = gh.api(f"{gh.root}/git/ref/tags/{tag}")
        require(
            ref["object"]["type"] == "commit" and ref["object"]["sha"] == value["commit"],
            "Existing tag points elsewhere; NexKit never moves release tags",
        )
    except Blocked as exc:
        if "HTTP 404" not in str(exc):
            raise
        authority()
        gh.api(f"{gh.root}/git/refs", "POST", {"ref": "refs/tags/" + tag, "sha": value["commit"]})
    marker = f"<!-- nexkit-candidate:{digest(value)} -->"
    releases = gh.api(f"{gh.root}/releases?per_page=100", pages=True)
    matches = [r for r in releases if r["tag_name"] == tag]
    require(len(matches) <= 1, "Duplicate release identity")
    if matches:
        release = matches[0]
        require(
            (release.get("body") or "").startswith(marker + "\n"),
            "Release identity is owned by another candidate",
        )
    else:
        authority()
        release = gh.api(
            f"{gh.root}/releases",
            "POST",
            {
                "tag_name": tag,
                "target_commitish": value["commit"],
                "name": value["version"],
                "body": marker + "\n" + value["notes"],
                "draft": True,
                "prerelease": "-" in value["version"],
            },
        )
    state.update(
        status="release-uploading",
        release_id=release["id"],
        tag=tag,
        manifest=manifest,
        updated_at=now(),
    )
    revision = gh.save_state(context["issue"]["number"], state, revision)
    remote = gh.api(f"{gh.root}/releases/{release['id']}/assets?per_page=100", pages=True)
    known = {a["name"]: a for a in remote}
    for asset in assets:
        authority()
        if asset["name"] in known:
            require(
                known[asset["name"]].get("digest") == "sha256:" + asset["sha256"],
                "Existing release asset differs or has no verifiable digest; refusing overwrite",
            )
        else:
            require(
                release["draft"], "Published release is missing an approved asset; cannot mutate it"
            )
            run(
                [
                    "gh",
                    "release",
                    "upload",
                    tag,
                    str(directory / asset["name"]),
                    "--repo",
                    gh.repository,
                ],
                timeout=300,
            )
    # Re-fetch server-side digests before publishing. No --clobber, no tag update.
    remote = gh.api(f"{gh.root}/releases/{release['id']}/assets?per_page=100", pages=True)
    require({a["name"] for a in remote} == expected_names, "Release contains unexpected assets")
    for asset in assets:
        require(
            any(
                a["name"] == asset["name"] and a.get("digest") == "sha256:" + asset["sha256"]
                for a in remote
            ),
            "Uploaded asset hash does not match approved-source build",
        )
    state, revision = authority()
    if release["draft"]:
        release = gh.api(f"{gh.root}/releases/{release['id']}", "PATCH", {"draft": False})
    state.update(status="released", release_url=release["html_url"], updated_at=now())
    gh.save_state(context["issue"]["number"], state, revision)
    return state


def main():
    from .ci import authorized_event, event_issue, output
    from .github import GitHub

    p = argparse.ArgumentParser()
    p.add_argument("operation", choices=("prepare", "guard", "build", "publish", "failed"))
    p.add_argument("--context", default="/tmp/nexkit/context.json")
    p.add_argument("--directory", default="/tmp/nexkit/release")
    p.add_argument("--workspace")
    p.add_argument("--kit-ref")
    p.add_argument(
        "--reason",
        default="Release job failed; inspect the Actions logs and resume the same candidate",
    )
    args = p.parse_args()
    try:
        if args.operation == "prepare":
            gh = GitHub(os.environ["GITHUB_REPOSITORY"])
            require(
                os.environ["GITHUB_REF"] == "refs/heads/" + gh.repo()["default_branch"],
                "Release workflow must run from the default branch",
            )
            key = os.environ["GITHUB_RUN_ID"] + "." + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            result = (
                prepare_release(
                    gh,
                    event_issue(),
                    key,
                    args.kit_ref,
                    pipeline=os.environ.get("NEXKIT_PIPELINE") or None,
                )
                if authorized_event(gh)
                else {"ready": False, "reason": "Event actor cannot authorize or resume release"}
            )
            write_json(args.context, result)
            locator = (
                result.get("build_run_key", "").split(".")
                if result.get("reuse_build")
                else ["", ""]
            )
            output(
                ready=result["ready"],
                commit=result.get("release", {}).get("commit", ""),
                artifact_run=locator[0],
                artifact_attempt=locator[1],
            )
        else:
            context = read_json(args.context)
            if args.operation == "build":
                result = build_release(context, args.workspace, args.directory)
            elif args.operation == "guard":
                revalidate_release(GitHub(context["repository"]), context)
                result = {"authorized": True}
            elif args.operation == "failed":
                result = failed_release(GitHub(context["repository"]), context, args.reason)
            else:
                result = publish_release(GitHub(context["repository"]), context, args.directory)
        print(canonical(result))
        return 0
    except Blocked as exc:
        print(f"NexKit release blocked: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

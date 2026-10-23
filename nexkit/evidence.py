"""Evidence from the CI runs an issue names, for the implement and fix agents.

The agent session holds no GitHub token, so it cannot download what a CI run left. The
step that collects the issue's context can: before the agent runs, it downloads the
failed jobs' logs and error annotations of each run the issue names, and the artifacts
that `evidence.artifacts` lists, into a folder outside the working tree. Text files are
redacted, like the result files of failing checks, and a round keeps at most
`RESULTS_LIMIT` of it.

A run counts when NexKit's own records name it, in the markers of the bug and flaky-run
issues that autopilot opens, or when the issue or a collaborator's comment links it. Only
runs of this repository count, never those of a fork or another repository.
"""

from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .autopilot import CI_RUNS, FAILED
from .checks import RESULTS_LIMIT, _is_text, size_text
from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .state import by_bot

# A link to a run, an attempt of it, or a job of it.
RUN_LINK = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)(?:/attempts?/(\d+))?(?:/job/(\d+))?"
)
# A link to something else under Actions, such as a workflow's page.
ACTIONS_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/(?!runs/\d)[^\s)>\]]*")
# The most runs one round collects: the ones named last.
MAX_RUNS = 5
# How much of the end of a failed job's log a round keeps.
LOG_TAIL = 2_000_000
# How a job's name becomes a file name.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def named_runs(gh, issue, comments):
    """[(run id, attempt or None)] for the runs of this repository that the issue names:
    links in its body and in collaborators' comments, and the runs that NexKit's own
    records hold. In the order they were named, without repeats, at most `MAX_RUNS` of
    the latest. Returns (runs, lines saying what was skipped)."""
    found, skipped = [], []
    texts = [(issue.get("body") or "", by_bot(issue))]
    for comment in comments:
        if by_bot(comment):
            texts.append((comment.get("body") or "", True))
        elif comment.get("author_association") in TRUSTED_ASSOCIATIONS:
            texts.append((comment.get("body") or "", False))
    for text, ours in texts:
        if ours:
            match = CI_RUNS.search(text)
            for item in match.group(1).split() if match else ():
                run, _, attempt = item.partition(".")
                if run.isdigit() and attempt.isdigit():
                    found.append((int(run), int(attempt)))
        for link in ACTIONS_LINK.finditer(text):
            skipped.append(f"{link.group(0)}: not a run")
        for link in RUN_LINK.finditer(text):
            repository, run, attempt = link.group(1), int(link.group(2)), link.group(3)
            if repository.lower() != gh.repository.lower():
                skipped.append(f"run {run} of {repository}: not this repository")
                continue
            found.append((run, int(attempt) if attempt else None))
    runs = {}
    for run, attempt in found:
        attempts = runs.setdefault(run, set())
        if attempt is not None:
            attempts.add(attempt)
    named = list(runs.items())[-MAX_RUNS:]
    return [(run, sorted(attempts)) for run, attempts in named], list(dict.fromkeys(skipped))


class Budget:
    """What a round may still keep."""

    def __init__(self, limit=RESULTS_LIMIT):
        self.left = limit
        self.used = 0
        self.left_out = []

    def take(self, size, shown):
        if size > self.left:
            self.left_out.append(f"{shown} ({size_text(size)})")
            return False
        self.left -= size
        self.used += size
        return True


def _write(path, data, redact):
    if _is_text(data):
        data = redact(data.decode("utf-8")).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def _safe_name(name):
    """A zip member's path inside the artifact's folder, or None for one that would leave
    it or is not a plain relative path."""
    if not name or "\\" in name or name.startswith("/"):
        return None
    parts = PurePosixPath(name).parts
    if any(part in ("", ".", "..") for part in parts) or ":" in parts[0]:
        return None
    return PurePosixPath(*parts)


def _unzip(data, folder, budget, redact, shown):
    """Extract an artifact's zip into `folder`: regular files only, inside it, within the
    budget. Returns the number of files kept."""
    kept = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode):
                continue
            relative = _safe_name(info.filename)
            if relative is None:
                name = json.dumps(info.filename)  # one line, whatever the zip holds
                print(f"{shown}: skipped {name}, which leads out of it.", flush=True)
                continue
            if not budget.take(info.file_size, f"{shown}/{relative}"):
                continue
            with archive.open(info) as member:
                content = member.read(info.file_size + 1)[: info.file_size]
            try:
                _write(folder / Path(*relative.parts), content, redact)
            except OSError as exc:  # such as a file and a folder of the same name
                print(f"{shown}: skipped {json.dumps(info.filename)}: {exc}", flush=True)
                continue
            kept += 1
    return kept


def _failed_attempts(gh, run, wanted):
    latest = run.get("run_attempt") or 1
    attempts = wanted or range(1, latest + 1)
    failed = []
    for attempt in attempts:
        found = run if attempt == latest else gh.run_attempt(run["id"], attempt) or {}
        if found.get("conclusion") in FAILED:
            failed.append(attempt)
    return failed


def _annotations(gh, job):
    lines = []
    for note in gh.check_run_annotations(job["id"]) if job.get("id") else []:
        if note.get("annotation_level") not in ("failure", "warning"):
            continue
        place = note.get("path") or ""
        if place and note.get("start_line"):
            place += f":{note['start_line']}"
        title = note.get("title") or ""
        lines.append(f"- {note['annotation_level']} {place} {title}: {note.get('message') or ''}")
    return "\n".join(lines)


def _shown(path, folder):
    return path.relative_to(folder.parent).as_posix()


def _jobs(gh, run_id, attempt, folder, budget, redact, files):
    """The failed jobs of one attempt: the end of each log and its annotations."""
    for job in gh.attempt_jobs(run_id, attempt):
        if job.get("conclusion") not in FAILED:
            continue
        name = UNSAFE.sub("-", job.get("name") or str(job.get("id"))).strip("-") or "job"
        place = folder / f"attempt-{attempt}"
        log_path, notes_path = place / f"{name}.log", place / f"{name}.annotations.md"
        try:
            tail = gh.job_log(job["id"])[-LOG_TAIL:]
            log = tail.decode("utf-8", errors="replace").replace("\0", "").encode("utf-8")
        except GitHubError as exc:
            files.append({"path": _shown(log_path, folder), "missing": f"its log: {exc}"})
            log = None
        if log is not None and budget.take(len(log), f"log of {job.get('name')}"):
            _write(log_path, log, redact)
            files.append({"path": _shown(log_path, folder)})
        notes = _annotations(gh, job)
        if notes and budget.take(len(notes), f"annotations of {job.get('name')}"):
            _write(notes_path, notes.encode(), redact)
            files.append({"path": _shown(notes_path, folder)})


def collect(gh, cfg, runs, folder, redact, budget=None):
    """Download the evidence of `runs`, from `named_runs`, into `folder`. Returns a
    record for the prompt: the folder and, for each run, its files and what is missing."""
    root = Path(folder)
    budget = budget or Budget()
    names = cfg["evidence"]["artifacts"]
    found = []
    for run_id, attempts in runs:
        item = {"id": run_id, "url": "", "files": [], "missing": []}
        found.append(item)
        try:
            run = gh.workflow_run(run_id)
        except GitHubError as exc:
            item["missing"].append(f"the run could not be read: {exc}")
            continue
        item["url"] = run.get("html_url") or ""
        head = (run.get("head_repository") or {}).get("full_name")
        if head and head.lower() != gh.repository.lower():
            item["missing"].append(f"it ran on a fork, {head}, which NexKit leaves alone")
            continue
        folder = root / f"run-{run_id}"
        try:
            failed = _failed_attempts(gh, run, attempts)
            for attempt in failed:
                _jobs(gh, run_id, attempt, folder, budget, redact, item["files"])
            if not failed:
                item["missing"].append("no attempt of it failed")
            available = gh.run_artifacts(run_id) if names else []
        except GitHubError as exc:
            item["missing"].append(f"its jobs could not be read: {exc}")
            continue
        for name in names:
            matching = [a for a in available if a.get("name") == name]
            if not matching:
                item["missing"].append(f"no artifact named `{name}`")
            for artifact in matching:
                _artifact(gh, artifact, folder, budget, redact, item)
    return {"folder": str(root), "runs": found, "left_out": budget.left_out, "used": budget.used}


def _artifact(gh, artifact, folder, budget, redact, item):
    name = artifact["name"]
    if artifact.get("expired"):
        item["missing"].append(f"the artifact `{name}` expired")
        return
    size = artifact.get("size_in_bytes") or 0
    if size > budget.left:
        budget.left_out.append(f"artifact `{name}` of run {item['id']} ({size_text(size)})")
        return
    try:
        data = gh.artifact_zip(artifact["id"], budget.left)
        target = folder / "artifacts" / UNSAFE.sub("-", name)
        count = _unzip(data, target, budget, redact, f"artifact `{name}`")
    except (GitHubError, zipfile.BadZipFile, OSError) as exc:
        item["missing"].append(f"the artifact `{name}` could not be downloaded: {exc}")
        return
    files = "1 file" if count == 1 else f"{count} files"
    item["files"].append(
        {"path": target.relative_to(folder.parent).as_posix() + "/", "count": files}
    )


def summary_lines(found):
    """Lines for the job log and the run summary: what was kept, missing or left out."""
    lines = []
    for run in found["runs"]:
        kept = ", ".join(f"`{f['path']}`" for f in run["files"] if "missing" not in f)
        lines.append(f"Run {run['id']}: " + (f"kept {kept}." if kept else "kept nothing."))
        lines += [f"Run {run['id']}: missing {reason}." for reason in run["missing"]]
        lines += [
            f"Run {run['id']}: missing {f['missing']}" for f in run["files"] if "missing" in f
        ]
    if found["left_out"]:
        limit = size_text(RESULTS_LIMIT)
        lines.append(f"Left out, past the {limit} a round keeps: " + "; ".join(found["left_out"]))
    return lines

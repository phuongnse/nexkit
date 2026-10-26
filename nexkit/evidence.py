"""Evidence from the CI runs an issue names, for the plan, implement and fix agents.

The agent session holds no GitHub token, so it cannot download what a CI run left. The
step that collects the issue's context can: before the agent runs, it downloads the
failed jobs' logs and error annotations of each run the issue names, and the artifacts
that `evidence.artifacts` lists, into a folder outside the working tree. Text files are
redacted, like the result files of failing checks, and a round keeps at most
`RESULTS_LIMIT` of it. For each run it also says whether a newer run of the same workflow
passed on the default branch since, so a round can tell a failure that came from outside
the repository and has passed from one that still needs a change.

A run counts when NexKit's own records name it, in the markers of the bug and flaky-run
issues that autopilot opens, or when the issue or a collaborator's comment links it. Links
in comments by bots, such as the run logs that NexKit's round comments link, never count.
Only runs of this repository count, never those of a fork or another repository, nor those
of NexKit's own workflow, wherever they are linked.
"""

from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

from .autopilot import CI_RUNS, FAILED, on_default_branch
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
# The runs of a named run's workflow on the default branch that show whether it passed since.
SINCE_RUNS = 20


def named_runs(gh, issue, comments):
    """[(run id, [attempts])] for the runs of this repository that the issue names: links
    in its body and in collaborators' comments, and the runs that NexKit's own records
    hold. Comments by bots name runs only in NexKit's markers. In the order they were
    named, without repeats; `collect` keeps the latest. Returns (runs, lines saying what
    was skipped)."""
    found, skipped = [], []
    # (text, whether its NexKit markers count, whether its links count)
    texts = [(issue.get("body") or "", by_bot(issue), True)]
    for comment in comments:
        if by_bot(comment):
            texts.append((comment.get("body") or "", True, False))
        elif (comment.get("user") or {}).get("type") == "Bot":
            continue
        elif comment.get("author_association") in TRUSTED_ASSOCIATIONS:
            texts.append((comment.get("body") or "", False, True))
    for text, records, links in texts:
        if records:
            match = CI_RUNS.search(text)
            for item in match.group(1).split() if match else ():
                run, _, attempt = item.partition(".")
                if run.isdigit() and attempt.isdigit():
                    found.append((int(run), int(attempt)))
        if not links:
            continue
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
    return [(run, sorted(attempts)) for run, attempts in runs.items()], list(dict.fromkeys(skipped))


def _latest(gh, runs, workflow):
    """The latest `MAX_RUNS` of `runs` that are not runs of NexKit's own workflow, the file
    `workflow`, in the order they were named, each with the run GitHub returned or the
    error reading it. Returns (runs, lines saying what was skipped)."""
    chosen, skipped = [], []
    for run_id, attempts in reversed(runs):
        if len(chosen) == MAX_RUNS:
            break
        try:
            run = gh.workflow_run(run_id)
        except GitHubError as exc:
            run = exc
        else:
            path = (run.get("path") or "").split("@", 1)[0]
            if workflow and path.rsplit("/", 1)[-1] == workflow:
                skipped.append(f"run {run_id}: a run of NexKit's own workflow, `{workflow}`")
                continue
        chosen.append((run_id, attempts, run))
    return chosen[::-1], skipped[::-1]


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


def _workflow_name(run):
    """The file of a run's workflow, such as `ci.yml`, or "" when the run does not say."""
    return (run.get("path") or "").split("@", 1)[0].rsplit("/", 1)[-1]


def passed_since(gh, run, branch):
    """The newest run of `run`'s workflow on the default branch `branch` that is newer than
    `run` and passed, as {id, url, sha, workflow, branch}, or None when none of its latest
    `SINCE_RUNS` did. Runs of pull requests and forks do not count."""
    name = _workflow_name(run)
    if not name or not branch:
        return None
    for later in gh.workflow_runs(name, branch, SINCE_RUNS):
        if (
            later.get("id", 0) > run["id"]
            and later.get("status") == "completed"
            and later.get("conclusion") == "success"
            and on_default_branch(gh, later)
        ):
            return {
                "id": later["id"],
                "url": later.get("html_url") or "",
                "sha": later.get("head_sha") or "",
                "workflow": name,
                "branch": branch,
            }
    return None


def _since(gh, run, branch, item):
    """Record in `item` whether a newer run of the run's workflow passed on `branch`."""
    name = _workflow_name(run)
    if not name or not branch:
        return
    item["workflow"], item["branch"] = name, branch
    try:
        item["passed_since"] = passed_since(gh, run, branch)
    except GitHubError as exc:
        item["missing"].append(f"whether a newer run of `{name}` passed on `{branch}`: {exc}")


def collect(gh, cfg, runs, folder, redact, workflow="", budget=None):
    """Download the evidence of the latest `MAX_RUNS` of `runs`, from `named_runs`, into
    `folder`, leaving out runs of NexKit's own workflow, the file `workflow`. Returns a
    record for the prompt: the folder, for each run its files, what is missing and the
    newer run of its workflow that passed on the default branch since, if any, and the
    runs skipped."""
    root = Path(folder)
    budget = budget or Budget()
    names = cfg["evidence"]["artifacts"]
    chosen, skipped = _latest(gh, runs, workflow)
    found = []
    branch = None
    for run_id, attempts, run in chosen:
        item = {"id": run_id, "url": "", "files": [], "missing": []}
        found.append(item)
        if isinstance(run, GitHubError):
            item["missing"].append(f"the run could not be read: {run}")
            continue
        item["url"] = run.get("html_url") or ""
        head = (run.get("head_repository") or {}).get("full_name")
        if head and head.lower() != gh.repository.lower():
            item["missing"].append(f"it ran on a fork, {head}, which NexKit leaves alone")
            continue
        folder = root / f"run-{run_id}"
        try:
            branch = gh.default_branch() if branch is None else branch
        except GitHubError as exc:
            branch = ""
            print(f"Could not read the default branch: {exc}", flush=True)
        _since(gh, run, branch, item)
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
    return {
        "folder": str(root),
        "runs": found,
        "skipped": skipped,
        "left_out": budget.left_out,
        "used": budget.used,
    }


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
        if run.get("workflow"):
            lines.append(f"Run {run['id']}: {since_text(run)}.")
    if found["left_out"]:
        limit = size_text(RESULTS_LIMIT)
        lines.append(f"Left out, past the {limit} a round keeps: " + "; ".join(found["left_out"]))
    return lines


def since_text(run):
    """Whether a newer run of a named run's workflow passed on the default branch, in words."""
    name, branch, passed = run["workflow"], run["branch"], run.get("passed_since")
    if not passed:
        return f"no newer run of `{name}` has passed on `{branch}` since"
    commit = f" at {passed['sha'][:7]}" if passed.get("sha") else ""
    link = f", {passed['url']}" if passed.get("url") else ""
    return f"run {passed['id']} of `{name}` passed on `{branch}` since{commit}{link}"

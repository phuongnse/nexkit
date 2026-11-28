"""Run one agent session for a pipeline stage and collect its result."""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from . import config as configuration
from . import program as engine_program
from . import protocol
from .checks import BASE_SHA, environment, known_base
from .context import keep_texts
from .first_state import agent_run
from .gitutil import GitError, git, is_ancestor, merge_tree, names
from .protocol.keys import camel, snake
from .protocol.stages import COMMIT, INPUTS, MAX_DIFF
from .redact import Redactor
from .runlog import RunLog, add_tokens, one_line, summary, write_summary
from .runtimes import PROVIDERS, RUNTIMES
from .runtimes.base import ACCESS, AgentConfigError, Spec
from .schema import filled, problems
from .state import conflict_names, protected_of, round_questions
from .textutil import checklist_item, person_decides
from .usage import schedule

# The most time a session gets to give its result alone, after a run without one that fits.
RESULT_SECONDS = 600
NO_RESULT_AGAIN = (
    "NexKit asked the same session for its result alone, and it gave none that fits either."
)
# A conflict marker at the start of a line: `<<<<<<< ours`, `>>>>>>> theirs`, `||||||| base`.
CONFLICT_MARKER = re.compile(rb"^(<{7}|>{7}|\|{7})( |$)", re.M)
BOT = ("-c", "user.name=nexkit", "-c", "user.email=nexkit@localhost")
# How many commits of the base branch, oldest first, NexKit tries to find the one that
# brought the change of a pull request that changes nothing once merged with it.
HOLDING_TRIES = 50

# Each stage's result schema is the protocol's (docs/engine-hosts.md#runners), so that every
# runner and host reads the same one. A plan's or a round's question names the option it
# recommends and the one that only closes the issue (`route.closing_answer`), counting from
# 1, and a blocked round's the options that change the issue's scope, which a new plan
# carries out (`host.stopped`, `host.after_go`).
SCHEMAS = {
    stage: protocol.load(f"results/{stage}.schema.json")
    for stage in ("plan", "implement", "fix", "triage", "review")
}

# The fields of a result whose empty value means the same as leaving them out, as each
# stage's prompt describes them: "only when …, otherwise empty". The schemas do not require
# them, so an agent that leaves one out gets this value instead of a result that does not
# fit. Every other field is required.
OPTIONAL = {
    "plan": {
        "questions": [],
        "too_large": False,
        "split": [],
        "protected": [],
        "no_change": "",
    },
    "implement": {
        "blocker": "",
        "questions": [],
        "protected": [],
        "outside_plan": [],
        "reviewer_notes": [],
        "no_change": "",
    },
    "fix": {
        "blocker": "",
        "questions": [],
        "protected": [],
        "cause": "",
        "cause_outside": False,
        "open_conflicts": [],
    },
}


def with_location(finding):
    """A review finding with `location`, when it has `file` and `line` instead, which the
    result files of an earlier NexKit hold: the location in GitHub's form."""
    if not isinstance(finding, dict) or "location" in finding or "file" not in finding:
        return finding
    line = finding.get("line", 0)
    if not isinstance(finding["file"], str) or not isinstance(line, int):
        return finding
    kept = {key: value for key, value in finding.items() if key not in ("file", "line")}
    return {**kept, "location": protocol.finding_location(finding["file"], line)}


def with_defaults(output, stage):
    """`output` with the empty value of each optional field it left out, for a review each
    finding at a `location`, for a fix each open conflict at a `location` too, and for a
    plan, implement or fix round what only a person changes as `protected`, which the
    results of an earlier NexKit name `protected_paths`."""
    if not isinstance(output, dict):
        return output
    if stage in ("plan", "implement", "fix") and "protected" not in output:
        if "protected_paths" in output:
            kept = {key: value for key, value in output.items() if key != "protected_paths"}
            output = {**kept, "protected": output["protected_paths"]}
    missing = {key: value for key, value in OPTIONAL.get(stage, {}).items() if key not in output}
    output = {**output, **copy.deepcopy(missing)}
    if stage == "review" and isinstance(output.get("findings"), list):
        output["findings"] = [with_location(item) for item in output["findings"]]
    if stage == "fix" and isinstance(output.get("open_conflicts"), list):
        output["open_conflicts"] = [conflict_names(item) for item in output["open_conflicts"]]
    return output


# What a plan and a review return for `review.checklist`, only when it has items.
CHECKLIST = {
    stage: entry["properties"]
    for stage, entry in protocol.load("results/checklist.schema.json")["$defs"].items()
}


def with_checklist(schema, stage, cfg):
    """`schema` with the checklist field, when this stage answers a checklist."""
    if stage not in CHECKLIST or not cfg["review"]["checklist"]:
        return schema
    properties = CHECKLIST[stage]
    entry = {"type": "object", "properties": properties, "required": list(properties)}
    return {
        **schema,
        "properties": {**schema["properties"], "checklist": {"type": "array", "items": entry}},
        "required": [*schema["required"], "checklist"],
    }


def drop_criteria(result, stage, context):
    """A person's pull request has no plan, so its review judges no acceptance criteria.
    The prompt asks for none; criteria that a reviewer still gives, which it can only have
    made up from the pull request's text, go here, so that none blocks the review or waits
    for a person."""
    output = result.get("output")
    if stage != "review" or not context.get("person") or not isinstance(output, dict):
        return
    if output.get("criteria"):
        count = len(output["criteria"])
        print(
            f"Dropped the {count} acceptance criteria the review gave: a person's pull "
            "request has no plan.",
            flush=True,
        )
    output["criteria"] = []


def answer_checklist(result, stage, cfg):
    """Put each item's text next to its answer, in the order of `review.checklist`, so what
    reads the result needs no configuration. An answer for an item that does not exist goes.
    A review that leaves an item out is incomplete: nothing shows that the item holds, so it
    gives no verdict. A plan that leaves one out is shown as it is; the review decides."""
    items = cfg["review"]["checklist"]
    if stage not in CHECKLIST or not items or result["status"] != "done":
        return
    output = result["output"]
    given = {entry["number"]: entry for entry in output.get("checklist") or []}
    missing = [number for number in range(1, len(items) + 1) if number not in given]
    if stage == "review" and missing:
        listed = ", ".join(str(number) for number in missing)
        result.update(
            status="error",
            output=None,
            error=f"The review gave no answer for item {listed} of `review.checklist`.",
        )
        return
    output["checklist"] = [
        {
            # Only the fields the schema asks for: what a person decides comes from the
            # configuration, never from the agent.
            **{key: given[number][key] for key in CHECKLIST[stage] if key in given[number]},
            "item": checklist_item(item),
            **({"person": True} if person_decides(item) else {}),
        }
        for number, item in enumerate(items, 1)
        if number in given
    ]


def _since(repo, commit, base):
    """What the review diffs against for "changes since the previous review": the reviewed
    commit, or, when base commits were merged in since, the automatic merge of the reviewed
    commit with them, so that changes from the base branch stay out."""
    try:
        merged = git(repo, "merge-base", "HEAD", f"origin/{base}").strip()
        if is_ancestor(repo, merged, commit):
            return commit
        return merge_tree(repo, commit, merged)[0]
    except GitError:
        return commit


def _clipped(diff, limit=MAX_DIFF):
    """A diff cut to one character past what a prompt shows of it, which it then clips the
    same way: no more reaches the engine than a request may carry."""
    return diff[: limit + 1]


def review_diff(repo, base):
    """The change against its base, as a review reads it."""
    return _clipped(git(repo, "diff", f"origin/{base}...HEAD"))


def since_review(repo, context, base):
    """The diff from the previously reviewed commit to HEAD, or None when the checkout does
    not have that commit or there was no previous review."""
    commit = context.get("previous_head") or ""
    if not COMMIT.fullmatch(commit):
        return None
    try:
        return _clipped(git(repo, "diff", _since(repo, commit, base), "HEAD"))
    except GitError:
        return None


def merge_sides(repo, context):
    """What the base and the pull request each changed, since their fork, in the files a
    merge of the base branch in this run's fix round conflicted in; None when the checkout
    cannot show them or there was no such merge."""
    merged = context.get("merged") or {}
    head, commit, files = merged.get("head"), merged.get("base"), merged.get("conflicts")
    if not files or not all(COMMIT.fullmatch(c or "") for c in (head, commit)):
        return None
    try:
        fork = git(repo, "merge-base", head, commit).strip()
        sides = [git(repo, "diff", fork, side, "--", *files) for side in (commit, head)]
        return [_clipped(side, MAX_DIFF // 4) for side in sides]
    except GitError:
        return None


def _patch_id(repo, old, new):
    """The id of the change from `old` to `new`, the same for the same change whatever its
    line numbers, or "" for no change. Binary content counts; no diff driver of the
    repository runs."""
    diff = git(
        repo,
        "diff",
        "--binary",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        old,
        new,
        binary=True,
    )
    if not diff:
        return ""
    found = git(repo, "patch-id", "--verbatim", input=diff, binary=True).decode().split()
    return found[0] if found else ""


def carried_review(repo, base, approved):
    """(True, "") when the review of the commit `approved` holds for HEAD: HEAD merges a
    commit of `origin/<base>` into it, and the pull request's change against that commit
    is the one the review approved, against the base commit it started from. Otherwise
    (False, why the review runs again)."""
    try:
        parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        if len(parents) != 2 or parents[0] != approved:
            return False, f"the new commit is not a merge of `{base}` into `{approved[:7]}`"
        merged = parents[1]
        if not is_ancestor(repo, merged, f"origin/{base}"):
            return False, f"the commit it merged is not on `{base}`"
        fork = git(repo, "merge-base", approved, merged).strip()
        before = _patch_id(repo, fork, approved)
        after = _patch_id(repo, merged, "HEAD")
    except GitError as exc:
        return False, f"git could not compare the changes: {exc}"
    if not before or before != after:
        return False, f"the change against `{base}` differs from the one the review approved"
    return True, ""


def conflicts_with_base(repo, base):
    """Whether HEAD conflicts with `origin/<base>`, decided by git without changing anything."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    return bool(merge_tree(repo, "HEAD", commit)[1])


def up_to_date(base):
    """The result of an automatic conflict round whose branch no longer conflicts: nothing
    to do, so the agent does not run."""
    return {
        "stage": "fix",
        "status": "up_to_date",
        "summary": "",
        "error": f"The pull request no longer conflicts with `{base}`.",
    }


def on_base(repo, base):
    """The result of a fix round whose branch, merged with `origin/<base>`, would change no
    file against it, because the base branch already holds its change: nothing to do, so
    neither setup nor the agent runs. None when the branch has a change of its own."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    tree, conflicts = merge_tree(repo, "HEAD", commit)
    if conflicts or tree != git(repo, "rev-parse", f"{commit}^{{tree}}").strip():
        return None
    return {
        "stage": "fix",
        "status": "already_on_base",
        "summary": "",
        "error": f"`{base}` already holds the change of this pull request.",
        "merge_base": commit,
        "on_base": holding_commit(repo, commit),
    }


def holding_commit(repo, commit):
    """The oldest commit of the base branch, up to `commit` and not yet on the branch, that
    already holds every change of the branch, or None when none does: the commit that
    brought the same change."""
    try:
        start = git(repo, "merge-base", "HEAD", commit).strip()
        files = names(git(repo, "diff", "--name-only", "--no-renames", "-z", start, "HEAD"))
        if not files:
            return None
        listed = git(repo, "rev-list", "--reverse", commit, "^HEAD", "--", *files).split()
        for candidate in listed[:HOLDING_TRIES]:
            tree, conflicts = merge_tree(repo, "HEAD", candidate)
            if not conflicts and tree == git(repo, "rev-parse", f"{candidate}^{{tree}}").strip():
                return candidate
    except GitError:
        return None
    return None


def merge_base_branch(repo, base, moved=False):
    """When the branch conflicts with its base branch, or with `moved` when the base branch
    has commits the branch lacks, start merging the base branch and leave any conflicts in
    the working tree. Return (merged commit, conflicted files), or (None, []) when nothing
    was changed."""
    commit = git(repo, "rev-parse", "--verify", f"origin/{base}^{{commit}}").strip()
    if not merge_tree(repo, "HEAD", commit)[1] and not (
        moved and not is_ancestor(repo, commit, "HEAD")
    ):
        return None, []
    git(repo, *BOT, "merge", "--no-commit", "--no-ff", "-q", commit, ok=(0, 1))
    conflicts = names(git(repo, "diff", "--name-only", "--diff-filter=U", "-z"))
    return commit, list(dict.fromkeys(conflicts))


def merge_heads(repo):
    """The commits that the merge in progress merges, as git's MERGE_HEAD lists them."""
    path = Path(git(repo, "rev-parse", "--git-path", "MERGE_HEAD").strip())
    if not path.is_absolute():
        path = Path(repo) / path
    return path.read_text(encoding="utf-8").split() if path.is_file() else []


def own_merge(repo, base, merged):
    """The commit of `base` that a fix agent merged itself, when NexKit merged nothing
    (`merged`): (commit, None), (None, None) without one, or (None, error) when the agent
    merged anything else, which NexKit does not publish."""
    heads = merge_heads(repo)
    if not heads or heads == [merged]:
        return None, None
    shown = ", ".join(head[:7] for head in heads)
    if merged:
        return None, (
            f"The agent merged {shown} in place of NexKit's merge of `{base}` "
            f"({merged[:7]}), so NexKit publishes nothing."
        )
    if len(heads) > 1 or not is_ancestor(repo, heads[0], f"origin/{base}"):
        return None, (
            f"The agent merged {shown}, which is not on `{base}`. NexKit merges only "
            f"`{base}`, so it publishes nothing."
        )
    return heads[0], None


def shortened_folders(paths):
    """The folders that hold the files a prompt's answer names: the whole texts it shortened,
    and the whole outputs of the failing checks it shows."""
    found = {str(Path(path).parent) for path in paths}
    return sorted(folder for folder in found if folder != "." and Path(folder).is_dir())


def _cause(result):
    return str((result.get("output") or {}).get("cause") or "").strip()


def no_change(result):
    """The finding of an implement result that says no change is needed, or ""."""
    return filled((result.get("output") or {}).get("no_change"))


def set_aside_finding(result):
    """An implement round that changed files and returned `done` is a change, whatever its
    `no_change` holds: agents put notes there such as "every planned change was made". The
    text becomes a note for the reviewer, in the pull request and the round's comment, and
    never closes the issue."""
    output = result["output"]
    note = no_change(result)
    output["no_change"] = ""
    output["reviewer_notes"] = [*(output.get("reviewer_notes") or []), note]
    result["no_change_note"] = note


def check_closing(output):
    """Keep `closes` only where it names one of its question's options: an option that only
    closes the issue is one NexKit carries out when a person picks it, so it must be the
    one the agent meant."""
    for item in output.get("questions") or []:
        if not isinstance(item, dict):
            continue
        closes, options = item.get("closes"), item.get("options") or []
        number = isinstance(closes, int) and not isinstance(closes, bool)
        if not number or not 0 <= closes <= len(options):
            item["closes"] = 0


def check_replans(output):
    """Keep in `replans` only numbers of its question's options, each once, and never the
    option that only closes the issue: autopilot answers such an option with a new plan
    instead of the same round, so each must be one the agent meant."""
    for item in output.get("questions") or []:
        if not isinstance(item, dict):
            continue
        found, options = item.get("replans"), item.get("options") or []
        closes = item.get("closes")
        numbers = [
            n
            for n in (found if isinstance(found, list) else [])
            if isinstance(n, int) and not isinstance(n, bool) and 0 < n <= len(options)
        ]
        item["replans"] = [n for n in dict.fromkeys(numbers) if n != closes]


def needed_protected(result, cfg):
    """The protected paths that a result says its change needs: only those that are
    protected, each once. The agent's list alone decides nothing."""
    found = protected_of(result.get("output"))
    paths = [str(p).strip().lstrip("/") for p in found if isinstance(p, str)] if found else []
    return list(dict.fromkeys(p for p in paths if p and configuration.is_protected(p, cfg)))[:20]


def passed_runs(found):
    """The newer runs that passed on the default branch since the named CI runs, from
    `evidence.collect`, each once: what a `no_change` finding about an outside failure
    rests on."""
    runs = {}
    for run in (found or {}).get("runs") or []:
        passed = run.get("passed_since")
        if passed:
            runs.setdefault(passed["id"], passed)
    return list(runs.values())


def outside_cause(result):
    """Whether a fix result names a cause outside the code, with its evidence."""
    return bool(_cause(result)) and (result.get("output") or {}).get("cause_outside") is True


def unchanged(result):
    """The error of an implement or fix round that changed no files, with what the agent
    found: its cause, else its summary."""
    found = _cause(result) or str((result.get("output") or {}).get("summary") or "").strip()
    error = "The agent finished without changing any files."
    return f"{error} What it found: {found}" if found else error


def no_cause(result, repeated):
    """A diagnosis round that found no cause and changed nothing ends as `blocked`, with
    what the evidence shows as the question for a person."""
    output = result["output"]
    names = ", ".join(f"`{item['name']}`" for item in repeated)
    shown = str(output.get("blocker") or output.get("summary") or "").strip()
    blocker = f"The diagnosis round found no cause for the repeated failure of {names}."
    if shown:
        blocker += f" What the evidence shows: {shown}"
    questions = output.get("questions") or [
        {
            "question": f"{names} failed the same way after two fix rounds, and the agent "
            "found no cause in the evidence. How should NexKit go on?",
            "options": [
                "Add logging or a smaller test that shows the failure, then comment `/nexkit fix`",
                "Name the cause or a direction in a note: `/nexkit fix <note>`",
                "Fix it yourself and push a commit",
            ],
            "recommended": 0,
        }
    ]
    output.update(status="blocked", blocker=blocker, questions=questions)
    result.update(status="blocked", error=blocker)


def own_changes(repo, files, merged, conflicts):
    """The files the agent changed itself: without what a merge of the base branch brought,
    and without the conflicted files it had to resolve. Call after `collect_changes`."""
    if not merged:
        return files
    tree = merge_tree(repo, "HEAD", merged)[0]
    edits = names(git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", tree))
    return [name for name in edits if name not in conflicts]


def leftover_markers(repo, files):
    """The conflicted files that still contain conflict markers."""
    found = []
    for name in files:
        path = Path(repo) / name
        if path.is_file() and CONFLICT_MARKER.search(path.read_bytes()):
            found.append(name)
    return found


# GitHub's sentences on how the agent sees and edits the change, for the `workspace` input's
# `text` in snake_case. They sit outside the prompt's `<untrusted>` tags, so they are fixed
# strings, never built from what anyone wrote on GitHub.
WORKSPACE_TEXT = {
    "location": "working in a checkout of the repository",
    "review_location": "The repository is checked out at the candidate commit.",
    "candidate": "the candidate commit",
    "instructions": "(CLAUDE.md or AGENTS.md)",
    "saving": "Do not commit, push or create branches. NexKit commits your working-tree changes.",
    "leftovers": "Leave no temporary files, logs or build output in the working tree.",
    "summary_use": "the commit message",
    "checks_on": "run by NexKit on this exact commit",
    "diff_heading": "Diff against",
    "pipeline": "CI configuration",
    "pipeline_files": "workflows",
    "pipeline_file": "a workflow",
    "protected_example": "`.github/workflows/ci.yml`",
    "person_change": "on this branch",
    "fix_location": "working in a checkout of",
    "earlier_work": "The branch already contains earlier work.",
    "merge_rule": (
        "Do not merge the base branch or any other branch yourself. NexKit merges the base\n"
        "  branch when the round needs it, and publishes nothing after a merge of another commit."
    ),
    "version_kind": "commit",
    "base_kind": "base branch",
    "merge_into": "into the branch, without committing it",
    "last_merged": "this branch last merged it",
    "merge_undo": "do not run `git merge --abort`, `git commit` or `git reset`.",
    "side_history": (
        "`git log --merge -p -- <file>` shows the commits that\n  each side made to a file."
    ),
    "side_note": "A commit message on one side is not an answer.",
    "other_versions": "Any later commits were pushed without a NexKit fix round.",
    "workspace_name": "this checkout",
    "location_form": (
        "the file and line, such as `nexkit/report.py:262`, the file alone when it is not about "
        "one line, or an empty string when it is about no file"
    ),
}


def workspace(cfg):
    """The `workspace` input of a prompt: the checks the agent can run, the paths only a
    person changes and GitHub's texts on how the agent sees and edits the change."""
    return {
        "checks": [
            {
                "name": check["name"],
                "run": check["run"],
                "slow": bool(check.get("slow")),
                **({"shards": {"max": check["shards"]["max"]}} if check.get("shards") else {}),
            }
            for check in cfg["checks"]
        ],
        "protected": list(cfg["protected_paths"]),
        "text": dict(WORKSPACE_TEXT),
    }


# The findings that show a plan or implement stage on GitHub that no change is needed, as the
# prompt call's `policies.noChange`: fixed text, each finding with its own line breaks and
# without the indent the engine gives its bullet. The two stages wrap them differently.
NO_CHANGE = {
    "plan": [
        "A commit on the default branch already fixed it, and the code shows that.",
        "A CI failure came from outside the repository, such as a download host, a package\n"
        "registry or a service that failed, or a runner that was lost, and nothing in the\n"
        "repository caused it or could prevent it. The evidence of the CI runs above must name\n"
        "that outside cause, and *Since* must show a newer run of the same workflow that passed\n"
        "on the default branch.",
    ],
    "implement": [
        "A commit on the base branch already fixed the failure.",
        "A CI failure came from outside the repository, such as a download host, a package\n"
        "registry or a service that failed, or a runner that was lost, and nothing in the\n"
        "repository caused it or could prevent it. The evidence of the CI runs above must name\n"
        "that outside cause, and *Since* must show a newer run of the same workflow that\n"
        "passed on the default branch.",
    ],
}


def policies(cfg, stage=None):
    """The policies a prompt reads: `review.checklist`, the profiles triage chooses from,
    the implement stage's time, which the plan prompt names, and for a plan or implement
    `stage` the findings that show no change is needed."""
    minutes = cfg["stages"]["implement"]["timeout_minutes"]
    found = {
        "checklist": cfg["review"]["checklist"],
        "profiles": {name: {"when": profile["when"]} for name, profile in cfg["profiles"].items()},
        "budgets": {"implement": {"seconds": minutes * 60}},
    }
    if stage in NO_CHANGE:
        found["noChange"] = list(NO_CHANGE[stage])
    return found


def prompt_words(issue, pr=None):
    """The words of GitHub's prompts, for the `prompt` request's `words` in snake_case: the
    issue's and the pull request's numbers, and fixed text. They sit outside the prompt's
    `<untrusted>` tags, so they come only from numbers and fixed strings, never from what
    anyone wrote on GitHub."""
    return {
        "item": f"GitHub issue #{issue}",
        "item_heading": f"Issue #{issue}",
        "item_kind": "issue",
        "item_kinds": "issues",
        "change": f"pull request #{pr}" if pr else "",
        "change_kind": "pull request",
        "source": "on GitHub",
        "readers": "on GitHub",
        "approve": "`/nexkit go`",
        "fix_round": "`/nexkit fix`",
        "commands": "`/nexkit` commands",
    }


def prompt_request(stage, context, added, cfg, folder):
    """The `prompt` request of `stage`: the inputs the context step fetched, with what this
    round adds in `added` (its diff, checks and workspace), in the protocol's camelCase and
    only the kinds the stage reads; the state document; the policies; and the `folder` for
    whole texts."""
    fetched = context.get("inputs") or {}
    inputs = {**fetched, **added}
    if "diff" in fetched:
        inputs["diff"] = {**fetched["diff"], **added.get("diff", {})}
    item = inputs.get("item") or {}
    inputs = {key: value for key, value in camel(inputs).items() if key in INPUTS[stage]}
    return {
        "protocol": protocol.VERSION,
        "call": "prompt",
        "state": context.get("state"),
        "stage": stage,
        "inputs": inputs,
        "policies": policies(cfg, stage),
        "words": camel(prompt_words(item.get("id"), item.get("change"))),
        "texts": str(folder),
    }


def prompt_call(request):
    """The answer of the `nexkit-engine` program to a `prompt` request: {text, shortened,
    notRead, files}, or {error} when the program cannot start, crashes, times out or refuses
    the request. Each caller handles `error`: nothing retries it."""
    return engine_program.call("prompt", request)


def no_prompt(answer):
    """The error of a round that got no prompt."""
    return f"The engine wrote no prompt: {answer['error']}"


def stage_schema(stage, cfg):
    """The schema of a stage's result. Triage may name only the configured profiles."""
    schema = SCHEMAS[stage]
    if stage == "triage":
        schema = copy.deepcopy(schema)
        schema["properties"]["profile"]["enum"] = list(cfg["profiles"])
    return with_checklist(schema, stage, cfg)


def stage_spec(stage, cfg, home, dirs=(), session=None, schema=None):
    """What the runtime needs to run `stage`, with `home` for the CLI's own files and
    `dirs`, folders outside the checkout that the agent reads, such as CI runs' evidence.
    `session` names a session that NexKit may continue, and `schema` replaces the stage's
    result schema, for a run outside the pipeline that keeps the stage's agent and access."""
    settings = configuration.stage(cfg, stage)
    return Spec(
        session=session,
        stage=stage,
        model=settings["model"],
        effort=settings["effort"],
        schema=schema or stage_schema(stage, cfg),
        home=Path(home),
        dirs=tuple(Path(d) for d in dirs),
    )


def run_process(prompt, argv, *, env, cwd, timeout_seconds, log):
    """Start the CLI with `prompt` on stdin and pass each output line to `log`, which
    shows it and hands it to the runtime's reader."""
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(5)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout_seconds, kill)
    timer.start()
    stderr_chunks = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()))
    reader.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            log.line(line)
        proc.wait()
    finally:
        timer.cancel()
        reader.join()
        log.close()
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out.is_set(),
        "stderr": "".join(stderr_chunks)[-4000:],
        "seconds": round(time.monotonic() - started, 1),
    }


def run_agent(
    stage,
    prompt,
    cfg,
    *,
    cwd,
    redact,
    out,
    title,
    commits=None,
    program=None,
    restore=None,
    dirs=(),
    schema=None,
):
    """Run the agent of `stage` on `prompt`. Return the interpreted result and the tool
    calls for the summary. `program` replaces the runtime's executable, for tests. `dirs`
    are folders outside `cwd` that the agent reads. `schema` replaces the stage's result
    schema, for a run outside the pipeline, such as the docs audit, that keeps the stage's
    agents, model, time and access.

    When the agent stops at a usage limit, the stage runs again from the start on each of
    its fallbacks in turn, after `restore` returned the checkout to where the agent started.
    Nothing else moves to a fallback. The stage pauses when every agent stopped at its
    limit, until the earliest reset. The result names the agent that ran last and lists
    every agent's run in `attempts`.

    `commits` gives NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA, so checks that the agent runs
    see the same commits as in the verify job."""
    settings = configuration.stage(cfg, stage)
    names = configuration.chain(cfg, settings["agent"])
    deadline = time.monotonic() + configuration.stage_minutes(cfg, stage) * 60
    attempts, calls, limited = [], [], []
    for index, name in enumerate(names):
        seconds = min(settings["timeout_minutes"] * 60, deadline - time.monotonic())
        if index and seconds < 60:
            skipped = {"skipped": True, "error": "No time was left in the stage."}
            attempts.append(_attempt_record(cfg, name, settings, skipped))
            break
        if index:
            if restore:
                restore()
            print(
                f"! The `{limited[-1][0]}` agent stopped at its usage limit; running the "
                f"{stage} stage on `{name}`.",
                flush=True,
            )
        attempt, attempt_calls = _run_one(
            stage,
            prompt,
            cfg,
            name,
            fallback=bool(index),
            seconds=seconds,
            cwd=cwd,
            redact=redact,
            out=Path(out) / f"agent-{name}" if index else Path(out),
            title=title,
            commits=commits,
            program=program,
            dirs=dirs,
            schema=schema,
        )
        attempts.append(_attempt_record(cfg, name, settings, attempt))
        if attempt.get("skipped"):
            continue
        result, calls = attempt, attempt_calls
        if attempt["status"] != "paused":
            break
        limited.append((name, attempt))
    # The agent's own run is never skipped, so some run ended the stage.
    if result["status"] == "paused" and len(limited) > 1:
        _all_limited(result, limited)
    _totals(result, attempts)
    ran = next(a["agent"] for a in reversed(attempts) if a["status"] != "skipped")
    result.update(agent=ran, attempts=attempts)
    return result, calls


def _attempt_record(cfg, name, settings, result):
    """One agent's run of a stage, for the run summary and the run comment."""
    chosen = configuration.agent(cfg, name)
    record = {
        "agent": name,
        "runtime": chosen["runtime"],
        "provider": chosen["provider"],
        "model": settings["model"],
        "status": "skipped" if result.get("skipped") else result["status"],
    }
    for key in ("seconds", "tokens", "limit"):
        if result.get(key) is not None:
            record[key] = result[key]
    if record["status"] in ("skipped", "error") and result.get("error"):
        record["error"] = result["error"]
    return record


def _all_limited(result, limited):
    """Pause until the earliest reset of the agents that stopped at their limits."""
    # `resume_at` is `YYYY-MM-DDTHH:MM:SSZ`, so text order is time order.
    first = min((attempt for _, attempt in limited), key=lambda a: a["resume_at"])
    text = "; ".join(f"`{name}`: {attempt['limit']}" for name, attempt in limited)
    result.update(
        error=f"Every agent stopped at its usage limit: {text}",
        limit=text,
        resume_at=first["resume_at"],
        reset_known=first["reset_known"],
    )


def _totals(result, attempts):
    """The stage's tokens and duration add up every agent's run."""
    ran = [a for a in attempts if a["status"] != "skipped"]
    if len(ran) < 2:
        return
    seconds = [a["seconds"] for a in ran if isinstance(a.get("seconds"), (int, float))]
    result["seconds"] = round(sum(seconds), 4) if seconds else None
    result["tokens"] = add_tokens(*(a.get("tokens") for a in ran))


def _run_one(
    stage,
    prompt,
    cfg,
    name,
    *,
    fallback,
    seconds,
    cwd,
    redact,
    out,
    title,
    commits,
    program,
    dirs,
    schema=None,
):
    """Run the agent `name` once. A fallback without its secret is skipped: the result has
    `skipped` and says why."""
    agent = configuration.agent(cfg, name)
    runtime = RUNTIMES[agent["runtime"]]
    provider = PROVIDERS[agent["provider"]]
    groups = provider.secrets(agent)
    # Unset secrets arrive as empty strings. The CLI gets only its agent's secrets.
    secrets = {key: os.environ[key] for group in groups for key in group if os.environ.get(key)}
    missing = [group for group in groups if not any(key in secrets for key in group)]
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    error = None
    # A fallback never runs on a person's own login: that is the login the agent used.
    if missing and (on_actions or fallback or not provider.local_login):
        error = (
            f"No secret for the `{name}` agent: set the {' or '.join(missing[0])} repository "
            "secret and pass it in the `secrets:` of the NexKit workflow."
        )
    else:
        try:
            provider.check_secrets(agent, secrets)
        except AgentConfigError as exc:
            error = f"{exc} (the `{name}` agent)."
    if error:
        result = {"stage": stage, "status": "error", "error": error}
        if fallback:
            result["skipped"] = True
        return result, []
    # Whatever names the configuration gave them.
    redact.add(secrets.values())
    out.mkdir(parents=True, exist_ok=True)
    log = RunLog(
        redact,
        reader=runtime.reader(),
        title=f"{title} ({name})" if fallback else title,
        speaker=runtime.label,
        tool_output=cfg["log"]["tool_output"],
        transcript_dir=out if cfg["transcript"] else None,
    )
    # Without any agent credential: the CLI gets only its own, through `launch.env`.
    env = environment(commits)
    session = str(uuid.uuid4()) if runtime.resumes else None

    def launch_once(spec, text, log, limit):
        launch = runtime.launch(agent, spec, secrets, program)
        for path, content in launch.files.items():
            Path(path).write_text(content, encoding="utf-8")
        run = run_process(
            text, launch.argv, env={**env, **launch.env}, cwd=cwd, timeout_seconds=limit, log=log
        )
        outcome = log.reader.outcome(run["returncode"], run["stderr"])
        found = interpret(
            stage,
            run,
            outcome,
            max(1, round(limit / 60)),
            cfg["usage_limit"]["retry_minutes"],
            label=runtime.label,
            # NexKit's own schema: triage names an unknown profile in its error, not here.
            schema=schema or with_checklist(SCHEMAS[stage], stage, cfg),
        )
        return found

    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP") or None) as home:
        spec = stage_spec(stage, cfg, home, dirs, session=session, schema=schema)
        result = launch_once(spec, prompt, log, seconds)
        calls = log.calls
        if unfit(result):
            left = min(RESULT_SECONDS, seconds - (result.get("seconds") or 0))
            if runtime.resumes and left >= 60:
                print(
                    f"! {runtime.label} gave no result that fits the {stage} schema; asking the "
                    "same session for its result alone.",
                    flush=True,
                )
                folder = out / "result-again"
                folder.mkdir(exist_ok=True)
                again_prompt = prompt_call(
                    {
                        "protocol": protocol.VERSION,
                        "call": "prompt",
                        "state": None,
                        "stage": stage,
                        "inputs": {},
                        "policies": {},
                        "unfit": redact(result["unfit"]),
                    }
                )
                if "error" in again_prompt:
                    # No second run: the result stays as the first run left it.
                    print(
                        f"! {no_prompt(again_prompt)} to ask {runtime.label} again.",
                        flush=True,
                    )
                else:
                    text = again_prompt["text"]
                    (folder / "prompt.md").write_text(text, encoding="utf-8")
                    again = RunLog(
                        redact,
                        reader=runtime.reader(),
                        title=f"{title} ({name}, asked for its result)",
                        speaker=runtime.label,
                        tool_output=cfg["log"]["tool_output"],
                        transcript_dir=folder if cfg["transcript"] else None,
                    )
                    second = launch_once(replace(spec, resume=True), text, again, left)
                    result = asked_again(result, second)
                    calls = calls + again.calls
            if unfit(result):
                # Another run may pass where this session did not: the stage's command runs
                # again, as after a transient error.
                result["transient"] = True
                if result.get("asked_again"):
                    result["error"] = f"{result['error']} {NO_RESULT_AGAIN}".strip()
    return result, calls


def unfit(result):
    """Whether a stage result is an error only because it gave no result that fits."""
    return result["status"] == "error" and bool(result.get("unfit")) and not result.get("transient")


def asked_again(first, second):
    """The result of a session asked for its result alone: the second answer, with the time
    and tokens of both runs. `asked_again` says it was asked. A request that ran out of its
    time or budget leaves the first result: no result that fits, not work that ran out of
    time, which would lead autopilot to split work that was done."""
    if second["status"] == "error" and not second.get("transient"):
        # Timed out, over its budget, or still no result that fits.
        unfit = second.get("unfit") or first["unfit"]
        second = {**first, "unfit": unfit, **{k: second.get(k) for k in ("tokens", "seconds")}}
    second["seconds"] = round((first.get("seconds") or 0) + (second.get("seconds") or 0), 1)
    tokens = add_tokens(first.get("tokens"), second.get("tokens"))
    if tokens:
        second["tokens"] = tokens
    else:
        second.pop("tokens", None)
    second["asked_again"] = True
    return second


def paused(result, limit, label="The agent"):
    """Mark a stage result as paused at the usage limit."""
    result.update(
        status="paused",
        error=f"{label} hit the account's usage limit: {limit['message']}",
        limit=limit["message"],
        resume_at=limit["resume_at"],
        reset_known=limit["reset_known"],
    )
    return result


def contract_problems(output, stage):
    """What a result breaks that its JSON Schema cannot say: a blocked implement or fix
    result needs a `blocker`, unless it holds a question or an open conflict to answer."""
    if stage not in ("implement", "fix") or output.get("status") != "blocked":
        return []
    if filled(output.get("blocker")) or round_questions({"status": "blocked", "output": output}):
        return []
    return [
        "result.blocker must say why you stopped: status is blocked and there is no "
        "question or open conflict to answer"
    ]


def interpret(
    stage, run, outcome, timeout_minutes, retry_minutes=60, *, label="The agent", schema=None
):
    """Map a run and the runtime's outcome to a stage result: done, blocked, paused or
    error. With `schema`, the structured result must fit it, once the optional fields it
    left out have their empty values. A run that ended without a result that fits has
    `unfit`: what was wrong, for a request of the result alone (see `_run_one`)."""
    result = {
        "stage": stage,
        "seconds": run["seconds"],
        "output": None,
        "status": "error",
        "summary": "",
        "error": "",
    }
    if outcome.tokens:
        result["tokens"] = outcome.tokens
    given = with_defaults(outcome.output, stage)
    found = problems(given, schema) if schema and given is not None else []
    if not found and isinstance(given, dict):
        found = contract_problems(given, stage)
    if run["timed_out"]:
        result["error"] = f"{label} did not finish within {timeout_minutes} minutes."
        result["timed_out"] = True
    elif outcome.error:
        result["error"] = f"{label} {outcome.error}"[:2000].strip()
        if outcome.no_result:
            said = one_line(outcome.result_error)[:1500]
            result["unfit"] = said or f"The result did not fit the {stage} schema."
    elif not isinstance(given, dict):
        result["error"] = f"{label} finished without the required structured result."
        result["unfit"] = "There was no structured result."
    elif found:
        result["error"] = (
            f"{label} returned a result that does not fit the {stage} schema: "
            + "; ".join(found[:5])
        )
        result["unfit"] = "; ".join(found[:20])
    else:
        output = given
        result["output"] = output
        result["summary"] = output.get("summary", "")
        if stage in ("implement", "fix"):
            result["status"] = output.get("status")
            if result["status"] == "blocked":
                result["error"] = (
                    filled(output.get("blocker")) or "The agent reported it was blocked."
                )
        else:
            result["status"] = "done"
    if result["status"] == "error" and not run["timed_out"] and outcome.limit:
        paused(result, schedule(outcome.limit, retry_minutes=retry_minutes), label)
    elif result["status"] == "error" and not run["timed_out"] and outcome.transient:
        result["transient"] = True
    return result


# Caches and build output that tests leave behind. Ignored only in the agent's checkout,
# through .git/info/exclude; the repository's own .gitignore is unchanged.
GENERATED = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".venv/",
    ".coverage",
    "node_modules/",
    ".DS_Store",
)


def baseline(repo):
    """Commit setup side effects so the patch contains only the agent's changes."""
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude.is_absolute():
        exclude = Path(repo) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n# NexKit: generated files\n" + "\n".join(GENERATED) + "\n")
    start = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "add", "-A")
    git(
        repo,
        *BOT,
        "commit",
        "-q",
        "--allow-empty",
        "--no-verify",
        "-m",
        "nexkit: setup baseline",
    )
    return start, git(repo, "rev-parse", "HEAD").strip()


def collect_changes(repo, base_commit, out_dir):
    git(repo, "add", "-A")
    patch = git(repo, "diff", "--cached", "--binary", base_commit, binary=True)
    names = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", base_commit)
    files = [name for name in names.split("\0") if name]
    (Path(out_dir) / "changes.patch").write_bytes(patch)
    return files, len(patch)


def run_triage(context, cfg, previous, out_dir, *, program=None):
    """Choose the profile for a plan with one agent call that cannot see the repository.

    Triage never stops the plan: after an error or an unknown name, the plan uses the
    `previous` profile, or `default_profile` when there is none."""
    out = Path(out_dir) / "triage"
    out.mkdir(parents=True, exist_ok=True)
    profiles = cfg["profiles"]
    # Beside the plan's texts, outside the working tree; the agent's artifact keeps them.
    folder = (Path(out_dir) / "texts").resolve()
    added = {"previous_plan": {"profile": previous}}
    answer = prompt_call(prompt_request("triage", context, added, cfg, folder))
    redact = Redactor()
    if "error" in answer:
        # No prompt, so no agent: the plan falls back like after any triage error.
        result, calls = {"stage": "triage", "status": "error", "error": no_prompt(answer)}, []
    else:
        keep_texts(answer["files"], folder)
        text = answer["text"]
        (out / "prompt.md").write_text(text, encoding="utf-8")
        # An empty directory, so the repository's agent settings and instructions do not load.
        with tempfile.TemporaryDirectory() as empty:
            result, calls = run_agent(
                "triage",
                text,
                cfg,
                cwd=empty,
                redact=redact,
                out=out,
                title="NexKit triage",
                program=program,
            )
    output = result.pop("output", None) or {}
    result.pop("summary", None)
    name = output.get("profile")
    if result["status"] == "done" and name in profiles:
        result.update(profile=name, chosen_by="triage", reason=str(output.get("reason") or ""))
    else:
        if result["status"] == "done":
            result.update(status="error", error=f"Triage chose an unknown profile: {name!r}.")
        fallback = "previous" if previous in profiles else "default"
        result.update(
            profile=previous if fallback == "previous" else cfg["default_profile"],
            chosen_by=fallback,
            reason="",
        )
    result = redact.data(result)
    write_summary(summary(result, calls), redact)
    return result


def run_stage(
    stage,
    context,
    cfg,
    repo,
    out_dir,
    *,
    program=None,
    checks=None,
    base=None,
    commits=None,
    triage=None,
):
    """Run one stage. A plan's `triage` result is stored with it.

    `commits` gives NEXKIT_BASE_SHA and NEXKIT_VERIFIED_SHA (see `checks.find_commits`)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    start = base_commit = merged = None
    conflicts = []
    if stage in ("implement", "fix"):
        start, base_commit = baseline(repo)
    found = context.get("ci_evidence") if stage in ("plan", "implement", "fix") else None
    if stage == "fix":
        merged, conflicts = merge_base_branch(repo, base, bool(context.get("merge_moved_base")))
        if merged:
            # Once committed, the merge has the merged base commit as its merge base.
            commits = {**(commits or {}), BASE_SHA: known_base(repo, merged)}
    # The rest of the prompt's inputs, which the context job could not read: the engine
    # writes the prompt from them and what the context job fetched (`context.gather`).
    inputs = {
        "diff": {"base": base or "", "merging": {"commit": merged, "files": conflicts}},
        "workspace": workspace(cfg),
    }
    if stage == "review":
        inputs["diff"].update(
            text=review_diff(repo, base),
            since=since_review(repo, context, base),
            sides=merge_sides(repo, context),
        )
        inputs["checks"] = {"results": checks}
    # The whole texts go beside the context, outside the working tree; the agent's artifact
    # keeps them.
    folder = (out / "texts").resolve()
    answer = prompt_call(prompt_request(stage, context, inputs, cfg, folder))
    if "error" in answer:
        # No prompt, so no agent. Nothing retries it.
        result = {"stage": stage, "status": "error", "error": no_prompt(answer)}
        return _save(result, out, Redactor(), [], start=start, merged=merged, conflicts=conflicts)
    keep_texts(answer["files"], folder)
    text = answer["text"]
    (out / "prompt.md").write_text(text, encoding="utf-8")

    redact = Redactor()
    protected = [name for name in conflicts if configuration.is_protected(name, cfg)]
    if protected:
        result = {
            "stage": stage,
            "status": "error",
            "error": f"The pull request conflicts with `{base}` in protected paths, which NexKit "
            f"does not change: {', '.join(f'`{name}`' for name in protected)}. Merge `{base}` "
            "into the branch yourself, then comment `/nexkit review`.",
        }
        return _save(result, out, redact, [], start=start, merged=merged, conflicts=conflicts)

    def restore():
        """The checkout as the agent found it, for a fallback agent: the setup baseline,
        and the merge of the base branch with its conflicts."""
        git(repo, "reset", "-q", "--hard", base_commit)
        git(repo, "clean", "-q", "-fd")
        if merged:
            git(repo, *BOT, "merge", "--no-commit", "--no-ff", "-q", merged, ok=(0, 1))

    result, calls = run_agent(
        stage,
        text,
        cfg,
        cwd=repo,
        redact=redact,
        out=out,
        title=f"NexKit {stage}",
        commits=commits,
        program=program,
        restore=restore if base_commit else None,
        dirs=[
            *([found["folder"]] if found and Path(found["folder"]).is_dir() else []),
            *shortened_folders([item["path"] for item in answer["files"]]),
        ],
    )
    answer_checklist(result, stage, cfg)
    drop_criteria(result, stage, context)
    if stage in ("implement", "fix") and result["status"] == "done":
        mistake = None
        if stage == "fix":
            # A merge of the base branch that the agent ran itself is published as NexKit's.
            own, mistake = own_merge(repo, base, merged)
            if own:
                merged, conflicts = own, merge_tree(repo, base_commit, own)[1]
        left = leftover_markers(repo, conflicts)
        files, size = collect_changes(repo, base_commit, out)
        result["changed_files"] = files
        if mistake:
            result.update(status="error", error=mistake)
        elif left:
            result.update(
                status="error",
                error="The agent left conflict markers in "
                + ", ".join(f"`{name}`" for name in left)
                + ".",
            )
        elif size > cfg["limits"]["max_patch_mb"] * 1_000_000:
            result.update(status="error", error=f"The change is too large ({size} bytes).")
        elif stage == "fix" and context.get("diagnose") and not _cause(result):
            changed = own_changes(repo, files, merged, conflicts)
            if changed:
                result.update(
                    status="error",
                    error="The diagnosis round changed code without naming the cause of the "
                    "repeated failure, so NexKit publishes nothing. Changed: "
                    + ", ".join(f"`{name}`" for name in changed[:20])
                    + ".",
                )
            elif not merged:
                no_cause(result, context["diagnose"])
            # A merge of a moved base branch is published alone, as in any round after
            # failing checks: the base branch may have fixed the failure, and the next
            # checks show whether it did.
        elif stage == "implement" and no_change(result):
            if files:
                set_aside_finding(result)
            else:
                # The issue or its plan says to stop without a change here: NexKit closes
                # the issue with the finding.
                result["no_change"] = True
        elif not files and not merged:
            # After a merge an empty patch is valid: it keeps this branch's side everywhere,
            # or publishes a clean merge alone.
            if stage == "fix" and context.get("merge_moved_base") and outside_cause(result):
                # An automatic round after failing checks whose cause lies outside the code,
                # such as a registry that failed: the checks and the review run once more
                # on the same commit.
                result["recheck"] = True
            else:
                result.update(status="error", error=unchanged(result), unchanged=True)
    if stage in ("plan", "implement") and result.get("output"):
        check_closing(result["output"])
    if stage in ("implement", "fix") and result.get("output"):
        check_replans(result["output"])
    if stage in ("plan", "implement", "fix") and result.get("output"):
        # Only paths that are protected count, so a person is asked only for those.
        result["output"].pop("protected_paths", None)
        result["output"]["protected"] = needed_protected(result, cfg)
        if stage in ("plan", "implement") and no_change(result):
            result["passed_since"] = passed_runs(found)
    if triage:
        result.update(profile=triage["profile"], triage=triage)
    if answer["shortened"]:
        result["shortened"] = snake(answer["shortened"])
    read = (context.get("linked") or {}).get("read") or []
    skipped = list(answer["notRead"])
    if read or skipped:
        result["linked"] = {"read": read, "skipped": skipped}
    return _save(result, out, redact, calls, start=start, merged=merged, conflicts=conflicts)


def _save(result, out, redact, calls, *, start, merged, conflicts):
    """Store the redacted result and write the run summary."""
    # `start_base` is the base commit merged into the branch; publish makes it a parent.
    result.update(start_head=start, start_base=merged, conflicts=conflicts)
    result = redact.data(result)
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_summary(summary(result, calls), redact)
    return result


def action_for(stage, cfg, version=None):
    """The `runAgent` action for `stage` as the configuration describes it: its agent, model,
    effort and time budget. `version` is the change the stage starts from."""
    settings = configuration.stage(cfg, stage)
    return {
        **agent_run(stage, version),
        "agent": settings["agent"],
        "model": settings["model"],
        "effort": settings["effort"],
        "budget": {
            "tokens": None,
            "seconds": configuration.stage_minutes(cfg, stage) * 60,
        },
    }


def finished(result, stage):
    """The protocol's `runAgent` result for NexKit 6's `result` of `stage`. It reads only a
    result that is already redacted. The session and the change version are null: the
    publish job makes the commit, and no job asks the same session again."""
    status = result.get("status")
    if stage == "triage":
        chosen = result.get("chosen_by") == "triage"
        output = {"profile": result["profile"], "reason": result.get("reason") or ""}
        output = output if chosen else None
    else:
        output = result["output"] if isinstance(result.get("output"), dict) else None
    paused = status == "paused"
    seconds = result.get("seconds")
    return {
        "output": output,
        "tokens": result.get("tokens") or None,
        # Without a known reset, `resume_at` is NexKit's own guess, not the provider's.
        "limit": {
            "message": result["limit"],
            "reset": result["resume_at"] if result.get("reset_known") else None,
        }
        if paused
        else None,
        "transient": bool(result.get("transient")),
        "noResult": bool(result.get("unfit")),
        "error": (result.get("error") or None) if status not in ("done", "blocked") else None,
        "session": None,
        "changeVersion": None,
        "timedOut": bool(result.get("timed_out")),
        "seconds": round(seconds) if isinstance(seconds, (int, float)) else None,
    }


def run_action(action, context, cfg, repo, out, *, previous=None, program=None, **kw):
    """Run the `runAgent` action `action` and save the protocol's result as `run.json`, in
    `out` (`out/triage` for triage). Returns NexKit 6's result and the protocol's.

    The runner starts the agent the configuration names, so it refuses an action that does
    not fit the protocol, or that names another agent, model, effort, access or result
    schema, before any agent starts."""
    found = protocol.check(action, "actions/runAgent.schema.json", "action")
    if not found:
        stage = action["stage"]
        settings = configuration.stage(cfg, stage)
        wanted = {
            "agent": settings["agent"],
            "model": settings["model"],
            "effort": settings["effort"],
            "access": ACCESS[stage],
            "result": f"results/{stage}.schema.json",
        }
        found = [
            f"The action's {key} is {action[key]!r}, but the {stage} stage runs {value!r}."
            for key, value in wanted.items()
            if action[key] != value
        ]
    if found:
        raise ValueError("NexKit refuses the runAgent action: " + " ".join(found))
    stage = action["stage"]
    if stage == "triage":
        result = run_triage(context, cfg, previous, out, program=program)
        path = Path(out) / "triage" / "run.json"
    else:
        result = run_stage(stage, context, cfg, repo, out, program=program, **kw)
        path = Path(out) / "run.json"
    ran = finished(result, stage)
    path.write_text(json.dumps(ran, indent=2), encoding="utf-8")
    return result, ran

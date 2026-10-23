"""Publish a stage result: plan comments, commits and pull requests.

This job holds write tokens but never executes repository code: it only applies the
agent's patch with git and calls the GitHub API.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from . import config as configuration
from .github import GitHubError
from .gitutil import BOT_EMAIL, BOT_NAME, GitError, git, is_ancestor, merge_tree, names
from .runlog import stage_agent
from .state import (
    PLAN_MARKER,
    depends_on,
    plan_data_record,
    profile_record,
    profile_text,
    questions,
)


class PublishError(RuntimeError):
    pass


class BranchMovedError(PublishError):
    """The pull request branch moved while a fix round's agent worked."""


class WorkflowMergeError(PublishError):
    """A fix round's merge of the base branch brings workflow changes that the push token
    may not make. The round does not count against the automatic rounds."""


def _section(title, lines, level="###"):
    """A Markdown section, or nothing when it has no lines."""
    return [f"{level} {title}", "", *lines, ""] if lines else []


def _bullets(items):
    return [f"- {item.strip()}" for item in items or [] if item.strip()]


def _numbered(items):
    """A numbered list. Later lines of an item are indented under it, so an item can hold a
    code block without ending the list."""
    lines = []
    for number, item in enumerate(items or [], 1):
        marker = f"{number}. "
        first, *rest = item.strip().splitlines() or [""]
        lines.append(marker + first)
        lines += [" " * len(marker) + line if line.strip() else "" for line in rest]
    return lines


def render_profile(result, cfg):
    """(hidden record, lines for people) for a plan comment: the profile, its models and
    why it was chosen."""
    name = result.get("profile")
    if name not in cfg["profiles"]:
        raise PublishError(f"The plan names no configured profile: {name!r}")
    triage = result.get("triage") or {}
    chosen_by = triage.get("chosen_by") or "default"
    settings = configuration.with_profile(cfg, name)
    # In the format of the run comment's **Agents:** line.
    stages = []
    for stage in configuration.STAGES:
        chosen = configuration.stage(settings, stage)
        stages.append(stage_agent(stage, chosen["agent"], chosen["model"]))
    stages = " · ".join(stages)
    if chosen_by == "triage":
        reason = (triage.get("reason") or "").strip()
    else:
        error = (triage.get("error") or "no result").strip().splitlines()[0][:300]
        which = "the previous plan's profile" if chosen_by == "previous" else "the default profile"
        reason = f"Triage failed ({error}), so this plan uses {which}."
    text = (
        f"**Profile: {name}**: {stages}. {reason}\n\n"
        "To use another profile, say which in a comment and comment `/nexkit plan`, or "
        "comment `/nexkit plan <request>`."
    )
    return profile_record({"profile": name, "chosen_by": chosen_by}), profile_text(text)


def _revision(output):
    """The lines of a re-plan's *Since the last plan* section."""
    revision = output.get("revision") or {}
    changes = _bullets(revision.get("changes"))
    if revision.get("started_over"):
        head = "Planned again from scratch, as asked."
    else:
        head = "Revised the last plan."
        changes = changes or ["No decision, acceptance criterion or scope item changed."]
    return [head, *(["", *changes] if changes else [])]


def _after(part, number):
    """` (after 1 and 2)` for a part that needs earlier parts first."""
    found = [str(d) for d in depends_on(part, number)]
    if not found:
        return ""
    return (
        " (after " + (", ".join(found[:-1]) + " and " if len(found) > 1 else "") + found[-1] + ")"
    )


def question_lines(items):
    """A numbered list of questions, each with its options and the recommended one."""
    lines = []
    for number, item in enumerate(questions(items), 1):
        lines.append(f"{number}. {item['question']}")
        for option in item["options"]:
            mark = " (recommended)" if option == item["recommended"] else ""
            lines.append(f"   - {option}{mark}")
    return lines


def render_plan(output, profile=None, replan=False):
    """The plan comment: the part a person approves, then the agent's notes collapsed.
    `profile` comes from `render_profile`, when profiles are configured. A re-plan says how
    it differs from the latest plan."""
    record, text = profile or (None, None)
    parts = [PLAN_MARKER, *([record] if record else []), "## Plan", "", output["summary"].strip()]
    parts += ["", *([text, ""] if text else [])]
    if replan:
        parts += _section("Since the last plan", _revision(output))
    parts += _section("What changes", _bullets(output.get("changes")))
    decisions = [
        f"- **{item['decision'].strip()}** {item['reason'].strip()}"
        for item in output.get("decisions") or []
    ]
    parts += _section("Decisions to check", decisions)
    parts += _section("Risks and limits", _bullets(output.get("risks")))
    parts += _section(
        "Acceptance criteria", [f"- [ ] {item}" for item in output.get("acceptance_criteria", [])]
    )
    parts += _section("Questions", question_lines(output.get("questions")))
    if output.get("too_large"):
        split = output.get("split") or []
        parts += _section(
            "Suggested split",
            [
                f"{i}. **{item['title']}**{_after(item, i)}: {item['body']}"
                for i, item in enumerate(split, 1)
            ],
        )
    notes = _numbered(output.get("implementation_notes"))
    if notes:
        # GitHub renders Markdown inside <details> only after a blank line.
        parts += [
            "<details><summary>Implementation notes</summary>",
            "",
            *notes,
            "",
            "</details>",
            "",
        ]
    parts.append("---")
    if output.get("too_large"):
        parts.append(
            "This looks too large for one session. Consider opening the smaller issues above. "
            "Comment `/nexkit go` only if you want to try it as one change."
        )
    elif output.get("questions"):
        parts.append(
            "Answer the questions in a comment, then comment `/nexkit plan` to revise the plan, "
            "or `/nexkit go` to proceed with reasonable defaults."
        )
    else:
        parts.append(
            "Comment `/nexkit go` to implement this plan, or reply with changes and comment "
            "`/nexkit plan` to revise it."
        )
    parts += ["", plan_data_record(output)]
    return "\n".join(parts)


def publish_plan(gh, decision, result, context, cfg):
    profile = render_profile(result, cfg) if cfg["profiles"] else None
    body = render_plan(result["output"], profile, replan="since_plan" in context)
    comment = gh.comment(decision["issue"], body)
    return {"published": True, "comment": comment.get("html_url")}


def pull_body(decision, result, auto_merge=False):
    """Summary, what changed, how it is tested, then the optional sections."""
    output = result.get("output") or {}
    testing = (output.get("testing") or "").strip()
    parts = [f"Closes #{decision['issue']}", ""]
    parts += _section("Summary", [result["summary"].strip()], "##")
    parts += _section("What changed", _bullets(output.get("changes")), "##")
    parts += _section("How it is tested", [testing] if testing else [], "##")
    parts += _section("Outside the plan", _bullets(output.get("outside_plan")), "##")
    parts += _section("Notes for the reviewer", _bullets(output.get("reviewer_notes")), "##")
    merge = (
        "NexKit merges it once the checks pass and the review approves, unless the review "
        "leaves a criterion for a person to confirm."
        if auto_merge
        else "A person decides whether to merge."
    )
    parts += [
        "---",
        f"Implemented by NexKit from the plan in #{decision['issue']}. NexKit runs the project "
        f"checks and an AI review on every commit. {merge}",
    ]
    return "\n".join(parts)


WORKFLOWS = ".github/workflows/"
# What GitHub answers when a push changes workflow files without the permission to.
WORKFLOW_REFUSAL = re.compile(r"refusing to allow .* to create or update workflow")


def _changed(repo, *args):
    return names(git(repo, "diff", "--name-only", "--no-renames", "-z", *args))


def workflow_message(base, files):
    """Why a merge of the base branch cannot be pushed, and what a person can do."""
    listed = ", ".join(f"`{name}`" for name in files) if files else "workflow files"
    return (
        f"Merging `{base}` brings changes to {listed}, and the token NexKit pushes with may not "
        "change workflow files. Merge the base branch into the pull request yourself and "
        "comment `/nexkit review`, or name a token that may update workflows in "
        "`publish.push_token_secret`."
    )


def _workflow_error(base, files):
    return WorkflowMergeError(workflow_message(base, files))


def _on_base(repo, commit, base):
    """Whether `commit` is a full SHA that the fetched base branch contains."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return False
    # A commit that is gone from the base branch, say after a force push, may not exist here.
    if git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", ok=(0, 1)) == "":
        return False
    return is_ancestor(repo, commit, f"origin/{base}")


def _merged_base(repo, decision, result):
    """The base commit that the fix round merged into the branch, checked, or None."""
    commit = result.get("start_base")
    if decision["action"] != "fix" or not commit:
        return None
    base = decision["base"]
    if not _on_base(repo, commit, base):
        raise PublishError(f"The merged commit {str(commit)[:12]} is not on `{base}`.")
    return commit


def _moved_workflows(base, files):
    listed = ", ".join(f"`{name}`" for name in files) if files else "workflow files"
    return (
        f"`{base}` changed {listed} while the agent worked, and the token NexKit pushes with "
        "may not change workflow files, so the change must start from the current "
        f"`{base}`"
    )


def _implement_start(repo, decision, result):
    """The base commit the implement agent started from, checked. The pull request starts
    there, so the patch applies even when the base branch moved during the run."""
    commit = result.get("start_head")
    base = decision["base"]
    if not _on_base(repo, commit, base):
        raise PublishError(
            f"The agent started from {str(commit)[:12]}, which is no longer on `{base}`. "
            "Comment `/nexkit go` to run again on the current base branch."
        )
    return commit


def apply_change(repo, decision, result, title, patch_path, cfg, *, can_push_workflows=False):
    """Create the commit on the NexKit branch. Return the new head SHA.

    An implement round commits on the base commit the agent started from, which may be
    behind the base branch by now; a fix round brings it up to date. When the fix round
    merged the base branch, the commit is a merge commit whose parents are the branch head
    and the merged base commit."""
    branch = decision["branch"]
    refs = [decision["base"]] + ([branch] if decision["action"] == "fix" else [])
    git(repo, "fetch", "--quiet", "origin", *refs)
    moved = []
    if decision["action"] == "implement":
        start = _implement_start(repo, decision, result)
        if not can_push_workflows:
            # A branch that starts behind the base would carry its old workflow files, and
            # GitHub refuses such a push from a token that may not change workflows.
            moved = _changed(repo, start, f"origin/{decision['base']}", "--", WORKFLOWS)
        git(repo, "checkout", "-q", "-B", branch, f"origin/{decision['base']}" if moved else start)
    else:
        git(repo, "checkout", "-q", "-B", branch, f"origin/{branch}")
        current = git(repo, "rev-parse", "HEAD").strip()
        if current != result["start_head"]:
            raise BranchMovedError(
                f"The branch moved from {result['start_head'][:7]} to {current[:7]} while the "
                "agent was working. Comment `/nexkit fix` to run again on the new commit."
            )
    merged = _merged_base(repo, decision, result)
    if Path(patch_path).stat().st_size:
        try:
            git(repo, "apply", "--index", "--whitespace=nowarn", str(patch_path))
        except GitError:
            if not moved:
                raise
            raise PublishError(
                f"{_moved_workflows(decision['base'], moved)}, where the change does not "
                "apply. Comment `/nexkit go` to run again on the current base branch, or name "
                "a token that may update workflows in `publish.push_token_secret`."
            ) from None
    files = _changed(repo, "--cached", "HEAD")
    edits = files
    if merged:
        # Only what differs from git's own merge result is the agent's work. Changes that
        # merged cleanly from the base branch may touch protected paths.
        edits = _changed(repo, "--cached", merge_tree(repo, "HEAD", merged)[0])
    blocked = [name for name in edits if configuration.is_protected(name, cfg)]
    if blocked:
        raise PublishError(
            "The change modifies protected paths, which NexKit does not publish: "
            + ", ".join(f"`{name}`" for name in blocked)
        )
    if not files and not merged:
        raise PublishError("The agent's patch is empty after applying it to the branch.")
    workflows = [name for name in files if name.startswith(WORKFLOWS)]
    if merged and workflows and not can_push_workflows:
        raise _workflow_error(decision["base"], workflows)
    if decision["action"] == "implement":
        message = f"{title} (#{decision['issue']})"
    elif merged:
        message = f"Merge {decision['base']} and address feedback on #{decision['pr']}"
    else:
        message = f"Address feedback on #{decision['pr']}"
    message = f"{message}\n\n{result['summary'].strip()}\n"
    cause = str((result.get("output") or {}).get("cause") or "").strip()
    if decision["action"] == "fix" and cause:
        message += f"\nCause: {cause}\n"
    bot = ("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}")
    if merged:
        tree = git(repo, "write-tree").strip()
        commit = git(
            repo, *bot, "commit-tree", tree, "-p", "HEAD", "-p", merged, "-F", "-", input=message
        ).strip()
        git(repo, "reset", "-q", "--soft", commit)
    else:
        git(repo, *bot, "commit", "-q", "--no-verify", "-F", "-", input=message)
    push = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
    if decision["action"] == "implement":
        # No open PR uses this branch (checked when routing); replace any stale attempt.
        push.insert(1, "--force")
    try:
        git(repo, *push)
    except GitError as exc:
        if merged and WORKFLOW_REFUSAL.search(str(exc)):
            raise _workflow_error(decision["base"], workflows) from None
        if decision["action"] == "implement" and WORKFLOW_REFUSAL.search(str(exc)):
            raise PublishError(
                f"GitHub refused the push because it would change workflow files: `"
                f"{decision['base']}` probably changed them again while NexKit published. "
                "Comment `/nexkit go` to run again on the current base branch, or name a token "
                "that may update workflows in `publish.push_token_secret`."
            ) from None
        raise
    return git(repo, "rev-parse", "HEAD").strip()


def publish(gh, decision, result, context, cfg, repo, out_dir, *, author=None, push_token=False):
    """`gh` posts as the Actions bot. `author` opens pull requests; a personal or App token
    there lets the repository's own CI run on NexKit pull requests. `push_token` says that
    git pushes with such a token rather than the Actions token, which may never change
    workflow files."""
    if result.get("status") != "done":
        return {"published": False}
    if decision["action"] == "plan":
        return publish_plan(gh, decision, result, context, cfg)
    patch = (Path(out_dir) / "changes.patch").resolve()
    # A merge whose resolution keeps this branch's side everywhere has an empty patch.
    empty_ok = decision["action"] == "fix" and result.get("start_base")
    if not patch.is_file() or not (patch.stat().st_size or empty_ok):
        raise PublishError("The agent result has no patch to publish.")
    head = apply_change(
        repo, decision, result, context["title"], patch, cfg, can_push_workflows=push_token
    )
    pr = decision["pr"]
    if decision["action"] == "implement":
        pull = (author or gh).create_pull(
            context["title"],
            decision["branch"],
            decision["base"],
            pull_body(decision, result, cfg["merge"]["auto"]),
        )
        pr = pull["number"]
        copy_issue_fields(gh, decision["issue"], pr)
    return {"published": True, "pr": pr, "head": head}


def copy_issue_fields(gh, issue, pr):
    """Give a new pull request the labels and milestone of its issue. The Actions bot sets
    them, so no workflow starts for it. The pull request exists by now, so a failure only
    warns."""
    try:
        found = gh.issue(issue)
        milestone = (found.get("milestone") or {}).get("number")
        labels = [label["name"] for label in found.get("labels") or [] if label.get("name")]
        if milestone is not None or labels:
            gh.update_issue(pr, milestone=milestone, labels=labels)
    except GitHubError as exc:
        print(
            f"warning: could not copy the labels and milestone of #{issue}: {exc}", file=sys.stderr
        )

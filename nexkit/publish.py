"""Publish a stage result: plan comments, commits and pull requests.

This job holds write tokens but never executes repository code: it only applies the
agent's patch with git and calls the GitHub API.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config as configuration
from .gitutil import BOT_EMAIL, BOT_NAME, GitError, git, is_ancestor, merge_tree, names
from .state import PLAN_MARKER


class PublishError(RuntimeError):
    pass


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


def render_plan(output):
    """The plan comment: the part a person approves, then the agent's notes collapsed."""
    parts = [PLAN_MARKER, "## Plan", "", output["summary"].strip(), ""]
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
    parts += _section(
        "Questions", [f"{i}. {q}" for i, q in enumerate(output.get("questions") or [], 1)]
    )
    if output.get("too_large"):
        split = output.get("split") or []
        parts += _section(
            "Suggested split",
            [f"{i}. **{item['title']}**: {item['body']}" for i, item in enumerate(split, 1)],
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
    return "\n".join(parts)


def publish_plan(gh, decision, result):
    comment = gh.comment(decision["issue"], render_plan(result["output"]))
    return {"published": True, "comment": comment.get("html_url")}


def pull_body(decision, result):
    """Summary, what changed, how it is tested, then the optional sections."""
    output = result.get("output") or {}
    testing = (output.get("testing") or "").strip()
    parts = [f"Closes #{decision['issue']}", ""]
    parts += _section("Summary", [result["summary"].strip()], "##")
    parts += _section("What changed", _bullets(output.get("changes")), "##")
    parts += _section("How it is tested", [testing] if testing else [], "##")
    parts += _section("Outside the plan", _bullets(output.get("outside_plan")), "##")
    parts += _section("Notes for the reviewer", _bullets(output.get("reviewer_notes")), "##")
    parts += [
        "---",
        f"Implemented by NexKit from the plan in #{decision['issue']}. NexKit runs the project "
        "checks and an AI review on every commit. A person decides whether to merge.",
    ]
    return "\n".join(parts)


WORKFLOWS = ".github/workflows/"
# What GitHub answers when a push changes workflow files without the permission to.
WORKFLOW_REFUSAL = re.compile(r"refusing to allow .* to create or update workflow")


def _changed(repo, *args):
    return names(git(repo, "diff", "--name-only", "--no-renames", "-z", *args))


def _workflow_error(base, files):
    listed = ", ".join(f"`{name}`" for name in files) if files else "workflow files"
    return PublishError(
        f"Merging `{base}` brings changes to {listed}, and the token NexKit pushes with may not "
        "change workflow files. Merge the base branch into the pull request yourself and "
        "comment `/nexkit review`, or set a `NEXKIT_PUSH_TOKEN` secret that may update "
        "workflows."
    )


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
    if decision["action"] == "implement":
        git(repo, "checkout", "-q", "-B", branch, _implement_start(repo, decision, result))
    else:
        git(repo, "checkout", "-q", "-B", branch, f"origin/{branch}")
        current = git(repo, "rev-parse", "HEAD").strip()
        if current != result["start_head"]:
            raise PublishError(
                f"The branch moved from {result['start_head'][:7]} to {current[:7]} while the "
                "agent was working. Comment `/nexkit fix` to run again on the new commit."
            )
    merged = _merged_base(repo, decision, result)
    if Path(patch_path).stat().st_size:
        git(repo, "apply", "--index", "--whitespace=nowarn", str(patch_path))
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
        return publish_plan(gh, decision, result)
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
            context["title"], decision["branch"], decision["base"], pull_body(decision, result)
        )
        pr = pull["number"]
    return {"published": True, "pr": pr, "head": head}

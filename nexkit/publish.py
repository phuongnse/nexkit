"""Publish a stage result: plan comments, commits and pull requests.

This job holds write tokens but never executes repository code: it only applies the
agent's patch with git and calls the GitHub API.
"""

from __future__ import annotations

from pathlib import Path

from . import config as configuration
from .gitutil import BOT_EMAIL, BOT_NAME, git
from .state import PLAN_MARKER


class PublishError(RuntimeError):
    pass


def render_plan(output):
    parts = [PLAN_MARKER, "## Plan", "", output["summary"].strip(), ""]
    if output.get("approach", "").strip():
        parts += ["### Approach", "", output["approach"].strip(), ""]
    if output.get("acceptance_criteria"):
        parts += ["### Acceptance criteria", ""]
        parts += [f"- [ ] {item}" for item in output["acceptance_criteria"]]
        parts.append("")
    if output.get("questions"):
        parts += ["### Questions", ""]
        parts += [f"{i}. {q}" for i, q in enumerate(output["questions"], 1)]
        parts.append("")
    if output.get("too_large") and output.get("split"):
        parts += ["### Suggested split", ""]
        parts += [
            f"{i}. **{item['title']}**: {item['body']}" for i, item in enumerate(output["split"], 1)
        ]
        parts.append("")
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
    return (
        f"Closes #{decision['issue']}\n\n{result['summary'].strip()}\n\n---\n"
        f"Implemented by NexKit from the plan in #{decision['issue']}. NexKit runs the project "
        "checks and an AI review on every commit. A person decides whether to merge."
    )


def apply_change(repo, decision, result, title, patch_path, cfg):
    """Create the commit on the NexKit branch. Return the new head SHA."""
    branch = decision["branch"]
    refs = [decision["base"]] + ([branch] if decision["action"] == "fix" else [])
    git(repo, "fetch", "--quiet", "origin", *refs)
    if decision["action"] == "implement":
        git(repo, "checkout", "-q", "-B", branch, f"origin/{decision['base']}")
    else:
        git(repo, "checkout", "-q", "-B", branch, f"origin/{branch}")
        current = git(repo, "rev-parse", "HEAD").strip()
        if current != result["start_head"]:
            raise PublishError(
                f"The branch moved from {result['start_head'][:7]} to {current[:7]} while the "
                "agent was working. Comment `/nexkit fix` to run again on the new commit."
            )
    git(repo, "apply", "--index", "--whitespace=nowarn", str(patch_path))
    names = git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD")
    files = [name for name in names.split("\0") if name]
    blocked = [name for name in files if configuration.is_protected(name, cfg)]
    if blocked:
        raise PublishError(
            "The change modifies protected paths, which NexKit does not publish: "
            + ", ".join(f"`{name}`" for name in blocked)
        )
    if not files:
        raise PublishError("The agent's patch is empty after applying it to the branch.")
    if decision["action"] == "implement":
        message = f"{title} (#{decision['issue']})"
    else:
        message = f"Address feedback on #{decision['pr']}"
    git(
        repo,
        "-c",
        f"user.name={BOT_NAME}",
        "-c",
        f"user.email={BOT_EMAIL}",
        "commit",
        "-q",
        "--no-verify",
        "-F",
        "-",
        input=f"{message}\n\n{result['summary'].strip()}\n",
    )
    push = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
    if decision["action"] == "implement":
        # No open PR uses this branch (checked when routing); replace any stale attempt.
        push.insert(1, "--force")
    git(repo, *push)
    return git(repo, "rev-parse", "HEAD").strip()


def publish(gh, decision, result, context, cfg, repo, out_dir, *, author=None):
    """`gh` posts as the Actions bot. `author` opens pull requests; a personal or App token
    there lets the repository's own CI run on NexKit pull requests."""
    if result.get("status") != "done":
        return {"published": False}
    if decision["action"] == "plan":
        return publish_plan(gh, decision, result)
    patch = (Path(out_dir) / "changes.patch").resolve()
    if not patch.is_file() or not patch.stat().st_size:
        raise PublishError("The agent result has no patch to publish.")
    head = apply_change(repo, decision, result, context["title"], patch, cfg)
    pr = decision["pr"]
    if decision["action"] == "implement":
        pull = (author or gh).create_pull(
            context["title"], decision["branch"], decision["base"], pull_body(decision, result)
        )
        pr = pull["number"]
    return {"published": True, "pr": pr, "head": head}

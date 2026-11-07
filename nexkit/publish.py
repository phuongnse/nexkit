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
from .gitutil import git, git_retrying, is_ancestor, merge_tree, names
from .runlog import stage_agent
from .schema import filled
from .state import (
    PLAN_MARKER,
    PROTECTED_ENTRY,
    depends_on,
    passed_lines,
    plan_data_record,
    profile_record,
    profile_text,
    questions,
)

# How long `publish` waits before each further try of a push, a fetch or a GitHub API call
# that failed on a server error, a rate limit or a dropped connection, in seconds. When
# every try fails, the round stops as transient, and NexKit runs its command once more.
WAITS = (10, 30, 60)


class PublishError(RuntimeError):
    pass


class BranchMovedError(PublishError):
    """The pull request branch moved while a fix round's agent worked."""


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


def _protected(paths):
    """The plan comment's lines about a change in protected paths."""
    listed = ", ".join(f"`{path}`" for path in paths)
    return [
        f"The change modifies protected paths, which NexKit's agents may not change: {listed}. "
        "A person makes that change by hand, as [the troubleshooting entry]"
        f"({PROTECTED_ENTRY}) says."
    ]


def render_plan(output, profile=None, replan=False, passed=()):
    """The plan comment: the part a person approves, then the agent's notes collapsed.
    `profile` comes from `render_profile`, when profiles are configured. A re-plan says how
    it differs from the latest plan. `passed` holds the runs that passed since the CI runs
    the issue names, shown with a finding that no change is needed."""
    finding = filled(output.get("no_change"))
    protected = output.get("protected_paths") or []
    record, text = profile or (None, None)
    parts = [PLAN_MARKER, *([record] if record else []), "## Plan", "", output["summary"].strip()]
    parts += ["", *([text, ""] if text else [])]
    if replan:
        parts += _section("Since the last plan", _revision(output))
    if finding:
        parts += _section(
            "No change needed", [finding, *(["", *passed_lines(passed)] if passed else [])]
        )
    if protected:
        parts += _section("Needs a hand-made change", _protected(protected))
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
        "Checklist",
        [
            f"- **{entry['item']}** {entry['how'].strip()}"
            for entry in output.get("checklist") or []
        ],
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
    if finding:
        parts.append(
            "This plan finds that no change is needed. With autopilot, NexKit closes the issue "
            "with this finding. Otherwise close it yourself, or reply with what still needs "
            "changing and comment `/nexkit plan`."
        )
    elif protected:
        parts.append(
            "A person makes the change in protected paths by hand, in a pull request that "
            "closes this issue. Comment `/nexkit go` only for what the plan changes outside "
            "them, or reply with changes and comment `/nexkit plan` to revise it."
        )
    elif output.get("too_large"):
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
    parts += ["", plan_data_record(output, passed)]
    return "\n".join(parts)


def publish_plan(gh, decision, result, context, cfg):
    profile = render_profile(result, cfg) if cfg["profiles"] else None
    passed = result.get("passed_since")
    body = render_plan(result["output"], profile, replan="since_plan" in context, passed=passed)
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


def _changed(repo, *args):
    return names(git(repo, "diff", "--name-only", "--no-renames", "-z", *args))


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


def identity(gh, cfg):
    """The `git -c` options that make a commit the App's: its bot's name and noreply address,
    which needs the bot's user id, so GitHub links the commit to the bot."""
    login = configuration.app_login(cfg)
    try:
        email = f"{gh.user(login)['id']}+{login}@users.noreply.github.com"
    except (GitHubError, KeyError):
        email = f"{login}@users.noreply.github.com"
    return ("-c", f"user.name={login}", "-c", f"user.email={email}")


def apply_change(repo, decision, result, title, patch_path, cfg, bot, *, up_to_date=False):
    """Create the commit on the NexKit branch and push it. Return (the new head SHA, what
    the base branch's tip meant for it, see `catch_up`).

    An implement round commits on the base commit the agent started from, which may be
    behind the base branch by now. When the fix round merged the base branch, the commit is
    a merge commit whose parents are the branch head and the merged base commit. With
    `up_to_date`, the base branch's rules want pull requests up to date, and a merge of its
    tip follows the round's commit when it merges cleanly."""
    branch = decision["branch"]
    refs = [decision["base"]] + ([branch] if decision["action"] == "fix" else [])
    git_retrying(repo, "fetch", "--quiet", "origin", *refs, waits=WAITS)
    if decision["action"] == "implement":
        git(repo, "checkout", "-q", "-B", branch, _implement_start(repo, decision, result))
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
    if merged:
        tree = git(repo, "write-tree").strip()
        commit = git(
            repo, *bot, "commit-tree", tree, "-p", "HEAD", "-p", merged, "-F", "-", input=message
        ).strip()
        git(repo, "reset", "-q", "--soft", commit)
    else:
        git(repo, *bot, "commit", "-q", "--no-verify", "-F", "-", input=message)
    found = catch_up(repo, decision, cfg, bot, up_to_date)
    push = ["push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"]
    if decision["action"] == "implement":
        # No open PR uses this branch (checked when routing); replace any stale attempt.
        push.insert(1, "--force")
    git_retrying(repo, *push, waits=WAITS)
    return git(repo, "rev-parse", "HEAD").strip(), found


def catch_up(repo, decision, cfg, bot, up_to_date):
    """Compare the round's commit at HEAD with the base branch's tip as `publish` fetched
    it. Returns {} when HEAD contains the tip, or when it is behind and nothing needs it.

    - `conflicts`: the files where the tip conflicts with HEAD, with
      `conflicts.auto_resolve`. Such a head cannot merge, so its checks and review do not
      run, and the conflict round starts at once.
    - `updated`: the tip, which a merge commit on top of HEAD now brings in, when the rules
      want pull requests up to date (`up_to_date`), so the checks run on a head that can
      merge."""
    base = decision["base"]
    tip = git(repo, "rev-parse", f"origin/{base}").strip()
    if is_ancestor(repo, tip, "HEAD"):
        return {}
    tree, conflicts = merge_tree(repo, "HEAD", tip)
    if conflicts:
        return {"conflicts": conflicts} if cfg["conflicts"]["auto_resolve"] else {}
    if not up_to_date:
        return {}
    message = (
        f"Merge {base} into {decision['branch']}\n\nThe rules of `{base}` want pull requests "
        "up to date, so the checks run on a head that contains it.\n"
    )
    commit = git(
        repo, *bot, "commit-tree", tree, "-p", "HEAD", "-p", tip, "-F", "-", input=message
    ).strip()
    git(repo, "reset", "-q", "--soft", commit)
    return {"updated": tip}


def publish(gh, decision, result, context, cfg, repo, out_dir):
    """`gh` and the checkout's git both act as the repository's GitHub App, so what they do
    starts the repository's own workflows, as a person's push would."""
    if result.get("status") != "done":
        return {"published": False}
    if decision["action"] in ("plan", "implement") and issue_closed(gh, decision["issue"]):
        # A person closed the issue while the round ran: nothing is pushed or posted, and
        # the issue stays as they left it.
        return {"published": False, "issue_closed": True}
    if decision["action"] == "plan":
        return publish_plan(gh, decision, result, context, cfg)
    if result.get("no_change"):
        # Nothing to publish: `report` closes the issue with the agent's finding.
        return {"published": False, "no_change": True}
    if result.get("recheck"):
        # Nothing to publish: the checks and the review run again on the same commit.
        return {"published": False, "recheck": True, "pr": decision["pr"], "head": decision["head"]}
    patch = (Path(out_dir) / "changes.patch").resolve()
    # A merge whose resolution keeps this branch's side everywhere has an empty patch.
    empty_ok = decision["action"] == "fix" and result.get("start_base")
    if not patch.is_file() or not (patch.stat().st_size or empty_ok):
        raise PublishError("The agent result has no patch to publish.")
    head, found = apply_change(
        repo,
        decision,
        result,
        context["title"],
        patch,
        cfg,
        identity(gh, cfg),
        up_to_date=wants_up_to_date(gh, decision["base"]),
    )
    pr = decision["pr"]
    if decision["action"] == "implement":
        pull = open_pull(
            gh,
            context["title"],
            decision["branch"],
            decision["base"],
            pull_body(decision, result, cfg["merge"]["auto"]),
        )
        pr = pull["number"]
        copy_issue_fields(gh, decision["issue"], pr)
    return {"published": True, "pr": pr, "head": head, **found}


def open_pull(gh, title, branch, base, body):
    """Open the pull request of `branch`. GitHub may open it and still answer a try with a
    server error, so the client's next try is refused as a duplicate (422): then the open
    pull request of the branch is the one. `route` checked that none was open before."""
    try:
        return gh.create_pull(title, branch, base, body)
    except GitHubError as exc:
        found = gh.open_pulls(branch) if exc.status == 422 else []
        if found:
            return found[0]
        raise


def wants_up_to_date(gh, base):
    """Whether the rules of `base` want pull requests up to date with it (see
    `GitHub.wants_up_to_date`). A rule NexKit cannot read counts as off: the round then
    publishes what it published before."""
    try:
        return gh.wants_up_to_date(base)
    except GitHubError as exc:
        print(f"warning: could not read the rules of `{base}`: {exc}", file=sys.stderr)
        return False


def issue_closed(gh, issue):
    """Whether the issue is no longer open. `route` checked it when the round started."""
    return gh.issue(issue).get("state") != "open"


def copy_issue_fields(gh, issue, pr):
    """Give a new pull request the labels and milestone of its issue. The pull request
    exists by now, so a failure only warns."""
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

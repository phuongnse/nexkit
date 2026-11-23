"""Collect the GitHub material an agent stage needs, before the agent runs.

Gathering happens in its own step so the agent process never holds a GitHub token. This
module only reads: it fetches the texts a stage's prompt needs, as the inputs of the engine's
`prompt` call (docs/engine.md#the-engine), and the engine writes the prompt's texts from them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .autopilot import (
    LINEAGE,
    PART,
    edited_after_vouch,
    in_repository,
    part_of_parent,
    vouch,
)
from .checks import RESULTS_DIR, failing_signatures, results_limit
from .config import NAME
from .engine import prompt
from .engine.prompt import NO_PLAN as NO_PLAN
from .engine.prompt import PERSON_PLAN as PERSON_PLAN
from .evidence import EVIDENCE_LIMIT, collect, named_runs, summary_lines
from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .redact import Redactor
from .route import parse_command
from .runlog import write_summary
from .state import (
    PLAN_MARKER,
    answers_text,
    by_bot,
    latest_plan,
    plan_text,
    read_state,
)
from .state import TEXT_LIMIT as TEXT_LIMIT

# The section of a NexKit pull request description that `publish.pull_body` writes from the
# implement result's `outside_plan`.
OUTSIDE_PLAN = re.compile(r"^## Outside the plan\n(.*?)(?=^## |^---$|\Z)", re.M | re.S)


def _trusted(item):
    return item.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(item)


def entries(comments):
    """A discussion as the prompt reads it: collaborators' comments, and the answers
    autopilot gave for the owner (`prompt.discussion`). A command's comment counts only
    with a note: the agent that acts on a command gets its note as the round's note."""
    found = []
    for comment in comments:
        entry = {"id": comment["id"], "at": comment["created_at"]}
        answered = answers_text(comment)
        if answered is not None:
            login = (comment.get("user") or {}).get("login") or ""
            found.append({**entry, "author": login, "text": answered, "kind": "answers"})
            continue
        if not _trusted(comment):
            continue
        command, note = parse_command(comment.get("body"))
        if command and not note:
            continue
        found.append(
            {
                **entry,
                "author": comment["user"]["login"],
                "text": comment.get("body") or "",
                "kind": "note" if command else "comment",
            }
        )
    return found


def _saved_feedback(comments):
    """The checks and review that NexKit last recorded on the pull request."""
    _, state = read_state(comments)
    return (state or {}).get("feedback") or {}


def _after_failed_checks(decision, comments):
    """Whether this is an automatic round started after failing checks, NexKit's own or
    required checks that failed when NexKit merged. It merges the base
    branch whenever the base has moved, so a failure that the base branch already fixed
    goes away without an agent change."""
    if not decision.get("auto") or decision.get("conflicts"):
        return False
    feedback = _saved_feedback(comments)
    if feedback.get("required"):
        return True
    return any(not check.get("passed") for check in feedback.get("checks") or [])


def _inside(path):
    """Whether a recorded result path stays inside the folder it is relative to."""
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith(("/", "~"))
        and ".." not in path.split("/")
    )


def evidence(feedback, decision, folder):
    """The result files that the failing checks of the run in `results_run` kept, as the
    agent job downloaded them into `folder`, outside the working tree, or None. Says in
    the log why there are none."""
    run_id = decision.get("results_run")
    if not run_id:
        return None
    root = Path(folder).resolve() / RESULTS_DIR if folder else None
    if not root or not root.is_dir():
        print(
            "No result files of the failing checks: the `nexkit-checks` artifact of run "
            f"{run_id} could not be downloaded, for example because it expired. The fix "
            "prompt has no section about them.",
            flush=True,
        )
        return None
    found = []
    for check in feedback.get("checks") or []:
        name = check.get("name")
        if check.get("passed") or not isinstance(name, str) or not NAME.match(name):
            continue
        paths = []
        for path in check.get("results") or []:
            target = root / name / path if _inside(path) else None
            if not target or not target.exists():
                continue
            files = sum(f.is_file() for f in target.rglob("*")) if target.is_dir() else 1
            paths.append({"path": path, "folder": target.is_dir(), "files": files})
        if paths:
            left_out = check.get("results_left_out")
            found.append(
                {
                    "name": name,
                    "run": str(check.get("run") or ""),
                    "folder": str(root / name),
                    "paths": paths,
                    "left_out": left_out if isinstance(left_out, int) else 0,
                }
            )
    if not found:
        print(f"Run {run_id} kept no result files of the failing checks.", flush=True)
        return None
    print(f"Result files of the failing checks are in {root}.", flush=True)
    return {"folder": str(root), "checks": found}


def earlier_fixes(comments):
    """The fix prompt's list of the pull request's earlier fix rounds (`prompt.earlier_fixes`),
    from NexKit's state on it."""
    _, state = read_state(comments)
    return prompt.earlier_fixes((state or {}).get("fixes"))


def diagnosis(decision, feedback):
    """The checks that this automatic round diagnoses, each with the failure it repeated,
    or []. `route` named them in the decision."""
    names = decision.get("diagnose") or []
    signatures = failing_signatures(feedback.get("checks"))
    return [{"name": name, "signature": signatures[name]} for name in names if name in signatures]


def people(gh, decision):
    """What collaborators said on the pull request's head: their reviews with a text, then
    their review comments on a line."""
    pr, head = decision["pr"], decision["head"]
    found = []
    for review in gh.reviews(pr):
        if _trusted(review) and review.get("commit_id") == head and (review.get("body") or ""):
            found.append(
                {
                    "kind": "review",
                    "id": review.get("id"),
                    "author": review["user"]["login"],
                    "state": review["state"],
                    "text": review["body"],
                }
            )
    for comment in gh.review_comments(pr):
        if _trusted(comment) and comment.get("line") is not None:
            found.append(
                {
                    "kind": "comment",
                    "id": comment.get("id"),
                    "author": comment["user"]["login"],
                    "path": comment["path"],
                    "line": comment["line"],
                    "text": comment["body"],
                }
            )
    return found


def _feedback(gh, decision, comments, shelf=None):
    """Earlier NexKit results plus current human review feedback on the pull request."""
    saved = _saved_feedback(comments)
    return prompt.feedback(
        saved.get("checks"),
        saved.get("required"),
        saved.get("review"),
        people(gh, decision),
        shelf,
    )


def current_fix(decision, fix_result):
    """This run's fix round, which the review after it reads, or None."""
    if not _fixed(decision, fix_result):
        return None
    return {
        "auto": decision.get("auto"),
        "summary": fix_result.get("summary"),
        "cause": (fix_result.get("output") or {}).get("cause"),
        "note": decision.get("note"),
        "conflicts": fix_result.get("conflicts"),
        "head": fix_result.get("start_head"),
        "base": fix_result.get("start_base"),
    }


def previous_review(gh, decision, fix_result):
    """The last NexKit review of the pull request and the fix rounds since, or None."""
    _, state = read_state(gh.comments(decision["pr"]))
    state = state or {}
    current = current_fix(decision, fix_result)
    return prompt.previous_review(state.get("last_review"), state.get("fixes"), current)


def outside_section(pull):
    """The section of the pull request description that NexKit wrote about what the
    implementation agent did outside the plan, or None."""
    body = ((pull or {}).get("body") or "").replace("\r\n", "\n")
    match = OUTSIDE_PLAN.search(body)
    return match.group(1) if match else None


def outside_plan(pull):
    """What the implementation agent did outside the plan, and why, from the pull request
    description that NexKit wrote."""
    return prompt.outside_plan(outside_section(pull))


def _fixed(decision, fix_result):
    return decision["action"] == "fix" and (fix_result or {}).get("status") == "done"


def ci_evidence(gh, cfg, issue, comments, folder, workflow=""):
    """Download the evidence of the CI runs the issue names into `folder`, before the agent
    runs, and say in the log and the run summary what was kept, missing or skipped. Runs of
    NexKit's own workflow, the file `workflow`, are skipped. None when the issue names no
    run."""
    runs, skipped = named_runs(gh, issue, comments)
    if not runs and not skipped:
        return None
    redact = Redactor()
    try:
        found = collect(gh, cfg, runs, folder, redact, workflow)
        found["skipped"] = [*skipped, *found["skipped"]]
    except Exception as exc:  # noqa: BLE001 - the round runs without the evidence
        print(redact(f"Could not collect the evidence of CI runs: {exc}"), flush=True)
        return None
    lines = [*summary_lines(found), *(f"Not collected: {reason}." for reason in found["skipped"])]
    for line in lines:
        print(redact(line), flush=True)
    write_summary("\n".join(["### NexKit: evidence from CI runs", "", *lines]) + "\n", redact)
    return found


# The most earlier parts a part's plan reads.
MAX_EARLIER = 6


def _part_of(issue):
    """The number of the issue that a part of a split came from, or None."""
    match = PART.match(issue.get("body") or "")
    return int(match.group(1)) if match else None


def earlier_parts(gh, issue):
    """{parent, parts} for a part of a split that needs earlier parts first: the issues
    that block it and are parts of the same issue, oldest first, each with its title,
    state, text and latest plan, as `prompt.earlier_parts` reads them. None for any other
    issue. A failed read leaves out what it could not read: the plan goes on without it."""
    parent = _part_of(issue)
    if parent is None:
        return None
    found = []
    try:
        blockers = sorted(gh.blocked_by(issue["number"]), key=lambda b: b["number"])
        for blocker in blockers[: MAX_EARLIER * 2]:
            earlier = gh.issue(blocker["number"])
            if _part_of(earlier) != parent:
                continue
            plan = latest_plan(gh.comments(earlier["number"]))
            found.append(
                {
                    "id": earlier["number"],
                    "title": earlier.get("title") or "",
                    "state": earlier.get("state") or "",
                    "body": PART.sub("", earlier["body"]),
                    "plan": plan_text(plan) if plan else None,
                }
            )
    except GitHubError:
        pass
    parts = found[:MAX_EARLIER]
    return {"parent": parent, "parts": parts} if parts else None


# A reference such as `#12`, which GitHub links to an issue or pull request of this
# repository, or `acme/app#12`; and a bare anchor such as `(#issuecomment-123)`, which
# names a comment of this repository by its id. GitHub links nothing inside code, so no
# link counts there.
ISSUE_REF = re.compile(r"(?<![\w/&#.-])(?:([\w-]+/[\w.-]+))?#(\d+)\b")
COMMENT_ANCHOR = re.compile(r"(?<![\w/.-])#issuecomment-(\d+)\b")
CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`|<!--.*?-->", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# The most linked comments, and issues or pull requests, one round tries to read, in the
# order they were linked, and the most links the prompt lists as not read.
MAX_LINKED = 20
MAX_LINKED_ISSUES = 10
MAX_SKIPPED = 30


def _links():
    """The links to GitHub's pages of a repository, on the server the workflow runs on:
    to a comment, to a review or a review comment of a pull request, to a discussion, and
    to an issue or a pull request as a whole. Each names the repository first."""
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    host = re.escape(re.sub(r"^https?://(?:www\.)?", "", server.rstrip("/")))
    base = rf"https?://(?:www\.)?{host}/([\w-]+/[\w.-]+)"
    return [
        ("comment", re.compile(base + r"/(?:issues|pull)/\d+#issuecomment-(\d+)")),
        (
            "review",
            re.compile(base + r"/pull/(\d+)(?:/files)?#(?:discussion_r|pullrequestreview-|r)\d+"),
        ),
        ("discussion", re.compile(base + r"/discussions/(\d+)")),
        ("issue", re.compile(base + r"/(?:issues|pull)/(\d+)(?![\d#])(?!/files#)")),
    ]


def ancestors(gh, issue):
    """(the issues `issue` is part of, nearest first, lines saying what was not read). That
    is its parent on GitHub, the parent's parent, and so on: for a part of a split, the
    issues it was split from, by NexKit or by a person, up to the root. Each step counts
    only when the sub-issue is a part whose round reads its parent
    (`autopilot.part_of_parent`); a part marker without such a parent names no issue."""
    found, child, lines, seen = [], issue, [], {issue["number"]}
    try:
        for _ in range(LINEAGE):
            parent = gh.parent_issue(child["number"])
            if not parent or not in_repository(gh, parent) or parent["number"] in seen:
                break
            if not part_of_parent(gh, child, parent):
                where = f"#{parent['number']}, which #{child['number']} is a sub-issue of"
                lines.append(
                    f"{where}: #{child['number']} is no part of it that NexKit or a writer opened"
                )
                break
            found.append(parent)
            seen.add(parent["number"])
            child = parent
    except GitHubError as exc:
        lines.append(
            f"the issue #{child['number']} is a sub-issue of: could not be read ({exc.status})"
        )
    claimed = _part_of(issue)
    if claimed is not None and not found and not lines:
        lines.append(
            f"#{claimed}, which this issue's text names as the issue it was split from: "
            "NexKit did not split this issue from it"
        )
    return found, lines


def chain_edited(gh, chain):
    """Who changed an issue of `chain`, the issues a part was split from, after a writer
    vouched for it who may not, or None. A part's round reads their texts too."""
    for issue in chain:
        edited = edited_after_vouch(gh, issue)
        if edited:
            return f"#{issue['number']}, which this issue was split from: {edited}"
    return None


def _references(gh, texts, own, skip):
    """(what `texts` link to, each once and in the order linked: ("comment", id) or
    ("issue", number) of this repository, lines about the links NexKit does not read).
    Comments in `own` and issues in `skip` are shown already, so they are left out."""
    found, skipped = [], []
    this = gh.repository.lower()
    patterns = _links()
    for text in texts:
        plain = CODE.sub(lambda m: " " * len(m.group()), text)
        links = []
        for kind, pattern in patterns:
            links += [(m.start(), m.end(), kind, *m.groups()) for m in pattern.finditer(plain)]
        links += [
            (m.start(), m.end(), "comment", None, m.group(1))
            for m in COMMENT_ANCHOR.finditer(plain)
        ]
        links += [(m.start(), m.end(), "issue", *m.groups()) for m in ISSUE_REF.finditer(plain)]
        end = -1
        for start, stop, kind, repository, number in sorted(links, key=lambda link: link[:2]):
            if start < end:
                continue  # a reference inside a link already counted, such as its `#12`
            end = stop
            number = int(number)
            if repository and repository.lower() != this:
                what = (
                    f"comment {number} of {repository}"
                    if kind == "comment"
                    else (f"{repository}#{number}")
                )
                skipped.append(f"{what}: not this repository")
            elif kind == "review":
                skipped.append(f"a review comment on #{number}: NexKit reads no review comments")
            elif kind == "discussion":
                skipped.append(f"discussion {number}: NexKit reads no discussions")
            elif kind == "comment" and number not in own:
                found.append(("comment", number))
            elif kind == "issue" and number not in skip:
                found.append(("issue", number))
    return list(dict.fromkeys(found)), skipped


def _issue_number(comment):
    """The number of the issue or pull request a comment is on, or None."""
    match = re.search(r"/issues/(\d+)$", comment.get("issue_url") or "")
    return int(match.group(1)) if match else None


def _linked_comment(gh, comment_id):
    """(the linked comment as `prompt.linked` reads it, what people read it as) or (None,
    why it was not read)."""
    try:
        comment = gh.issue_comment(comment_id)
    except GitHubError as exc:
        return None, f"comment {comment_id}: could not be read ({exc.status})"
    plan = by_bot(comment) and (comment.get("body") or "").startswith(PLAN_MARKER)
    if not (_trusted(comment) or plan):
        return None, f"comment {comment_id}: not written by a collaborator"
    number = _issue_number(comment)
    where = f" on #{number}" if number else ""
    found = {
        "kind": "plan" if plan else "comment",
        "id": comment_id,
        "author": comment["user"]["login"],
        "at": comment["created_at"],
        "on": number,
        # NexKit's own plan, as the agents read a plan: without the records for people.
        "text": plan_text(comment) if plan else (comment.get("body") or ""),
    }
    return found, f"{'NexKit plan' if plan else 'comment'} {comment_id}{where}"


def _text(item):
    """The text of an issue or pull request without NexKit's markers and other HTML
    comments, which nobody reading the page sees."""
    return HTML_COMMENT.sub("", item.get("body") or "").strip()


def _linked_issue(gh, number):
    """(the text of a linked issue or pull request as `prompt.linked` reads it, what people
    read it as) or (None, why it was not read)."""
    try:
        item = gh.issue(number)
    except GitHubError as exc:
        return None, f"#{number}: could not be read ({exc.status})"
    if not (_trusted(item) or by_bot(item)):
        return None, f"#{number}: not written by a collaborator"
    found = {
        "kind": "item",
        "id": number,
        "title": item.get("title") or "",
        "change": bool(item.get("pull_request")),
        "author": (item.get("user") or {}).get("login"),
        "nexkit": by_bot(item),
        "text": _text(item),
    }
    return found, f"#{number}"


def _vouched_text(gh, issue):
    """Whether the text of an issue a part was split from counts: NexKit's own, or written
    by someone who can write, or vouched for by a writer (`autopilot.vouch`)."""
    login = (issue.get("user") or {}).get("login") or ""
    try:
        return by_bot(issue) or (login and gh.can_write(login)) or vouch(gh, issue) is not None
    except GitHubError:
        return False


def linked(gh, issue, comments, chain=(), problems=(), skip=(), own=None):
    """(what the issue refers to and the agent cannot open itself, since it holds no GitHub
    token, as `prompt.linked` reads it, or None when there is nothing; what was read, in
    words). `chain` are the issues it was split from, nearest first (`ancestors`): each
    with its text and its collaborators' discussion. Then the comments and the issues or
    pull requests of this repository that the texts of the issue, of `chain` and of their
    collaborators' comments link to: comments of collaborators and NexKit's plans, texts of
    issues that collaborators or NexKit wrote, the first linked first. Comments in `own`
    (by default all of the issue's) and issues in `skip` are shown elsewhere. Each link that
    was not read is listed with why, as `problems` are, in the log and the run summary too;
    the round goes on."""
    skipped = list(problems)
    talks, counts = {}, {}
    for above in chain:
        number = above["number"]
        try:
            talks[number] = gh.comments(number)
        except GitHubError as exc:
            skipped.append(f"the discussion of #{number}: could not be read ({exc.status})")
            talks[number] = []
        counts[number] = _vouched_text(gh, above)
        if not counts[number]:
            why = "not written by a collaborator, and no writer vouched for it"
            skipped.append(f"the text of #{number}: {why}")
    texts = [issue.get("body") or ""] + [c.get("body") or "" for c in comments if _trusted(c)]
    for above in chain:
        if counts[above["number"]]:
            texts.append(above.get("body") or "")
        texts += [c.get("body") or "" for c in talks[above["number"]] if _trusted(c)]
    shown = {issue["number"], *(above["number"] for above in chain), *skip}
    own = {c["id"] for c in comments} if own is None else own
    refs, outside = _references(gh, texts, own, shown)
    skipped += outside
    found, names = [], []
    tried = {"comment": 0, "issue": 0}
    most = {"comment": MAX_LINKED, "issue": MAX_LINKED_ISSUES}
    for kind, number in refs:
        if tried[kind] >= most[kind]:
            continue
        tried[kind] += 1
        entry, line = (_linked_comment if kind == "comment" else _linked_issue)(gh, number)
        if entry is None:
            skipped.append(line)
            continue
        found.append(entry)
        names.append(line)
    beyond = []
    for kind, label in (("comment", "comments"), ("issue", "issues or pull requests")):
        left_over = sum(1 for ref in refs if ref[0] == kind) - tried[kind]
        if left_over:
            beyond.append(f"{left_over} more linked {label}: past the {most[kind]} a round reads")
    above = []
    for issue_above in chain:
        number = issue_above["number"]
        above.append(
            {
                "id": number,
                "title": issue_above.get("title") or "",
                "body": _text(issue_above) if counts[number] else None,
                "discussion": entries(talks[number]),
            }
        )
        names.append(f"#{number} with its discussion")
    skipped = list(dict.fromkeys(skipped))
    if len(skipped) > MAX_SKIPPED:
        more = len(skipped) - MAX_SKIPPED
        skipped = skipped[:MAX_SKIPPED] + [f"{more} more links not read: not listed here"]
    skipped += beyond
    redact = Redactor()
    for name in names:
        print(redact(f"Texts the issue links to: read: {name}."), flush=True)
    for line in skipped:
        print(redact(f"Texts the issue links to: not read: {line}."), flush=True)
    if not names and not skipped:
        return None, []
    lines = [f"- Read: {name}." for name in names] + [f"- Not read: {line}." for line in skipped]
    write_summary("\n".join(["### NexKit: texts the issue links to", "", *lines]) + "\n", redact)
    return {"texts": found, "chain": above, "not_read": skipped}, names


def fetch(gh, decision, stage, fix_result=None, results=None, ci=None, cfg=None, workflow=""):
    """(the inputs of `stage`'s prompt, by kind, as the engine's `prompt` reads them; what
    only the GitHub host keeps of the round: an edit nobody vouched for, the names of the
    texts the issue links to that were read, and whether a fix round merges a moved base).
    See `gather` for the arguments."""
    issue = gh.issue(decision["issue"])
    comments = gh.comments(decision["issue"])
    plan = latest_plan(comments)
    on_item = entries(comments)
    inputs = {
        "item": {
            "id": decision["issue"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "change": decision.get("pr") or None,
            "person": bool(decision.get("person")),
        },
        "notes": decision.get("note") or "",
        "discussion": {"item": on_item, "change": []},
    }
    host = {}
    if stage != "plan":
        inputs["plan"] = plan_text(plan) if plan else None
    reads = stage in ("plan", "implement", "fix", "review")
    chain, problems = ancestors(gh, issue) if reads else ([], [])
    if stage in ("plan", "implement", "fix") and (
        decision.get("auto") or decision.get("autopilot")
    ):
        # After the issue's text was read, so this round acts on no edit it does not see. A
        # person who starts a round answers for the text themselves. A part's round also
        # reads the issues it was split from.
        edited = edited_after_vouch(gh, issue) or chain_edited(gh, chain)
        if edited:
            host["untrusted"] = edited
    evidence_found = {}
    if stage in ("plan", "implement", "fix") and ci and cfg:
        found = ci_evidence(gh, cfg, issue, comments, ci, workflow)
        evidence_found["ci"] = {**found, "keeps": EVIDENCE_LIMIT} if found else None
    if reads:
        pull = [decision["pr"]] if decision.get("pr") else []
        # Links to the issue's own comments that its discussion shows are not read again.
        # The fix prompt shows no discussion, and a command's comment shows only with a note.
        own = (
            set()
            if stage == "fix"
            else {c["id"] for c in comments if _trusted(c) and not parse_command(c.get("body"))[0]}
        )
        inputs["linked"], host["read"] = linked(gh, issue, comments, chain, problems, pull, own)
    if stage == "plan":
        earlier = earlier_parts(gh, issue)
        if earlier:
            inputs["parts"] = earlier
        previous = {}
        if plan:
            at = comments.index(plan)
            previous.update(text=plan_text(plan), before=len(entries(comments[:at])))
        if decision.get("previous_profile"):
            previous["profile"] = decision["previous_profile"]
        if previous:
            inputs["previous_plan"] = previous
    if decision.get("person") or (stage == "review" and decision.get("pr")):
        _, state = read_state(gh.comments(decision["pr"]))
        state = state or {}
        inputs["findings"] = {"review": state.get("last_review")}
        inputs["rounds"] = {
            "fixes": state.get("fixes") or [],
            "current": current_fix(decision, fix_result),
        }
        if not decision.get("person"):
            inputs["discussion"]["change"] = entries(gh.comments(decision["pr"]))
            inputs["rounds"]["outside_plan"] = outside_section(gh.pull(decision["pr"]))
    elif stage == "fix":
        pr_comments = gh.comments(decision["pr"])
        saved = _saved_feedback(pr_comments)
        _, state = read_state(pr_comments)
        inputs["checks"] = {"results": saved.get("checks"), "required": saved.get("required")}
        inputs["findings"] = {"review": saved.get("review"), "people": people(gh, decision)}
        host["merge_moved_base"] = _after_failed_checks(decision, pr_comments)
        kept = evidence(saved, decision, results)
        keeps = {"one": results_limit(1), "several": results_limit(2)}
        evidence_found["results"] = {**kept, "keeps": keeps} if kept else None
        inputs["rounds"] = {
            "fixes": (state or {}).get("fixes") or [],
            "diagnose": diagnosis(decision, saved),
        }
        inputs["discussion"]["change"] = entries(pr_comments)
    if evidence_found:
        inputs["evidence"] = evidence_found
    return inputs, host


def keep_texts(files):
    """Write the whole texts that a prompt names, each `{path, text}`, before the agent
    reads them."""
    for item in files:
        path = Path(item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["text"], encoding="utf-8")


def gather(
    gh,
    decision,
    stage=None,
    fix_result=None,
    results=None,
    ci=None,
    cfg=None,
    workflow="",
    texts=None,
):
    """Context for an agent stage. `stage` defaults to the decision's action; the review
    stage also gets the previous review round, with `fix_result` from this run's fix. A
    fix round also gets the failing checks' result files, which the agent job downloaded
    into the folder `results`. With `ci` and `cfg`, a plan, implement or fix round also
    gets the evidence of the CI runs the issue names, downloaded into the folder `ci`,
    other than runs of NexKit's own workflow, the file `workflow`. A text that the prompt
    keeps only the start of goes whole to the folder `texts`, and `shortened` names it.

    The engine writes the texts from what `fetch` fetched (`prompt.texts`)."""
    stage = stage or decision["action"]
    inputs, host = fetch(gh, decision, stage, fix_result, results, ci, cfg, workflow)
    shelf = prompt.Shelf(Path(texts).resolve() if texts else None)
    context = prompt.texts(stage, inputs, shelf)
    keep_texts({"path": path, "text": text} for path, text in shelf.files.items())
    if host.get("untrusted"):
        context["untrusted"] = host["untrusted"]
    if context.get("linked") is not None:
        context["linked"]["read"] = host["read"]
    if "merge_moved_base" in host:
        context["merge_moved_base"] = host["merge_moved_base"]
    return context

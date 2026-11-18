"""Collect the GitHub material an agent stage needs, before the agent runs.

Gathering happens in its own step so the agent process never holds a GitHub token.
"""

from __future__ import annotations

import os
import re
from functools import partial
from pathlib import Path

from .autopilot import MAX_DEPTH, PART, edited_after_vouch, split_from, vouch
from .checks import RESULTS_DIR, failing_signatures, output_text, shown_command
from .config import NAME
from .evidence import collect, named_runs, summary_lines
from .github import TRUSTED_ASSOCIATIONS, GitHubError
from .redact import Redactor
from .route import parse_command
from .runlog import write_summary
from .state import (
    PLAN_MARKER,
    TEXT_LIMIT,
    answers_text,
    by_bot,
    clip,
    latest_plan,
    plan_text,
    read_state,
)

MAX_COMMENTS = 30
# What one block of comments, such as a discussion, keeps in a prompt; above TEXT_LIMIT, so
# the latest comment always comes whole. Older comments past it keep their start, with the
# whole text in a file.
BLOCK_LIMIT = 80_000
SHORT = 1_000
# What the fix prompt shows of a failing check's output: its end, and before it this many
# lines that report a failure.
FIX_OUTPUT_TAIL = 6_000
FIX_FAILURE_LINES = 20
NO_PLAN = "No plan was posted. Implement the issue as described."
PERSON_PLAN = "None: a person wrote this pull request."
# The section of a NexKit pull request description that `publish.pull_body` writes from the
# implement result's `outside_plan`.
OUTSIDE_PLAN = re.compile(r"^## Outside the plan\n(.*?)(?=^## |^---$|\Z)", re.M | re.S)


def _trusted(item):
    return item.get("author_association") in TRUSTED_ASSOCIATIONS and not by_bot(item)


class Shelf:
    """The texts that a prompt keeps only the start of, or leaves out. Each whole text goes
    to a file in `folder`, outside the working tree, which the agent reads; `cut` names
    them for the prompt and the round's comment. Without a folder no file is written."""

    def __init__(self, folder=None):
        self.folder = Path(folder).resolve() if folder else None
        self.cut = []
        self.stored = []

    def store(self, key, text):
        """Write `text` whole to a file of its own, for a text that the prompt shows part of
        in its own way, such as a failing check's output, and names the file of. Its path,
        or "" without a folder."""
        if not self.folder:
            return ""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', key)}.txt"
        path.write_text(text, encoding="utf-8")
        self.stored.append(str(path))
        return str(path)

    def _write(self, key, text):
        """The file that holds `text` whole, or "" without a folder."""
        if not self.folder:
            return ""
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{key}.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _note(self, item):
        if not any(known["key"] == item["key"] for known in self.cut):
            self.cut.append(item)

    def put(self, name, key, text, keep):
        """`text` with at most `keep` characters, and where the rest is when it has more.
        `name` says which text it is, `key` names its file."""
        if len(text) <= keep:
            return text
        path = self._write(key, text)
        self._note({"name": name, "key": key, "path": path, "kept": keep, "length": len(text)})
        where = f"The whole text is in `{path}`." if path else "The rest was left out."
        return (
            f"{text[:keep]}\n[NexKit shortened this text here: this prompt has the first "
            f"{keep:,} of its {len(text):,} characters. {where}]"
        )

    def leave_out(self, name, key, text, count):
        """Where `text`, `count` texts that the prompt leaves out, is whole: a path, or ""
        without a folder. `name` says which texts they are, `key` names their file."""
        path = self._write(key, text)
        self._note({"name": name, "key": key, "path": path, "left_out": count})
        return path


def _fit(entries, shelf, limit=BLOCK_LIMIT):
    """Texts of `entries`, each `(header, body, name, key)`, with at most `limit`
    characters in all, latest first: a body that no longer fits keeps its first SHORT
    characters, and its whole text goes to `shelf`."""
    left = limit
    shown = []
    for header, body, name, key in reversed(entries):
        body = body or ""
        whole = min(len(body), TEXT_LIMIT)
        keep = whole if len(header) + whole <= left else SHORT
        text = header + shelf.put(name, key, body, keep)
        left -= len(text)
        shown.append(text)
    return shown[::-1]


def _discussion(comments, notes=False, shelf=None, limit=BLOCK_LIMIT):
    """Collaborators' comments, and the answers autopilot gave for the owner. Command
    comments are left out, or with `notes` kept when they carry a note: the agent that acts
    on a command gets its note through the decision, the reviewer only through the
    discussion. The latest MAX_COMMENTS count, each whole as long as the block fits (see
    `_fit`); older ones go whole to `shelf`, and a note says how many were left out."""
    entries, times = [], []
    for comment in comments:
        when = comment["created_at"]
        key = f"comment-{comment['id']}"
        answered = answers_text(comment)
        if answered is not None:
            # Autopilot answered for the owner, with the options NexKit recommended.
            header = f"NexKit autopilot for the owner ({when}):\n"
            entries.append((header, answered, f"autopilot's answers of {when}", key))
            times.append(when)
            continue
        if not _trusted(comment):
            continue
        command, note = parse_command(comment.get("body"))
        if command and not (notes and note):
            continue
        login = comment["user"]["login"]
        name = f"the comment of `@{login}` of {when}"
        entries.append((f"@{login} ({when}):\n", comment["body"], name, key))
        times.append(when)
    shelf = shelf or Shelf()
    older, entries = entries[:-MAX_COMMENTS], entries[-MAX_COMMENTS:]
    texts = _fit(entries, shelf, limit)
    if older:
        texts.insert(0, _left_out(older, times[0], times[len(older) - 1], shelf))
    return "\n\n---\n\n".join(texts) if texts else "No discussion."


def _left_out(older, first, last, shelf):
    """The note that stands for the `older` entries a discussion leaves out, of the times
    `first` to `last`, which go whole to `shelf`."""
    count = len(older)
    span = first if first == last else f"{first} to {last}"
    comments = "comment" if count == 1 else "comments"
    name = f"{count} older {comments} of a discussion, of {span}"
    whole = "\n\n---\n\n".join(header + (body or "") for header, body, _, _ in older)
    path = shelf.leave_out(name, f"older-{older[-1][3]}", whole, count)
    where = ""
    if path:
        where = f" {'It is' if count == 1 else f'All {count} are'} whole in `{path}`."
    return (
        f"[NexKit left out the {count} older {comments} of this discussion here: this "
        f"prompt keeps its latest {MAX_COMMENTS}.{where}]"
    )


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


def _failing_text(fix):
    """Which checks still failed after a recorded fix round, or "" when unknown."""
    failing = fix.get("failing")
    if not isinstance(failing, list):
        return ""  # the checks did not run, or the round was recorded before NexKit kept them
    if not failing:
        return "Every check passed after it."
    items = [f"`{item.get('name')}` (`{clip(item.get('signature'), 200)}`)" for item in failing]
    return "Checks that still failed after it, with their first failure: " + ", ".join(items)


def earlier_fixes(comments):
    """The fix prompt's list of the pull request's earlier fix rounds, oldest first: who
    started each, its note, the fix agent's summary and cause, and the checks that still
    failed after it. "" before the first."""
    _, state = read_state(comments)
    lines = []
    for fix in (state or {}).get("fixes") or []:
        who = "automatic" if fix.get("auto") else "requested by a person"
        kind = ", a diagnosis round" if fix.get("diagnosis") else ""
        lines += [
            f"### Round {fix.get('round')}: fix ({who}{kind})",
            "",
            f"Note: {clip(fix.get('note'), 2000) or 'None.'}",
            "",
            "Summary from the fix agent:",
            "",
            clip(fix.get("summary")) or "None.",
            "",
        ]
        if fix.get("cause"):
            lines += ["Cause it found:", "", clip(fix["cause"], 2000), ""]
        failing = _failing_text(fix)
        if failing:
            lines += [failing, ""]
    return "\n".join(lines).strip()


def diagnosis(decision, feedback):
    """The checks that this automatic round diagnoses, each with the failure it repeated,
    or []. `route` named them in the decision."""
    names = decision.get("diagnose") or []
    signatures = failing_signatures(feedback.get("checks"))
    return [{"name": name, "signature": signatures[name]} for name in names if name in signatures]


def _required_text(item):
    """A required check that failed, so GitHub refused the merge, after NexKit's own
    checks and review passed: its job's failed commands and the tests it names."""
    name = item.get("name") or "a required check"
    lines = [f"### Required check `{name}` failed, so GitHub refused the merge", ""]
    lines.append(
        "NexKit's own checks and review passed on this commit, but this check of the "
        "repository's CI, which branch protection requires, failed."
    )
    if item.get("url"):
        lines.append(f"Its job: {item['url']}")
    commands = [f"`{clip(command, 300)}`" for command in item.get("commands") or []]
    if commands:
        lines.append(f"Commands that failed in it: {', '.join(commands)}.")
    tests = [f"`{clip(test, 300)}`" for test in (item.get("tests") or [])[:30]]
    if tests:
        lines.append(f"Tests its annotations name: {', '.join(tests)}.")
    return "\n".join(lines)


def _feedback(gh, decision, comments, shelf=None):
    """Earlier NexKit results plus current human review feedback on the pull request."""
    pr = decision["pr"]
    head = decision["head"]
    parts = []
    saved = _saved_feedback(comments)
    shelf = shelf or Shelf()
    for number, check in enumerate(saved.get("checks") or [], 1):
        if not check.get("passed"):
            key = f"check-{number}-{check['name']}"
            output = output_text(
                check,
                FIX_OUTPUT_TAIL,
                FIX_FAILURE_LINES,
                partial(shelf.store, key),
            )
            parts.append(
                f"### Failing check `{check['name']}` (`{shown_command(check)}`)\n\n"
                f"Exit code {check.get('exit_code')}. {output}"
            )
    for item in saved.get("required") or []:
        parts.append(_required_text(item))
    review = saved.get("review") or {}
    findings = [f for f in review.get("findings") or [] if f.get("severity") == "blocking"]
    if findings:
        lines = ["### Blocking findings from the NexKit review"]
        for f in findings:
            where = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "general"
            lines.append(f"- `{where}`: {f.get('body')}")
        parts.append("\n".join(lines))

    entries = []
    for review in gh.reviews(pr):
        if _trusted(review) and review.get("commit_id") == head and (review.get("body") or ""):
            login = review["user"]["login"]
            name = f"the review of `@{login}`"
            key = f"review-{review.get('id') or len(entries)}"
            entries.append((f"@{login} ({review['state']}): ", review["body"], name, key))
    for comment in gh.review_comments(pr):
        if _trusted(comment) and comment.get("line") is not None:
            login = comment["user"]["login"]
            where = f"{comment['path']}:{comment['line']}"
            name = f"the review comment of `@{login}` on `{where}`"
            key = f"review-comment-{comment.get('id') or len(entries)}"
            entries.append((f"@{login} on `{where}`: ", comment["body"], name, key))
    human = _fit(entries, shelf)
    if human:
        parts.append("### Review comments from collaborators\n\n" + "\n\n".join(human))
    return "\n\n".join(parts) if parts else "No recorded feedback. Follow the note."


def previous_review(gh, decision, fix_result):
    """The last NexKit review of the pull request and the fix rounds since, or None."""
    _, state = read_state(gh.comments(decision["pr"]))
    last = (state or {}).get("last_review")
    if not last:
        return None
    fixes = [f for f in state.get("fixes") or [] if f["round"] > last["round"]]
    if _fixed(decision, fix_result):
        fixes.append(
            {
                "auto": decision.get("auto"),
                "summary": fix_result.get("summary"),
                "cause": (fix_result.get("output") or {}).get("cause"),
                "note": decision.get("note"),
                "conflicts": fix_result.get("conflicts") if fix_result.get("start_base") else None,
            }
        )
    lines = [f"### Findings of the review of commit {last['head'][:7]} ({last['verdict']})", ""]
    for number, f in enumerate(last["findings"], 1):
        where = f"{f['file']}:{f['line']}" if f.get("file") else "general"
        lines.append(f"{number}. {f['severity']}, `{where}`: {clip(f['body'], 1500)}")
    if not last["findings"]:
        lines.append("No findings.")
    left_out = last.get("left_out") or 0
    if left_out:
        lines += [
            "",
            f"That review had {left_out} more finding{'s' if left_out != 1 else ''}, left out "
            "here: NexKit keeps a bounded number, blocking findings first. Report previous "
            "findings only for those listed.",
        ]
    lines += ["", "### Fix rounds since that review"]
    if not fixes:
        lines += ["", "None. Any later commits were pushed without a NexKit fix round."]
    for number, fix in enumerate(fixes, 1):
        who = "automatic" if fix.get("auto") else "requested by a person"
        lines += [
            "",
            f"#### Fix {number} ({who})",
            "",
            "Note from the person who requested it:",
            "",
            clip(fix.get("note")) or "None.",
            "",
            "Summary from the fix agent:",
            "",
            clip(fix.get("summary")) or "None.",
        ]
        if str(fix.get("cause") or "").strip():
            lines += ["", "Cause the fix agent found:", "", clip(fix["cause"], 2000)]
        if fix.get("conflicts"):
            files = ", ".join(f"`{name}`" for name in fix["conflicts"])
            lines += ["", f"This round merged the base branch and resolved conflicts in {files}."]
        elif fix.get("conflicts") == []:
            lines += ["", "This round merged the base branch, which merged without conflicts."]
    return {"previous_head": last["head"], "previous_round": "\n".join(lines)}


def outside_plan(pull):
    """What the implementation agent did outside the plan, and why, from the pull request
    description that NexKit wrote."""
    body = ((pull or {}).get("body") or "").replace("\r\n", "\n")
    match = OUTSIDE_PLAN.search(body)
    return clip(match.group(1)) if match and match.group(1).strip() else "None."


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


# The most earlier parts a part's plan reads, and what it keeps of each one's text.
MAX_EARLIER = 6
EARLIER_BODY = 3_000
EARLIER_PLAN = 6_000


def _part_of(issue):
    """The number of the issue that a part of a split came from, or None."""
    match = PART.match(issue.get("body") or "")
    return int(match.group(1)) if match else None


def earlier_parts(gh, issue):
    """{parent, parts} for a part of a split that needs earlier parts first: the issues
    that block it and are parts of the same issue, oldest first, each with its title,
    state, text and latest plan. None for any other issue. A failed read leaves out what
    it could not read: the plan goes on without it."""
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
                    "number": earlier["number"],
                    "title": earlier.get("title") or "",
                    "state": earlier.get("state") or "",
                    "body": clip(PART.sub("", earlier["body"]), EARLIER_BODY),
                    "plan": clip(plan_text(plan), EARLIER_PLAN) if plan else "",
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
    """(the issues NexKit split `issue` from, nearest first, a line saying what could not be
    read or None). For a part of a split that is the issue it was split from, that issue's
    own when it is a part too, and so on, as deep as parts go. Only a part that NexKit's App
    created as a sub-issue of the issue its marker names counts (`autopilot.split_from`):
    for any other issue whose text starts with a part marker, the line says so."""
    found, child = [], issue
    try:
        for _ in range(MAX_DEPTH):
            parent = split_from(gh, child)
            if parent is None:
                break
            found.append(parent)
            child = parent
    except GitHubError as exc:
        where = f"#{_part_of(child)}, which #{child['number']} was split from"
        return found, f"{where}: could not be read ({exc.status})"
    claimed = _part_of(issue)
    if claimed is not None and not found:
        return found, (
            f"#{claimed}, which this issue's text names as the issue it was split from: "
            "NexKit did not split this issue from it"
        )
    return found, None


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
    """(the entry `_fit` shows of a linked comment, what people read it as) or (None, why
    it was not read)."""
    try:
        comment = gh.issue_comment(comment_id)
    except GitHubError as exc:
        return None, f"comment {comment_id}: could not be read ({exc.status})"
    plan = by_bot(comment) and (comment.get("body") or "").startswith(PLAN_MARKER)
    if not (_trusted(comment) or plan):
        return None, f"comment {comment_id}: not written by a collaborator"
    login, when = comment["user"]["login"], comment["created_at"]
    number = _issue_number(comment)
    where = f" on #{number}" if number else ""
    # NexKit's own plan, as the agents read a plan: without the records for people.
    who, body = (
        ("NexKit's plan", plan_text(comment)) if plan else (f"@{login}", comment.get("body") or "")
    )
    header = f"{who}{where} ({when}), comment {comment_id}:\n"
    name = f"the comment {comment_id} of {'NexKit' if plan else f'`@{login}`'}{where}"
    read = f"{'NexKit plan' if plan else 'comment'} {comment_id}{where}"
    return (header, body, name, f"linked-{comment_id}"), read


def _text(item):
    """The text of an issue or pull request without NexKit's markers and other HTML
    comments, which nobody reading the page sees."""
    return HTML_COMMENT.sub("", item.get("body") or "").strip() or "No description."


def _linked_issue(gh, number):
    """(the entry `_fit` shows of the text of a linked issue or pull request, what people
    read it as) or (None, why it was not read)."""
    try:
        item = gh.issue(number)
    except GitHubError as exc:
        return None, f"#{number}: could not be read ({exc.status})"
    if not (_trusted(item) or by_bot(item)):
        return None, f"#{number}: not written by a collaborator"
    kind = "pull request" if item.get("pull_request") else "issue"
    login = (item.get("user") or {}).get("login") or "someone"
    who = "NexKit" if by_bot(item) else f"@{login}"
    header = f"#{number}, {kind} of {who}: {item.get('title') or ''}\n"
    return (header, _text(item), f"the text of #{number}", f"issue-{number}"), f"#{number}"


def _vouched_text(gh, issue):
    """Whether the text of an issue a part was split from counts: NexKit's own, or written
    by someone who can write, or vouched for by a writer (`autopilot.vouch`)."""
    login = (issue.get("user") or {}).get("login") or ""
    try:
        return by_bot(issue) or (login and gh.can_write(login)) or vouch(gh, issue) is not None
    except GitHubError:
        return False


def linked(gh, issue, comments, shelf, chain=(), problem=None, skip=(), own=None):
    """What the issue refers to and the agent cannot open itself, since it holds no GitHub
    token. `chain` are the issues it was split from, nearest first (`ancestors`): each
    with its text and its collaborators' discussion. Then the comments and the issues or
    pull requests of this repository that the texts of the issue, of `chain` and of their
    collaborators' comments link to: comments of collaborators and NexKit's plans, texts of
    issues that collaborators or NexKit wrote, the first linked first. Comments in `own`
    (by default all of the issue's) and issues in `skip` are shown elsewhere. Each block
    keeps at most BLOCK_LIMIT characters, and texts past that go whole to `shelf`. Each
    link that was not read is listed with why, as `problem` is, in the log and the run
    summary too; the round goes on. None when there is nothing."""
    skipped = [problem] if problem else []
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
    entries, read, names = [], set(), []
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
        if kind == "comment":
            read.add(number)
        entries.append(entry)
        names.append(line)
    beyond = []
    for kind, label in (("comment", "comments"), ("issue", "issues or pull requests")):
        left_over = sum(1 for ref in refs if ref[0] == kind) - tried[kind]
        if left_over:
            beyond.append(f"{left_over} more linked {label}: past the {most[kind]} a round reads")
    # The first linked first: `_fit` keeps the last entries whole.
    texts_shown = _fit(entries[::-1], shelf)[::-1] if entries else []
    found = {"comments": "\n\n---\n\n".join(texts_shown), "chain": []}
    bodies = {}
    for above in chain:
        number = above["number"]
        text = _text(above) if counts[number] else "NexKit did not read this issue's text."
        bodies[number] = shelf.put(f"the body of #{number}", f"issue-{number}", text, TEXT_LIMIT)
    left = BLOCK_LIMIT - sum(len(body) for body in bodies.values())
    for above in chain:
        number = above["number"]
        # A comment already shown above, whole, is not repeated in the discussion.
        rest = [c for c in talks[number] if c["id"] not in read]
        discussion = _discussion(rest, notes=True, shelf=shelf, limit=max(0, left))
        left -= len(discussion)
        title = (above.get("title") or "") if counts[number] else "(not read)"
        found["chain"].append(
            {"number": number, "title": title, "body": bodies[number], "discussion": discussion}
        )
        names.append(f"#{number} with its discussion")
    skipped = list(dict.fromkeys(skipped))
    if len(skipped) > MAX_SKIPPED:
        more = len(skipped) - MAX_SKIPPED
        skipped = skipped[:MAX_SKIPPED] + [f"{more} more links not read: not listed here"]
    skipped += beyond
    found["skipped"], found["read"] = skipped, names
    redact = Redactor()
    for name in names:
        print(redact(f"Texts the issue links to: read: {name}."), flush=True)
    for line in skipped:
        print(redact(f"Texts the issue links to: not read: {line}."), flush=True)
    if not names and not skipped:
        return None
    lines = [f"- Read: {name}." for name in names] + [f"- Not read: {line}." for line in skipped]
    write_summary("\n".join(["### NexKit: texts the issue links to", "", *lines]) + "\n", redact)
    return found


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
    keeps only the start of goes whole to the folder `texts`, and `shortened` names it."""
    issue = gh.issue(decision["issue"])
    comments = gh.comments(decision["issue"])
    plan = latest_plan(comments)
    shelf = Shelf(texts)
    number = decision["issue"]
    body = issue.get("body") or "No description."
    context = {
        "issue": number,
        "pr": decision.get("pr") or "",
        "title": issue["title"],
        "body": shelf.put(f"the body of #{number}", f"issue-{number}", body, TEXT_LIMIT),
        "plan": plan_text(plan) if plan else NO_PLAN,
        "note": decision.get("note") or "None.",
        "feedback": "",
        "outside_plan": "None.",
    }
    stage = stage or decision["action"]
    discussion = None
    reads = stage in ("plan", "implement", "fix", "review")
    chain, problem = ancestors(gh, issue) if reads else ([], None)
    if stage in ("plan", "implement", "fix") and (
        decision.get("auto") or decision.get("autopilot")
    ):
        # After the issue's text was read, so this round acts on no edit it does not see. A
        # person who starts a round answers for the text themselves. A part's round also
        # reads the issues it was split from.
        edited = edited_after_vouch(gh, issue) or chain_edited(gh, chain)
        if edited:
            context["untrusted"] = edited
    if stage in ("plan", "implement", "fix") and ci and cfg:
        context["ci_evidence"] = ci_evidence(gh, cfg, issue, comments, ci, workflow)
    if reads:
        pull = [decision["pr"]] if decision.get("pr") else []
        # Links to the issue's own comments that its discussion shows are not read again.
        # The fix prompt shows no discussion, and a command's comment shows only with a note.
        own = (
            set()
            if stage == "fix"
            else {c["id"] for c in comments if _trusted(c) and not parse_command(c.get("body"))[0]}
        )
        context["linked"] = linked(gh, issue, comments, shelf, chain, problem, pull, own)
    if stage == "plan":
        earlier = earlier_parts(gh, issue)
        if earlier:
            context["earlier"] = earlier
        # Triage reads the notes on earlier commands too: a later note can change the
        # profile that an earlier comment asked for.
        # Its prompt has no tools to read a file with, so it names nothing it shortened.
        context["requests"] = _discussion(comments, notes=True, shelf=Shelf())
        if plan:
            # A re-plan revises the latest plan with what was said since it.
            at = comments.index(plan)
            discussion = _discussion(comments[:at], shelf=shelf)
            context["since_plan"] = _discussion(comments[at + 1 :], notes=True, shelf=shelf)
    if decision.get("person"):
        # Its issue is the pull request itself, with its description and comments, so the
        # review needs neither a plan nor the same discussion twice.
        discussion = _discussion(comments, notes=True, shelf=shelf)
        context.update(person=True, plan=PERSON_PLAN)
        context.update(previous_review(gh, decision, fix_result) or {})
    elif stage == "review" and decision.get("pr"):
        pr_comments = gh.comments(decision["pr"])
        discussion = (
            _discussion(comments, notes=True, shelf=shelf)
            + "\n\n---\n\nOn the pull request:\n\n"
            + _discussion(pr_comments, notes=True, shelf=shelf)
        )
        context["outside_plan"] = outside_plan(gh.pull(decision["pr"]))
        context.update(previous_review(gh, decision, fix_result) or {})
        if _fixed(decision, fix_result) and fix_result.get("start_base"):
            context["merged"] = {
                "head": fix_result.get("start_head"),
                "base": fix_result["start_base"],
                "conflicts": fix_result.get("conflicts") or [],
            }
    elif stage == "fix":
        pr_comments = gh.comments(decision["pr"])
        context["feedback"] = _feedback(gh, decision, pr_comments, shelf)
        context["merge_moved_base"] = _after_failed_checks(decision, pr_comments)
        saved = _saved_feedback(pr_comments)
        context["evidence"] = evidence(saved, decision, results)
        context["earlier_fixes"] = earlier_fixes(pr_comments)
        context["diagnose"] = diagnosis(decision, saved)
        # The fix prompt shows the feedback rather than the discussion, so it names nothing
        # shortened here.
        discussion = (
            _discussion(comments, shelf=Shelf())
            + "\n\n---\n\nOn the pull request:\n\n"
            + _discussion(pr_comments, shelf=Shelf())
        )
    context["discussion"] = discussion or _discussion(comments, shelf=shelf)
    if shelf.cut:
        context["shortened"] = shelf.cut
    if shelf.stored:
        context["check_outputs"] = shelf.stored
    return context

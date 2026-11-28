"""`prompt(state, stage, inputs, policies, texts) -> (text, shortened, notRead, files)`: the
prompt of a stage, written from the texts the host fetched for it, with the limits each text
keeps and the list of what was shortened or not read (docs/engine.md#the-engine).

The host fetches; the engine writes. The inputs are named by kind, as `runAgent` names them
(`protocol.stages.INPUTS`). Inside the engine they are snake_case, as NexKit's records are; the
protocol's call sends them in camelCase, and `answer` converts them. The earlier fix rounds and
the last review are not an input: they come from the call's `state`, as `rounds.fixes` and
`rounds.lastReview`.

A prompt is written in two parts, which `write` runs one after the other: `texts`, from what
the host read about the work item, and `finish`, from what its round adds: the change's diff,
the checks' results and the workspace. The GitHub host runs them in two jobs, since the job
that reads GitHub holds a token and the one with the checkout holds none.

A text that a prompt keeps only the start of goes whole to a file in the host's folder for
texts, which the agent reads: the engine names the file in the prompt and answers its path
and text, and the host writes it. The engine reads no file but its own templates.
"""

from __future__ import annotations

import functools
import hashlib
import re
from importlib import resources
from string import Template

from .. import protocol
from ..protocol.stages import COMMIT, MAX_DIFF
from ..textutil import (
    MAX_PARTS,
    SHARD,
    TEXT_LIMIT,
    checklist_item,
    clip,
    output_text,
    person_decides,
    shown_command,
    since_text,
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
# What the review prompt shows of a failing check's output: its end, and before it this many
# lines that report a failure.
REVIEW_OUTPUT_TAIL = 3_000
REVIEW_FAILURE_LINES = 10
# What a part's plan keeps of each earlier part's text and plan.
EARLIER_BODY = 3_000
EARLIER_PLAN = 6_000
NO_PLAN = "No plan was posted. Implement the issue as described."
PERSON_PLAN = "None: a person wrote this pull request."
NO_EARLIER_FIXES = "None. This is the first fix round of this pull request."
# How the prompts mark a checklist item that a person decides.
PERSON_MARK = "(a person decides)"
# The stages whose prompts answer `review.checklist`: those with a checklist result.
CHECKLIST_STAGES = tuple(protocol.load("results/checklist.schema.json")["$defs"])
# The inputs a stage's prompt cannot be written without; `plan` may be null.
NEEDS = {
    "triage": ("item",),
    "plan": ("item", "workspace"),
    "implement": ("item", "plan", "workspace"),
    "fix": ("item", "plan", "diff", "workspace"),
    "review": ("item", "plan", "diff", "workspace"),
}
# What a file of whole texts may be named: anything else in a host's id becomes `-`.
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# An id that already ends like the hash `file_name` adds, so no two ids share a name.
HASHED = re.compile(r"-[0-9a-f]{8}$")
# An absolute Windows path: a drive and a separator, or a UNC path `\\host\share`.
WINDOWS = re.compile(r"[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+")


def absolute(folder):
    """Whether `folder` is an absolute path, POSIX or Windows (docs/engine.md#the-prompt)."""
    return folder.startswith("/") or bool(WINDOWS.match(folder))


def file_name(key):
    """The name of the file for a host's id `key`: its characters a file name can hold, and a
    short hash of the id when cleaning changed it or it ends like a hash, so two ids never
    share a file. An id that is safe keeps its name."""
    name = UNSAFE.sub("-", key)
    if name == key and not HASHED.search(key):
        return name
    return f"{name}-{hashlib.sha256(key.encode('utf-8', 'surrogatepass')).hexdigest()[:8]}"


@functools.cache
def template(name):
    """A prompt's template: the engine's own package data, the same in every call."""
    return resources.files(__package__).joinpath(f"prompts/{name}.md").read_text()


# The protocol names keys in camelCase; the engine's records, as NexKit's, in snake_case.


def _camel_key(key):
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _snake_key(key):
    return re.sub(r"[A-Z]", lambda match: "_" + match.group().lower(), key)


def _keys(value, rename):
    if isinstance(value, dict):
        return {rename(key): _keys(item, rename) for key, item in value.items()}
    if isinstance(value, list):
        return [_keys(item, rename) for item in value]
    return value


def camel(value):
    """`value` with each key of its records in camelCase, as the protocol sends it."""
    return _keys(value, _camel_key)


def snake(value):
    """`value` with each key of its records in snake_case, as the engine reads it."""
    return _keys(value, _snake_key)


class Shelf:
    """The texts that a prompt keeps only the start of, or leaves out. Each whole text goes
    to a file in `folder`, the host's absolute path outside the working tree, which the agent
    reads; `cut` names them for the prompt and the round's comment, and `files` holds each
    file's text for the host to write. Without a folder no file is named."""

    def __init__(self, folder=None):
        self.folder = str(folder) if folder else None
        # A Windows host's folder is joined with its own separator.
        self.sep = "\\" if self.folder and WINDOWS.match(self.folder) else "/"
        self.cut = []
        self.stored = []
        self.files = {}

    def _path(self, key, suffix, text):
        # A key holds a host's ids, so it is cleaned: no file lands outside the folder.
        path = f"{self.folder}{self.sep}{file_name(key)}{suffix}"
        self.files[path] = text
        return path

    def store(self, key, text):
        """Keep `text` whole in a file of its own, for a text that the prompt shows part of
        in its own way, such as a failing check's output, and names the file of. Its path,
        or "" without a folder."""
        if not self.folder:
            return ""
        path = self._path(key, ".txt", text)
        self.stored.append(path)
        return path

    def _write(self, key, text):
        """The file that holds `text` whole, or "" without a folder."""
        return self._path(key, ".md", text) if self.folder else ""

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


def discussion(entries, notes=False, shelf=None, limit=BLOCK_LIMIT):
    """A discussion's entries as the prompt shows them, oldest first. Each entry is
    {id, author, at, text, kind}: a person's `comment`, a `note` on a command, which counts
    only with `notes`, or the `answers` that autopilot gave for the owner. The latest
    MAX_COMMENTS count, each whole as long as the block fits (see `_fit`); older ones go
    whole to `shelf`, and a note says how many were left out."""
    rows, times = [], []
    for entry in entries:
        when = entry["at"]
        key = f"comment-{entry['id']}"
        if entry["kind"] == "answers":
            # Autopilot answered for the owner, with the options NexKit recommended.
            header = f"NexKit autopilot for the owner ({when}):\n"
            rows.append((header, entry["text"], f"autopilot's answers of {when}", key))
            times.append(when)
            continue
        if entry["kind"] == "note" and not notes:
            continue
        login = entry["author"]
        name = f"the comment of `@{login}` of {when}"
        rows.append((f"@{login} ({when}):\n", entry["text"], name, key))
        times.append(when)
    shelf = shelf or Shelf()
    older, rows = rows[:-MAX_COMMENTS], rows[-MAX_COMMENTS:]
    texts = _fit(rows, shelf, limit)
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


def _failing_text(fix):
    """Which checks still failed after a recorded fix round, or "" when unknown."""
    failing = fix.get("failing")
    if not isinstance(failing, list):
        return ""  # the checks did not run, or the round was recorded before NexKit kept them
    if not failing:
        return "Every check passed after it."
    items = [f"`{item.get('name')}` (`{clip(item.get('signature'), 200)}`)" for item in failing]
    return "Checks that still failed after it, with their first failure: " + ", ".join(items)


def earlier_fixes(fixes):
    """The fix prompt's list of the change's earlier fix rounds, oldest first: who started
    each, its note, the fix agent's summary and cause, and the checks that still failed
    after it. "" before the first."""
    lines = []
    for fix in fixes or []:
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


def feedback(checks=None, required=None, review=None, people=None, shelf=None):
    """The fix prompt's feedback: the failing `checks` and `required` checks NexKit last
    recorded, the blocking findings of its last `review`, and what `people` said on the
    change's version, each {kind: review, id, author, state, text} or {kind: comment, id,
    author, path, line, text}."""
    parts = []
    shelf = shelf or Shelf()
    for number, check in enumerate(checks or [], 1):
        if not check.get("passed"):
            key = f"check-{number}-{check['name']}"
            output = output_text(
                check,
                FIX_OUTPUT_TAIL,
                FIX_FAILURE_LINES,
                functools.partial(shelf.store, key),
            )
            parts.append(
                f"### Failing check `{check['name']}` (`{shown_command(check)}`)\n\n"
                f"Exit code {check.get('exit_code')}. {output}"
            )
    for item in required or []:
        parts.append(_required_text(item))
    review = review or {}
    findings = [f for f in review.get("findings") or [] if f.get("severity") == "blocking"]
    if findings:
        lines = ["### Blocking findings from the NexKit review"]
        for f in findings:
            where = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "general"
            lines.append(f"- `{where}`: {f.get('body')}")
        parts.append("\n".join(lines))
    entries = []
    for said in people or []:
        login = said["author"]
        if said["kind"] == "review":
            name = f"the review of `@{login}`"
            key = f"review-{said.get('id') or len(entries)}"
            entries.append((f"@{login} ({said['state']}): ", said["text"], name, key))
        else:
            where = f"{said['path']}:{said['line']}"
            name = f"the review comment of `@{login}` on `{where}`"
            key = f"review-comment-{said.get('id') or len(entries)}"
            entries.append((f"@{login} on `{where}`: ", said["text"], name, key))
    human = _fit(entries, shelf)
    if human:
        parts.append("### Review comments from collaborators\n\n" + "\n\n".join(human))
    return "\n\n".join(parts) if parts else "No recorded feedback. Follow the note."


def previous_review(last, fixes=(), merged=None):
    """{previous_head, previous_round}: the `last` NexKit review of the change and the
    recorded `fixes` since, the newest of which is the round the review follows, whose merge of
    the base is `merged`; None without a review."""
    if not last:
        return None
    fixes = [dict(f) for f in fixes or [] if f["round"] > last["round"]]
    if fixes and merged and merged.get("base"):
        fixes[-1]["conflicts"] = merged.get("conflicts")
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


def outside_plan(text):
    """What the implementation agent did outside the plan, and why, as it wrote it."""
    return clip(text) if text and text.strip() else "None."


def earlier_parts(found):
    """{parent, parts} of the earlier parts that a part of a split relies on, each text and
    plan clipped, or None."""
    parts = [
        {
            "number": part["id"],
            "title": part.get("title") or "",
            "state": part.get("state") or "",
            "body": clip(part.get("body"), EARLIER_BODY),
            "plan": clip(part.get("plan"), EARLIER_PLAN),
        }
        for part in (found or {}).get("parts") or []
    ]
    return {"parent": found["parent"], "parts": parts} if parts else None


def _linked_entry(text):
    """The entry `_fit` shows of a linked text: a comment, a NexKit `plan` or an `item`."""
    if text["kind"] == "item":
        kind = "pull request" if text.get("change") else "issue"
        who = "NexKit" if text.get("nexkit") else f"@{text.get('author') or 'someone'}"
        header = f"#{text['id']}, {kind} of {who}: {text.get('title') or ''}\n"
        body = text.get("text") or "No description."
        return header, body, f"the text of #{text['id']}", f"issue-{text['id']}"
    plan = text["kind"] == "plan"
    number, login = text["id"], text.get("author")
    where = f" on #{text['on']}" if text.get("on") else ""
    who = "NexKit's plan" if plan else f"@{login}"
    header = f"{who}{where} ({text['at']}), comment {number}:\n"
    name = f"the comment {number} of {'NexKit' if plan else f'`@{login}`'}{where}"
    return header, text.get("text") or "", name, f"linked-{number}"


def linked(found, shelf):
    """What the issue refers to, as the prompt shows it: the `texts` its texts link to, the
    first linked first, and the issues of `chain` it was split from, nearest first, each
    with its text, or None when its `body` was not read, and its discussion. Each block keeps
    at most BLOCK_LIMIT characters; texts past that go whole to `shelf`. `not_read` lists
    the links not read and why. None when there is nothing."""
    if not found:
        return None
    texts, chain = found.get("texts") or [], found.get("chain") or []
    not_read = list(found.get("not_read") or [])
    if not texts and not chain and not not_read:
        return None
    entries = [_linked_entry(text) for text in texts]
    # The first linked first: `_fit` keeps the last entries whole.
    shown = _fit(entries[::-1], shelf)[::-1] if entries else []
    out = {"comments": "\n\n---\n\n".join(shown), "chain": []}
    read = {text["id"] for text in texts if text["kind"] != "item"}
    # The issues above share one block, the nearest first, each text before its discussion.
    left = BLOCK_LIMIT
    for above in chain:
        number, body = above["id"], above.get("body")
        text = (
            (body or "No description.")
            if body is not None
            else ("NexKit did not read this issue's text.")
        )
        keep = max(SHORT, min(TEXT_LIMIT, left))
        text = shelf.put(f"the body of #{number}", f"issue-{number}", text, keep)
        left -= len(text)
        # A comment already shown above, whole, is not repeated in the discussion.
        rest = [entry for entry in above.get("discussion") or [] if entry["id"] not in read]
        talk = discussion(rest, notes=True, shelf=shelf, limit=max(0, left))
        left -= len(talk)
        title = (above.get("title") or "") if body is not None else "(not read)"
        out["chain"].append({"number": number, "title": title, "body": text, "discussion": talk})
    out["skipped"] = not_read
    return out


def texts(stage, inputs, shelf, state=None):
    """The texts of a stage's prompt that come from what the host fetched, as NexKit 6's
    `context.gather` gave them: the item, its plan and discussions, what it links to, and the
    stage's own, in the order a prompt shortens them. The earlier fix rounds and the last
    review come from `state`'s `rounds`, and the version the last review must have read for
    the fix prompt to show its findings from the change's. A text the prompt shortens goes
    whole to `shelf`, and `shortened` names it."""
    item = inputs.get("item") or {}
    talk = inputs.get("discussion") or {}
    on_item, on_change = talk.get("item") or [], talk.get("change") or []
    number = item.get("id")
    plan = inputs.get("plan")
    context = {
        "issue": number,
        "pr": item.get("change") or "",
        "title": item.get("title") or "",
        "body": shelf.put(
            f"the body of #{number}",
            f"issue-{number}",
            item.get("body") or "No description.",
            TEXT_LIMIT,
        ),
        "plan": plan if plan is not None else NO_PLAN,
        "note": inputs.get("notes") or "None.",
        "feedback": "",
        "outside_plan": "None.",
    }
    planning = stage in ("plan", "triage")
    evidence = inputs.get("evidence") or {}
    if stage in ("plan", "implement", "fix") and "ci" in evidence:
        context["ci_evidence"] = _without_keeps(evidence["ci"])
    if stage in ("plan", "implement", "fix", "review"):
        context["linked"] = linked(inputs.get("linked"), shelf)
    history = snake((state or {}).get("rounds") or {})
    fixes, last = history.get("fixes") or [], history.get("last_review")
    if last:
        last = {**last, "head": last["version"]}
    merged = (inputs.get("diff") or {}).get("merged") or {}
    shown = None
    if planning:
        earlier = earlier_parts(inputs.get("parts"))
        if earlier:
            context["earlier"] = earlier
        # Triage reads the notes on earlier commands too: a later note can change the
        # profile that an earlier comment asked for.
        # Its prompt has no tools to read a file with, so it names nothing it shortened.
        context["requests"] = discussion(on_item, notes=True, shelf=Shelf())
        previous = inputs.get("previous_plan") or {}
        if previous.get("text") is not None:
            # A re-plan revises the latest plan with what was said since it.
            context["plan"] = previous["text"]
            before = previous.get("before") or 0
            shown = discussion(on_item[:before], shelf=shelf)
            context["since_plan"] = discussion(on_item[before:], notes=True, shelf=shelf)
    reviewed = previous_review(last, fixes, merged)
    if item.get("person"):
        # Its item is the change itself, with its description and comments, so the review
        # needs neither a plan nor the same discussion twice.
        shown = discussion(on_item, notes=True, shelf=shelf)
        context.update(person=True, plan=PERSON_PLAN)
        context.update(reviewed or {})
    elif stage == "review" and item.get("change"):
        shown = (
            discussion(on_item, notes=True, shelf=shelf)
            + "\n\n---\n\nOn the pull request:\n\n"
            + discussion(on_change, notes=True, shelf=shelf)
        )
        context["outside_plan"] = outside_plan(item.get("outside_plan"))
        context.update(reviewed or {})
        if merged.get("base"):
            context["merged"] = {
                "head": merged.get("head"),
                "base": merged["base"],
                "conflicts": merged.get("conflicts") or [],
            }
    elif stage == "fix":
        checks = inputs.get("checks") or {}
        version = ((state or {}).get("change") or {}).get("version")
        context["feedback"] = feedback(
            checks.get("results"),
            checks.get("required"),
            last if last and last["version"] == version else None,
            talk.get("said"),
            shelf,
        )
        results = evidence.get("results")
        context["evidence"] = _without_keeps(results) if results else None
        context["earlier_fixes"] = earlier_fixes(fixes)
        context["diagnose"] = list(checks.get("diagnose") or [])
        # The fix prompt shows the feedback rather than the discussion, so it names nothing
        # shortened here.
        shown = (
            discussion(on_item, shelf=Shelf())
            + "\n\n---\n\nOn the pull request:\n\n"
            + discussion(on_change, shelf=Shelf())
        )
    context["discussion"] = shown or discussion(on_item, shelf=shelf)
    if shelf.cut:
        context["shortened"] = shelf.cut
    if shelf.stored:
        context["check_outputs"] = shelf.stored
    return context


def _without_keeps(found):
    """Found evidence without what the host keeps of it, which only its text names."""
    if not isinstance(found, dict):
        return found
    return {key: value for key, value in found.items() if key != "keeps"}


# The texts of a stage's prompt that come from its checkout, its checks and its evidence.


def checklist_text(stage, items):
    """The prompt's section about `review.checklist`, or "" without items or for a stage
    that does not answer them, so that its prompt stays as it is."""
    if stage not in CHECKLIST_STAGES or not items:
        return ""
    numbered = "\n".join(
        f"{number}. {checklist_item(item)}" + (f" {PERSON_MARK}" if person_decides(item) else "")
        for number, item in enumerate(items, 1)
    )
    text = Template(template(f"{stage}_checklist")).safe_substitute(items=numbered).rstrip()
    if any(person_decides(item) for item in items):
        person = Template(template(f"{stage}_checklist_person")).safe_substitute(mark=PERSON_MARK)
        text = f"{text}\n\n{person.rstrip()}"
    return text


def _check_lines(checks, indent="  "):
    return [
        f"{indent}- `{c['name']}`: `{c['run']}`"
        + (
            f" (NexKit runs it in up to {c['shards']['max']} shards, each with `{SHARD}=k/N`)"
            if c.get("shards")
            else ""
        )
        for c in checks
    ]


def _checks_text(checks):
    if not checks:
        return "  - No checks are configured. Run the project's usual tests if there are any."
    return "\n".join(_check_lines(checks))


def _round_checks_text(checks):
    """The checks of the implement and fix prompts. The agent runs the fast ones in full; of
    a slow check, only the tests that cover its change, since NexKit runs every check after
    the round. With no check marked slow, the list is the same as `_checks_text`."""
    slow = [c for c in checks if c.get("slow")]
    if not slow:
        return _checks_text(checks)
    lines = _check_lines([c for c in checks if not c.get("slow")])
    lines.append(
        "  - NexKit runs these slow checks after your round. Do not run them in full. Run\n"
        "    only the tests in them that cover your change or that failed, as narrowly as\n"
        "    the repository's instructions allow (for example one test file):"
    )
    return "\n".join(lines + _check_lines(slow, indent="    "))


def check_results_text(results, shelf=None):
    """The review prompt's check results. The whole output of a failing check that it does
    not show whole goes to `shelf`, and the prompt names its file."""
    if not results:
        return "No checks are configured."
    shelf = shelf or Shelf()
    lines = []
    for number, item in enumerate(results, 1):
        state = "passed" if item["passed"] else f"FAILED (exit {item.get('exit_code')})"
        if item["passed"] and item.get("skipped"):
            # NexKit did not run it: no path it reads changed since it passed there.
            state = f"passed on `{item['skipped'][:7]}`; no path it reads changed since"
        lines.append(f"- `{item['name']}` (`{shown_command(item)}`): {state}")
        if not item["passed"]:
            key = f"check-{number}-{item['name']}"
            output = output_text(
                item,
                REVIEW_OUTPUT_TAIL,
                REVIEW_FAILURE_LINES,
                functools.partial(shelf.store, key),
            )
            lines.append(f"\n{output}\n")
    return "\n".join(lines).strip()


def clip_diff(diff, limit=MAX_DIFF):
    if len(diff) > limit:
        diff = diff[:limit] + "\n[diff truncated; read the changed files directly]"
    return diff


def diff_text(diff):
    """The review prompt's diff of the change against its base."""
    return clip_diff(diff or "") or "(no changes)"


def _files(files):
    return "\n".join(f"- `{name}`" for name in files)


def previous_round(context, base, since):
    """The review prompt's section about the previous review round, or "" for the first.
    `since` is the diff from the reviewed commit to the change, or None when the checkout
    does not have that commit."""
    commit = context.get("previous_head") or ""
    if not commit:
        return ""
    if not COMMIT.fullmatch(commit):
        changes = "(the previously reviewed commit is unknown; use the full diff)"
    elif since is None:
        changes = f"(commit {commit[:7]} is not in this checkout; use the full diff)"
    else:
        changes = clip_diff(since) or "(no changes since the previous review)"
    return Template(template("review_previous")).safe_substitute(
        previous_head=commit[:7],
        previous_round=context["previous_round"],
        changes=changes,
        base=base,
    )


def merge_review(context, base, sides):
    """The review prompt's section about a merge of the base branch in this run's fix round,
    with what each side changed in the conflicted files, or "". `sides` holds the base's
    diff and the change's in those files since their fork, or None when the checkout cannot
    show them."""
    merged = context.get("merged") or {}
    head, commit, files = merged.get("head"), merged.get("base"), merged.get("conflicts")
    if not files or not all(COMMIT.fullmatch(c or "") for c in (head, commit)):
        return ""
    if sides is None:
        sides = ["(not available in this checkout; read the files directly)"] * 2
    else:
        sides = [clip_diff(side or "", MAX_DIFF // 4) or "(none)" for side in sides]
    return Template(template("review_merge")).safe_substitute(
        base=base,
        base_commit=commit[:7],
        files=_files(files),
        base_side=sides[0],
        pr_side=sides[1],
    )


def previous_plan(context):
    """The plan prompt's section about the latest plan, or "" for a first plan."""
    if "since_plan" not in context:
        return ""
    return Template(template("plan_previous")).safe_substitute(
        plan=context["plan"], since_plan=context["since_plan"]
    )


def earlier_parts_text(context):
    """The plan prompt's section about the earlier parts that a part of a split relies
    on, or "" for an issue that is not such a part."""
    earlier = context.get("earlier") or {}
    if not earlier.get("parts"):
        return ""
    blocks = []
    for part in earlier["parts"]:
        state = "closed" if part.get("state") == "closed" else "still open"
        lines = [f"### #{part['number']}: {part['title']} ({state})", "", part["body"]]
        if part.get("plan"):
            lines += ["", "Its latest plan:", "", part["plan"]]
        blocks.append("\n".join(lines))
    return Template(template("plan_parts")).safe_substitute(
        parent=earlier["parent"], parts="\n\n".join(blocks)
    )


# What each stage does with the texts NexKit read for it, and with those it could not read.
LINKED_USE = {
    "plan": "When what this issue asks depends on a text listed under *Links NexKit did not "
    "read*, and nothing shown here, in the discussion or in the repository settles it, do not "
    "guess: ask a question about it with `recommended` 0, so that a person answers it.",
    "implement": "When what this issue asks depends on a text listed under *Links NexKit did "
    "not read*, and nothing shown here, in the plan, in the discussion or in the repository "
    "settles it, do not guess: stop and return `status: blocked` with a question about it, "
    "with `recommended` 0, so that a person answers it.",
    "review": "Check the change against what these texts require, as you check the plan's "
    "criteria: a change that breaks a rule they set is a blocking finding. When a text listed "
    "under *Links NexKit did not read* may hold a rule the change must follow, say so in a "
    "finding that is not blocking.",
}
LINKED_USE["fix"] = LINKED_USE["implement"]


def linked_text(context, stage=None):
    """The plan, implement, fix and review prompts' section about what the issue refers to:
    for a part of a split, the issues it was split from, root first, and the texts that the
    issue and those issues link to, with what NexKit could not read, or "" without any.
    `context["linked"]` comes from `linked`."""
    found = context.get("linked") or {}
    blocks = []
    chain = found.get("chain") or []
    if chain:
        names = ["this issue"] + [f"#{above['number']}" for above in chain]
        steps = ", and ".join(
            f"{names[index]} from {names[index + 1]}" for index in range(len(chain))
        )
        lines = [
            "### The issues this issue was split from",
            "",
            f"NexKit or a person split {steps}: each is a sub-issue of the next. Their texts "
            "and their discussions hold what this issue refers to, such as rules agreed there. "
            "Where they differ, the nearer issue decides, and this issue's own text decides "
            "over all of them.",
        ]
        for index in range(len(chain) - 1, -1, -1):
            above = chain[index]
            number = above["number"]
            lines += [
                "",
                f"#### #{number}: {above['title']}",
                "",
                f"The issue {names[index]} was split from.",
                "",
                above["body"],
                "",
                f"##### Discussion of #{number}",
                "",
                above["discussion"],
            ]
        blocks.append("\n".join(lines))
    if found.get("comments"):
        sources = (
            "the issue, the issues above or a collaborator's comment on them"
            if chain
            else "the issue or a collaborator's comment on it"
        )
        blocks.append(
            "### Texts linked from these issues\n\n"
            f"Each comment, issue and pull request of this repository that {sources} links "
            "to, the first linked first. Of an issue or a pull request, NexKit read only its "
            "text: a comment of its discussion counts only when it is linked itself.\n\n"
            f"{found['comments']}"
        )
    skipped = found.get("skipped") or []
    if skipped:
        lines = "\n".join(f"- {line}" for line in skipped)
        blocks.append(f"### Links NexKit did not read\n\n{lines}")
    if not blocks:
        return ""
    use = LINKED_USE.get(stage, "")
    if stage != "review" and not skipped:
        use = ""
    return (
        Template(template("linked")).safe_substitute(
            texts="\n\n".join(blocks), use=f"\n{use}\n" if use else ""
        )
        + "\n"
    )


def conflicts_text(base, commit, files):
    """The fix prompt's section about a merge of the base branch, or ""."""
    if not commit:
        return ""
    text = Template(template("fix_conflicts" if files else "fix_merged")).safe_substitute(
        base=base, base_commit=commit[:7], files=_files(files)
    )
    return text + "\n"


def _megabytes(size):
    return size // 1_000_000


def evidence_text(found, keeps):
    """The fix prompt's section about the failing checks' result files, or "" without any.
    `keeps` is what one check job keeps of them, and one of several."""
    if not found:
        return ""
    lines = []
    for check in found["checks"]:
        name, run, folder = check["name"], check["run"], check["folder"]
        lines.append(f"- Failing check `{name}` (`{run}`): `{folder}/`")
        for item in check["paths"]:
            if item["folder"]:
                files = "1 file" if item["files"] == 1 else f"{item['files']} files"
                lines.append(f"  - `{item['path']}/`: a folder with {files}")
            else:
                lines.append(f"  - `{item['path']}`: a file")
        if check.get("left_out"):
            lines.append(
                f"  - {check['left_out']} more files were left out: a check job keeps at most "
                f"{_megabytes(keeps['one'])} MB of result files, "
                f"{_megabytes(keeps['several'])} MB when the checks run in several jobs."
            )
    text = Template(template("fix_evidence")).safe_substitute(
        folder=found["folder"], checks="\n".join(lines)
    )
    return text + "\n"


def ci_evidence_text(found, keeps):
    """The plan, implement and fix prompts' section about the CI runs the issue names, or
    "" without any. `keeps` is the most a round keeps of them."""
    if not found or not (found["runs"] or found.get("skipped")):
        return ""
    lines = []
    for run in found["runs"]:
        lines.append(f"- Run {run['id']}" + (f", {run['url']}" if run["url"] else "") + ":")
        for item in run["files"]:
            if "missing" in item:
                lines.append(f"  - `{item['path']}`: missing, {item['missing']}")
            elif item.get("count"):
                lines.append(f"  - `{item['path']}`: an artifact, {item['count']}")
            else:
                lines.append(f"  - `{item['path']}`")
        lines += [f"  - Missing: {reason}." for reason in run["missing"]]
        if run.get("workflow"):
            lines.append(f"  - Since: {since_text(run)}.")
    if found.get("left_out"):
        lines.append(
            f"- Left out, past the {_megabytes(keeps)} MB a round keeps: "
            + "; ".join(found["left_out"])
        )
    lines += [f"- Not collected: {reason}." for reason in found.get("skipped") or []]
    text = Template(template("ci_evidence")).safe_substitute(
        folder=found["folder"], runs="\n".join(lines)
    )
    return text + "\n"


def shortened_text(context):
    """The plan, implement, fix and review prompts' section about the texts the prompt
    keeps only the start of or leaves out, or "" without any. `context["shortened"]` comes
    from `texts`."""
    items = [item for item in context.get("shortened") or [] if item.get("path")]
    if not items:
        return ""
    lines = [
        f"- {item['name']}: left out here, all {item['left_out']} whole in `{item['path']}`"
        if item.get("left_out")
        else f"- {item['name']}: the first {item['kept']:,} of {item['length']:,} characters "
        f"here, the whole text in `{item['path']}`"
        for item in items
    ]
    return Template(template("shortened")).safe_substitute(texts="\n".join(lines)) + "\n"


def shortened_names(context):
    """For the round's comment: which texts the prompt shortened, and by how much, or how
    many it left out."""
    return [
        {key: item[key] for key in ("name", "kept", "length", "left_out") if key in item}
        for item in context.get("shortened") or []
    ]


def diagnosis_text(repeated):
    """The fix prompt's section for a diagnosis round, or "" for any other round.
    `repeated` names each check it diagnoses with the failure it repeated."""
    if not repeated:
        return ""
    items = "\n".join(f"- `{item['name']}`: `{item['signature']}`" for item in repeated)
    return Template(template("fix_diagnosis")).safe_substitute(repeated=items)


def extras(
    stage,
    context,
    *,
    base="",
    diff=None,
    since=None,
    merging=None,
    sides=None,
    checks=None,
    keeps=None,
    shelf=None,
):
    """The texts of a stage's prompt that come from the round itself, as NexKit 6's
    `agent.run_stage` wrote them: the evidence, a fix round's merge of `base` (`merging`,
    {commit, files}), and a review's `diff`, its diff `since` the previous review, the
    `sides` of a merge in the fix round and the `checks`' results, whose whole outputs go
    to `shelf`. `keeps` says what the host keeps of evidence: {ci, results: {one, several}}."""
    keeps = keeps or {}
    extra = {}
    if stage in ("plan", "implement", "fix"):
        extra["ci_evidence"] = ci_evidence_text(context.get("ci_evidence"), keeps.get("ci"))
    if stage == "fix":
        merging = merging or {}
        files = merging.get("files") or []
        extra["conflicts"] = conflicts_text(base, merging.get("commit"), files)
        extra["evidence"] = evidence_text(context.get("evidence"), keeps.get("results"))
        extra["earlier_fixes"] = context.get("earlier_fixes") or NO_EARLIER_FIXES
        extra["diagnosis"] = diagnosis_text(context.get("diagnose"))
    if stage == "plan":
        extra["previous"] = previous_plan(context)
    if stage == "review":
        extra = {
            "base": base,
            "diff": diff_text(diff),
            "check_results": check_results_text(checks, shelf),
            "previous": previous_round(context, base, since),
            "merge": merge_review(context, base, sides),
        }
    extra["shortened"] = shortened_text(context)
    return extra


def triage_extras(context, profiles, previous):
    """The triage prompt's texts: the requests, the `profiles` to choose from, each
    {name: {when}}, and the profile the `previous` plan used."""
    return {
        "requests": context.get("requests") or context["discussion"],
        "profiles": "\n".join(
            f"- `{name}`: {profile['when'].strip()}" for name, profile in profiles.items()
        ),
        "previous": f"The latest plan of this issue used the profile `{previous}`."
        if previous
        else "None. This is the first plan of this issue with a profile.",
    }


def build(stage, context, settings, extra=None):
    """The prompt of `stage` from its texts. `settings` holds the workspace's `checks`,
    `protected_paths` and `implement_minutes`, and the host's `checklist`."""
    protected = ", ".join(f"`{p}`" for p in settings.get("protected_paths") or [])
    person = bool(context.get("person"))
    limits = Template(template("review_limits_person" if person else "review_limits"))
    checks = settings.get("checks") or []
    values = {
        **context,
        "checks": _checks_text(checks),
        "round_checks": _round_checks_text(checks),
        "writing": template("writing"),
        "protected": protected,
        "limits": limits.safe_substitute(protected=protected).rstrip(),
        "person": template("review_person") if person else "",
        "implement_minutes": settings.get("implement_minutes"),
        "checklist": checklist_text(stage, settings.get("checklist")),
        "earlier_parts": earlier_parts_text(context),
        "linked": linked_text(context, stage),
        "max_parts": MAX_PARTS,
        **(extra or {}),
    }
    return Template(template(stage)).safe_substitute({k: str(v) for k, v in values.items()})


def result_prompt(stage, wrong):
    """The prompt that asks a session that did its work for its result alone."""
    return Template(template("result_again")).safe_substitute(stage=stage, wrong=wrong)


def refusals(stage, inputs, policies):
    """Why a call cannot have a prompt of `stage`, beyond what its schema says: an input the
    stage needs, a plan's implement time, a profile triage could not read."""
    found = [f"request.inputs.{kind} is missing" for kind in NEEDS[stage] if kind not in inputs]
    seconds = _implement_seconds(policies)
    if stage == "plan" and (not isinstance(seconds, int) or isinstance(seconds, bool)):
        found.append("request.policies.budgets.implement.seconds must be an integer for a plan")
    profiles = policies.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        found.append("request.policies.profiles must be an object")
    for name, profile in (profiles if isinstance(profiles, dict) else {}).items():
        where = f"request.policies.profiles.{name}"
        if not isinstance(profile, dict):
            found.append(f"{where} must be an object")
        elif not isinstance(profile.get("when"), str):
            found.append(f"{where}.when must be a string")
    return found


def _implement_seconds(policies):
    return ((policies.get("budgets") or {}).get("implement") or {}).get("seconds")


def settings(inputs, policies):
    """What `build` reads beside the texts: the workspace's `checks` and `protected_paths`,
    the implement stage's time and the host's `checklist`."""
    workspace = inputs.get("workspace") or {}
    seconds = _implement_seconds(policies)
    return {
        "checks": workspace.get("checks") or [],
        "protected_paths": workspace.get("protected_paths") or [],
        "implement_minutes": seconds // 60 if seconds is not None else None,
        "checklist": policies.get("checklist") or [],
    }


def _keeps(inputs):
    evidence = inputs.get("evidence") or {}
    return {
        "ci": (evidence.get("ci") or {}).get("keeps"),
        "results": (evidence.get("results") or {}).get("keeps"),
    }


def finish(stage, context, inputs, policies, folder=None, keeps=None):
    """The prompt of `stage` from what `texts` wrote, `context`, and what its round adds in
    `inputs`: the `diff`, a review's `checks` and the `workspace`, with the `policies`.
    {text, shortened, not_read, files}: `files` are the whole outputs of the checks a review
    shows part of. `keeps` says what the host keeps of evidence, by default as its inputs
    say."""
    shelf = Shelf(folder)
    if stage == "triage":
        previous = (inputs.get("previous_plan") or {}).get("profile")
        extra = triage_extras(context, policies.get("profiles") or {}, previous)
    else:
        diff = inputs.get("diff") or {}
        extra = extras(
            stage,
            context,
            base=diff.get("base") or "",
            diff=diff.get("text"),
            since=diff.get("since"),
            merging=diff.get("merging"),
            sides=diff.get("sides"),
            checks=(inputs.get("checks") or {}).get("results") if stage == "review" else None,
            keeps=keeps or _keeps(inputs),
            shelf=shelf,
        )
    return {
        "text": build(stage, context, settings(inputs, policies), extra),
        "shortened": shortened_names(context),
        "not_read": list((context.get("linked") or {}).get("skipped") or []),
        "files": [{"path": path, "text": text} for path, text in shelf.files.items()],
    }


def write(stage, inputs, policies=None, folder=None, unfit=None, state=None):
    """The `prompt` call, on inputs in snake_case: {text, shortened, not_read, files}. The
    prompt of `stage` from `inputs` and the `state` it reads earlier rounds from, or, with
    `unfit`, the prompt that asks the same session for its result alone. `folder` is where the
    host keeps the texts the prompt shortens; `files` are those the host writes there before
    the agent runs."""
    if unfit is not None:
        text = result_prompt(stage, unfit)
        return {"text": text, "shortened": [], "not_read": [], "files": []}
    shelf = Shelf(folder)
    context = texts(stage, inputs, shelf, state)
    done = finish(stage, context, inputs, policies or {}, folder)
    kept = [{"path": path, "text": text} for path, text in shelf.files.items()]
    return {**done, "files": kept + done["files"]}


def answer(request):
    """The answer of the `prompt` call to a request in the protocol's camelCase."""
    written = write(
        request["stage"],
        snake(request["inputs"]),
        request["policies"],
        request.get("texts"),
        request.get("unfit"),
        request.get("state"),
    )
    return camel(written)

"""Complete approved read-only work without a source publication or merge."""

from __future__ import annotations

import re

from .common import Blocked, digest, short_summary
from .pipelines import task_pipeline
from .policy import now, require

COMMENT_BYTES = 60000


def recorded_summary(state, item):
    if "summary" in item:
        # Completed state from earlier kit revisions kept a separate copy.
        return item["summary"]
    group, key = item["record"]
    return state[group][key]["summary"]


def fence_events(body):
    """Track ordinary and quoted code fences at original line boundaries."""
    events, fence, offset = [], None, 0
    prefix = r" {0,3}(?:>[ \t]? {0,3})*"
    for line in body.splitlines(keepends=True):
        depth = re.match(prefix, line)[0].count(">")
        if fence and depth < fence[0].count(">"):
            fence = None
            events.append((offset, fence))
        match = re.match(rf"^({prefix})(`{{3,}}|~{{3,}})([^\r\n]*)", line)
        offset += len(line)
        if not match:
            continue
        quote, token, info = match.groups()
        if fence is None:
            if token[0] == "`" and "`" in info:
                continue
            fence = (quote, token, line.rstrip("\r\n"))
        elif (
            depth == fence[0].count(">")
            and token[0] == fence[1][0]
            and len(token) >= len(fence[1])
            and not info.strip()
        ):
            fence = None
        else:
            continue
        events.append((offset, fence))
    return events, fence


def fence_text(fence, limit, *, opening=False):
    if not fence:
        return ""
    value = fence[2] if opening else fence[0] + fence[1]
    # Wrappers must leave room for the original text, even with huge fences or
    # info strings. Only the wrapper changes; the original content stays intact.
    if len(value.encode()) > limit // 4:
        value = fence[1][0] * 3
    return value + "\n"


def reference_definitions(body, events):
    definitions, event, fence = {}, 0, None
    pattern = (
        r"(?m)^ {0,3}(?:>[ \t]? {0,3})*\[([^\]\r\n]+)\]:[ \t]*(?:\r?\n[ \t]*)?"
        r"(?:<[^>\r\n]*>|[^\s]+)"
        r"(?:(?:[ \t]+|[ \t]*\r?\n[ \t]*)(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?"
        r"[ \t]*(?:\r?\n|$)"
    )
    for match in re.finditer(pattern, body):
        while event < len(events) and events[event][0] <= match.start():
            fence = events[event][1]
            event += 1
        if not fence:
            label = " ".join(match[1].split()).casefold()
            definitions.setdefault(label, match[0].rstrip("\r\n"))
    return definitions


def result_chunks(body, limit):
    """Split original UTF-8 text while keeping links and continued code readable."""
    events, _ = fence_events(body)
    definitions = reference_definitions(body, events)
    links = list(
        re.finditer(
            r"!?\[[^\]\r\n]*\](?:\([^()\r\n]*(?:\([^()\r\n]*\)[^()\r\n]*)*\)|\[[^\]\r\n]*\])?",
            body,
        )
    )
    chunks, offset, event, fence = [], 0, 0, None
    while offset < len(body):
        opening = fence_text(fence, limit, opening=True)
        remaining = body[offset:]
        encoded = remaining.encode()
        capacity = limit - len(opening.encode())
        while True:
            piece = encoded[:capacity].decode("utf-8", "ignore")
            if len(piece) < len(remaining):
                for separator in ("\n\n", "\n", " "):
                    boundary = piece.rfind(separator) + len(separator)
                    if boundary >= len(piece) // 2:
                        piece = piece[:boundary]
                        break
                for link in links:
                    if offset < link.start() < offset + len(piece) < link.end():
                        piece = body[offset : link.start()]
                        break
            next_event, next_fence = event, fence
            end = offset + len(piece)
            while next_event < len(events) and events[next_event][0] <= end:
                next_fence = events[next_event][1]
                next_event += 1
            closing = fence_text(next_fence, limit)
            if closing and not piece.endswith("\n"):
                closing = "\n" + closing
            labels = {
                " ".join(label.split()).casefold() for label in re.findall(r"\[([^\]]+)\]", piece)
            }
            references = [
                value
                for label, value in definitions.items()
                if label in labels
                and value not in piece
                # A definition larger than one comment remains in the original
                # text. Repeating it as a helper must not prevent progress.
                and len(value.encode()) + len(opening.encode()) + len(closing.encode()) + 8 <= limit
            ]
            supplement = "\n\n" + "\n".join(references) if references else ""
            chunk = opening + piece + closing + supplement
            overflow = len(chunk.encode()) - limit
            if overflow <= 0:
                break
            capacity = max(4, capacity - overflow)
        chunks.append(chunk)
        offset, event, fence = end, next_event, next_fence
    return chunks


def notice(gh, number, state):
    summaries = state.get("task_result", [])
    segments, sections, definitions = [], [], {}
    for item in summaries:
        summary = recorded_summary(state, item)
        events, fence = fence_events(summary)
        references = reference_definitions(summary, events)
        if any(
            label in definitions and value != definitions[label]
            for label, value in references.items()
        ):
            segments.append("".join(sections))
            sections, definitions = [], {}
        definitions.update(references)
        closing = "\n" + fence_text(fence, COMMENT_BYTES) if fence else ""
        sections.append(f"### {item['name']}\n\n{summary}{closing}\n\n")
    segments.append("".join(sections))
    run = state.get("approval_execution", {}).get("run_key", state["run_key"]).split(".")[0]
    body = f"[View the workflow run](https://github.com/{gh.repository}/actions/runs/{run}). "
    runs = sorted({item.get("execution", state["run_key"]).split(".")[0] for item in summaries})
    if runs:
        body += (
            "Result artifacts: "
            + ", ".join(
                f"[run {value}](https://github.com/{gh.repository}/actions/runs/{value})"
                for value in runs
            )
            + ". "
        )
    body += "This issue remains open for follow-up."
    # Separate conflicting reference definitions rather than changing any
    # result's text or allowing another result to replace its link targets.
    segments = [segment + body for segment in segments]
    publication = digest(segments)

    def header(index, total):
        marker = f"<!-- nexkit:task-complete:{state['run_key']}:{publication}:{index} -->"
        title = "Work completed" if total == 1 else f"Work completed (part {index} of {total})"
        return marker + f"\n**{title}**\n\n"

    # There cannot be more parts than characters. Reserve enough header space
    # before splitting, including both part numbers and the content identity.
    characters = sum(map(len, segments))
    overhead = len(header(characters, characters).encode())
    chunks = [
        chunk for segment in segments for chunk in result_chunks(segment, COMMENT_BYTES - overhead)
    ]
    posted = {
        item.get("body")
        for item in gh.comments(number)
        if item.get("user", {}).get("login") == "github-actions[bot]"
        and item.get("user", {}).get("type") == "Bot"
    }
    for index, chunk in enumerate(chunks, 1):
        comment = header(index, len(chunks)) + chunk
        if comment not in posted:
            gh.comment(number, comment)


def finish(gh, context, *, jobs_succeeded):
    from .approvals import ApprovalRequired, record_denial, require_complete
    from .delivery import failed, revalidate
    from .steps import require_results

    require(task_pipeline(context["config"]), "Task completion requires a tasks entrypoint")
    number = context["issue"]["number"]
    state, _ = gh.get_state(number)
    require(state.get("run_key") == context["run_key"], "Cannot finalize another work round")
    if state.get("status") == "completed":
        notice(gh, number, state)
        return state
    if state.get("status") == "waiting_for_approval":
        return state
    try:
        state, revision = revalidate(gh, context)
        require(jobs_succeeded, "A required native job failed or was skipped")
        require_results(state, context)
        require_complete(gh, context, state)
        summaries = []
        prefix = context["run_key"] + "/"
        for group in ("invocations", "steps"):
            for key, record in state.get(group, {}).items():
                if key.startswith(prefix):
                    summaries.append(
                        {
                            "name": key[len(prefix) :],
                            # Keep the complete text once, in its validated
                            # invocation/step record, within the state bound.
                            "record": [group, key],
                            "execution": record.get("execution", context["run_key"]),
                        }
                    )
        require(summaries, "No managed work was completed")
        state.update(
            status="completed",
            task_result=summaries,
            activity=short_summary(f"Completed: {context['issue']['title']}"),
            feedback=None,
            updated_at=now(),
        )
        state.pop("reason", None)
        gh.save_state(number, state, revision)
    except Blocked as exc:
        if isinstance(exc, ApprovalRequired):
            record_denial(gh, context, exc)
        return failed(gh, context, str(exc))
    notice(gh, number, state)
    return state

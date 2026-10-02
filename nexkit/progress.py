"""Update one bot progress notice from persisted controller state."""

from __future__ import annotations

import os
import re
import sys

from .common import Blocked, digest
from .observability import Redactor, action_run, markdown_text, native_run_key


def enabled(repository):
    return (
        os.environ.get("NEXKIT_PROGRESS") == "1"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == repository
        and bool(action_run(repository, native_run_key()))
    )


def stamp(repository, value):
    if enabled(repository):
        value["progress"] = {"run_key": native_run_key()}


def body(gh, number, state, artifacts):
    from .github import state_message

    phase = state if state.get("status") else state.get("clarification", {})
    keys = [
        state.get("progress", {}).get("run_key"),
        phase.get("run_key"),
        state.get("run_key"),
    ]
    keys = list(dict.fromkeys(key for key in keys if action_run(gh.repository, key)))
    text = f"<!-- nexkit:progress:{int(number)} -->\n"
    text += "### NexKit progress\n\n"
    text += markdown_text(Redactor().text(state_message(number, state).split(": ", 1)[-1]))
    text += "\n\n"
    for index, key in enumerate(keys):
        text += f"[{'View current run' if index == 0 else 'Earlier work run'}]"
        text += f"({action_run(gh.repository, key)}) · attempt {key.split('.')[1]}\n\n"
    if state.get("pr"):
        text += f"[Pull request #{int(state['pr'])}](https://github.com/{gh.repository}/pull/{int(state['pr'])})\n\n"
    if artifacts:
        text += "Agent reports and activity:\n\n"
        for run, artifact in artifacts[:50]:
            label = markdown_text(artifact["name"])
            url = f"https://github.com/{gh.repository}/actions/runs/{run}/artifacts/{int(artifact['id'])}"
            text += f"- [{label}]({url})\n"
        text += "\nArtifacts expire according to the run's retention settings.\n"
    else:
        text += "Open the run to follow live job logs. Reports and activity artifacts appear as jobs finish.\n"
    return text


def sync(gh, number):
    if not enabled(gh.repository):
        return
    # Notice failures cannot undo a committed reservation or spend another call.
    try:
        for _ in range(3):
            state, _ = gh.get_state(number)
            phase = state if state.get("status") else state.get("clarification", {})
            keys = {
                state.get("progress", {}).get("run_key"),
                phase.get("run_key"),
                state.get("run_key"),
            }
            artifacts = []
            for key in sorted(key for key in keys if action_run(gh.repository, key)):
                run, attempt = key.split(".")
                try:
                    candidates = gh.api(
                        f"{gh.root}/actions/runs/{run}/artifacts?per_page=100",
                        pages=True,
                        collection="artifacts",
                    )
                except Blocked:
                    candidates = []
                for item in candidates:
                    if (
                        not item.get("expired")
                        and re.fullmatch(
                            rf"(?:agent-diagnostics|nexkit-report)-.+-{attempt}|nexkit-requirement-{attempt}",
                            item.get("name", ""),
                        )
                        and type(item.get("id")) is int
                    ):
                        artifacts.append((run, item))
            text = body(gh, number, state, artifacts)
            notices = bot_notices(gh, number)
            current, _ = gh.get_state(number)
            if digest(current) != digest(state):
                continue
            if notices:
                notice = min(notices, key=lambda item: item["id"])
                if notice["body"] != text:
                    gh.api(
                        f"{gh.root}/issues/comments/{int(notice['id'])}", "PATCH", {"body": text}
                    )
            else:
                gh.comment(number, text)
            # Concurrent controllers can both observe no notice before POST.
            # Reconcile after creation, keeping a stable canonical comment so
            # neither writer can delete the comment selected by the other.
            notices = bot_notices(gh, number)
            if notices:
                notice = min(notices, key=lambda item: item["id"])
                if notice["body"] != text:
                    gh.api(
                        f"{gh.root}/issues/comments/{int(notice['id'])}", "PATCH", {"body": text}
                    )
                for duplicate in notices:
                    if duplicate["id"] != notice["id"]:
                        try:
                            gh.api(f"{gh.root}/issues/comments/{int(duplicate['id'])}", "DELETE")
                        except Blocked as exc:
                            if "HTTP 404" not in str(exc):
                                raise
            current, _ = gh.get_state(number)
            if digest(current) != digest(state):
                continue
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a", encoding="utf-8") as output:
                    output.write(text.split("\n", 1)[1])
            return
    except (Blocked, OSError, ValueError, TypeError, KeyError):
        print(
            "NexKit progress notice could not be updated; persisted work is unchanged.",
            file=sys.stderr,
        )


def bot_notices(gh, number):
    return [
        item
        for item in gh.comments(number)
        if item.get("user", {}).get("login") == "github-actions[bot]"
        and item.get("user", {}).get("type") == "Bot"
        and item.get("body", "").startswith(f"<!-- nexkit:progress:{int(number)} -->\n")
    ]

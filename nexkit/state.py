"""Pipeline state lives in GitHub comments written by the Actions bot.

Every run has its own run comment, posted where the run was started: on the issue for
`plan` and `go`, on the pull request for a round. A hidden marker in it holds the run URL,
so `report` edits the comment its run started. The state that later rounds read lives in
one state comment on the pull request, edited in place.
"""

from __future__ import annotations

import base64
import json
import re

from .runlog import NO_DATA, agent_text, stage_agent, tokens_text
from .schema import filled

BOT_LOGIN = "github-actions[bot]"
PLAN_MARKER = "<!-- nexkit:plan -->"
STATE_PREFIX = "<!-- nexkit:state "
STATE_PATTERN = re.compile(r"<!-- nexkit:state ([A-Za-z0-9+/=]+) -->")
RUN_PREFIX = "<!-- nexkit:run "
# Only the marker that ends the comment: the text before it can quote agent output.
RUN_PATTERN = re.compile(r"<!-- nexkit:run ([A-Za-z0-9+/=]+) -->\s*\Z")
COMMIT = re.compile(r"[0-9a-f]{40}")
# A plan's profile: a hidden record on the line after the plan marker, where agent text
# cannot be, and lines for people that the agents do not receive.
PROFILE_PREFIX = "<!-- nexkit:profile "
PROFILE_RECORD = re.compile(r"\n<!-- nexkit:profile ([A-Za-z0-9+/=]+) -->")
PROFILE_TEXT = ("<!-- nexkit:profile-text -->", "<!-- /nexkit:profile-text -->")
PROFILE_TEXT_PATTERN = re.compile(
    r"<!-- nexkit:profile-text -->.*?<!-- /nexkit:profile-text -->\n*", re.S
)

# What autopilot reads from a plan: its questions and its split. A hidden record at the
# end of the plan comment, after everything the agent wrote.
PLAN_DATA_PREFIX = "<!-- nexkit:plan-data "
PLAN_DATA = re.compile(r"\n*<!-- nexkit:plan-data ([A-Za-z0-9+/=]+) -->\s*\Z")
# Answers that autopilot gave for the owner. Only where the marker ends a bot's comment.
ANSWERS_PREFIX = "<!-- nexkit:answers "
ANSWERS = re.compile(r"\n*<!-- nexkit:answers ([A-Za-z0-9+/=]+) -->\s*\Z")

RUNNING, SUCCESS, FAILURE, PAUSED = "running", "success", "failure", "paused"
# A paused run whose command another run carries on: resumed, replaced by a later command,
# or continued on the pull request.
CONTINUED = "continued"
RUN_ICONS = {RUNNING: "⏳", SUCCESS: "✅", FAILURE: "❌", PAUSED: "⏸️", CONTINUED: "⏭️"}
# Outcomes of rounds that published nothing for a reason outside the agent's work, or that
# NexKit stopped in their checks because the base branch moved. They count against neither
# `fix.max_auto_rounds` nor `conflicts.max_rounds`.
UNCOUNTED = {"needs_merge", "already_on_base", "on_base_failed", "superseded", "stop_failed"}


def conflict_rounds_used(rounds):
    """How many of a pull request's rounds count against `conflicts.max_rounds`: automatic
    conflict rounds, a resumed or retried one once, and none that stopped outside the
    agent's work."""
    return len(
        [
            run
            for run in rounds
            if run.get("conflicts")
            and not run.get("resumed")
            and not run.get("retry")
            and run.get("outcome") not in UNCOUNTED
        ]
    )


RUN_LABELS = {RUNNING: "running", PAUSED: "paused", CONTINUED: "continued"}
# Bounds for what a plan's record and a run's marker keep.
MAX_PARTS = 10
# Where the docs say what a person does at each stop.
TROUBLESHOOTING = "https://github.com/phuongnse/nexkit/blob/main/docs/troubleshooting.md"
PROTECTED_ENTRY = f"{TROUBLESHOOTING}#the-plans-change-is-in-protected-paths"
MAX_QUESTIONS = 10


def by_bot(comment):
    return (comment.get("user") or {}).get("login") == BOT_LOGIN


def clip(text, limit=4000):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def latest_plan(comments):
    """The most recent plan comment posted by NexKit, or None."""
    plans = [c for c in comments if by_bot(c) and c.get("body", "").startswith(PLAN_MARKER)]
    return plans[-1] if plans else None


def _profile_record(comment):
    """The match of the hidden profile record on the line after the plan marker, or None."""
    body = comment["body"]
    return PROFILE_RECORD.match(body, len(PLAN_MARKER)) if body.startswith(PLAN_MARKER) else None


def plan_text(comment):
    """The plan for the agents: without the profile, which is for people, and without the
    record for autopilot."""
    record = _profile_record(comment)
    body = comment["body"][record.end() if record else len(PLAN_MARKER) :]
    return PLAN_DATA.sub("", PROFILE_TEXT_PATTERN.sub("", body)).strip()


def passed_lines(passed):
    """Lines that link the newer runs that passed on the default branch since the CI runs
    an issue names, from the agent result's `passed_since`."""
    lines = []
    for run in passed or []:
        name = f"run {run['id']} of `{run['workflow']}`"
        link = f"[{name}]({run['url']})" if run.get("url") else name
        commit = f" at {run['sha'][:7]}" if run.get("sha") else ""
        lines.append(f"Passed on `{run['branch']}` since: {link}{commit}.")
    return lines


def plan_data_record(output, passed=()):
    """The hidden record at the end of a plan comment: its questions, whether it is too
    large and its split, the protected paths its change needs, and its finding when no
    change is needed."""
    data = {
        "questions": questions(output.get("questions")),
        "too_large": bool(output.get("too_large")),
    }
    protected = [clip(p, 300) for p in output.get("protected_paths") or []][:20]
    if protected:
        data["protected_paths"] = protected
    finding = filled(output.get("no_change"))
    if finding:
        lines = passed_lines(passed)
        data["no_change"] = clip(
            "\n\n".join([finding, *(["\n".join(lines)] if lines else [])]), 3500
        )
    if data["too_large"]:
        data["split"] = [
            {
                "title": clip(item.get("title"), 256),
                "body": clip(item.get("body"), 6000),
                "depends_on": depends_on(item, number),
            }
            for number, item in enumerate((output.get("split") or [])[:MAX_PARTS], 1)
        ]
    return _encode(PLAN_DATA_PREFIX, data)


def depends_on(part, number):
    """The earlier parts that part `number` of a split needs, as the agent named them.
    Only earlier parts count, so the parts never wait for each other in a cycle."""
    found = part.get("depends_on") or []
    found = found if isinstance(found, list) else []
    valid = {d for d in found if isinstance(d, int) and not isinstance(d, bool) and 0 < d < number}
    return sorted(valid)


def plan_data(comment):
    """The record of a plan comment, or None for a plan from before it was kept."""
    match = PLAN_DATA.search(comment.get("body") or "") if by_bot(comment) else None
    if not match:
        return None
    try:
        data = json.loads(base64.b64decode(match.group(1)))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def options_of(item):
    return [str(o).strip() for o in item.get("options") or [] if str(o).strip()]


def recommended_option(item):
    """The text of a question's recommended option, or "". Agents name it by its number,
    from 1, with 0 for none; records keep its text."""
    options, chosen = options_of(item), item.get("recommended")
    if isinstance(chosen, int) and not isinstance(chosen, bool):
        return options[chosen - 1] if 0 < chosen <= len(options) else ""
    chosen = str(chosen or "").strip()
    return chosen if chosen in options else ""


def questions(items):
    """Questions as {question, options, recommended}, with the recommended option's text
    or "". Plans from before 4.1 hold plain strings, without options."""
    found = []
    for item in items or []:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        options = [clip(o, 500) for o in options_of(item)[:6]]
        recommended = recommended_option(item)
        found.append(
            {
                "question": clip(item["question"], 1500),
                "options": options,
                "recommended": clip(recommended, 500) if recommended else "",
            }
        )
    return found[:MAX_QUESTIONS]


def round_questions(agent):
    """Every question of a blocked implement or fix round, for the run's marker: the
    decisions, then the conflicts, each with the file it is about."""
    if not agent or agent.get("status") != "blocked":
        return []
    output = agent.get("output") or {}
    conflicts = [
        {**item, "question": f"`{item.get('file') or ''}`: {item.get('question') or ''}"}
        for item in output.get("open_conflicts") or []
        if isinstance(item, dict)
    ]
    return questions([*(output.get("questions") or []), *conflicts])


def answers_marker(record):
    return _encode(ANSWERS_PREFIX, record)


def answers_text(comment):
    """The text of an autopilot answers comment, without its marker, or None for any other
    comment."""
    body = comment.get("body") or ""
    if not by_bot(comment) or not ANSWERS.search(body):
        return None
    return ANSWERS.sub("", body).strip()


def profile_record(record):
    """The hidden line after a plan's marker. `record` holds `profile` and `chosen_by`."""
    return _encode(PROFILE_PREFIX, record)


def profile_text(text):
    """The profile lines of a plan comment, marked so the agents do not receive them."""
    return f"{PROFILE_TEXT[0]}\n\n{text.strip()}\n\n{PROFILE_TEXT[1]}"


def plan_profile(comment):
    """The profile record of a plan comment, or None for a plan without profiles."""
    match = _profile_record(comment)
    if not match:
        return None
    try:
        record = json.loads(base64.b64decode(match.group(1)))
    except ValueError:
        return None
    return record if isinstance(record, dict) and isinstance(record.get("profile"), str) else None


def _marked(comments, pattern):
    """(comment_id, data) for each bot comment with the marker, newest first."""
    for comment in reversed(comments):
        if not by_bot(comment):
            continue
        match = pattern.search(comment.get("body", ""))
        if match:
            yield comment["id"], json.loads(base64.b64decode(match.group(1)))


def _find(comments, pattern):
    return next(_marked(comments, pattern), (None, None))


def _encode(prefix, data):
    return f"{prefix}{base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()} -->"


def run_link(run):
    """`⏳ [running](url)`, `✅ [run log](url)`, `❌ [run log](url)`, `⏸️ [paused](url)` or
    `⏭️ [continued](url)`."""
    icon = RUN_ICONS.get(run["status"], "❔")
    label = RUN_LABELS.get(run["status"], "run log")
    return f"{icon} [{label}]({run['url']})" if run.get("url") else f"{icon} {label}"


# -- run comments -------------------------------------------------------------------


def find_run(comments, url):
    """Return (comment_id, run) for the run comment of the workflow run at `url`."""
    if url:
        for comment_id, run in _marked(comments, RUN_PATTERN):
            if run.get("url") == url:
                return comment_id, run
    return None, None


def run_comments(comments):
    """(comment, run) for each NexKit run comment, oldest first."""
    for comment in comments:
        if by_bot(comment):
            match = RUN_PATTERN.search(comment.get("body", ""))
            if match:
                yield comment, json.loads(base64.b64decode(match.group(1)))


def run_marker(run):
    return _encode(RUN_PREFIX, run)


def with_run(body, run, line=""):
    """A run comment's `body` with its marker replaced by `run`'s and `line` added to its
    text, for a change after the run ended. The status line follows `run`'s status."""
    match = RUN_PATTERN.search(body)
    text = RUN_PATTERN.sub("", body).rstrip()
    if match:
        old = json.loads(base64.b64decode(match.group(1)))
        text = text.replace(run_link(old), run_link(run), 1)
    if line:
        text += f"\n\n{line}"
    return f"{text}\n\n{run_marker(run)}"


def verified_head(comments):
    """The head of the latest round whose checks all passed, from NexKit's own round
    comments on the pull request, or None."""
    passed = [
        run
        for _, run in _marked(comments, RUN_PATTERN)
        if run.get("round")
        and run.get("checks") == "passed"
        and COMMIT.fullmatch(str(run.get("head") or ""))
    ]
    return max(passed, key=lambda run: run["round"])["head"] if passed else None


def results_run(comments):
    """The id of the run whose checks the state comment last recorded, when one of those
    checks failed and kept result files in that run's `nexkit-checks` artifact, or None."""
    _, state = read_state(comments)
    feedback = (state or {}).get("feedback") or {}
    run_id = feedback.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        return None
    kept = [
        check
        for check in feedback.get("checks") or []
        if not check.get("passed") and check.get("results")
    ]
    return run_id if kept else None


def repeated_failures(comments):
    """The checks that failed the same way before and after the last fix round, as the
    state comment recorded them: the next automatic fix round diagnoses them first."""
    _, state = read_state(comments)
    repeated = ((state or {}).get("feedback") or {}).get("repeated")
    if not isinstance(repeated, list):
        return []
    return [name for name in repeated if isinstance(name, str)][:MAX_QUESTIONS]


def next_round(comments):
    """The number for a new pull request round, counting rounds of v1.1.0 state comments."""
    numbers = [run.get("round") or 0 for _, run in _marked(comments, RUN_PATTERN)]
    for _, state in _marked(comments, STATE_PATTERN):
        numbers += [row.get("round") or 0 for row in state.get("rounds") or []]
    return max(numbers, default=0) + 1


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _known(value):
    # Earlier rounds stored `-` for a value that was not known yet.
    return NO_DATA if value in (None, "", "-") else value


def render_run(run, text=""):
    """A run comment: what started the run, its status and link, and the result text."""
    if "round" in run:
        cells = (
            _known((run.get("head") or "")[:7]),
            _known(run.get("checks")),
            _known(run.get("verdict")),
            tokens_text(run.get("tokens")),
        )
        lines = [
            f"### NexKit round {run['round']}: {run['trigger']}",
            "",
            run_link(run),
            "",
            "| Commit | Checks | AI review | Tokens |",
            "|---|---|---|---|",
            "| " + " | ".join(_cell(v) for v in cells) + " |",
        ]
    else:
        by = " (autopilot)" if run.get("autopilot") else ""
        lines = [f"### NexKit `/nexkit {run['command']}`{by}", "", run_link(run)]
    agents = agents_text(run.get("agents") or [])
    if agents:
        lines += ["", agents]
    text = (text or "").strip()
    if text:
        lines += ["", text]
    return "\n".join(lines) + "\n\n" + _encode(RUN_PREFIX, run)


def _names(agents):
    agents = [agent_text(name, model) for name, model in agents]
    return ", ".join(agents[:-1]) + f" and {agents[-1]}" if len(agents) > 1 else agents[0]


def agents_text(entries):
    """Which agent ran each stage, with its model, from `report.agents_ran`:
    `**Agents:** plan `claude` (`claude-opus-5-5`) · implement `claude-api`
    (`claude-sonnet-5-5`) after `claude` (`claude-sonnet-5-5`) hit its usage limit`.
    Runs recorded before models were kept show the agents' names only."""
    parts = []
    for entry in entries:
        names, stage = entry.get("agents") or [], entry.get("stage")
        if not names:
            continue
        models = entry.get("models") or []
        agents = [(name, models[i] if i < len(models) else None) for i, name in enumerate(names)]
        limit = "its usage limit" if len(agents) == 1 else "their usage limits"
        if entry.get("paused"):
            parts.append(f"{stage}: {_names(agents)} hit {limit}")
        elif len(agents) > 1:
            limit = "its usage limit" if len(agents) == 2 else "their usage limits"
            parts.append(
                f"{stage_agent(stage, *agents[-1])} after {_names(agents[:-1])} hit {limit}"
            )
        else:
            parts.append(stage_agent(stage, *agents[0]))
    return "**Agents:** " + " · ".join(parts) if parts else ""


# -- pull request state -------------------------------------------------------------


def empty_state(issue):
    return {"version": 2, "issue": issue, "auto_fixes": 0, "feedback": None}


def read_state(comments):
    """Return (comment_id, state) for the PR's NexKit state comment, or (None, None).

    Pull requests from v1.1.0 have a state comment with a rounds table. It is read like
    any other, but `progress.save_state` leaves it unchanged and posts a new one."""
    return _find(comments, STATE_PATTERN)


def approved_head(comments):
    """The commit that the pull request's last completed review approved, or None."""
    _, state = read_state(comments)
    last = (state or {}).get("last_review") or {}
    head = last.get("head")
    if last.get("verdict") == "approve" and isinstance(head, str) and len(head) == 40:
        return head
    return None


def ci_wait_reason(wait):
    """Why a pull request's round waits for a bug issue about the default branch, from the
    round's `ci_wait` record."""
    return (
        f"waits for #{wait.get('bug')}: its failing checks fail as "
        f"`{wait.get('workflow')}` does on `{wait.get('branch')}`"
    )


def render_state(state):
    lines = [
        "### NexKit",
        "",
        f"Working on #{state['issue']}. Automatic fix rounds used: {state['auto_fixes']}. "
        "Each round has its own NexKit comment with a link to its run.",
    ]
    wait = state.get("ci_wait")
    if isinstance(wait, dict):
        lines += [
            "",
            f"⏳ This pull request {ci_wait_reason(wait)}. Once `{wait.get('workflow')}` "
            f"passes there again, or #{wait.get('bug')} is closed, NexKit autopilot starts "
            f"an automatic fix round that merges `{wait.get('branch')}` first.",
        ]
    lines += [
        "",
        "Comment `/nexkit fix <instructions>` to request another round, or `/nexkit review` "
        "to re-check the current commit.",
    ]
    return "\n".join(lines) + "\n\n" + _encode(STATE_PREFIX, state)

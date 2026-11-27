"""The differences from NexKit 6 that a change called out on purpose. `harness.py` reports a
difference that one of these matches apart from the rest, and fails only on the rest.

Each entry names the family, the pull request that made the difference and why, and a test of
the scenario and both records that holds only where the difference is reachable. A change that
differs from NexKit 6 on purpose, such as one that fixes a NexKit 6 bug, adds its entry here,
and its pull request calls it out."""

import re

KNOWN = [
    {
        "name": "mergeable race",
        "family": "released",
        "by": "#347",
        "why": (
            "NexKit 6 read `mergeable` a second time when a held conflict round was released; "
            "when GitHub flipped it between the two reads, it started a conflict round that "
            "the first read and the rules would not. The engine waits for a person with the "
            "first read's reason."
        ),
        "match": lambda sc, old, new: (
            sc["mergeable"] != sc["again"]
            and isinstance(old.get("step"), dict)
            and old["step"].get("command") == "fix"
            and old["step"].get("auto") == "conflicts"
            and isinstance(new.get("step"), dict)
            and new["step"].get("person") is True
        ),
    },
    {
        "name": "a command NexKit never writes starts again",
        "family": "lost",
        "by": "#399",
        "why": (
            "NexKit 6 started again whatever command a run that handed the work on named, such "
            "as `close`. NexKit never writes such a command, and the engine refuses to decide "
            "what follows it, so the issue waits for a person with the refusal as its reason."
        ),
        "match": lambda sc, old, new: (
            sc["where"] in {"issue_continued", "pull_chained"}
            and sc.get("next") == "close"
            and any(
                isinstance(old.get(f"step{n}"), dict) and old[f"step{n}"].get("command") == "close"
                for n in (1, 2, 3)
            )
        ),
    },
    {
        "name": "a closing answer after a stopped round",
        "family": "stop",
        "by": "#346",
        "why": (
            "After a blocked round whose recommended answer only closes the issue, NexKit 6's "
            "report said autopilot carries on and mentioned nobody; its next sweep then "
            "waited for a person and mentioned them. The report mentions the person, as it "
            "does for any other wait, and the sweep does not mention them again."
        ),
        "match": lambda sc, old, new: (
            sc["kind"] == "blocked"
            and not sc["protected"]
            and sc["answer"] == "recommended"
            and sc["qset"] in {"close", "close_replan", "replan_close", "rec_close"}
            and not old["report"]["attention"]
            and bool(new["report"]["attention"])
        ),
    },
    {
        "name": "409 on the same head",
        "family": "refusals",
        "by": "#391",
        "why": (
            "When GitHub answered 409, as if the branch had moved, but the pull request still "
            "showed the head that the round checked, NexKit 6 started `/nexkit review` on that "
            "same commit. The engine has no newer version to check, so it asks a person: the "
            "merge is `merge_refused`, and no review starts."
        ),
        "match": lambda sc, old, new: (
            sc["refusal"] == "409_same_head"
            and _outcomes(old, "head_moved")
            and not _outcomes(new, "head_moved")
            and _outcomes(new, "merge_refused")
            and not _reviews(new)
            and _reviews(old)
        ),
    },
    {
        "name": "the parts of a split start one sweep later",
        "family": "replan",
        "by": "#468",
        "why": (
            "NexKit 6's sweep looked again after a split and started the parts at once. The "
            "`step` run that splits starts a sweep instead, and that sweep sends the parts "
            "their steps, so they are filed in the first sweep and start in the next."
        ),
        "match": lambda sc, old, new: (
            any(line.startswith("Split #") for line in _lines(new))
            and "Started an autopilot sweep." in _lines(new)
            and any(line.startswith("Started /nexkit plan on") for line in _lines(old))
        ),
    },
    {
        "name": "no note that a start goes beyond max_parallel",
        "family": "start",
        "by": "#468",
        "why": (
            "NexKit 6's sweep started a bug issue that pull requests wait for beyond "
            "`autopilot.max_parallel`, and said so in the start's reason. The `step` run "
            "starts that issue alone, with nothing else counted, so it cannot know the pull "
            "requests that wait and leaves the note out. The sweep still sends the start "
            "beyond the limit."
        ),
        "match": lambda sc, old, new: (
            any("starts beyond max_parallel" in line for line in _lines(old))
            and not any("starts beyond max_parallel" in line for line in _lines(new))
        ),
    },
    {
        "name": "no step is sent while GitHub refuses dispatches",
        "family": "refusals",
        "by": "#468",
        "why": (
            "While GitHub refuses a dispatch, the sweep sends no `step`, so it makes none of "
            "the writes NexKit 6's sweep made itself, and says so in its log. The next sweep "
            "sends the step again. Only that is excused: GitHub holds the same after each "
            "sweep as before the first, apart from the empty `blockers` entries that the "
            "stored state's import reads (see the entries for it below)."
        ),
        "match": lambda sc, old, new: (
            sc["refusal"] == "dispatch_fails"
            and any(line.startswith("Could not send a step to") for line in _lines(new))
            and isinstance(new.get("gh"), dict)
            and _without_empty_blockers(new).get("gh1") == _without_empty_blockers(new)["gh"]
            and _without_empty_blockers(new).get("gh2") == _without_empty_blockers(new)["gh"]
        ),
    },
    {
        "name": "a vouch with no automatic fix round left waits for a person",
        "family": "vouch",
        "by": "#538",
        "why": (
            "After an untrusted edit stopped a person's fix round on a pull request whose "
            "automatic fix rounds are used up, NexKit 6 started a counted round when a writer "
            "vouched again, which went past `fix.max_auto_rounds`. The engine keeps the budget "
            "and a person decides when no automatic round is left (`roundsUsedUp`)."
        ),
        "match": lambda sc, old, new: (
            sc["where"] == "pull_round"
            and sc["rounds"] == "used_up"
            and sc["vouch"] == "writer"
            and isinstance(old.get("step"), dict)
            and old["step"].get("command") == "fix"
            and old["step"].get("count") is True
            and isinstance(new.get("step"), dict)
            and new["step"].get("state") == "waits"
            and new["step"].get("person") is True
        ),
    },
    {
        "name": "a part closed with no reason counts as completed",
        "family": "parents",
        "by": "#570",
        "why": (
            "NexKit 6 counted a closed sub-issue as completed only when GitHub's `state_reason` "
            "said `completed`, so a parent whose parts were all closed with no reason, as old "
            "issues are, waited for a person. The engine reads a part with no reason as accepted, "
            "as `maintain.close_parent` does for a part that a person closed, so under "
            "`close_parent_issues` it closes the parent, in the sweep too, which asks the engine "
            "the same question. Only a parent with no part completed in NexKit 6's sense and a "
            "part closed with no reason is excused."
        ),
        "match": lambda sc, old, new: (
            sc["reason"] is None and sc["close"] and sc["sibling"] in ("none", "not_planned")
        ),
    },
]

# What NexKit 6 wrote as the mention of a blocked run, and what the tree writes when the run
# holds no question to answer (#691).
_BLOCKED_WHY = "The agent stopped at a decision that is the owner's to make"
_BLOCKED_MENTIONS = (
    (
        "answer the questions in the run's comment with `/nexkit plan <answers>`, or "
        f"`/nexkit go <answers>` when they leave the plan as it is. {_BLOCKED_WHY}",
        "read why the agent stopped in the run's comment, then answer with `/nexkit go "
        "<instructions>` or `/nexkit plan <changes>`. The agent stopped before it finished and "
        "asked no question, and NexKit never runs a blocked round again by itself",
    ),
    (
        f"answer the questions in the round's comment with `/nexkit fix <answers>`. {_BLOCKED_WHY}",
        "read why the agent stopped in the round's comment, then comment `/nexkit fix "
        "<instructions>` on the pull request, or `/nexkit plan <changes>` on the issue to plan "
        "it anew. The agent stopped before it finished and asked no question, and NexKit never "
        "runs a blocked round again by itself",
    ),
)


def _replaced(value, pairs):
    """`value` with each text of `pairs` replaced in every string it holds."""
    if isinstance(value, str):
        for old, new in pairs:
            value = value.replace(old, new)
        return value
    if isinstance(value, dict):
        return {key: _replaced(item, pairs) for key, item in value.items()}
    if isinstance(value, list):
        return [_replaced(item, pairs) for item in value]
    return value


def _differs_by_the_blocked_mention(sc, old, new):
    """Whether the two records differ by the mention of a blocked run with nothing to answer
    alone, apart from the empty `blockers` entries that a read adds."""
    said = _replaced(old, _BLOCKED_MENTIONS)
    return said != old and _without_empty_blockers(said) == _without_empty_blockers(new)


KNOWN += [
    {
        "name": "a blocked run with nothing to answer asks for no answers",
        "family": family,
        "by": "#691",
        "why": (
            "NexKit 6 mentioned a blocked run that asked no question as if it had asked some: "
            "'answer the questions in the run's comment'. The mention now sends the person to "
            "why the agent stopped, which the run's comment shows, and to the commands that "
            "answer it. Only a record that differs by this mention is excused."
        ),
        "match": _differs_by_the_blocked_mention,
    }
    for family in ("stop", "give")
]


def _state_count(record):
    """The `auto_fixes` that the state comment of pull request 6 holds in `record`, or None."""
    for comment in (record.get("gh") or {}).get("issue_comments", {}).get("6", []):
        found = re.search(r'"auto_fixes": (\d+)', str(comment.get("body")))
        if found:
            return int(found.group(1))
    return None


def _kept_a_retried_rounds_count(sc, old, new):
    """Whether a retried or resumed automatic fix round that the host stopped before its
    review keeps its count in `new` where NexKit 6 gave it back: the stored state does not mark
    it as automatic, since the import marks only a counted automatic fix round."""
    used = sc.get("fixes")
    return (
        sc.get("part") == "report"
        and sc["auto"]
        and not sc["conflicts"]
        and sc["again"] in ("retry", "resumed")
        and sc["agent"] in ("untrusted", "on_base", "on_base_failed")
        and isinstance(used, int)
        and used >= 1
        and _state_count(old) == used - 1
        and _state_count(new) == used
    )


KNOWN += [
    {
        "name": "a retried or resumed automatic fix round keeps its count when NexKit stops it",
        "family": "give",
        "by": "#748",
        "why": (
            "NexKit 6's report gave the count back of a retried or resumed automatic fix "
            "round that NexKit stopped before its review, as it does for any other automatic "
            "round, though its push-run path does not. The engine decides from the stored "
            "state alone, and the import marks a running round as automatic only when "
            "`state.counted_fix` says it counts, which leaves out a retried or resumed one. "
            "So such a round that was running at the upgrade keeps its count. Only a record "
            "that differs by that count is excused."
        ),
        "match": _kept_a_retried_rounds_count,
    }
]

_READ = (
    "reads the item's stored state, which is the import in memory when the item has no state "
    "comment, and the import reads the issues that block it. A read by `FakeGitHub` adds the "
    "issue to its `blockers` with no blocker, where NexKit 6's flow never read it. Only a "
    "record that differs by such an empty entry is excused."
)

KNOWN += [
    {
        "name": "the stored state is read before the decision",
        "family": family,
        "by": by,
        "why": f"{what} {_READ}",
        "match": lambda sc, old, new: _without_empty_blockers(old) == _without_empty_blockers(new),
    }
    for by, what, families in (
        (
            "#643",
            "A decision after a plan, for a first plan or after a writer's vouch",
            ("replan", "vouch"),
        ),
        (
            "#645",
            "A decision after a blocked or stopped round, at a usage limit or a transient "
            "error, at a resume or for a lost command",
            (
                "stop",
                "lost",
                "pause",
                "report",
                "required",
                "give",
                "conflict",
                "review",
                "waits",
                "refusals",
                "conflicted",
                "behind",
            ),
        ),
    )
    for family in families
]

KNOWN += [
    {
        "name": "the stored state is read before a command or an approval is decided",
        "family": family,
        "by": "#644",
        "why": (
            "A command, a closing command and an approval read the item's stored state, which is "
            "the import in memory when the item has no state comment, and the import reads the "
            "issues that block it. So do the families whose flows send NexKit's own dispatch of "
            "a command or an approval: the waits, refusals, ends, conflicts, branches behind "
            "their base and parents. A read by `FakeGitHub` adds the issue to its `blockers` "
            "with no blocker, where NexKit 6's flow never read it. Only a record that differs "
            "by such an empty entry is excused."
        ),
        "match": lambda sc, old, new: _without_empty_blockers(old) == _without_empty_blockers(new),
    }
    for family in (
        "command",
        "approvals",
        "waits",
        "refusals",
        "ended",
        "conflicted",
        "behind",
        "parents",
    )
]

KNOWN += [
    {
        "name": "the stored state is read before a merge decision",
        "family": family,
        "by": "#647",
        "why": (
            "A decision at a merge, for a refusal, a wait for required checks, a branch update "
            "or what the merge found, reads the item's stored state, which is the import in "
            "memory when the item has no state comment, and the import reads the issues that "
            "block it. A read by `FakeGitHub` adds the issue to its `blockers` with no "
            "blocker, where NexKit 6's flow never read it. Only a record that differs by such "
            "an empty entry is excused."
        ),
        "match": lambda sc, old, new: _without_empty_blockers(old) == _without_empty_blockers(new),
    }
    for family in (
        "required",
        "waits",
        "refusals",
        "behind",
        "conflicted",
        "cleared",
        "ended",
        "approvals",
        "sweep",
        "parents",
    )
]


# The families that play the decisions of a pull request's rounds (#646).
FAMILIES_646 = (
    "report",
    "round",
    "conflict",
    "give",
    "diag",
    "many",
    "chains",
    "review",
    "stop",
    "sweep",
    "moved",
    "required",
    "waits",
    "pause",
    "ended",
    "conflicted",
    "behind",
    "refusals",
)

KNOWN += [
    {
        "name": "the stored state is read before a round's decision",
        "family": family,
        "by": "#646",
        "why": (
            "The decisions about a pull request's rounds, which are a round's end, its conflict, "
            "the wait for a hold or the base branch, the checks a fix round diagnoses and the "
            "rounds that count, read the item's stored state, which is the import in memory "
            "when the item has no state comment, and the import reads the issues that block "
            "the item and its comments. A read by `FakeGitHub` adds the issue to its "
            "`blockers` and its `issue_comments` with nothing in them, where NexKit 6's flow "
            "never read them. Only a record that differs by such an empty entry is excused."
        ),
        "match": lambda sc, old, new: (
            _without_empty_reads_in_steps(old) == _without_empty_reads_in_steps(new)
        ),
    }
    for family in FAMILIES_646
]


KNOWN += [
    {
        "name": "the parent's stored state is read before its end is decided",
        "family": family,
        "by": "#648",
        "why": (
            "The engine decides the end of a part on the parent's stored state, which is the "
            "import in memory when the parent has no state comment, and the import reads the "
            "issues that block it. A read by `FakeGitHub` adds the parent to its `blockers` "
            "and its comments with nothing in them, where NexKit 6's flow never read them. "
            "Only a record that differs by such an empty entry is excused."
        ),
        "match": lambda sc, old, new: _without_empty_reads(old) == _without_empty_reads(new),
    }
    for family in ("ended", "parents")
]

KNOWN += [
    {
        "name": "a part's end is sent to its parent's group",
        "family": "parents",
        "by": "#648",
        "why": (
            "When a sub-issue closes, its run sends the end of the part as one `step` into its "
            "parent's group, whose run stores the answer in the parent's stored state. The "
            "part's run still closes the parent and leaves its notes as NexKit 6 did, and the "
            "`step` run writes nothing else on GitHub. Only that dispatch and its line in the "
            "log are excused, and nothing else may differ."
        ),
        "match": lambda sc, old, new: _without_empty_reads(old)
        == _without_empty_reads(_without_part_ends(new)),
    }
]


def _parent_left_open(old, new):
    """Whether NexKit 6 closed the parent issue 1 and the tree left it open, in a record's
    GitHub (`gh`, and `gh1`, `gh2` of a record with more than one)."""
    found = False
    for key in ("gh", "gh1", "gh2"):
        was, now = old.get(key), new.get(key)
        if not (isinstance(was, dict) and isinstance(now, dict)):
            continue
        before = (was.get("issues") or {}).get("1") or {}
        after = (now.get("issues") or {}).get("1") or {}
        found = found or (before.get("state") == "closed" and after.get("state") == "open")
    return found


KNOWN += [
    {
        "name": "a completed part beyond the ten that a state holds leaves the parent to a person",
        "family": "parents",
        "by": "#720",
        "why": (
            "A parent's state holds at most ten parts (`MOST['split']`), and the engine refuses "
            "the end of a part it does not hold, which no event carries. NexKit 6 counted every "
            "sub-issue, so a parent of more than ten whose only completed part was beyond the "
            "first ten, with the others closed as not planned, closed. Now the engine finds none "
            "completed and a person closes the parent: the difference only keeps a parent open. "
            "Only that case is excused: more than ten sub-issues, the part closed as not "
            "planned, the only completed one a sibling, and NexKit 6 closing the parent where "
            "the tree leaves it open."
        ),
        "match": lambda sc, old, new: (
            sc["extra"] == "many"
            and sc["sibling"] == "completed"
            and sc["reason"] == "not_planned"
            and sc["close"]
            and _parent_left_open(old, new)
        ),
    }
]


KNOWN += [
    {
        "name": "a stored count of conflict rounds above the markers'",
        "family": "chains",
        "by": "#698",
        "why": (
            "The chain never runs the conflict round that a step's report dispatches. The engine "
            "counts that round in the stored state as it decides it, while NexKit 6 counts it "
            "only once the round's comment exists. So a later step finds the stored count above "
            "the markers'. NexKit 6 then started another automatic conflict round, and the "
            "engine finds the budget used up (`conflicts.max_rounds`) and leaves the pull "
            "request to a person, with that reason. Only a step whose round differs this way is "
            "excused: the round's outcome, its comment and the dispatch that NexKit 6 sent. "
            "Every other step and everything else in it must be equal."
        ),
        "match": lambda sc, old, new: _stored_ahead(sc, old, new),
    }
]


def _without_empty_reads(record):
    """`_without_empty_blockers` of `record`, also without the entries of `issue_comments` that
    hold no comment."""
    found = _without_empty_blockers(record)
    for key, item in found.items():
        if isinstance(item, dict) and isinstance(item.get("issue_comments"), dict):
            kept = {number: held for number, held in item["issue_comments"].items() if held}
            found[key] = {**item, "issue_comments": kept}
    return found


def _without_empty_reads_in_steps(record):
    """`_without_empty_reads` of `record`, also without the empty `blockers` and `issue_comments`
    entries of what GitHub holds after each `step` of a family that plays steps."""
    found = _without_empty_reads(record)
    if isinstance(found.get("steps"), list):
        found["steps"] = [
            {**step, "gh": _without_empty_reads({"gh": step["gh"]})["gh"]}
            if isinstance(step, dict) and isinstance(step.get("gh"), dict)
            else step
            for step in found["steps"]
        ]
    return found


def _without_part_ends(record):
    """`record` without the `step` dispatches that send the end of a part to its parent's
    group (`part N`) and the lines of the log that say so."""
    found = dict(record)
    for key in ("gh", "gh1", "gh2"):
        item = record.get(key)
        if isinstance(item, dict) and isinstance(item.get("dispatches"), list):
            kept = [
                dispatch
                for dispatch in item["dispatches"]
                if not (
                    dispatch["inputs"].get("command") == "step"
                    and re.fullmatch(r"part \d+", dispatch["inputs"].get("note") or "")
                )
            ]
            found[key] = {**item, "dispatches": kept}
    if isinstance(record.get("out"), list):
        found["out"] = [
            line
            for line in record["out"]
            if not (isinstance(line, str) and line.startswith("Sent the end of #"))
        ]
    return found


def _stored_ahead(sc, old, new):
    """Whether `old` and `new`, the records of a chain, differ only in one step, in which NexKit 6
    started an automatic conflict round and the engine found the `conflicts.max_rounds` of
    the configuration used up, and in that step only in the round's outcome, its comment and the
    dispatches."""
    steps = old.get("steps"), new.get("steps")
    if not all(isinstance(each, list) for each in steps) or len(steps[0]) != len(steps[1]):
        return False
    if {k: v for k, v in old.items() if k != "steps"} != {
        k: v for k, v in new.items() if k != "steps"
    }:
        return False
    found = [i for i, (a, b) in enumerate(zip(*steps, strict=True)) if a != b]
    if len(found) != 1:
        return False
    before, after = steps[0][found[0]], steps[1][found[0]]
    try:
        most = sc["cfg"]["max_rounds"]
        started = before["outcome"]["outcome"] == "conflict_fix"
        used = (
            after["outcome"]["outcome"] == "conflicting"
            and f"its {most} automatic conflict rounds are used up" in after["outcome"]["summary"]
        )
    except (KeyError, TypeError):
        return False
    if not (started and used):
        return False
    return _round_differs(before, after)


def _round_differs(before, after):
    """Whether `before` and `after`, two steps of a chain, hold the same GitHub apart from the last
    comment of one pull request, a round's, and the last dispatch, and the same record apart from
    the round's outcome."""
    one, two = dict(before), dict(after)
    one.pop("outcome", None)
    two.pop("outcome", None)
    gh_one, gh_two = dict(one.pop("gh", None) or {}), dict(two.pop("gh", None) or {})
    if one != two:
        return False
    comments_one = dict(gh_one.pop("issue_comments", None) or {})
    comments_two = dict(gh_two.pop("issue_comments", None) or {})
    sent_one, sent_two = gh_one.pop("dispatches", []), gh_two.pop("dispatches", [])
    if gh_one != gh_two or set(comments_one) != set(comments_two):
        return False
    if len(sent_one) != len(sent_two) or sent_one[:-1] != sent_two[:-1]:
        return False
    # The round's comment is the last of the pull request: every other comment is the same.
    other = [n for n in comments_one if comments_one[n] != comments_two[n]]
    if len(other) != 1:
        return False
    one_list, two_list = comments_one[other[0]], comments_two[other[0]]
    return (
        len(one_list) == len(two_list)
        and one_list[:-1] == two_list[:-1]
        and all(
            str(each.get("body")).startswith("### NexKit round")
            for each in (one_list[-1], two_list[-1])
        )
    )


def _without_empty_blockers(record):
    """`record` without the entries of `blockers` that hold no blocker, in what GitHub holds
    after the command, after a report and after each sweep."""
    found = dict(record)
    for key, item in record.items():
        if isinstance(item, dict) and isinstance(item.get("blockers"), dict):
            kept = {number: held for number, held in item["blockers"].items() if held}
            found[key] = {**item, "blockers": kept}
    return found


def _lines(record):
    """The lines of the log of the sweep that `record` holds, or none when it crashed."""
    found = [
        line for key, value in record.items() if key.startswith("lines") for line in _list(value)
    ]
    return found


def _list(value):
    return [line for line in value if isinstance(line, str)] if isinstance(value, list) else []


def _after(record):
    """What GitHub holds in `record` after the command and after each sweep."""
    found = (record.get(key) for key in ("gh", "gh1", "gh2"))
    return [item for item in found if isinstance(item, dict)]


def _outcomes(record, outcome):
    """Whether a round's comment in `record` ends as `outcome`."""
    return any(f'"outcome": "{outcome}"' in str(item) for item in _after(record))


def _reviews(record):
    """Whether `record` has a dispatch of `/nexkit review`."""
    return any(
        dispatch["inputs"].get("command") == "review"
        for item in _after(record)
        for dispatch in item.get("dispatches", [])
    )


def listed(family, sc, old, new):
    """The name of the listed difference that explains how `old` and `new` differ, or None."""
    for known in KNOWN:
        if known["family"] == family and known["match"](sc, old, new):
            return known["name"]
    return None

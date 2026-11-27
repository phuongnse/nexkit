"""The differences from NexKit 6 that a change called out on purpose. `harness.py` reports a
difference that one of these matches apart from the rest, and fails only on the rest.

Each entry names the family, the pull request that made the difference and why, and a test of
the scenario and both records that holds only where the difference is reachable. A change that
differs from NexKit 6 on purpose, such as one that fixes a NexKit 6 bug, adds its entry here,
and its pull request calls it out."""

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
            "sweep as before the first."
        ),
        "match": lambda sc, old, new: (
            sc["refusal"] == "dispatch_fails"
            and any(line.startswith("Could not send a step to") for line in _lines(new))
            and isinstance(new.get("gh"), dict)
            and new.get("gh1") == new["gh"]
            and new.get("gh2") == new["gh"]
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

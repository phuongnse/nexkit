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
]


def listed(family, sc, old, new):
    """The name of the listed difference that explains how `old` and `new` differ, or None."""
    for known in KNOWN:
        if known["family"] == family and known["match"](sc, old, new):
            return known["name"]
    return None

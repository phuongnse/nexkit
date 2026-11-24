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
        "match": lambda sc, old, new: sc["mergeable"] != sc["again"],
    },
]


def listed(family, sc, old, new):
    """The name of the listed difference that explains how `old` and `new` differ, or None."""
    for known in KNOWN:
        if known["family"] == family and known["match"](sc, old, new):
            return known["name"]
    return None

"""#347: the chains of `diag.py` with sets of 40 checks mixed in: 40 checks all failing
before, then only five past the thirtieth, 17 of them, or all with another signature. A
diagnosis round names at most ten checks, and the state keeps up to 300 failing checks and 150
repeated ones, so more than 30 failing results, or more than ten that repeat, must give
NexKit 6's `feedback.repeated` and `diagnose`."""

from families.diag import chains, run  # noqa: F401

COUNT = 20000
SEED = 312  # #347's own, as it ran these chains with MANY=1


def scenarios():
    yield from chains(COUNT, SEED, many=True)

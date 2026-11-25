"""Play one family's scenarios on one tree, and write one JSON line per scenario.

usage: python3 play.py ROOT FAMILY OUT [--shard I --of N] [--sample K] [--only I,J]

`harness.py` runs this for both trees; run it by hand to look at one scenario
(`--only`). OUT is gzipped JSON lines: {"i": index, "sc": scenario, "rec": record}.
Run it with PYTHONHASHSEED=0, as `harness.py` does: the order of a set NexKit iterates shows
on GitHub, such as the order of statuses.
"""

import argparse
import gzip
import importlib
import json
import random
import sys


def chosen(total, sample, family):
    """The indices of the scenarios to play: all, or `sample` of them, the same every time."""
    if sample is None or sample >= total:
        return list(range(total))
    return sorted(random.Random(f"nexkit-differential-{family}").sample(range(total), sample))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="the NexKit tree to play")
    parser.add_argument("family")
    parser.add_argument("out")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--of", type=int, default=1)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--only", help="comma-separated scenario indices")
    args = parser.parse_args(argv)

    import common

    common.enter(args.root)
    module = importlib.import_module(f"families.{args.family}")
    scenarios = list(module.scenarios())
    if args.only:
        indices = [int(i) for i in args.only.split(",")]
    else:
        indices = chosen(len(scenarios), args.sample, args.family)
    with gzip.open(args.out, "wt", compresslevel=3) as out:
        for i in indices[args.shard :: args.of]:
            sc = scenarios[i]
            try:
                rec = module.run(json.loads(json.dumps(sc)))
            except Exception as exc:  # noqa: BLE001
                # The harness itself broke on this tree: a difference all the same.
                rec = {"harness": f"EXC {type(exc).__name__}: {exc}"}
            out.write(json.dumps({"i": i, "sc": sc, "rec": rec}, sort_keys=True, default=str))
            out.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

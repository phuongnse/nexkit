"""The differential check: play every scenario family on NexKit 6 at 8bf5190 and on a tree,
each scenario on a fresh `FakeGitHub`, and compare what each records: the command's result,
everything on GitHub after it (comments with markers decoded, statuses, reviews, dispatches
with their inputs, merges, issues, pull requests, labels), the preview and `nexkit status`.

usage: python3 tests/differential/harness.py --v6 PATH [--tree PATH] [--family NAME ...]
                                             [--sample K] [--jobs N] [--keep DIR]

`--v6` is a checkout of 8bf5190 (`git worktree add --detach PATH 8bf5190`); `--tree` is the
tree to check, this one by default. Without `--sample` every scenario plays, which takes a
while; `--sample K` plays the same K of each family every time. It prints each family's
count, the differences that `known.py` lists, and every other difference with an example, and
exits 1 when there is one.
"""

import argparse
import collections
import gzip
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from families import FAMILIES  # noqa: E402
from known import listed  # noqa: E402

# The commit of NexKit 6 that the engine's decisions keep GitHub's behaviour of.
V6 = "8bf51903c4ec3200bb480ff1a380d622cd5a08d3"


def play(root, family, out, shard, of, sample):
    command = [sys.executable, str(HERE / "play.py"), str(root), family, str(out)]
    command += ["--shard", str(shard), "--of", str(of)]
    if sample is not None:
        command += ["--sample", str(sample)]
    env = {**os.environ, "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    found = subprocess.run(command, capture_output=True, text=True, env=env)
    return found.returncode, found.stderr[-2000:]


def records(path):
    with gzip.open(path, "rt") as lines:
        for line in lines:
            yield json.loads(line)


def differing(a, b, path=""):
    """The paths at which `a` and `b` differ, most specific first."""
    if type(a) is not type(b):
        return [path or "."]
    if isinstance(a, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if a.get(key, "<missing>") != b.get(key, "<missing>"):
                out += differing(a.get(key), b.get(key), f"{path}.{key}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [f"{path}#len"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                out += differing(x, y, f"{path}[{i}]")
        return out
    return [path or "."] if a != b else []


def at(value, path):
    for part in re.findall(r"\.([^.\[#]+)|\[(\d+)\]", path):
        key, index = part
        if isinstance(value, dict) and key:
            value = value.get(key)
        elif isinstance(value, list) and index:
            value = value[int(index)] if int(index) < len(value) else None
        else:
            return value
    return value


def compare(family, pairs):
    """(count, listed differences by name, unlisted differences by their paths, examples,
    scenarios that broke the harness) of the records in each pair of files, one of NexKit 6
    and one of the tree, which hold the same scenarios in the same order."""
    count, broken = 0, 0
    seen, unlisted, examples = collections.Counter(), collections.Counter(), {}
    for old_path, new_path in pairs:
        old_records, new_records = records(old_path), records(new_path)
        for before, item in itertools.zip_longest(old_records, new_records):
            if before is None or item is None or before["i"] != item["i"]:
                unlisted[("the trees played other scenarios",)] += 1
                break
            count += 1
            if "harness" in item["rec"] or "harness" in before["rec"]:
                broken += 1
            if before["rec"] == item["rec"]:
                continue
            sc = item["sc"]
            name = listed(family, sc, before["rec"], item["rec"])
            if name:
                seen[name] += 1
                continue
            paths = differing(before["rec"], item["rec"])
            shape = tuple(sorted({re.sub(r"\[\d+\]", "[]", p) for p in paths}))[:6]
            unlisted[shape] += 1
            if shape not in examples:
                first = paths[0]
                examples[shape] = {
                    "i": item["i"],
                    "sc": sc,
                    "path": first,
                    "v6": at(before["rec"], first),
                    "tree": at(item["rec"], first),
                }
    return count, seen, unlisted, examples, broken


def short(value, most=600):
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= most else text[:most] + "…"


def report(family, count, seen, unlisted, examples, broken):
    """Print what a family found; the number of problems in it."""
    line = f"{family} ({FAMILIES[family]}): {count} scenarios, {sum(unlisted.values())} differ"
    if seen:
        line += ", listed: " + ", ".join(f"{n} × {name}" for name, n in seen.items())
    if broken:
        line += f", {broken} broke the harness"
    print(line, flush=True)
    if not count:
        print(f"  {family} played no scenario")
    for shape, n in unlisted.most_common():
        print(f"  {n} × {', '.join(shape)}")
        example = examples.get(shape)
        if example:
            print(f"    e.g. scenario {example['i']}: {short(example['sc'])}")
            print(f"    at {example['path']}")
            print(f"    NexKit 6: {short(example['v6'])}")
            print(f"    tree:     {short(example['tree'])}")
    return sum(unlisted.values()) + broken + (0 if count else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--v6", required=True, help=f"a checkout of NexKit 6 at {V6[:7]}")
    parser.add_argument("--tree", default=str(HERE.parent.parent), help="the tree to check")
    parser.add_argument("--family", action="append", choices=sorted(FAMILIES))
    parser.add_argument("--sample", type=int, help="play K scenarios of each family")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--keep", help="keep the records in this folder")
    args = parser.parse_args(argv)
    head = subprocess.run(
        ["git", "-C", args.v6, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if head != V6:
        parser.error(f"--v6 must be a checkout of {V6}, not {head or 'a tree git cannot read'}")
    families = args.family or list(FAMILIES)
    folder = Path(args.keep or tempfile.mkdtemp(prefix="nexkit-differential-"))
    folder.mkdir(parents=True, exist_ok=True)
    sides = {"v6": Path(args.v6).resolve(), "tree": Path(args.tree).resolve()}
    started = time.monotonic()
    total, problems = 0, 0
    with ThreadPoolExecutor(args.jobs) as pool:
        tasks = {}
        for family in families:
            for side, root in sides.items():
                for shard in range(args.jobs):
                    out = folder / f"{family}.{side}.{shard}.jsonl.gz"
                    task = pool.submit(play, root, family, out, shard, args.jobs, args.sample)
                    tasks.setdefault(family, []).append((side, shard, out, task))
        # Each family is compared once both trees played it, and its records go then.
        for family in families:
            outputs = collections.defaultdict(list)
            failed = False
            for side, shard, out, task in tasks[family]:
                code, err = task.result()
                if code:
                    failed = True
                    print(f"{family} on {side} (shard {shard}) failed:\n{err}", flush=True)
                outputs[side].append(out)
            if failed:
                problems += 1
                continue
            pairs = list(zip(outputs["v6"], outputs["tree"], strict=True))
            count, *found = compare(family, pairs)
            total += count
            problems += report(family, count, *found)
            if not args.keep:
                for path in outputs["v6"] + outputs["tree"]:
                    path.unlink()
    seconds = time.monotonic() - started
    print(f"{total} scenarios in {seconds:.0f}s, {problems} differences not listed")
    if args.keep:
        print(f"records in {folder}")
    else:
        shutil.rmtree(folder, ignore_errors=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

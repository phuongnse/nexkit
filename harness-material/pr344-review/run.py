"""Generate the differential scenarios, run them on NexKit 6 (8bf5190) and a tree, compare.

Usage: python3 run.py [families,...] [--tree PATH] [--out NAME]
"""

import itertools
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD = "$SCRATCH/r337/main"
NEW = os.path.expanduser("~/repos/nexkit-r344")


def grid(**axes):
    keys = list(axes)
    for values in itertools.product(*axes.values()):
        yield dict(zip(keys, values))


def scenarios(families):
    out = []
    if "hold" in families:
        for p in grid(back=["fix", "merge", "conflicts", None], label=[True, False], moved=[False, True],
                      conflict=[False, True], used=[None, 0, 1, 2, 3], max=[0, 1, 2],
                      autopilot=[True, False], merge_auto=[True, False], resolve=[True, False],
                      closed=[False, True], between=[None, "unlabel", "push", "close"]):
            if not p["autopilot"] and (p["merge_auto"] is False or p["resolve"] is False or p["between"] not in (None, "unlabel")):
                continue
            if p["closed"] and p["between"] == "close":
                continue
            out.append({"family": "hold", **p})
        # `fix.max_auto_rounds` changed between the sweeps while the label held it.
        for p in grid(back=["fix", None], used=[None, 0, 1, 2, 3], max=[0, 1, 2, 3], max2=[0, 1, 2, 3], moved=[False, True]):
            out.append({"family": "hold", "label": True, "conflict": False, "closed": False, "between": "unlabel", **p})
    if "ci" in families:
        for p in grid(end=["closed", "passes", "none"], used=[None, 0, 1, 2, 3], max=[0, 1, 2],
                      moved=[False, True], label=[False, True], closed=[False, True],
                      merge_auto=[True, False], between=[None, "end", "unlabel", "push", "close"],
                      action=["implement", "fix"]):
            if p["closed"] and p["between"] == "close":
                continue
            out.append({"family": "ci", **p})
        for p in grid(end=["closed", "passes", "none"], used=[0, 1, 2], max=[0, 1, 2, 3], max2=[0, 1, 2, 3], between=[None, "end"]):
            out.append({"family": "ci", "moved": False, "label": False, "closed": False, **p})
        # A person's `/nexkit fix` or `/nexkit review` during the wait.
        for p in grid(end=["closed", "passes", "none"], used=[0, 1, 2, 3], max=[0, 1, 2],
                      person=["fix", "review"], person_same=[True, False], label=[False, True], between=[None, "end"]):
            out.append({"family": "ci", "moved": False, "closed": False, **p})
    if "required" in families:
        for p in grid(used=[None, 0, 1, 2, 3], max=[0, 1, 2], same=["yes", "no", "error"], named=[True, False],
                      label=[False, True], autopilot=[True, False], resolve=[True, False], merge_auto=[True, False],
                      between=[None, "unlabel", "push"]):
            out.append({"family": "required", **p})
    if "own_merge" in families:
        for p in grid(used=[None, 0, 1, 2, 3], max=[0, 1, 2], same=["yes", "no"], named=[True, False],
                      label=[False, True], autopilot=[True, False], action=["implement", "fix"], auto=["true", False]):
            if p["action"] == "implement" and p["auto"] is False:
                continue
            out.append({"family": "own_merge", **p})
    if "stop" in families:
        for p in grid(used=[None, 0, 1, 2, 3], max=[0, 2], status=["already_on_base", "up_to_date", "error", "blocked", "publish_failed"],
                      action=["fix", "review"], auto=["true", "conflicts", False], conflicts=[False, True], retry=[False, True],
                      autopilot=[True, False], issue_closed=[False, True]):
            if p["issue_closed"] and p["status"] != "already_on_base":
                continue
            out.append({"family": "stop", **p})
    if "superseded" in families:
        for p in grid(used=[None, 0, 1, 2, 3], action=["fix", "review", "implement"],
                      kinds=[[], ["auto"], ["auto", "autopilot"], ["person"]], conflicting=[False, True],
                      conflicts=[False, True], retry=[False, True], resumed=[False, True], strict=[True, False], max_rounds=[0, 2]):
            out.append({"family": "superseded", **p})
    return out


def run_tree(tree, lines, chunks):
    size = (len(lines) + chunks - 1) // chunks
    parts = [lines[i:i + size] for i in range(0, len(lines), size)]

    def one(part):
        proc = subprocess.run([sys.executable, str(HERE / "drive.py"), tree], input="".join(part),
                              capture_output=True, text=True)
        if proc.returncode:
            print(proc.stderr[-2000:], file=sys.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines()]

    with ThreadPoolExecutor(chunks) as pool:
        results = [r for found in pool.map(one, parts) for r in found]
    return results


def main():
    args = sys.argv[1:]
    tree = NEW
    name = "diff"
    if "--tree" in args:
        tree = args[args.index("--tree") + 1]
    if "--out" in args:
        name = args[args.index("--out") + 1]
    fams = (args[0] if args and not args[0].startswith("--") else "hold,ci,required,own_merge,stop,superseded").split(",")
    found = scenarios(fams)
    lines = [json.dumps(p) + "\n" for p in found]
    print(f"{len(lines)} scenarios", flush=True)
    cache = HERE / f"old_{'_'.join(sorted(fams))}.json"
    if cache.exists():
        old = json.loads(cache.read_text())
    else:
        old = run_tree(OLD, lines, 8)
        cache.write_text(json.dumps(old))
    new = run_tree(tree, lines, int(os.environ.get("CHUNKS", "8")))
    assert len(old) == len(new) == len(lines), (len(old), len(new), len(lines))
    diffs = []
    crashes = 0
    for a, b in zip(old, new):
        assert a["p"] == b["p"]
        if isinstance(a["out"], dict) or isinstance(b["out"], dict):
            crashes += 1
        if a["out"] != b["out"]:
            diffs.append({"p": a["p"], "old": a["out"], "new": b["out"]})
    by = {}
    for d in diffs:
        by[d["p"]["family"]] = by.get(d["p"]["family"], 0) + 1
    total = {}
    for p in found:
        total[p["family"]] = total.get(p["family"], 0) + 1
    print("scenarios by family", total)
    print("differences", len(diffs), by, "crashed scenarios", crashes)
    (HERE / f"{name}.json").write_text(json.dumps(diffs[:300], indent=1, sort_keys=True))


if __name__ == "__main__":
    main()

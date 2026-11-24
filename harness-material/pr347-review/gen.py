"""Scenarios for the differential check of report + route (NexKit 6 vs PR 347).
python3 gen.py N seed > scenarios.jsonl"""
import json, random, sys

N, SEED = int(sys.argv[1]), int(sys.argv[2])
rng = random.Random(SEED)
LONG = "x" * 36  # the longest sharded check name (config: 36), shards up to 39 chars
NAMESETS = {
    "plain": ["test", "lint"],
    "long": ["a" * 40, "b" * 40],
    "shard41": [LONG + "-1", LONG + "-2"],          # both clip to the same 40 chars
    "shard43": ["y" * 36 + "-10", "y" * 36 + "-11"],
    "many": [f"c{i:02d}" for i in range(36)],
    "mixed": ["test", LONG + "-1", LONG + "-2", "lint"],
}
OUTPUTS = ["AssertionError", "TypeError: x", "", "E" * 200, "AssertionError\nmore"]

def checks_for(names, mode):
    if mode == "none":
        return None
    found = []
    for i, name in enumerate(names):
        if mode == "pass":
            passed = True
        elif mode == "allfail":
            passed = False
        elif mode == "tailfail":  # only the later checks fail
            passed = i < len(names) - 4
        else:
            passed = rng.random() < 0.4
        out = "" if passed else rng.choice(OUTPUTS)
        found.append({"name": name, "run": f"run {name}", "exit_code": 0 if passed else 1,
                      "passed": passed, "output": out})
    if found and rng.random() < 0.1:
        found.append(dict(found[0], passed=False, exit_code=1, output="dup"))  # a name twice
    return found

def review():
    return rng.choice(["approve", "approve", "request_changes", "request_changes", "error",
                       "transient", "paused", "paused_unknown", "missing", "blocking_approve"])

def round_():
    kind = rng.choice(["auto_fix", "auto_fix", "auto_fix", "person_fix", "conflict_fix",
                       "resumed_fix", "retry_fix", "auto_review", "person_review", "implement",
                       "person_pr"])
    return {
        "kind": kind,
        "checks": rng.choice(["fail", "fail", "allfail", "same", "same", "pass", "none", "tailfail"]),
        "review": review(),
        "conflicts": rng.random() < 0.25,
        "recheck": rng.random() < 0.1,
        "held": rng.choice([None, None, None, "on-hold", "UNREAD"]),
        "base_failing": rng.random() < 0.15,
        "used_conflicts": rng.choice([0, 0, 1, 2, 3]),
    }

for index in range(N):
    nameset = rng.choice(list(NAMESETS))
    names = NAMESETS[nameset]
    before_mode = rng.choice(["fail", "allfail", "pass", "none", "nofeedback", "nosig", "tailfail"])
    before = None if before_mode == "nofeedback" else checks_for(
        names, {"nosig": "allfail"}.get(before_mode, before_mode))
    if before and before_mode != "nosig":
        for c in before:
            if not c["passed"]:
                c["signature"] = c["output"].splitlines()[0] if c["output"] else ""
    scenario = {
        "index": index,
        "names": names,
        "before": before,
        "before_mode": before_mode,
        "prior_repeated": rng.choice([None, None, names[:1], names[:3]]),
        "auto_fixes": rng.choice([0, 0, 1, 2, 3]),
        "cfg": {
            "max_auto_rounds": rng.choice([0, 1, 2, 3]),
            "merge_auto": rng.random() < 0.5,
            "autopilot": rng.random() < 0.6,
            "auto_resolve": rng.random() < 0.6,
            "max_rounds": rng.choice([1, 2, 3]),
            "resume": rng.random() < 0.6,
            "retry": rng.choice([[0], [], [5]]),
        },
        "rounds": [round_() for _ in range(rng.choice([1, 2, 2, 3, 3, 4]))],
    }
    if not scenario["cfg"]["auto_resolve"]:
        # `publish` reports a conflict only with `conflicts.auto_resolve` on.
        for r in scenario["rounds"]:
            r["conflicts"] = False
    print(json.dumps(scenario))

"""Fill a conformance case's states, `changed` and actions from the engine's answers, and
check the case's `expect`s. usage: python3 mkcase.py SPEC.py  (SPEC defines CASES = {name: case})"""
import copy, json, sys, runpy
from pathlib import Path
ROOT = Path.home() / "repos/nexkit-342"
sys.path.insert(0, str(ROOT))
from tests.test_conformance import start_of, event_of, run_program, at

spec = runpy.run_path(sys.argv[1])
for name, case in spec["CASES"].items():
    state = start_of(case)
    policies = case.get("policies", {})
    out = {k: v for k, v in case.items() if k != "steps"}
    out.setdefault("policies", {})
    steps = []
    for number, step in enumerate(case["steps"], 1):
        policies = step.get("policies", policies)
        req = {"protocol": 1, "call": "step", "state": state, "event": event_of(step["event"], state, number), "policies": policies}
        ans = json.loads(run_program(req))
        if "error" in ans:
            raise SystemExit(f"{name} step {number}: {ans['error']}")
        new = {"event": step["event"], "state": ans["state"]["state"], "changed": ans["changed"], "actions": [a["kind"] for a in ans["actions"]]}
        for key in ("expect", "policies", "note"):
            if key in step:
                new[key] = step[key]
        for path, value in step.get("expect", {}).items():
            got = at(ans["state"], path)
            if got != value:
                raise SystemExit(f"{name} step {number}: {path} is {got!r}, not {value!r}\n{json.dumps(ans['state']['rounds'])}")
        print(name, number, step["event"]["kind"], step["event"].get("what", ""), "->", new["state"], new["changed"], new["actions"], json.dumps(ans["state"]["rounds"].get("counted")), ans["state"]["waits"], (ans["state"]["attention"] or {}).get("reason"))
        steps.append(new)
        state = ans["state"]
    out["steps"] = steps
    (ROOT / "tests/conformance" / f"{name}.json").write_text(json.dumps(out, indent=2) + "\n")

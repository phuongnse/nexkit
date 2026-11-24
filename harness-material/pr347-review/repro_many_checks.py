"""Finding 1: with more than 30 check results, an automatic fix round's diagnosis differs.
A pull request has 36 checks (or 2 sharded checks of 20 shards); before the fix round all 36
fail; after it only c32..c35 fail, the same way. NexKit 6 diagnoses c32..c35; the PR nothing.
python3 repro_many_checks.py <repo>"""
import io, contextlib, json, os, sys, tempfile
from pathlib import Path
REPO = sys.argv[1]; sys.path.insert(0, REPO); os.chdir(REPO)
os.environ["GITHUB_REPOSITORY"] = "acme/app"; os.environ["GITHUB_RUN_ID"] = "101"
from nexkit.report import report
from nexkit.route import route
from nexkit.state import empty_state, render_state, read_state
from tests.support import FakeGitHub, make_config
names = [f"c{i:02d}" for i in range(36)]
cfg = make_config(checks=[{"name": n, "run": f"run {n}"} for n in names], fix={"max_auto_rounds": 3})
gh = FakeGitHub(); gh.add_issue(5); gh.add_pull(6, 5, head_sha="1" * 40)
before = [{"name": n, "run": f"run {n}", "exit_code": 1, "passed": False, "output": "AssertionError",
           "signature": "AssertionError"} for n in names]
gh.comment(6, render_state({**empty_state(5), "auto_fixes": 1,
                            "feedback": {"head": "c" * 40, "checks": before, "review": None}}))
art = Path(tempfile.mkdtemp())
def write(a, n, d):
    p = art / a / n; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(d))
write("nexkit-agent", "result.json", {"status": "done", "tokens": {"input": 1, "cached": 0, "output": 1}})
write("nexkit-checks", "checks.json", [{"name": n, "run": f"run {n}", "exit_code": int(i >= 32), "passed": i < 32,
                                         "output": "AssertionError" if i >= 32 else ""} for i, n in enumerate(names)])
write("nexkit-review", "result.json", {"status": "done", "tokens": {"input": 1, "cached": 0, "output": 1}, "output": {
    "verdict": "approve", "summary": "ok", "criteria": [], "findings": []}})
d = {"action": "fix", "issue": 5, "pr": 6, "target": 6, "branch": "nexkit/issue-5", "base": "main",
     "head": "c" * 40, "note": "", "auto": True, "autopilot": True}
needs = {"publish": {"outputs": {"result": json.dumps({"published": True, "pr": 6, "head": "1" * 40})}}}
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    out = report(gh, d, cfg, needs, art, workflow_ref="acme/app/.github/workflows/nexkit.yml@refs/heads/main", default_branch="main")
    inputs = {"command": "fix", "number": "6", "note": "", "auto": "true"}
    dec = route(gh, "workflow_dispatch", {"inputs": inputs, "sender": {"login": "alice"}}, inputs)
print("outcome:", out["outcome"])
print("state comment feedback.repeated:", read_state(gh.comments(6))[1]["feedback"].get("repeated"))
print("route diagnose for the next automatic fix round:", dec.get("diagnose"))

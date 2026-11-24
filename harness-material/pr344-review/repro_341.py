"""#341 claim: a blocked `/nexkit go` whose recommended answers both close the issue and
need a new plan. Usage: python3 repro_341.py TREE [close-only]"""
import contextlib, io, json, os, sys, tempfile
from pathlib import Path
from unittest import mock
TREE = sys.argv[1]; sys.path.insert(0, TREE); os.chdir(TREE)
from nexkit import autopilot
from nexkit.report import report
from nexkit.state import PLAN_MARKER, render_run
from tests.support import FakeGitHub, make_config
from tests.test_autopilot import WORKFLOW

only_close = len(sys.argv) > 2
gh = FakeGitHub(); gh.add_milestone(1)
gh.add_issue(5, title="Issue 5", milestone=1)
cfg = make_config(autopilot={"enabled": True, "answer": "recommended"}, notify=["olivia"])
gh.comment(5, render_run({"url": "https://github.com/acme/app/actions/runs/1", "command": "plan", "status": "success", "outcome": "planned"}, "Done."))
gh.comment(5, f"{PLAN_MARKER}\n## Plan\n\nAdd it.")
qs = [{"question": "Keep the old API?", "options": ["Keep it", "Close the work item"], "recommended": 2, "closes": 2, "replans": []}]
if not only_close:
    qs.append({"question": "One change or two?", "options": ["One", "Split it into two"], "recommended": 2, "closes": 0, "replans": [2]})
output = {"status": "blocked", "summary": "s", "blocker": "Two ways to read.", "questions": qs, "checks_run": [], "protected_paths": [], "changes": [], "testing": "", "outside_plan": [], "reviewer_notes": [], "no_change": ""}
tmp = Path(tempfile.mkdtemp()); (tmp / "nexkit-agent").mkdir()
(tmp / "nexkit-agent" / "result.json").write_text(json.dumps({"status": "blocked", "error": "Two ways to read.", "output": output, "tokens": {"input": 1, "output": 1}}))
decision = {"action": "implement", "issue": 5, "pr": None, "target": 5, "branch": "nexkit/issue-5", "base": "main", "head": None, "note": "", "auto": False, "autopilot": True}
with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app", "GITHUB_RUN_ID": "7"}), contextlib.redirect_stdout(io.StringIO()):
    out = report(gh, decision, cfg, {"publish": {"outputs": {"result": json.dumps({"published": False})}}}, tmp, workflow_ref=WORKFLOW, default_branch="main")
print("report outcome:", out["outcome"], "| attention:", out.get("attention"))
print("report says carries on:", "carries on from here" in out["summary"])
print("mentions after report:", [c["body"][:160].replace("\n", " ") for c in gh.comments_matching(5, "@olivia")])
n = len(gh.comments_matching(5, "@olivia"))
with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "acme/app"}), contextlib.redirect_stdout(io.StringIO()):
    lines = autopilot.sweep(gh, cfg, "nexkit.yml", "main")
print("sweep:", lines)
print("new mentions after sweep:", [c["body"][:200].replace("\n", " ") for c in gh.comments_matching(5, "@olivia")][n:])
print("dispatches:", [d["inputs"] for d in gh.dispatches if d["inputs"]["command"] != "autopilot"])
print("issue state:", gh.issues[5]["state"])

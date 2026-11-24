"""A fix round on the pull request stops as blocked with two questions whose recommended
options are: one that only closes the issue, one that only a new plan carries out.
NexKit 6 (8bf5190): report keeps the attention and mentions `notify` on the pull request.
#341: report says "NexKit autopilot carries on from here", leaves no attention, and
`nexkit status` lists the pull request with "the last round failed".
usage: python3 repro_close_replan.py ROOT"""
import json, runpy, sys
root = sys.argv[1]
sys.argv = ["x", root, "/dev/null"]
m = runpy.run_path(__file__.replace("repro_close_replan.py", "hstop.py"), run_name="h")
sc = {"place": "pull", "kind": "blocked", "qset": "close_replan", "protected": False,
      "answer": "recommended", "answered": "no", "record": False, "depth": "top", "auto": True}
r = m["run"](sc)
print("report.attention:", (r["report"]["attention"] or "None")[:120])
print("carries on line in summary:", "autopilot carries on" in r["report"]["summary"])
print("mentions:", [(n, c["body"][:90]) for n, cs in r["after_sweep"]["issue_comments"].items() for c in cs if c["body"].startswith("@olivia")])
print("status PR reason:", [x["reason"][:90] for x in r["status"]["needs_person"] if x["kind"] == "pull_request"])

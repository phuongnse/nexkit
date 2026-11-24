"""An imported base-failure wait that names no bug (`on: null`) can never end: a
`blockerClosed` must name an id, and the engine matches `on` exactly. host.base_passes uses
"none" for the same case, so the two disagree."""
import os, sys
TREE = os.environ.get("TREE", os.path.expanduser("~/repos/nexkit-r344"))
from nexkit import host
from tests.support import make_config
cfg = make_config(autopilot={"enabled": True})
doc = host._pull_doc(cfg, 5, 6, "c" * 40, "fixing", 0)
doc["rounds"]["due"] = True
doc["waits"] = [{"id": "w1.1", "for": "baseFailure", "until": None, "on": None}]  # as first_state writes it
for blocker in ("none", "None", ""):
    ev = host._event("blockerClosed", 5, None, f"b-{blocker}", blocker=blocker, completed=True)
    try:
        ans = host.step(doc, ev, host.policies(cfg))
        print(repr(blocker), "changed:", ans["changed"], "waits:", [w["for"] for w in ans["state"]["waits"]])
    except host.Refused as e:
        print(repr(blocker), "refused:", e)

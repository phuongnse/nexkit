"""Make mutant copies of the PR head. python3 mutants.py <src repo> <out dir>"""
import os, shutil, sys
SRC, OUT = sys.argv[1], sys.argv[2]
M = "nexkit/engine/machine.py"; H = "nexkit/host.py"; R = "nexkit/report.py"
MUTANTS = {
    "m01-compare-always": (M, "after_fix = rounds.pop(\"fixed\", None) == self.version", "rounds.pop(\"fixed\", None); after_fix = True"),
    "m02-base-wait-keeps-repeated": (M, "                rounds.pop(\"repeated\", None)\n                self.wait(\"baseFailure\", base)", "                self.wait(\"baseFailure\", base)"),
    "m03-diagnose-always": (M, "fields = {\"diagnose\": repeated[:DIAGNOSES] if diagnose else []}", "fields = {\"diagnose\": repeated[:DIAGNOSES]}"),
    "m04-resume-no-diagnose": (M, "self.run(stage, again=True, diagnose=automatic)", "self.run(stage, again=True, diagnose=False)"),
    "m05-hold-ignored-on-conflict": (M, "        if self.s[\"hold\"] and policy != \"person\":\n            return self.conflict_held()\n", ""),
    "m06-conflict-round-keeps-automatic": (M, "            rounds.pop(\"automatic\", None)\n            self.run(\"fix\")\n        else:", "            self.run(\"fix\")\n        else:"),
    "m07-conflict-publish-keeps-failing": (M, "            for key in (\"failing\", \"repeated\", \"fixed\"):\n                rounds.pop(key, None)", "            pass"),
    "m08-request-keeps-fixed": (M, "        self.s[\"rounds\"].pop(\"fixed\", None)\n        return True", "        return True"),
    "m09-limit-no-reset-waits": (M, "        if limit[\"reset\"] is None:\n            self.move(\"failed\")", "        if False:\n            self.move(\"failed\")"),
    "m10-host-no-signature": (H, "found.append(_check(name, passed, sign or None))", "found.append(_check(name, passed, None))"),
    "m11-host-fixed-only-auto": (H, "    if decision[\"action\"] == \"fix\":\n        # The checks of a fix round compare", "    if decision[\"action\"] == \"fix\" and decision.get(\"auto\"):\n        # The checks of a fix round compare"),
    "m12-host-diagnosis-always": (H, "    if automatic:\n        doc[\"rounds\"][\"automatic\"] = True", "    if True:\n        doc[\"rounds\"][\"automatic\"] = True"),
    "m13-report-paused-no-engine": (R, "            outcome = ended((\"paused\", resume and resume[\"at\"]))", "            pass"),
    "m14-conflict-held-no-move": (M, "        if self.state == \"checking\" and not running:\n            return False\n        self.move(\"checking\")\n        return True", "        if self.state == \"checking\" and not running:\n            return False\n        return True"),
    "m15-host-conflicting-ignores-used": (H, "    doc[\"rounds\"][\"conflicts\"] = max(int(used or 0), 0)\n    doc[\"pending\"] = [{\"id\": first_state.RUN_ID", "    doc[\"rounds\"][\"conflicts\"] = 0\n    doc[\"pending\"] = [{\"id\": first_state.RUN_ID"),
    "m16-released-ignores-moved": (H, "    seen.update(conflicts=bool(conflicts) and not moved, merged=False, empty=False)", "    seen.update(conflicts=bool(conflicts), merged=False, empty=False)"),
    "m17-name-only-compare": (M, "repeated = [name for name, found in now.items() if after_fix and before.get(name) == found]", "repeated = [name for name, found in now.items() if after_fix and name in before]"),
    "m18-diagnoses-5": (M, "DIAGNOSES = 10", "DIAGNOSES = 5"),
    "m19-person-request-old-policies": (M, "            self.s[\"policies\"], self.p = {}, self.given\n            self.check()", "            self.s[\"policies\"] = {}\n            self.check()"),
    "m20-failed-review-ignored": (R, "            ended((\"again\", None) if again else (\"failed\", found[\"error\"]))", "            pass"),
    "m21-person-pr-not-review-only": (H, "    if person:\n        doc[\"itemKind\"] = \"reviewOnly\"", "    if False:\n        doc[\"itemKind\"] = \"reviewOnly\""),
    "m22-import-no-repeated": ("nexkit/first_state.py", "        if repeated:\n            rounds[\"repeated\"] = repeated[: MOST[\"repeated\"]]", "        pass"),
}
for name, (path, old, new) in MUTANTS.items():
    dest = os.path.join(OUT, name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    text = open(os.path.join(dest, path)).read()
    assert text.count(old) == 1, (name, text.count(old))
    open(os.path.join(dest, path), "w").write(text.replace(old, new))
    print(name)

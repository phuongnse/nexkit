# Throwaway harnesses: raw material for #349

Not for merging. These are the one-off scripts that the authors and the independent reviews of #341, #344 and #347 wrote to check the engine against NexKit 6 at 8bf5190. #349 builds the maintained harness in `tests/differential/` from them.

- `pr341-review/`: the review of #341. `hreport.py` plays `report` after a round's checks and review; `hstop.py` plays a stopped round, then the sweep and `nexkit status`.
- `pr344-author/`, `pr344-review/`: #344. Sweeps across hold and base-failure waits (`hsweep.py`), a round after a wait (`hround.py`), required checks at the merge (`hreq.py`), stopped rounds and their count (`hgive.py`), an engine walker (`fuzz_engine.py`, `walk.py`), a builder of conformance cases (`mkcase.py`, `spec*.py`).
- `pr347-author/`: #347. Diagnosis chains (`hdiag.py`), conflicting and held conflict rounds (`hconf.py`), failed, paused or missing reviews and a person's own pull request (`hrev.py`), runners (`all.sh`, `many.sh`, `mut.sh`).
- `pr347-review/`: the review of #347. A scenario generator to JSONL (`gen.py`, `gen30.py`), a driver that plays one scenario on a fresh `FakeGitHub` in a given tree and hashes everything (`drive.py`), the runner and compare (`run.sh`), a released conflict round (`rel.py`), an engine walker with invariants (`walk.py`, `eng.py`).

Paths were machine-specific: `$SCRATCH` stood for a scratch folder, and the NexKit 6 tree was a checkout of 8bf5190. Each script takes the tree to play as an argument or an environment variable.

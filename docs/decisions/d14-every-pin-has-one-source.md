# D14. Every pin has one source

Status: **Proposed**

## Context

The pipeline installs the agent CLI on a runner, and the engine's image has a base. If each version were set in
several files, the files would drift apart, and the checks would test a version the pipeline does
not install.

GitHub reads a workflow's `runs-on` and the `python-version` of `setup-python` before any step runs. So a
workflow cannot read the runner or Python from `nexkit/__init__.py`, and it must write them out.

## Decision

- `nexkit/__init__.py` is the one source of the pins: the agent runtimes, the image's base, the runner
  and Python. The pipeline installs exactly those versions.
- Code and steps that can read that file read it, so a runtime's version and the image's base are
  never repeated.
- The workflows, and the caller template that `nexkit init` installs, repeat the runner and Python as
  literals. A test keeps each copy equal to its pin.
- This repository's own development tools, ruff and actionlint, are pinned once in `scripts/lint.sh`.
  No workflow proposes them.
- Tests use no matrix. They run the pinned versions and no others.
- `update-agents.yml` and `update-image.yml` each propose a new pin, and each proposal needs its own
  check before it merges.

The detail is in [The smoke test](../testing.md#the-smoke-test).

## Consequences

- A runtime version or the image's base changes in one file, in a change that the checks cover.
- A change to the runner or Python edits the pin and each copy. The test fails until they match.
- Users get a tested version rather than the newest one. The weekly proposals shorten the wait for
  a new version.
- Only the pinned versions get the checks. Any other version runs untested.

## Alternatives

- Version ranges, which pick up new releases without a change. A range would install versions that
  no check has run.
- No copies at all. GitHub reads `runs-on` before any step, so a workflow cannot take the runner from Python.
- Generating the workflows from the pins. That adds a build step that every workflow change would need.

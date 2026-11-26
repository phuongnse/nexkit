# D9. Pins live in one place

Status: **Agreed**

## Context

The pipeline installs the agent CLI and the engine's image has a base. If each version were set in
several files, the files would drift apart, and the checks would test a version the pipeline does
not install.

## Decision

- Pins live only in `nexkit/__init__.py`. The pipeline installs exactly those versions.
- Tests use no matrix. They run the pinned versions and no others.
- `update-agents.yml` and `update-image.yml` each propose a new pin, and each proposal needs its
  own check before it merges.

The detail is in [The smoke test](../testing.md#the-smoke-test).

## Consequences

- A version changes in one file, in a change that the checks cover.
- Users get a tested version rather than the newest one. The weekly proposals shorten the wait for
  a new version.
- Only the pinned versions get the checks. Any other version runs untested.

## Alternatives

- Version ranges, which pick up new releases without a change. A range would install versions that
  no check has run.

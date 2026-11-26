# D8. Standard library only

Status: **Agreed**

## Context

The NexKit package runs in each repository's pipeline and on a person's machine. Each third-party
package adds a pin to keep, an install step to run and a supply chain to trust.

## Decision

- The `nexkit/` package uses the Python standard library only.
- It needs Python 3.12 or later, which the pipeline installs.
- Lint and test tools are not runtime dependencies. They run only in the checks.

The detail is in [The package](../architecture.md#the-package) and
[Supported versions](../../README.md#supported-versions).

## Consequences

- The pipeline installs no third-party packages for NexKit, so a run starts sooner and has fewer
  versions to keep working.
- Code that a library would provide, such as HTTP calls and schema checks, is written and tested
  in NexKit.
- A security fix in a library reaches NexKit only through a NexKit change.

## Alternatives

- Use libraries for HTTP and schema validation. Each saves code, and each adds a pin, an install
  step and a version to test.

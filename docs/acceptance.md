# Verification guide

[Documentation](README.md) / Verification

Use [current support](support.md) to choose the integrations and environments
being verified, and [How to verify NexKit](live-acceptance.md) for the acceptance
criteria and repeatable procedure. This guide describes the checks and how to
interpret their evidence. Results belong to their run or release, outside the
source tree.

## Choose checks by behavior and environment

| Boundary | Required verification |
|---|---|
| Shared behavior | Run the automated suite, Python lint and formatting checks, and workflow syntax validation |
| Installation | Verify extraction, packaged file hashes, source identity, native CLI commands, skill discovery, reinstall and removal |
| Native commands and workspaces | Exercise actual account permissions, private results, command I/O, failures, timeouts and trusted workspace restoration |
| Container runtime | Exercise actual CLI tools, role permissions, credential isolation and the configured network boundary on a supported Docker host |
| Host lifecycle | Exercise the host's actual process and service behavior, including WSL supervision when selected |
| Live delivery | Use an actual project, runner, model access, checks, approvals, independent review and merge |
| Release | Verify the separately approved candidate, selected-source build, publication and downloaded assets |

Choose the actual prerequisites for each boundary. A compatible local machine
or CI runner can provide them. Follow the
[native verification procedure](live-acceptance.md#native-checks-omitted-by-the-shared-suite)
for environment-specific checks; the operator's current operating system does
not change their requirements.

## Interpret results

Record discovered, executed, passed, failed and skipped checks for each run.
A skipped check leaves its boundary unverified. Passing checks in one environment
do not establish behavior in another, and a passing shared suite does not replace
required native checks.

Keep these evidence types separate:

- Static syntax and configuration checks.
- Automated tests with simulated repository state, model replies or approvals.
- Actual CLI, tool and container execution with simulated model responses.
- Live local agent sessions using a real model.
- Live repository integration using the project's actual workflows and approvals.

A package installation probe does not establish a complete delivery. A container
check on a Windows host establishes its WSL runtime, not native Windows application
behavior. Verify application behavior with the appropriate native checks.

## Keep evidence with the run

CI logs and artifacts identify the checks and environment for that run. Release
assets identify the packaged source and file hashes in `candidate.json` and
`SHA256SUMS`. Keep local verification records, environment snapshots and detailed
command output outside the source tree. Attach relevant evidence to the
corresponding run or release when it needs to be shared.

Record the exact commit or release, selected configuration, environment, commands,
outcomes, skipped checks and remaining limits. Preserve evidence needed beyond
artifact retention. Do not commit verification results or development journals
to the project source.

## Verify a project after setup

Run `nexkit doctor --online --checks` after the accepted configuration and
workflows reach the default branch. Then start a small request and follow its
actual runner, model access, commands and configured approvals through independent
review, merge and issue completion. Verify publication separately if releases
are enabled.

Record real run links and the exact kit pin. Use the
[acceptance procedure](live-acceptance.md) without resetting budgets or supplying
an approval on another person's behalf.

# Manual setup example

Use this with the [manual setup guide](../../manual-setup.md). Copy the whole
directory to a separate proposal directory before editing it.

This example selects one pipeline, `maintenance`, with issue intake,
clarification and the built-in delivery adapter. It uses API authentication
initially; the guide explains the subscription changes.

| File | Purpose |
|---|---|
| `project.json` | Full settings example; edit identity, models, budgets and actual check commands |
| `bundle/.github/workflows/capture.yml` | Receive issue start comments and CLI submissions |
| `bundle/.github/workflows/discuss.yml` | Run requirement discussion |
| `bundle/.github/workflows/change.yml` | Run delivery after approval |
| `branch-rules.json` | Administrative rule proposal for a repository with no existing branch policy |

The zero kit pins, empty file manifest, `OWNER/REPO` and zero App IDs are
placeholders. The guide fills the pins, file hashes and App IDs. The sample test
commands assume a Python project with unittest suites in `tests/unit` and
`tests/e2e`; replace them with your project's commands. No tests or application
are created by copying this example.

The model, effort, budgets and workflow structure are example choices. They are
not core defaults or a required pipeline shape. The selected delivery adapter
has a fixed internal job sequence. Use the
[individual jobs](../../agent-invocations.md) and
[approval wiring](../../stage-approvals.md) when you need a different sequence
or a PR approval gate.

This example requires requirement approval and separate AI review. It does not
configure an additional person to review the PR, or a release entrypoint.
The shared intake adapter exposes a release operation, but this configuration
does not enable it.

These files are documentation templates derived from the current configuration
and workflow contracts. Verify the installed example in your actual project
using the [verification guide](../../acceptance.md).

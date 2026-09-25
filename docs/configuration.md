# Configuration from project decisions

`nexkit survey` reads the repository's files, instructions, workflows and
manifests. The setup skill combines this evidence with user decisions; the core
has no language, framework or project-type presets. Consumer configuration lives
in `.nexkit/project.json`.

| Field | Required decision |
|---|---|
| `schema` | `1` |
| `repository`, `default_branch` | GitHub owner/name and actual integration branch |
| `kit` | `repository`, full commit SHA `ref`, exact `version` |
| `engine` | `name: codex` and exact CLI `version`; currently the only CI engine |
| `models` | Accessible models for `implement` and `review`; no implicit defaults |
| `limits` | `attempts` (1–20), `agent_calls` (3–40), `minutes` (1–1440), `command_seconds` (1–3600) |
| `environment` | `runner: ubuntu-24.04`; `setup` commands as argument arrays |
| `application` | `present` or `absent` at setup; new repositories still declare intended checks before delivery |
| `checks` | Actual commands, each with `name`, `kind`, `argv`, `timeout_seconds` |
| `decisions` | Accepted product and technical decisions as a list of strings |
| `knowledge` | Relative paths to the project's knowledge sources |
| `merge_method` | `squash`, `merge` or `rebase`, according to project policy |
| `release` | Explicit `enabled`; when enabled, `build` argv, `artifacts` paths and `tag_prefix` |

Check kinds are `build`, `lint`, `test` and `e2e`. Test/E2E checks require a native
`report` with `format: junit|tap|unittest`; JUnit also needs `path`. Missing,
zero-case, failed, skipped or TODO results cannot pass. The independent reviewer
assesses assertion quality and its relationship to the requirement. Include
build/lint when relevant; they do not replace E2E. Commands are argument arrays.
If a project genuinely needs a shell, declare that choice explicitly in its argv
and execute it only without privileged credentials.

Release build arguments may contain `{version}` and `{commit}`; artifact paths
may contain `{version}`. Commit source version changes before selecting the
candidate. This candidate publishes artifacts through GitHub Releases. Package
registry publication and production deployment are not implemented.

Preview and apply authorized setup decisions:

```sh
nexkit setup --config /path/to/accepted-project.json --host codex
nexkit setup --config /path/to/accepted-project.json --host codex --apply --online
nexkit doctor --online --checks
```

Setup writes configuration and runs verification. Exit code 2 identifies a
capability that is not ready; an empty application does not receive fake passing
checks. `doctor` inspects secret metadata but cannot infer model access from a
secret name. Live agent acceptance remains necessary.

## GitHub settings

An administrator must inspect and accept the setup:

- Actions may create PRs; wrappers exist on the default branch.
- An active ruleset requires exactly `NexKit verification` and `NexKit review`,
  bound to the GitHub Actions App, with strict up-to-date checks.
- There are no blanket bypasses, mandatory human PR reviews, required deployments
  or unsupported merge queues. Inspect classic protection and inherited rules too.
- Job tokens can write the `nexkit/state` and delivery branches.
- Existing required verification behavior is preserved through declared commands
  or a verified dispatch integration; do not simply remove checks to obtain green CI.
- `OPENAI_API_KEY` is an Actions secret, selected models are accessible and usage
  limits have been accepted. Never copy local ChatGPT authentication into public CI.

Runtime reads active rules through the Metadata:read API. Full bypass/classic
protection auditing uses the setup administrator; CI retains no administrative
credential. Ruleset availability depends on GitHub plan/repository visibility
and must be checked in the actual environment.

The setup command does not change GitHub settings itself. The setup agent uses
GitHub tooling to apply accepted administrative changes and runs doctor again.
This is setup work, not an additional approval for each feature.

## Budget accounting

Clarification uses the `implement` model and reserves one invocation. Each
delivery round reserves two for implementation and review. Failed runs consume
reservations. At least three calls are needed for one clarification and one
delivery round. Delivery elapsed time starts at its first attempt and survives
retries. Clarification reserves each session's runtime; waiting for human answers
does not consume that reserved runtime. These are invocation/time bounds, not
measured tokens or a provider spending cap.

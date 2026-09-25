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
| `engine` | `name: codex`, exact CLI `version`, and `auth` set to `api-key` or `chatgpt` (omitted means `api-key`) |
| `models` | Accessible models for `implement` and `review`; no implicit defaults |
| `reasoning_effort` | Optional object declaring `implement` and `review`; forwarded to the official action or CLI |
| `limits` | `attempts` (1–20), `agent_calls` (3–40), `minutes` (1–1440), `command_seconds` (1–3600) |
| `environment` | `runner: ubuntu-24.04` for controller/check/release jobs; optional `agent_runner` for CLI jobs; `setup` commands as argument arrays |
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

When `reasoning_effort` is supplied, both roles must be explicit. Accepted values
are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` and `ultra`; the selected
model and pinned CLI must support the chosen value. Clarification uses the
implementation role's model and effort. If the object is omitted, the CLI uses
its model default. NexKit never silently substitutes an unsupported effort.
`doctor` reports the configured values; live account access is verified on the
runner. For example, the acceptance owner's selected configuration is:

```json
{
  "models": {"implement": "gpt-6-luna", "review": "gpt-6-luna"},
  "reasoning_effort": {"implement": "max", "review": "max"}
}
```

The [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna)
documents support for `max`. This is a consumer decision, not a core model default.

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
secret name. For a self-hosted runner it checks online registration and matching
labels. `ready_scope` describes configuration/check readiness; `live_agent_verified`
is reported separately. Live agent acceptance remains necessary.

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
- Authentication matches `engine.auth`, selected models are accessible and usage
  limits have been accepted. API mode requires `OPENAI_API_KEY`; subscription
  mode requires the dedicated runner setup in [self-hosted operation](self-hosted.md).
- Public consumers using that runner require approval for all outside-contributor
  fork workflows. Direct PR jobs never run on the credential-bearing runner.

Runtime reads active rules through the Metadata:read API. Full bypass/classic
protection auditing uses the setup administrator; CI retains no administrative
credential. Ruleset availability depends on GitHub plan/repository visibility
and must be checked in the actual environment.

The setup command does not change GitHub settings itself. The setup agent uses
GitHub tooling to apply accepted administrative changes and runs doctor again.
This is setup work, not an additional approval for each feature.

## OpenAI API authentication for Actions

With `engine.auth: api-key`, the pipeline runs the official Codex CLI through the
official GitHub action. It uses an OpenAI Platform API key stored as the repository Actions
secret `OPENAI_API_KEY`. The key authenticates the API project; `models` chooses
the model used by each role.

Create a key in the [OpenAI API dashboard](https://platform.openai.com/api-keys),
with API billing available, then add it in each consumer's **Settings → Secrets
and variables → Actions → New repository secret**. Name it `OPENAI_API_KEY`.
Alternatively, `gh secret set OPENAI_API_KEY --repo OWNER/REPO` prompts for the
value without placing it in shell command history. Never paste the key into
chat, source, issue bodies or logs.

Codex's ChatGPT sign-in uses subscription access. API-key usage is billed through
OpenAI Platform at API rates, separately from included ChatGPT plan usage. See
[official Codex authentication](https://learn.chatgpt.com/docs/auth) and the
[official GitHub action guide](https://learn.chatgpt.com/docs/github-action).
Accept the API usage/spending limits before enabling live model runs. A present
secret does not establish that the project can access the configured model.

### Using an existing ChatGPT subscription

Codex CLI also supports ChatGPT sign-in with `codex login` or
`codex login --device-auth`. OpenAI documents an
[advanced account-auth flow for CI](https://learn.chatgpt.com/docs/auth/ci-cd-auth):
Codex maintains the login cache, and automation preserves the refreshed file
between runs. That guide restricts this flow to trusted private automation and
explicitly excludes public and open-source repositories. NexKit's separately
implemented public-repository mode is an experimental integration accepted by
this project's owner; it is not an OpenAI-recommended public CI configuration.
See [self-hosted operation](self-hosted.md) for its boundaries, setup and live
verification status. No subscription credential is stored in repository content
or Actions artifacts.

[Codex access tokens](https://learn.chatgpt.com/docs/enterprise/access-tokens)
are currently documented for Business and Enterprise workspaces. Do not assume
they are available to a personal Pro account or invent a `codex setup-token`
command. The installed CLI supports browser and device-code login.

`environment.agent_runner` defaults to `environment.runner`. A dedicated runner
uses a label list such as `["self-hosted", "linux", "x64", "project-runner"]`.
Choose a project-specific label and register the runner to that repository.
Labels route jobs; the installed admission hook enforces the repository/branch/
workflow boundary. Subscription mode requires Codex `0.156.1` and this dedicated
container integration. A plugin installation never selects NexKit's own VPS.

## Budget accounting

Clarification uses the `implement` model and reserves one invocation. Each
delivery round reserves two for implementation and review. Failed runs consume
reservations. At least three calls are needed for one clarification and one
delivery round. Delivery elapsed time starts at its first attempt and survives
retries. Clarification reserves each session's runtime; waiting for human answers
does not consume that reserved runtime. `limits.minutes` applies separately to
total clarification reservations, elapsed delivery time and elapsed release time;
it is not one end-to-end wall-clock cap. These are invocation/time bounds, not
measured tokens or a provider spending cap.

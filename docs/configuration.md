# Configuration from project decisions

`nexkit survey` reads the repository's files, instructions, workflows and
manifests. The setup skill combines this evidence with user decisions; the core
has no language, framework or project-type presets. Consumer configuration lives
in `.nexkit/project.json`.

The table below documents schema 1 and the shared execution settings. Schema 2
adds consumer-defined pipeline bindings and accepted native workflow bundles;
see [workflow composition](workflow-composition.md) for its contract and current
implementation limits.

For individually composed delivery, a pipeline's `invocations` supplies each
call's model, reasoning, task, skills, runner and timeout. Pipeline-level models
are only needed for integrations that use them, such as clarification. See the
[invocation reference](agent-invocations.md); `doctor` reports resolved values
and checks all selected self-hosted runner labels.

Optional pipeline `approvals` configure approval steps at selected boundaries
of composed delivery. Choose the subject, reviewers, quorum, waiting limit,
rejection behavior and exact continuation workflow during setup. See
[stage approvals](stage-approvals.md) for the configuration and native event wiring.
Reviewer selection can use current repository permissions or an explicit login
list. Both the required count and waiting window belong to the consumer config.

| Field | Required decision |
|---|---|
| `schema` | `1` |
| `repository`, `default_branch` | GitHub owner/name and actual integration branch |
| `kit` | `repository`, full commit SHA `ref`, exact `version` |
| `engine` | `name: codex`, exact CLI `version`, and `auth` set to `api-key` or `chatgpt` (omitted means `api-key`) |
| `models` | Accessible models for `implement` and `review`; no implicit defaults |
| `reasoning_effort` | Optional object declaring `implement` and `review`; forwarded to the official action or CLI |
| `limits` | `attempts` (1–20), `agent_calls` (2–40 with separate clarification, otherwise 3–40), `minutes` (1–1440), `command_seconds` (1–3600) |
| `clarification` | Optional separate conversation settings: required `agent_minutes` (1–60), optional positive `max_calls`; omitted/null `max_calls` means no conversation-count cap |
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
- There are no blanket bypasses, required deployments or unsupported merge queues.
  Native PR reviews match the accepted [approval policy](stage-approvals.md):
  zero by default, or the configured count with stale-review dismissal. Inspect
  classic protection and inherited rules too; unintegrated additional gates block setup.
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

New setup can give requirement conversation its own limits:

```json
{
  "clarification": {"agent_minutes": 15, "max_calls": null},
  "limits": {"attempts": 2, "agent_calls": 4, "minutes": 60, "command_seconds": 120}
}
```

Each authorized collaborator comment can trigger a time-bounded clarification call.
Omitting `max_calls`, or setting it to `null`, allows further conversation without
a total call-count cap. Set a positive integer to cap the total clarification
calls for a work item. An unchanged successful input does not call the model
again; failed input requires a new collaborator comment or a fresh `/nexkit resume`
comment before another reservation. Bot responses never trigger more agent calls.

With this separate configuration, `limits.agent_calls` covers implementation and
review. Each delivery round reserves two calls. At least two are required;
failed runs still consume reservations. Total calls and the delivery subtotal
remain recorded across retries and administrative migrations. Delivery elapsed
time starts at its first attempt and survives retries. Release has its own
elapsed window. Neither is reset by conversation or resume.

Existing configurations **without the `clarification` object** retain their
previous shared accounting: one call per clarification, two per delivery round,
at least three available calls, and `limits.attempts` also caps clarification.
Their clarification session duration remains capped at 15 minutes, with total
reserved clarification minutes bounded by `limits.minutes`. Adding the separate
object is an explicit administrative change; upgrading the kit alone does not
raise an existing project's allowance. `doctor` reports the effective mode.

Clarification uses the `implement` model. These are invocation/time bounds, not
measured tokens or a provider spending cap. An uncapped conversation still uses
the configured account's allowance when an authorized collaborator submits new input.

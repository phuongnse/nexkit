# Agent adapters

[Documentation](README.md) / Agent adapters

An adapter translates NexKit's accepted session into a provider's commands and
settings. Core code owns approval, usage accounting, candidate identity, workspace
preparation and result validation. Provider details live in
[nexkit/adapters](../nexkit/adapters/).

The current registered integration is Codex. This boundary allows another agent
to implement the same contract without introducing version branches into the
controller or a CI matrix of CLI releases.

## Contract

The [adapter interface and registry](../nexkit/adapters/__init__.py) define the
methods used by configuration, session preparation and runner administration.
Only adapters registered in trusted toolkit code can be selected.

| Adapter responsibility | Called by |
|---|---|
| Validate provider configuration, authentication, effort and runner requirements | [policy.py](../nexkit/policy.py) |
| Produce provider session settings and execute one prepared role | [agent_session.py](../nexkit/agent_session.py) |
| Declare trusted control directories and HOME configuration | [workspace.py](../nexkit/workspace.py), [ci.py](../nexkit/ci.py) |
| Add provider execution constraints to the prompt | [ci.py](../nexkit/ci.py) |
| Check CLI capabilities, choose an installation image and form a login command | [runner_host.py](../nexkit/runner_host.py), [runner administration](../scripts/manage_runner.py) |
| Declare the required model secret | [project diagnostics](../nexkit/project.py) |
| Prepare public activity observation and stop isolated session accounts before collection | [agent_observability.py](../nexkit/agent_observability.py) |

`engine.name` selects the adapter. Authentication and optional `engine.install`
fields belong to that integration. Core logic does not interpret CLI release
numbers. Installation pins select artifacts, and reported versions are recorded
for diagnosis. See [versions and artifact identity](versions.md).

Codex checks the options needed for isolated execution and structured output.
Its [native subscription integration](../nexkit/adapters/codex_subscription.py)
also exercises sandbox behavior with harmless credentials before using a real
login. The official API action validates its arguments and uses its unprivileged
user strategy. A release label alone cannot establish either execution boundary.

Codex public JSONL activity is translated in
[codex_events.py](../nexkit/adapters/codex_events.py). Subscription execution drains
stdout and stderr while retaining its existing private runner log. API mode
retains the pinned official `openai/codex-action` for authentication and its
unprivileged user strategy. A trusted `sudo` shim observes only its exact
`-u nexkit-agent -- /absolute/codex exec` child, adds `--json`, and delegates
other operations to `/usr/bin/sudo`. Prompt input, arguments, environment and
execution identity remain those selected by the official action. The observer
does not implement API authentication. Native tests verify the actual sudo
account, prompt forwarding and timeout behavior.

The shared logger selects public messages, commands, changes and usage, excludes
reasoning and unknown payloads, redacts recognizable credentials, and bounds
retained events and output. Cleanup runs outside the timed session before reading
bounded regular files without links. These artifacts are diagnostics only; the
normal result schema, reservation and candidate checks remain authoritative.
See [operator guidance](operations.md#read-progress-and-recover-a-run).

## Add an integration

1. Implement the contract in a provider module and register it. Keep provider
   configuration validation, CLI syntax, authentication and output translation
   there. Unknown adapters and missing capabilities must fail before execution.
2. Preserve the prepared workspace and role permissions. Return the existing
   result schema; model output never grants authority or spends a new reservation.
   Add actual credential, network and filesystem checks for the native runtime.
3. Add its Actions implementation behind
   [the session action](../actions/agent-session/action.yml). GitHub's static
   `uses:` routing must explicitly select the trusted provider action. The
   current action fails for an adapter without a registered Actions integration.
4. Extend [the shared adapter contract tests](../tests/test_agent_adapters.py)
   with the new adapter, plus provider-specific translation and failure cases.
   Exercise public activity filtering, account cleanup and diagnostic collection;
   provider protocol parsing belongs to the adapter, not core scheduling.
   Run that suite in the existing CI jobs. Use one reproducible native CLI
   reference for the integration; do not create a release-version matrix.
5. Update [support scope](support.md), setup examples and applicable
   [acceptance checks](live-acceptance.md). Report actual tool execution, simulated
   model boundaries and live model/GitHub runs separately.

The example adapter in the tests demonstrates a different authentication and
effort vocabulary without a second installed CLI. It is a test fixture, not a
supported product integration.

# How agents and Actions work together

Python's standard library connects Git, `gh`, the official agent CLI and consumer
commands. NexKit uses Codex's existing tool loop and connects bounded repair
rounds through Actions. It has no model API client, agent runtime, daemon,
database or model router.

```mermaid
flowchart LR
  H[Host and shared skills] --> I[Serialized GitHub intake]
  I --> S[Issue: authoritative requirement]
  S --> Q[Codex clarifies from repository and human answers]
  Q -->|questions| S
  Q -->|specification ready| A[Human approves exact hash]
  A --> P[Controller reserves budget]
  P --> C[Official Codex CLI: implement]
  C --> B[Publish candidate data]
  B --> T[Real commands in isolated job]
  T --> R[Independent Codex review job]
  R --> G[Recheck authority and candidate]
  G -->|findings| P
  G -->|all conditions hold| M[Merge]
  M --> RC[Separate release candidate]
  RC --> HA[Human release decision]
  HA --> REL[Verify, build, tag, release]
```

`plugins/nexkit/skills` is the shared methodology source. Consumers keep their own
configuration, knowledge, managed installation files and small wrappers pinned
to a kit commit. The runner checks out that pin, installs the role's skill in a
fresh workspace and supplies the trusted method with task context and an output
schema. Inputs include requirements, configuration, source/base, feedback,
execution limits and the selected model.

The official `openai/codex-action` wraps `codex exec`, installing its CLI and
credential proxy. The API key stays outside the agent account. Implementer and
reviewer use separate jobs and sessions under a dedicated unprivileged OS user.
The reviewer cannot change the candidate and approve those changes. Collectors
use trusted checkout Git metadata and never execute candidate Git hooks.
Consumer commands have no GitHub write token. Publication, merge and release
control jobs process validated data without executing consumer code.

The issue title/body is the requirement authority. Human approval binds to its
SHA-256; the approver must currently have write, maintain or admin access. Body
edit timestamps and native title rename events prevent old approval reviving
after edit/revert. Progress is posted as separate comments.

Small JSON state on `nexkit/state` records budgets, reservations, candidates,
feedback and outcomes. GitHub Contents API SHA provides compare-and-swap writes.
Native `queue: max` serializes runs. Clarification and delivery share a queue;
intake has its own queue to deduplicate submissions from concurrent hosts.
Counters survive retries and resumes.

Intake dispatches clarification automatically. Authorized human answers on the
issue trigger a new bounded session with the previous questions and answers.
The requirement agent reads source; the controller updates the issue and rejects
changes if approval arrived while the agent was working.

Checks and review bind to spec, config, kit, base and head. The controller checks
approval, cancellation and the current PR again before merging. Strict required
checks protect against a changed base. Explicit `workflow_dispatch` continues
repair rounds without relying on PR events emitted using `GITHUB_TOKEN`.

A release issue binds commit, version, notes, repository and config digest.
Post-approval builds use that exact source and produce a provenance/hash manifest.
The publisher checks server-side asset digests, never clobbers assets and never
moves tags. A partial release persists its original artifact run locator; retries
download those exact bytes instead of rebuilding a partly uploaded draft.

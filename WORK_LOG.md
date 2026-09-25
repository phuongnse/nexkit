# NexKit implementation record

Original objective: the owner's attached 12-section specification, recorded in
the bootstrap GitHub issue. Completion means the full specification, including
live integrations; local simulations never establish live acceptance.

## Current state (2026-09-25)

- Starting repository was empty, with public remote `phuongnse/nex-kit`.
- Python 3.12.3, Node 24.18.0, Docker 29.1.3, GitHub CLI are available.
- Local Codex CLI 0.156.1 has ChatGPT login. No model API keys or repository
  Actions secrets/variables are configured. Do not copy local login into CI.
- Owner was asked for two authorized private consumer repositories, permission
  for a test release there, CI model choices/limits and a CI API secret.
- No public release, package publication or unrelated repository changes are
  authorized.

## Decisions

- Python >=3.11 standard library, Git and `gh`: a small CLI with deterministic
  GitHub guards, installation and task-specific Actions helpers. No agent loop,
  model API client, database or service.
- One source of portable Agent Skills. Thin official host packaging. Codex is
  the first CI engine; other hosts may submit to that engine.
- Use pinned `openai/codex-action` (official wrapper around `codex exec`, Apache
  2.0) with its secret proxy and an unprivileged OS user. Implementer and reviewer
  run in different jobs. GitHub write credentials only exist in control jobs
  that never execute consumer code.
- GitHub issue body is the requirement authority; human comments bind approval
  to SHA-256 of its title/body. Progress lives in separate bot comments.
- Explicit `workflow_dispatch` joins bounded delivery runs; do not depend on PR
  events emitted using `GITHUB_TOKEN`. One repository-wide concurrency group
  serializes delivery; state and reserved budgets persist on GitHub.
- Candidate identity includes requirement, config, base SHA, head SHA and kit
  revision. Merge rechecks authority and current state. Release is a separate
  workflow with a human decision bound to immutable source/version/notes.
- Spec Kit 1.0.11, Superpowers 6.4.1 and Agentic Workflows 0.89.21 were examined
  from their upstreams (MIT). They are not dependencies: adopting their broader
  lifecycle alongside this one would add duplication. No source was copied.

## Work remaining

1. Implement CLI, setup/install, approval/state guards and a complete bounded
   delivery/release Actions path.
2. Exercise local end-to-end behavior with two distinct consumers, including
   failure injection, cancellation, stale decisions, retries and ownership.
3. Install/load package on at least two actual hosts; document other hosts.
4. Independent agent review, fixes and regression checks.
5. Live Actions/agent/two-consumer/approval/release tests when authorized and
   credentials are ready. Record exact evidence and unresolved blockers.
6. Build installation archives and an unreleased candidate, audit every group
   A-H and hand over usage docs and candid limitations.

Do not mark the goal complete while any required live evidence is missing.

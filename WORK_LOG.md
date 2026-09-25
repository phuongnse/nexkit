# NexKit implementation record

Date: 2026-09-25. Objective: the owner's 12-section specification, recorded in
[bootstrap issue #1](https://github.com/phuongnse/nexkit/issues/1). This is a
bootstrap development record, not a fabricated product approval.

## Delivered implementation

- Empty phuongnse/nex-kit repository renamed phuongnse/nexkit as requested.
- Python standard library, Git and gh; official Codex CLI action; shared portable
  skills and native Codex/Claude packaging. No model API client or custom runtime.
- Survey, accepted setup preview/apply/verification, versioned installation and
  ownership-aware uninstall; status/cancel/resume/howto/knowledge.
- Serialized intake, bounded Actions clarification, exact human spec approval,
  implementation, real commands/E2E, independent review, feedback/retry and merge.
- Separate release decision, approved-source build/provenance, immutable tags
  and assets, recovery of original artifact bytes after partial publication.
- Pinned trusted controls, dedicated unprivileged execution, separate write jobs,
  protected config/skills, persistent counters and cancellation guards.
- Archive/checksums/source manifest, 66 tests with two distinct real local consumers,
  repeatable live local CLI smoke and installation/operations/acceptance docs.

## Verification and review

Native Codex CLI 0.156.1 and Claude Code 2.1.282 installed/loaded the plugin in
isolated configuration directories. Live local Codex fixed a real sign defect;
a separate reviewer used tools to check behavior without changing source.
Own GitHub CI and a read-only platform probe passed at 21f68b7. This proves native
queue/API support, not CI model authentication. Independent review found and
reproduced defects, drove fixes/regressions, and ultimately found no additional
code blocker in its reviewed scope. Evidence links are in docs/acceptance.md.

## External blockers and scope

An accepted CI authentication/deployment path, usage limits and real human approvals
remain prerequisites. Local ChatGPT auth was never copied to CI. Full acceptance
still requires live AI Actions, setup and delivery in
both consumers, human approvals, repair and release. Do not mark the goal
complete before then.

The owner clarified that all project artifacts must be in English; only our
conversation may be Vietnamese. README, reference docs and the GitHub goal issue
were translated, with the policy recorded in AGENTS.md. A later local CLI rerun
hit the existing account usage limit before its reviewer returned a verdict;
that rerun is incomplete. No provider was switched or spending limit increased.

The owner subsequently authorized creating the two private acceptance repos.
Created `phuongnse/nexkit-validation-new` as an empty repository and
`phuongnse/nexkit-validation-existing` with a Python HTTP service baseline at
`f22abc7`, before installing NexKit. Six unit tests and five HTTP tests passed
locally; HTTP checks launch the real service process. NexKit survey distinguishes
the empty repository from the existing application and reads its instructions
and documented commands. All project prose is English.

Both consumers report admin access. Their private rulesets API initially returned HTTP 403:
GitHub requires Pro or public visibility for this feature on the current account.
The owner subsequently authorized making both sample repos and the test release
public. Both repositories are now public, and ruleset listing succeeds. Actual
rules will be applied during consumer setup. No consumer NexKit workflow or
release has run yet.

The owner approved testing release `v0.1.0-test.1` in the
`phuongnse/nexkit-validation-existing` repository. This is administrative scope
authorization; the product must still bind a real human release approval to the
exact source commit, version and notes before publishing.

The owner selected `gpt-6-luna` with reasoning effort `max` for the pipeline.
Official OpenAI documentation confirms support for this model/effort. Added an
optional per-role `reasoning_effort` setting and forwarded it to the pinned
Codex action for clarification, implementation and independent review. Existing
configurations retain CLI defaults when the setting is omitted. Doctor exposes
the configured value, and changes invalidate candidate identity through the
existing config hash. Six additional regression tests pass, for 66 total; Ruff
also passes. API account access and live CI model behavior await credentials and
accepted usage limits. No model call was made for this configuration change.
The independent reviewer found no concrete defect in this diff, confirmed
66 passing tests and Ruff, and reproduced rejection of effort drift in all
three relevant guards. Live runner/model verification remains outstanding.

The owner clarified that they have ChatGPT Pro 5x and want the pipeline to use
their subscription rather than separately billed API usage. Read official
OpenAI guidance on headless login, managed-auth CI refresh and access tokens.
Codex supports ChatGPT browser/device login; the advanced managed-auth CI guide
is limited to trusted private automation and excludes public repositories.
Business/Enterprise access tokens are not a documented personal-Pro alternative.
The current public-consumer/API-key implementation has not been converted, and
no credentials were transferred or model calls made during this investigation.
An eligible deployment decision is needed before implementing subscription CI.

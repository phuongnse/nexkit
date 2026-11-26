# Decisions

A decision records a choice that NexKit made with real alternatives, which is costly to undo, and
the reasons for it. Each decision has its own file in the `decisions/` folder, and the table lists them
all. [Writing the docs](writing-docs.md#decisions) says how a decision is written and
when a change needs one.

Each status is **Agreed**, which the owner has accepted, or **Proposed**, a working default that
can change.

| Decision | Title | Status |
| --- | --- | --- |
| [D1](decisions/d1-state-lives-only-on-github.md) | State lives only on GitHub | Agreed |
| [D2](decisions/d2-nexkit-writes-only-as-the-app.md) | NexKit writes only as the App | Agreed |
| [D3](decisions/d3-agent-jobs-hold-no-write-token.md) | Agent jobs hold no write token | Agreed |
| [D4](decisions/d4-outside-text-is-untrusted.md) | Outside text is untrusted | Agreed |
| [D5](decisions/d5-agents-cannot-change-the-pipeline.md) | Agents cannot change the pipeline | Agreed |
| [D6](decisions/d6-agent-errors-never-retry.md) | Agent errors never retry | Agreed |
| [D7](decisions/d7-one-tested-runtime-claude-code.md) | One tested runtime, Claude Code | Agreed |
| [D8](decisions/d8-standard-library-only.md) | Standard library only | Agreed |
| [D9](decisions/d9-pins-live-in-one-place.md) | Pins live in one place | Agreed |
| [D10](decisions/d10-an-engine-decides-hosts-carry-out.md) | An engine decides, hosts carry out | Agreed |
| [D11](decisions/d11-nexkit-works-its-own-issues-on-a-release.md) | NexKit works its own issues on a release | Agreed |

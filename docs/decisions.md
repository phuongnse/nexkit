# Decisions

This page is the index of NexKit's decisions. Each decision has its own file in the `decisions/`
folder, and the table lists them all with their status.
[Writing the docs](writing-docs.md#decisions) says what a decision is, what each status means, how a
superseded decision is listed and when a change needs one.

| Decision | Title | Status |
| --- | --- | --- |
| [D1](decisions/d1-state-lives-only-on-github.md) | State lives only on GitHub | Agreed |
| D2 | NexKit writes only as the App | Superseded by [D13](decisions/d13-the-pipeline-writes-as-the-app-and-maintenance-jobs-with-the-actions-token.md), [last text](https://github.com/phuongnse/nexkit/blob/98397c7f45cc0e5cc09efb2c76673005074e0c97/docs/decisions/d2-nexkit-writes-only-as-the-app.md) |
| [D3](decisions/d3-agent-jobs-hold-no-write-token.md) | Agent jobs hold no write token | Agreed |
| [D4](decisions/d4-outside-text-is-untrusted.md) | Outside text is untrusted | Agreed |
| [D5](decisions/d5-agents-cannot-change-the-pipeline.md) | Agents cannot change the pipeline | Agreed |
| [D6](decisions/d6-agent-errors-never-retry.md) | Agent errors never retry | Agreed |
| [D7](decisions/d7-one-tested-runtime-claude-code.md) | One tested runtime, Claude Code | Agreed |
| [D8](decisions/d8-standard-library-only.md) | Standard library only | Agreed |
| D9 | Pins live in one place | Superseded by [D14](decisions/d14-every-pin-has-one-source.md), [last text](https://github.com/phuongnse/nexkit/blob/65f09a6257f73e9982a687224abb4472bd4c5bbb/docs/decisions/d9-pins-live-in-one-place.md) |
| [D10](decisions/d10-an-engine-decides-hosts-carry-out.md) | An engine decides, hosts carry out | Agreed |
| [D11](decisions/d11-nexkit-works-its-own-issues-on-a-release.md) | NexKit works its own issues on a release | Agreed |
| [D12](decisions/d12-a-host-runs-the-engine-as-a-program-or-over-http.md) | A host runs the engine as a program or over HTTP | Agreed |
| [D13](decisions/d13-the-pipeline-writes-as-the-app-and-maintenance-jobs-with-the-actions-token.md) | The pipeline writes as the App, and maintenance jobs with the Actions token | Agreed |
| [D14](decisions/d14-every-pin-has-one-source.md) | Every pin has one source | Proposed |

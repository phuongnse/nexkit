# Delivery

This page says how the work on NexKit is done: which issue a session takes, how pull requests are
written and merged, which labels and milestone each carries, how NexKit works this repository's own
issues, and how a release is made. It is for whoever works on NexKit, a person or an agent.
Issues, pull requests and their links hold the state of this work, never a local note.

## Issues and pull requests

An issue holds one change, and its pull request carries it out. These rules keep each link between
them true and each merge easy to review.

- A pull request closes at most one issue, with one closing keyword. One that does only part of
  its issue says "Part of #N", lists what is left under *Left for #N*, and writes "#N stays
  open". A closing keyword closes its issue even inside a negation, so never write "does not
  close #N".
- A pull request that builds on one not merged yet branches from it, targets its branch, and
  says near the top "Stacked on #N: review and merge that first." Pull requests are
  squash-merged. After a squash merge, the next pull request merges `main` into its branch,
  without a rebase, so that its reviewed commits stay, and then targets `main`.
- A session's next work is the open `hand-made` issues, in a milestone or in none, that no open
  issue blocks (GitHub's blocked-by), where a blocking issue with an open pull request counts as done and the
  work stacks on that pull request. Of those, take the lowest number, unless the owner names
  another.
- A question for the owner is a comment on the issue it concerns, and work goes on with the issues
  that do not depend on it.
- Code, docs, commits, issues and pull requests are in English. NexKit's docs describe NexKit for
  any repository and name no product that uses it, except the README's *Built with NexKit* list
  and the changelog, which may cite a case. Issues, pull requests and code comments may name one
  as context.
- A change that moves a decision of NexKit 6 into the engine proves it and writes its pull request
  as [Moving decisions into the engine](testing.md#moving-decisions-into-the-engine) says.

## Labels and milestones

Milestones group the work by feature and labels say what kind of change it is, so the
[roadmap](roadmap.md) and a search on GitHub find every piece of a feature, and a milestone holds
only the work that its feature needs.

- **An issue's milestone follows one test, taken in these steps.**
  1. **The milestone whose roadmap row names what the issue builds,** in its *What it delivers*:
     its features, and the fixes and follow-ups of that work while the milestone is open. A
     `process` or `documentation` issue that a row names is in that row's milestone too.
  2. **Otherwise no milestone, and the label that names its kind:**

     | Label | Kind |
     | --- | --- |
     | `nexkit:ci`, `nexkit:flaky`, `nexkit:dependabot` | NexKit's issues for a failure of CI on `main`, a flaky test, a Dependabot advisory |
     | `bug` | A fix of what a released NexKit already has |
     | `refactor` | One concern moved to one shared mechanism, which keeps its behaviour |
     | `process` | A change to how NexKit is built that no row names |
     | `documentation` | A change to the docs that no row names |
     | `docs-audit` | An issue that the [docs audit](writing-docs.md#how-the-docs-stay-correct) opens |

  3. **A part of a split and a follow-up take the same test.** A part builds part of its parent,
     so it keeps the parent's milestone.
- Every issue and pull request carries at least one label of its kind (`enhancement`, `bug`,
  `refactor`, `process` or `documentation`) and every other label whose description on GitHub fits
  it, such as `security`.
- A release pull request carries `release`, and one that updates a pin `dependencies`.
- A pull request that closes an issue carries that issue's labels, but for the holds `on-hold` and
  `hand-made`, and its milestone when it has one: NexKit sets them on the pull requests it opens,
  and a session sets them on its own. A pull request that closes no issue takes the same test.

## NexKit on this repository

NexKit works this repository's issues itself, so a session and NexKit must never work the same
issue, and NexKit must run a release that has proven itself elsewhere.

- NexKit works this repository's issues itself (`.nexkit/config.json`), pinned to a release, never
  `main`. Autopilot takes every open issue but those labelled `on-hold` or `hand-made`, and merges
  its own pull requests once their checks and review pass.
- A session outside NexKit takes only `hand-made` issues, those that change `.github/` or
  `.nexkit/` among them, so the two never work the same issue. It never merges.
- No agent pushes a tag or releases: the owner does, and moves the pin to a release only once it
  runs well elsewhere.

The reasons are in [D11](decisions/d11-nexkit-works-its-own-issues-on-a-release.md).

## Releases

A release is a pull request of its own, so a feature's pull request never conflicts with another's
over the version or the changelog.

- Feature pull requests change neither the version nor `CHANGELOG.md`.
- A release pull request sets the version in `nexkit/__init__.py`, `pyproject.toml`,
  `.claude-plugin/plugin.json`, the protocol example
  `nexkit/protocol/v1/examples/calls/describe.answer.json` and the README's `pipx install` line,
  and adds a `## X.Y.Z` changelog entry, ending with `### Upgrade` steps when the configuration,
  caller workflow or permissions must change. Its description holds the output of
  `python3 scripts/release_notes.py X.Y.Z`.
- The release pull request of 7.0, and each later one that raises `protocol.MINOR`, also runs
  `python3 scripts/protocol_freeze.py` and commits `tests/golden/protocol/`, the copy of the
  protocol's schemas that a test compares with the tree ([versions](engine.md#versions)).
- After the merge, the owner releases `vX.Y.Z` (titled `NexKit X.Y.Z`) on the merge commit with
  those notes.
- Publishing it runs `release.yml`, which builds `Dockerfile` and publishes the engine's image
  `ghcr.io/phuongnse/nexkit-engine:X.Y.Z`. It refuses a tag that is not `v` and the version in
  `nexkit/__init__.py`, and a commit that is not on `main`. Every pull request builds and checks
  the image the same way, without publishing it. Its `publish` job pushes the image with
  `github.token` and `packages: write`, since a GitHub App gets no access to packages. Like
  NexKit's other maintenance jobs, it runs no repository code, as
  [D13](decisions/d13-the-pipeline-writes-as-the-app-and-maintenance-jobs-with-the-actions-token.md) says.

# Changelog

## 1.2.0

- Every run gets its own NexKit comment, posted when it starts, right after the command
  or review that started it: one per `plan` or `go` run on an issue, one per round on a
  pull request. `report` finds the comment by a hidden marker that holds the run URL and
  edits it with the outcome, or posts a new one if it was deleted. Earlier runs' comments
  are no longer edited.
- A round's comment shows its commit, checks, AI review, cost and the next step, which
  was a separate comment before. The pull request state stays in one state comment,
  edited in place, that no longer has a rounds table.
- Status comments from 1.1.0 are left as they are. NexKit reads the state of a 1.1.0 pull
  request and continues it in a new state comment.

## 1.1.0

- Every accepted command gets 👀, on pull requests too: `route` now has
  `pull-requests: write`. A failed reaction is logged instead of ignored.
- Runs are visible while they run. A command on an issue gets one NexKit status comment,
  edited in place, with a link to the run and then its outcome. A pull request round
  appears in the status comment as running, with the run link, and `nexkit/checks` and
  `nexkit/review` turn `pending` on the current commit.
- The command comment gets 🚀 when the run finishes its work and 😕 when it stops on an
  error.
- A review after an earlier NexKit review gets the previous round: the earlier findings,
  the fix rounds since (summary and note) and the diff since the reviewed commit. It
  reports a `resolution` for each earlier finding (new `previous_findings` field in the
  review result), accepts justified rejections, and treats changes asked for in a note as
  in scope.
- The review prompt names the base branch of its diff (it showed `$base`).

## 1.0.0

First release.

- Issue commands `/nexkit plan` and `/nexkit go`; pull request commands `/nexkit fix` and
  `/nexkit review`; a *Request changes* review starts a fix round.
- One reusable workflow with separated jobs: Claude runs without GitHub write access,
  publishing never runs repository code, and checks run without secrets.
- Independent read-only Claude review against the plan's acceptance criteria and the check
  results, posted as a pull request review and `nexkit/checks` / `nexkit/review` statuses.
- Bounded automatic fix rounds for failing checks and blocking findings; agent errors and
  blocked results stop and ask for a person.
- State kept in GitHub comments; no state branch or external storage.
- `nexkit init` and `nexkit doctor`; Claude Code plugin with `setup`, `request` and
  `status` skills.
- Runs on GitHub-hosted `ubuntu-24.04` with Claude Code 2.1.285 and Python 3.12, both
  installed by the pipeline; the CLI needs Python 3.12 or newer. A weekly workflow proposes
  new stable Claude Code versions, tested with a live smoke test before release.
- Works with a Claude subscription token or an Anthropic API key.

# Changelog

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

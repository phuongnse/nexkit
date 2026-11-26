# Security

This page is NexKit's threat model. It says what NexKit trusts, lists each input it does not trust,
and gives for each one what an attacker could reach, the rule that contains it and the tests that
check the rule. Read it when you change a job, a token, a secret, or anything that reads text from
GitHub, and when you decide how far to trust what an agent or a stranger wrote. What a user
configures to meet these rules is in [How it works](how-it-works.md#security-notes) and in the
[configuration reference](configuration.md).

## What NexKit trusts

NexKit trusts three things, and nothing else:

- **People with write, maintain or admin access.** Their commands start runs, and their discussion
  and review feedback reach the agents. Everyone else is a stranger.
- **NexKit's own comments that no one else changed.** The App wrote them, and GitHub's edit history
  shows that no other account ever edited them.
- **The default branch's `.nexkit/config.json` and workflows.** They hold the checks, the setup
  commands and the workflows that autopilot trusts, and only a person's merged change alters them.

## Untrusted inputs

Each row is an input NexKit does not trust. The rule's name links its section, and the tests that
check the rule are named in the last cell.

| Input | What it could reach | Rule | Tests |
| --- | --- | --- | --- |
| Agent output | Pull requests, comments, logs, and NexKit's own pipeline and configuration | [Agent output](#agent-output) | `tests/test_publish.py`: `test_protected_paths_are_rejected`, `test_agent_edit_to_a_protected_path_is_still_rejected`; `tests/test_runlog.py`: `test_output_cannot_issue_workflow_commands`; `tests/test_redact.py`: `test_token_shapes`; `tests/test_agent.py`: `test_prompt_keeps_untrusted_text_literal` |
| A repository's code and CI | The write tokens and the App's key | [Repository code and CI](#repository-code-and-ci) | `tests/test_cli.py`: `test_jobs_that_run_agents_or_repository_code_cannot_write`, `test_jobs_with_write_tokens_never_receive_model_credentials`, `test_verify_holds_no_secrets` |
| Comments and markers | NexKit's record of its own state, and what autopilot does next | [Comments and markers](#comments-and-markers) | `tests/test_item_state.py`: `test_only_a_marker_of_the_app_where_it_ends_the_comment_counts`; `tests/test_publish.py`: `test_agent_text_around_the_profile_cannot_pass_for_its_markers`; `tests/test_github.py`: `test_an_edited_comment_of_a_bot_has_everyone_who_changed_it` |
| The Actions bot | Markers and issues that autopilot would take as NexKit's | [The Actions bot](#the-actions-bot) | `tests/test_cli.py`: `test_route_counts_only_the_comments_of_the_app_as_nexkits`; `tests/test_autopilot.py`: `test_only_the_issues_of_the_app_are_nexkits`, `test_a_marker_counts_only_from_nexkits_app` |
| People without write access | Runs, agent prompts and autopilot's choice of issues | [People without write access](#people-without-write-access) | `tests/test_route.py`: `test_someone_without_write_access_runs_nothing_and_is_told_why`, `test_a_pull_request_from_a_fork_runs_nothing_and_says_why`; `tests/test_context.py`: `test_only_collaborators_count`; `tests/test_autopilot.py`: `test_an_edit_by_someone_without_write_access_ends_the_trust`, `test_a_marker_that_names_a_run_of_another_workflow_is_left_alone` |
| An agent's reach into secrets | The credentials of the agents a job may run | [Secrets](#secrets) | `tests/test_runtimes.py`: `test_each_job_gets_only_the_secrets_its_stages_need`, `test_secret_fields_hold_an_allowlisted_name`; `tests/test_cli.py`: `test_only_the_steps_that_run_an_agent_receive_agent_secrets` |

## Agent output

An agent's plan, patch, findings and tool output are text from a model that read untrusted issues,
comments and code, so any of it may have been steered. NexKit publishes it only after it has
checked it, and never lets it decide what NexKit does next.

- **Validate before publishing.** NexKit rejects a change that touches `protected_paths`, which
  default to `.github/` and `.nexkit/`, so a change cannot alter its own pipeline or configuration.
  The App may push workflow changes, but only those a merge brings from the base branch, since
  `publish` rejects any such edit by an agent.
- **Redact what a session prints or stores.** Everything passes through `nexkit/redact.py` first,
  as [What is redacted](how-it-works.md#what-is-redacted) says. Redaction is best-effort, so the
  rules below that keep secrets out of the agent's reach are the real protection.
- **Print tool output with workflow commands stopped,** so a line of output cannot act as a command
  of the runner.
- **Keep untrusted text literal in a prompt,** marked as untrusted, so it reads as a description of
  the work and not as an instruction.
- **Never read agent text as a marker.** [Comments and markers](#comments-and-markers) says how.

## Repository code and CI

A job that runs an agent, a `setup` command or a check runs code that an attacker may have written,
in a pull request or through an issue the agent followed. That code can do anything its runner and
its token allow, so what it holds is what decides the damage.

- **A job that runs an agent or repository code has a read-only token and no App key.** It holds
  no token that can write, so the worst it can do is read.
- **A job with a write token never runs repository code and never gets an agent's credential.**
  Each writing job mints its own App token in a step of its own, and `route`, `publish`, `report`,
  `maintain` and `finish` are the jobs that do. See
  [Tokens and secrets](architecture.md#tokens-and-secrets) for each job's tokens.
- **Setup and check commands get no secrets,** and `verify` holds none, so a check that runs an
  agent's code cannot read a credential.
- **The checks and `setup` come from the default branch's configuration.** A change to them counts
  from its merge on, so a pull request cannot widen its own checks.

NexKit's pushes start the repository's own `pull_request` and `push` workflows on code an agent
wrote, and that CI may use secrets and write as the Actions bot. Giving those workflows least
privilege is the repository's to do, as
[The GitHub App](configuration.md#the-github-app) says under *Your CI runs what agents wrote*.

## Comments and markers

NexKit keeps its state in hidden HTML comments, its markers, in the comments and issues it writes,
and those quote text it does not trust: agent output, such as a plan, a fix round's cause or a
finding that no change is needed, and the output, test names and annotations of CI runs. If text
could pass for a marker, an attacker could write NexKit's record. So a marker counts only when all
of these hold:

- **The App wrote the comment.** A comment of any other account, a person's or another App's,
  proves nothing, and neither does [the Actions bot](#the-actions-bot).
- **No one else ever changed it.** GitHub lets anyone with write access, and any token, edit a
  comment of the App, which stays the App's by author. So NexKit counts a marker only in a comment
  that, by its edit history, no one else ever changed, even if the App edited it again afterwards.
  A changed comment's markers count for nothing, and autopilot leaves the issue and its pull
  request to a person (see [A comment of NexKit was changed by someone
  else](troubleshooting.md#a-comment-of-nexkit-was-changed-by-someone-else)). NexKit reads the edit
  history only of the comments of bots that were edited, with the read-only token every job holds.
- **It stands where NexKit writes it.** A record, such as the state of a pull request, a run's or
  autopilot's record, a plan's record and the markers that end a CI report, counts only where it
  ends the comment or issue. A marker that names what an issue or comment is, and the people a
  mention names, counts only at the start of its text. A plan's profile record and an advisory
  part's marker count only on the line after the first marker.
- **Agent text cannot open a marker.** Where agent text stands on both sides of a marker, as around
  a plan's profile lines, follows a part's marker, or ends a comment of NexKit's, as the finding
  autopilot closes an issue with, NexKit writes `<!-- nexkit:` in it as `‹!-- nexkit:`. That
  covers all of a plan's agent text, so the plan text the agents read and the parts filed from a
  split show it so too.

## The Actions bot

A pull request's CI can write as the Actions bot, also on code an agent wrote, so what that bot
wrote proves nothing. NexKit never counts a marker in the Actions bot's comment or an issue it
opened as NexKit's, which includes the plans and rounds of earlier releases, as
[Comments of earlier releases](configuration.md#the-github-app) says. An issue opened by a workflow
of the repository is trusted only through [the rule for such workflows](#people-without-write-access),
never because the bot opened it.

## People without write access

Anyone can open an issue, comment, or fork a repository, so what they write can reach an agent
only as marked, untrusted text, and what they do can never start work.

- **Only people with write, maintain or admin access start runs.** A command from anyone else runs
  nothing, and NexKit says why. Prompts include issue text from anyone, but mark it untrusted.
  Discussion and review feedback come only from people with write access.
- **Fork pull requests are never NexKit pull requests.** Commands on them are refused. Only a
  writer's `/nexkit review` runs anything on a person's pull request, and only from a branch of this
  repository, which only writers can push to.
- **Autopilot takes only issues that a trusted author opened.** Autopilot starts `/nexkit go`
  without a person's approval of the plan, so an outsider must not start an implement session by
  opening or editing an issue. It takes only issues opened by people with write access. A label
  grants no trust.
- **An issue that NexKit opens is trusted by a marker at the start of its text.** That covers the
  parts of a split and the issues for CI failures, flaky runs and Dependabot advisories. Such an
  issue opened by the App is NexKit's, unless a workflow of the repository that holds the App's key
  writes untrusted text there.
- **An issue from a workflow counts only on GitHub's record.** An issue from a workflow in
  `autopilot.trusted_workflows` is trusted only when GitHub's record of the run its marker names
  confirms it: the listed file, the default branch, an event a writer or a schedule controls, and a
  run that was running when the issue was opened (see [Issues from a
  workflow](configuration.md#issues-from-a-workflow)).
- **A writer vouches for any other issue.** [`/nexkit
  trust`](configuration.md#vouching-for-an-issue) is a comment that autopilot reads itself, so
  nothing an agent wrote into a comment of NexKit's can stand in for it.
- **An edit by anyone else ends the trust.** The issue's author may still edit such an issue, so an
  edit by anyone else after it ends the trust. Each round NexKit starts looks for one right after
  it read the text, before any agent or setup, and so does a merge.

## Secrets

A job's runner holds only the secrets its stages need, so that code running there can read no more.

- **NexKit reads only the secrets the configuration names,** by name, and the configuration holds
  never a value. A value that is not a secret name is rejected.
- **A job gets only the secrets of the agents its stages use,** and each agent's CLI only its own.
  Setup and check commands get none.
- **A job that runs an agent never indexes `secrets` itself,** since that puts every secret on its
  runner.
- **Agent secret names may not start with `GITHUB_` or `NEXKIT_`,** which belong to GitHub and
  NexKit.
- **Settings that identify an account or resource are secret fields,** and live in a repository
  secret like a key.

[Secrets](configuration.md#secrets) and [Settings and
secrets](configuration.md#settings-and-secrets) say how to set them, and
[Tokens and secrets](architecture.md#tokens-and-secrets) says which job holds which.

## What the rules do not stop

Some risks remain, and each is for a person to weigh.

- An implement session can run any command on the runner, so it can read its own job's agent
  secrets: approve plans only for issues you trust.
- Logs and artifacts can show anything the agent saw, and redaction is best-effort, so on a public
  repository they are public.
- An issue's text can steer triage towards a more expensive profile, for example through prompt
  injection.
- The repository's CI runs what agents wrote, with the secrets and permissions that CI has.

[Security notes](how-it-works.md#security-notes) says what to decide about each, and
[The GitHub App](configuration.md#the-github-app) says how to limit CI.

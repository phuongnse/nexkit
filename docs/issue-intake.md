# Start from an existing issue

[Documentation](README.md) / Existing issue intake

For a complete request-to-merge walkthrough, see [daily use](daily-use.md).
This page covers the start command, its setup and retry behavior.

Create a GitHub issue with your requirement, then post a standalone comment:

```text
/nexkit start maintenance
```

Replace `maintenance` with a pipeline selected during this project's setup.
The commenter needs current write, maintain or admin access. NexKit keeps the
same issue number, URL, title and comments. It places the existing body in the
collapsed Original request section and adds a visible Specification section for
clarification. The original wording is preserved without repeating it above
the current specification.

The bot acknowledges intake on that issue. If the pipeline has clarification,
Actions starts it and the bot asks any necessary questions there. Reply with
ordinary comments. When the requirement is ready, use the exact
`/nexkit approve <hash>` command it supplies. Implementation begins after that
approval and follows the configured stage decisions and execution limits.

If you already have a local consumer checkout, the equivalent command is:

```sh
nexkit --pipeline maintenance start 42
```

This command queues intake for issue #42. It does not create a second issue.

## Configure during setup

For a composed project, choose which pipelines accept existing issues and
connect their `entrypoints.intake` to a consumer-owned workflow. Use
[the intake connection example](examples/issue-intake.yml) as wiring guidance:

- Subscribe to `issue_comment` with `types: [created]`.
- Retain `workflow_dispatch` with `operation`, `payload` and `pipeline` inputs
  for the CLI. Its operations are `request`, `start`, `spec` and `release`.
- Call the pinned NexKit `intake.yml` with read-only contents, issue-write and
  Actions-write permissions. This job needs no model credential or agent runner.
- Record the workflow's exact bytes in the accepted `files` manifest. A shared
  intake workflow may be the accepted entrypoint of several pipelines. With no
  fixed reusable `pipeline` input, the controller reads the comment's selection
  and checks it against the configuration and executing caller.
- A workflow bound to one literal pipeline accepts only that name. Omit the
  comment trigger to retain CLI-only intake. Workflow names, pipeline names and
  the downstream jobs remain consumer choices.

Include the comment trigger and refreshed accepted hashes in the setup bundle.
Updating the local plugin alone does not update GitHub workflows. Each project
keeps its pinned NexKit version and installed workflows until an accepted update.

A pipeline without a clarification entrypoint receives the same issue, then
uses a manually prepared specification. Its notice gives the `nexkit spec` and
`nexkit approval` commands. The same guidance is posted for a new request in a
pipeline without clarification. `nexkit spec` queues publication through intake;
Actions updates the specification and posts the approval instructions as
`github-actions[bot]`. The local CLI does not edit the issue or post that reply
using the caller's account. Wait for the bot notice before approving. Intake
and manual publication invoke no model.

Manual publication records the issue version read at submission. A queued update
cannot overwrite a changed requirement; read it and submit again. Retrying a
completed update preserves the body, approval and consumed usage, and can recover
a missing bot notice. A matching comment from a user or another bot does not
replace the Actions notice.

## Retries and boundaries

Intake uses the existing serialized GitHub queue. A repeated start on the same
pipeline preserves the specification, approvals, state and consumed usage. It
can recover a missing first clarification dispatch; once work has started, use
the existing discussion or `nexkit resume` to continue it. A lost issue-update
response can be retried without adding another Original request section.

Commands cannot move an issue to another pipeline or restart completed work.
Closed issues, PRs, release candidates, cancelled work, and issues already under
another workflow are rejected. Edited commands must be posted again as new
comments. The controller re-fetches the command and checks permission before
changing the issue and before dispatching clarification. Unrelated comments go
through their existing requirement or delivery integrations.

The current issue body and edit history are re-read before the update. As with
requirement publication, GitHub issue PATCH is not an atomic conditional write;
use comments while intake is preparing the issue. Subsequent requirement changes
invalidate the content-bound approval. See [recovery details](operations.md).

## Verify your setup

The automated intake tests cover dispatch, pipeline selection, in-place adoption,
authority, changed inputs and permissions, interrupted writes and duplicate
requests. GitHub responses, model output and approvals in these tests are
simulated. See the [verification guide](acceptance.md) for required live checks.

After installing the accepted workflow in your repository:

1. Create an ordinary issue describing a small change.
2. Comment `/nexkit start PIPELINE` using your configured pipeline name.
3. Verify that Actions keeps the same issue and posts an acknowledgement as
   `github-actions[bot]`.
4. Answer any clarification questions and check that the bot publishes the
   specification with its exact approval command.
5. Approve the specification when ready, then follow the delivery and checks.

Check the configured model, runner, usage counters and issue status in the
actual run. A successful intake does not establish successful downstream
delivery or release. Follow the [verification procedure](live-acceptance.md)
for those paths.

---
name: nexkit-request
description: Record or receive a NexKit feature or bug requirement in a GitHub issue, clarify it and request the authorized collaborator's specification approval.
---

# NexKit request

When the user supplies an existing issue, keep it and use
`nexkit --pipeline <pipeline> start <issue>` to queue intake. An authorized collaborator can instead comment
`/nexkit start <pipeline>` when its intake workflow subscribes to comments.
Read `docs/issue-intake.md` for setup and retry behavior. Inspect
the current binding before starting an already tracked issue; use its existing
discussion or resume flow. Starting intake does not grant requirement approval.

For a new request, the first work-recording action is `nexkit request --title <title> --body-file
<request-file>`. It queues serialized GitHub intake and returns a key plus
`nexkit intake-status --key <key>` to find the issue. Reuse that issue on retries;
use a stable `--key` if the caller already has a request identifier. Select the
configured pipeline explicitly with `--pipeline`. Do not
implement before approval. Actions continues clarification even if the Codex session closes.

## In the Actions requirement job

When the supplied context has `stage: requirement`, intake already created the
issue. Read this skill, the actual repository, relevant accepted knowledge,
the original request, current spec, previous reply and authorized collaborator answers. Do not run
intake, post comments, edit files or call any GitHub write command. Return the
schema's `specification`, a direct English `reply` to the latest comment,
concrete `questions` and `ready_for_approval`, with tool evidence in `commands`.
Address the reader directly in issue replies. Use phrases such as "Ready for
approval" and "Please review"; do not refer to the reader as "the human".
Answer explanations and tradeoffs in `reply`; keep unchanged specification text
exactly as supplied when the user has not changed the requirement. The controller
posts the reply, questions and exact approval command on the issue. Use the configured project
decisions; ask only missing product decisions. Never invent answers. A completed
clarification may still have questions: use `status: done`, readiness false and
nonempty questions. Readiness is true only when questions are empty. End the run;
the next authorized collaborator answer triggers another time-bounded Actions session.
Conversation count limits are configured separately when selected during setup.

When the request spans several issues or only part of an umbrella issue, make
the intended completion scope explicit before approval. Use the exact
`## Issues to close` section documented in `docs/issue-completion.md`: one
`- #123` line per fully resolved issue, or `None.` to keep them open. Without
this section, delivery completes its own requirement issue. Keep partial or
related work in ordinary references. Do not add closure targets merely because
the user mentioned them; all listed work must be covered by the approved scope.

## In an interactive Codex session

Read only relevant project decisions, knowledge and source. Clarify the goal,
scope, expected behavior and observable acceptance criteria in proportion to
the work. Identify contradictions and ask for missing product decisions on the
GitHub issue so the discussion survives the local session. Actions normally
performs this clarification. A small bug needs
its trigger, actual behavior, expected behavior and a regression criterion.

Keep the authoritative specification in that issue. Use `nexkit spec <issue>
--body-file <spec-file>` to queue publication through intake. Actions preserves
the original request, updates the specification and posts the approval command
as `github-actions[bot]`. Wait for that notice before requesting approval; the
CLI's queued response is not proof of publication. If the issue changed while
queued, read the new version and submit again. Keep workflow replies in Actions
so they use the bot identity. Put progress in comments, never in the requirement body.

Ask an authorized collaborator to review the current issue and post the bot's
`/nexkit approve <hash>` command. Never post it yourself, impersonate the approver,
replace it with a label, or consider a comment from an unauthorized actor valid.
GitHub Actions checks authority and starts delivery from the approval event.
The local Codex session need not stay open. After requirement approval, delivery follows
the project's accepted policy. Additional task, plan, test or PR decisions apply
only when configured during setup; show the actual gate and exact requested
approval action. See `docs/stage-approvals.md` for optional approval behavior.

If delivery needs different product scope, update the issue and obtain a new
requirement approval. Do not edit requirements merely to fit completed code.

---
name: nexkit-request
description: Record and clarify a new NexKit feature or bug requirement in a GitHub issue and request the authorized human's specification approval.
---

# NexKit request

The first work-recording action is `nexkit request --title <title> --body-file
<request-file>`. It queues serialized GitHub intake and returns a key plus
`nexkit intake-status --key <key>` to find the issue. Reuse that issue on retries;
use a stable `--key` if the caller already has a request identifier. Do not
implement before approval. Actions continues clarification even if this host closes.

## In the Actions requirement job

When the supplied context has `stage: requirement`, intake already created the
issue. Read this skill, the actual repository, relevant accepted knowledge,
the original request, current spec, previous reply and authorized human answers. Do not run
intake, post comments, edit files or call any GitHub write command. Return the
schema's `specification`, a direct English `reply` to the latest comment,
concrete `questions` and `ready_for_approval`, with tool evidence in `commands`.
Answer explanations and tradeoffs in `reply`; keep unchanged specification text
exactly as supplied when the user has not changed the requirement. The controller
posts the reply, questions and exact approval command on the issue. Use the configured project
decisions; ask only missing product decisions. Never invent answers. A completed
clarification may still have questions: use `status: done`, readiness false and
nonempty questions. Readiness is true only when questions are empty. End the run;
the next authorized human answer triggers another time-bounded Actions session.
Conversation count limits are configured separately when selected during setup.

## In an interactive host

Read only relevant project decisions, knowledge and source. Clarify the goal,
scope, expected behavior and observable acceptance criteria in proportion to
the work. Identify contradictions and ask for missing product decisions on the
GitHub issue so the discussion survives the local session. Actions normally
performs this clarification. A small bug needs
its trigger, actual behavior, expected behavior and a regression criterion.

Keep the authoritative specification in that issue. Use `nexkit spec <issue>
--body-file <spec-file>`; it preserves the original request and returns the exact
approval command. Put progress in comments, never in the requirement body.

Ask an authorized human to review the current issue and post the returned
`/nexkit approve <hash>` command. Never post it yourself, impersonate the human,
replace it with a label, or consider a comment from an unauthorized actor valid.
GitHub Actions checks authority and starts delivery from the approval event.
The local host need not stay open. No task, plan, test or PR approval follows.

If delivery needs different product scope, update the issue and obtain a new
requirement approval. Do not edit requirements merely to fit completed code.

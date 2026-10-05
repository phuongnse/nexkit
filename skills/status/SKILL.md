---
name: status
description: Show the state of NexKit work in the current GitHub repository - open NexKit pull requests, their checks and AI review, plans awaiting approval and recent failures - and suggest the next action. Use when the user asks what NexKit is doing or why a run stopped.
---

# Show NexKit status

Use `gh` against the current repository and summarise, without changing anything.

1. **Open NexKit pull requests:**
   `gh pr list --search "head:nexkit/issue-" --json number,title,headRefName,url`.
   For each, read the NexKit state comment (`gh pr view <n> --comments`; it starts with
   `### NexKit` and has a rounds table whose Run column is ⏳ running, ✅ finished or ❌
   stopped, with a link to the run) and the commit statuses `nexkit/checks` and
   `nexkit/review` (`gh pr checks <n>`). Report round count, rounds still running, check
   and review results, and whether automatic fix rounds remain.

2. **Recent runs:** `gh run list --workflow nexkit.yml --limit 10`. For failed runs, read
   the failing job with `gh run view <id> --log-failed` and explain the cause.

3. **Issues waiting for a decision:** issues whose latest NexKit comment is a plan
   (`<!-- nexkit:plan -->`) with no `/nexkit go` after it. An issue's NexKit status comment
   (`### NexKit`, one line per `plan` or `go` run) shows runs in progress and why a run
   stopped.

4. **Next actions.** For each item, say what a collaborator can do:
   `/nexkit go` to implement a plan, `/nexkit fix <instructions>` for another round,
   `/nexkit review` after pushing a manual change, or merge when checks and review pass.

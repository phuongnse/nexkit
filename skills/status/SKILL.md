---
name: status
description: Show the state of NexKit work in the current GitHub repository - open NexKit pull requests, their checks and AI review, plans awaiting approval and recent failures - and suggest the next action. Use when the user asks what NexKit is doing or why a run stopped.
---

# Show NexKit status

Use `gh` against the current repository and summarise, without changing anything.

1. **Open NexKit pull requests:**
   `gh pr list --search "head:nexkit/issue-" --json number,title,headRefName,url`.
   For each, read the NexKit comments (`gh pr view <n> --comments`): one per round, headed
   `### NexKit round <n>: <trigger>`, showing ⏳ running, ✅ finished, ❌ stopped or ⏸️
   paused at the Claude usage limit (with when it resumes) with a
   link to the run, the commit, checks, AI review, cost and next step; and the state
   comment, headed `### NexKit`, with the automatic fix rounds used. Read the commit
   statuses `nexkit/checks` and `nexkit/review` too (`gh pr checks <n>`). Report the round
   count, rounds still running, check and review results, and whether automatic fix rounds
   remain.

2. **Recent runs:** `gh run list --workflow nexkit.yml --limit 10`. For failed runs, read
   the failing job with `gh run view <id> --log-failed` and explain the cause. For a run
   that stopped in the agent or review job, its log has one group per tool call titled
   `[mm:ss #turn] ▸ Tool …` with the output, and failed tool calls appear as warnings;
   the `transcript.md` in the `nexkit-agent` or `nexkit-review` artifact
   (`gh run download <id> -n nexkit-agent`) has the whole conversation. An *Artifact not
   found* annotation on `report` means that stage ran but stopped before uploading.

3. **Issues waiting for a decision:** issues whose latest NexKit comment is a plan
   (`<!-- nexkit:plan -->`) with no `/nexkit go` after it. Each `plan` or `go` run has its
   own NexKit comment on the issue (`### NexKit /nexkit <command>`) that shows whether it
   is running and why it stopped.

4. **Next actions.** For each item, say what a collaborator can do:
   `/nexkit go` to implement a plan, `/nexkit fix <instructions>` for another round,
   `/nexkit review` after pushing a manual change, or merge when checks and review pass.

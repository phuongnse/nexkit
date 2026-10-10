---
name: status
description: Show the state of NexKit work in the current GitHub repository - issues being planned or implemented, open NexKit pull requests and what each waits for, recent automatic merges and what needs a person - and suggest the next action. Use when the user asks what NexKit is doing, why a run stopped, or to pick up NexKit work in a new session.
---

# Show NexKit status

Change nothing. The NexKit CLI ships with this plugin; run it as
`python3 <base directory of this skill>/../../bin/nexkit` (Python 3.12 or newer). Below,
`nexkit` means that command.

1. **Snapshot.** Run `nexkit status --json` in the repository. It reads GitHub with the
   `gh` login and returns:
   - `issues`: open issues NexKit worked on, each with a `state` (`planning`, `planned`
     with the plan's link and profile, `implementing`, `paused`, `failed`,
     `in_pull_request`, `implemented`) and the link to its last run comment.
   - `pull_requests`: open NexKit pull requests, each with a `state` (`running`, `ready`,
     `blocked`, `failing`, `conflicting`, `merge_refused`, `paused`, `failed`), the round
     and the link to its last round.
   - `recent_merges`: pull requests NexKit merged, newest first by merge time, with the
     state of each `after_merge_workflows` run.
   - `needs_person`: only the items that need a person, with the reason.
   Rely on these states rather than reading comment text: they come from NexKit's
   markers. Do not post a command for an item whose state is `planning`,
   `implementing` or `running`; it would queue behind the running one. A `paused` item
   resumes by itself after the time shown.

2. **Why a run stopped.** For an item that failed, open its link. For the details, use
   `gh run view <id> --log-failed` on the run. In the agent or review job log, each tool
   call is a group titled `[mm:ss #turn] ▸ Tool …`, and failed calls appear as warnings;
   the `transcript.md` in the `nexkit-agent` or `nexkit-review` artifact
   (`gh run download <id> -n nexkit-agent`) has the whole conversation. An *Artifact not
   found* annotation on `report` means that stage ran but stopped before uploading.

3. **Next actions.** Start with `needs_person`. For each item, say what a collaborator
   can do: answer the plan's questions and comment `/nexkit plan`, or `/nexkit go` to
   implement a plan; `/nexkit fix <instructions>` for another round or to answer a
   blocked conflict; `/nexkit review` after pushing a manual change; or merge when the
   pull request is ready.

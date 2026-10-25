---
name: status
description: Show the state of NexKit work in the current GitHub repository - issues being planned or implemented, open NexKit pull requests and what each waits for, recent automatic merges and what needs a person - and suggest the next action. Use when the user asks what NexKit is doing, why a run stopped, or to pick up NexKit work in a new session.
---

# Show NexKit status

Change nothing. The NexKit CLI ships with this plugin; run it as
`python3 <base directory of this skill>/../../bin/nexkit` (Python 3.12 or newer). Below,
`nexkit` means that command.

The plugin can be newer or older than the release the repository runs, and states and
advice match what runs only at that release. So when the `nexkit_ref` input in
`.github/workflows/nexkit.yml` is not the plugin's version, run the CLI of the pinned
release, and read NexKit's docs at that ref:

```sh
repo=$(grep -oE 'nexkit_repository: \S+' .github/workflows/nexkit.yml | cut -d' ' -f2)
ref=$(grep -oE 'nexkit_ref: \S+' .github/workflows/nexkit.yml | cut -d' ' -f2)
kit="${TMPDIR:-/tmp}/nexkit-$ref"
[ -d "$kit" ] || git clone -q --depth 1 --branch "$ref" "https://github.com/$repo" "$kit"
python3 "$kit/bin/nexkit" status --json
gh api "repos/$repo/contents/docs/troubleshooting.md?ref=$ref" -H 'Accept: application/vnd.github.raw'
```

1. **Snapshot.** Run `nexkit status --json` in the repository. It reads GitHub with the
   `gh` login and returns:
   - `issues`: open issues NexKit worked on, each with a `state` (`planning`, `planned`
     with the plan's link and profile, `implementing`, `paused`, `failed`,
     `in_pull_request`, `implemented`) and the link to its last run comment.
   - `pull_requests`: open NexKit pull requests, each with a `state` (`running`, `ready`,
     `blocked`, `failing`, `conflicting`, `merge_refused`, `merge_waiting`, `paused`,
     `failed`, `waiting`), the round and the link to its last round. A `waiting` pull
     request has `waits_for`, the autopilot `bug` issue it waits for, and the `reason`
     (see *Failing checks the default branch has* under
     [Stages](../../docs/how-it-works.md#stages)). Like a `merge_waiting` one, it needs
     nobody unless it is also in `needs_person`.
   - `recent_merges`: pull requests NexKit merged, newest first by merge time, with the
     state of each `merge.after_workflows` run.
   - `needs_person`: only the items that need a person, with the reason.
   - `autopilot`, only with `autopilot.enabled`: the scope, `max_parallel`, and for each
     open issue in scope what the next sweep does with it, a `state` (`start` with its
     `command`, `active`, `waits`) and the `reason`.
   Rely on these states rather than reading comment text: they come from NexKit's
   markers. Do not post a command for an item whose state is `planning`,
   `implementing` or `running`; it would queue behind the running one. A `paused` item
   resumes by itself after the time shown, unless it is also in `needs_person` (see
   [Usage limits](../../docs/how-it-works.md#usage-limits)). A run comment showing ⏭️
   `continued` was carried on by a later run.

2. **Why a run stopped.** For an item that failed, open its link. For the details, use
   `gh run view <id> --log-failed` on the run. In the agent or review job log, each tool
   call is a group titled `[mm:ss #turn] ▸ Tool …`, and failed calls appear as warnings;
   the `transcript.md` in the `nexkit-agent` or `nexkit-review` artifact
   (`gh run download <id> -n nexkit-agent`) has the whole conversation. An *Artifact not
   found* annotation on `report` means that stage ran but stopped before uploading.

3. **Next actions.** Start with `needs_person`. For each item, find its stop in
   Troubleshooting, [When NexKit waits for a
   person](../../docs/troubleshooting.md#when-nexkit-waits-for-a-person), and suggest what
   it says, for example the answers to a plan's questions, `/nexkit fix <instructions>`
   for another round or to answer a blocked conflict, or `/nexkit review` after pushing a
   manual change. For a pull
   request that is ready to merge, suggest the checks in [Reviewing a pull
   request](../../docs/how-it-works.md#reviewing-a-pull-request); for a change to a plan,
   [Changing a plan](../../docs/how-it-works.md#stages). To see a plan, read the newest
   plan comment (`gh issue view <n> --comments`): it names the profile, the models of each
   stage and why triage chose them. With autopilot on, do not post `/nexkit plan` or
   `/nexkit go` on an issue whose autopilot state is `start` or `active`: autopilot starts
   it or already did. Nor suggest one for what autopilot does by itself after a comment
   (see the configuration's [Autopilot](../../docs/configuration.md#autopilot)). To make autopilot look again at once, for example after a
   *blocked by* relationship changed, start a sweep:
   `gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`.

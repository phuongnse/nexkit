---
name: setup
description: Set up the NexKit pipeline in the current GitHub repository - choose check commands, install the config and workflow, add the agents' credentials and verify. Use when the user asks to install, configure or repair NexKit for a repository.
---

# Set up NexKit in this repository

NexKit adds one workflow (`.github/workflows/nexkit.yml`) and one config file
(`.nexkit/config.json`). Collaborators then drive work with issue comments:
`/nexkit plan`, `/nexkit go`, and on NexKit pull requests `/nexkit fix` and `/nexkit review`.

The NexKit CLI ships with this plugin. Run it as
`python3 <base directory of this skill>/../../bin/nexkit` (Python 3.12 or newer; no
packages needed). If the installed Python is older, ask the user to install 3.12 first.
Below, `nexkit` means that command.

Work through these steps in order. Show the user what you will change before changing
files or repository settings, and wait for confirmation.

1. **Check prerequisites.** The current directory must be a git repository whose
   `origin` is on GitHub, and `gh auth status` must succeed. The pipeline runs on
   GitHub-hosted `ubuntu-24.04` runners.

2. **Find the setup and check commands.** Read the README, package manifests
   (`package.json`, `pyproject.toml`, `go.mod`, `*.csproj`, `Makefile`, ...) and existing
   CI workflows. Propose:
   - `setup`: commands that install dependencies on a fresh `ubuntu-24.04` runner
     (for example `npm ci`, `python3 -m pip install -r requirements.txt`).
   - `checks`: non-interactive commands that must pass before a change can merge, usually
     tests plus lint. They run without secrets or network credentials, so leave out
     anything that needs them. Each check needs a short lowercase name. A slow check that
     covers only part of the repository can skip itself when `NEXKIT_BASE_SHA` is set and
     none of its paths changed, or, in later rounds, none changed since
     `NEXKIT_VERIFIED_SHA`, the last commit whose checks passed (see the configuration
     reference, Checks). Offer `"slow": true` for a check that takes many minutes, such
     as end-to-end tests: the agent then runs only the tests in it that its change
     touches, and NexKit runs it in full after each round. Suggest saying in CLAUDE.md or
     AGENTS.md how to run part of it (see the configuration reference, Slow checks).
   - `results` for a check that writes traces, screenshots, test reports or logs, such as
     an end-to-end suite: the folders or files where it writes them, relative to the
     repository root (for example `["test-results", "playwright-report"]`). When the check
     fails, NexKit keeps them, at most 50 MB per round, and the next fix round reads them
     first (see the configuration reference, Result files of failing checks).
   - `parallel_checks` when two or more checks take many minutes each, such as integration
     and end-to-end suites: each check, or each `group` of checks, then runs in a job of its
     own at once, so a round takes about as long as its slowest job. Group the quick checks
     (build, lint, unit tests) into one job, since every job runs `setup` again (see the
     configuration reference, Parallel checks).
   If the repository has no tests yet, say so: NexKit can still run, but it cannot verify
   changes until a check exists. Suggest adding a test command first.
   If setup downloads toolchains or many dependencies, offer `cache` entries (see the
   configuration reference, Caches): each has a `name`, `paths` outside the repository
   (`~/.npm`, `~/.gradle/caches`) and the `key_files` that decide the key, such as lock
   files. NexKit only restores them. The repository's own CI on the default branch must
   save each one with an `actions/cache` step before its setup, with the same `path`
   lines in the same order and the key
   `nexkit-${{ runner.os }}-<name>-${{ hashFiles(<key_files, in order>) }}`; offer that
   change to the CI workflow too. `nexkit init` does not write `cache`; add it to
   `.nexkit/config.json` after step 4.

3. **Choose the agents and models.** Default: the `claude` agent with `sonnet` for every
   stage. Mention that `opus` gives stronger results at a higher cost, and that each stage
   can be set separately in the config (see the configuration reference in the NexKit
   repository).
   If the user wants to save cost on easy issues, offer `profiles`: for example a cheap
   `standard` profile and a stronger `hard` one, each with a `when` text that describes
   its issues in terms an issue shows (the parts of the system it touches). Set
   `default_profile` to the cheapest one. A triage call then picks the profile at every
   `/nexkit plan`, the plan comment shows it, and a collaborator changes it with
   `/nexkit plan <request>`. `nexkit init` does not write profiles; add them to
   `.nexkit/config.json` after step 4.
   If the repository is public, say that anyone signed in to GitHub can read NexKit's
   logs and artifacts. NexKit redacts known secrets, but offer `"transcript": false` in
   the config to stop storing the agents' transcripts.
   NexKit supports and tests one runtime, Claude Code (`claude-code`). Do not offer other
   runtimes. An existing configuration with a `codex` agent is rejected since 4.0: remove
   the agent and give the stages that chose it a Claude agent and model.
   If the user has a second Claude credential, such as an Anthropic API key next to the
   subscription, offer it as a fallback: declare another `claude-code` agent,
   `"claude-api": {"runtime": "claude-code", "provider": "anthropic", "api_key_secret": "ANTHROPIC_API_KEY"}`,
   and add `"fallback": ["claude-api"]` to the `claude` agent. A stage then moves to it
   only when `claude` stops at a usage limit, and the next run starts on `claude` again.
   Say that the fallback's runs are billed per token, so `max_budget_usd` is worth
   setting.

4. **Write the files.**
   `nexkit init --check "test=<command>" [--check "lint=<command>"] [--setup "<command>"] --model <model>`
   For each check that needs more than a name and a command, add the flags that name it:
   `--check-timeout <name>=<minutes>`, `--check-slow <name>`,
   `--check-results <name>=<path>` once for each path, and with `--parallel-checks`,
   `--check-group <name>=<group>`. When the repository's CI workflows
   upload artifacts that explain a failure, such as end-to-end test results, add
   `--evidence-artifact <name>` for each, so plan, implement and fix rounds on an issue
   that names a failed run get them.
   Show the generated `.nexkit/config.json` and `.github/workflows/nexkit.yml`. The config
   declares the `claude` agent with `"oauth_token_secret": "CLAUDE_CODE_OAUTH_TOKEN"`; if
   the user has an Anthropic API key instead, change it to
   `"api_key_secret": "ANTHROPIC_API_KEY"`. Every secret NexKit reads is named in the
   config this way. The workflow passes the pipeline `secrets: inherit`; keep it, because
   that is how the pipeline reads secrets by the names the config gives them.

5. **Add the agents' credentials** as repository secrets. With a Claude Pro or Max
   subscription, the user runs `claude setup-token` in their own terminal (in this
   session they can type `! claude setup-token`), then
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN`. With an API key instead:
   `gh secret set ANTHROPIC_API_KEY`, which a `claude-api` fallback also needs next to
   the subscription token. Never ask the user to paste a token or key into the chat, or to
   put one in `.nexkit/config.json`: the config holds only the secret's name.

6. **Allow Actions to open pull requests.** After confirmation, run
   `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true`.

7. **Optional: let the repository's own CI run on NexKit pull requests.** Pull requests
   opened with the default Actions token do not trigger other workflows. NexKit runs the
   configured checks itself, so this is optional. To also trigger the repository's CI,
   store a fine-grained token with Contents and Pull requests write access as a secret,
   for example `gh secret set NEXKIT_PUSH_TOKEN`, and name it in the config:
   `"publish": {"push_token_secret": "NEXKIT_PUSH_TOKEN"}`. NexKit uses a push token only
   when the config names it.

   If the user wants `"merge": {"auto": true}`, say that branch protection still applies,
   that a pull request whose review could not verify an acceptance criterion waits for a
   person, and that a merge by NexKit starts no `push` CI on the base branch. Offer to list those CI
   workflows in `merge.after_workflows` (for example `["ci.yml"]`, or
   `nexkit init --after-workflow ci.yml`) so NexKit starts them after each merge; each one
   needs a `workflow_dispatch` trigger, which is a change the user makes to that workflow
   file.

   Offer `"notify": ["<login>"]` so the user gets a GitHub notification when a run needs
   a person, for example a plan with questions or a pull request ready to merge.

   With `merge.auto`, also offer `"conflicts": {"auto_resolve": true}`: NexKit then merges
   the base branch into its pull requests that start to conflict, by itself.

   If the repository splits large issues into sub-issues and a parent means only the sum
   of its parts, offer `"close_parent_issues": true`: NexKit then closes a parent when its
   last sub-issue closes and at least one was completed.

   If the user wants NexKit to work through the issues without posting commands, offer
   `"autopilot": {"enabled": true}`. It plans each ready issue of the earliest open
   milestone, starts `/nexkit go` on a plan with no questions that is not too large, and
   moves on when an issue closes; `"scope": null` takes every open issue instead, in
   every milestone and without one, so *blocked by* relationships alone order the work
   across milestones. Say that it skips their approval of each plan, that it
   takes only issues opened by people with write access, and that it stops on questions,
   blocked or failed runs and mentions `notify` (after a stopped `/nexkit go`, a person's
   comment or a blocker that closes lets it plan again), so recommend setting `notify` with it
   (`nexkit init --notify <login>`, or `"notify": ["<login>"]`). `"max_parallel": <n>` limits how many
   issues are in progress at once; without it, every ready issue starts. It pairs with
   `merge.auto`, `conflicts.auto_resolve`, `close_parent_issues` and `notify`. With
   `"answer": "recommended"`, it also answers the questions of plans and blocked rounds
   with the option NexKit recommends, when every question has one, and waits for a person
   when the round after its answers asks again; it splits plans that
   are too large into sub-issues either way. With `merge.after_workflows`, it also runs a
   failed CI run on the default branch once more and, if it fails again, opens a `bug`
   issue that it plans first; runs that passed only when run again are recorded on their
   own `bug` issue, planned next. Then also add the `workflow_run` trigger that names
   those workflows by their `name:` (`nexkit init --after-workflow` writes it), and offer
   to add the `nexkit-sweep` job that `nexkit init --after-workflow` prints to the end of
   each of those workflows, with the user's agreement since it changes their CI: GitHub
   sends no `workflow_run` event for the runs NexKit starts after its merges, and that
   job starts the sweep instead, so a failure is noticed within minutes.
   A command that autopilot started and that never ran starts once more as soon as its
   workflow run ended, or after `"lost_after_minutes"` (30 by default) when that run
   cannot be read.
   With `autopilot` or `conflicts.auto_resolve`, recommend the push token above with
   Workflows write access too: without it, a round that merges a change to
   `.github/workflows/` from the base branch cannot push and waits for a person.
   `nexkit doctor` says so. After the commit, start the first sweep with
   `gh workflow run nexkit.yml -f command=autopilot -f number=autopilot`.
   If the work should go on while nobody watches, mention the `drive` skill: a Claude Code
   session on a schedule, such as a routine, runs it to act where autopilot stops for a
   person. The repository's own instructions for that session set its limits: what it may
   decide, whether it may push, and whom it notifies.

8. **Commit to the default branch.** Comment events only run workflows from the default
   branch. Commit both files (directly or through a pull request, as the user prefers).

9. **Verify.** Run `nexkit doctor` and fix every ✗ line. Then suggest a first trial:
   open a small, well-defined issue and comment `/nexkit plan`.

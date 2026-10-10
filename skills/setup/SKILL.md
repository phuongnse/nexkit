---
name: setup
description: Set up the NexKit pipeline in the current GitHub repository - choose check commands, install the config and workflow, add the Claude credential and verify. Use when the user asks to install, configure or repair NexKit for a repository.
---

# Set up NexKit in this repository

NexKit adds one workflow (`.github/workflows/nexkit.yml`) and one config file
(`.nexkit/config.json`). Collaborators then drive work with issue comments:
`/nexkit plan`, `/nexkit go`, and on NexKit pull requests `/nexkit fix` and `/nexkit review`.

The NexKit CLI ships with this plugin. Run it as
`python3 <base directory of this skill>/../../bin/nexkit` (Python 3.12 or newer; no
packages needed). If the installed Python is older, ask the user to install 3.12 first. Below, `nexkit` means that command.

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
     anything that needs them. Each check needs a short lowercase name.
   If the repository has no tests yet, say so: NexKit can still run, but it cannot verify
   changes until a check exists. Suggest adding a test command first.

3. **Choose the model.** Default `sonnet` for every stage. Mention that `opus` gives
   stronger results at a higher cost, and that each stage can be set separately in the
   config (see the configuration reference in the NexKit repository).
   If the user wants to save cost on easy issues, offer `profiles`: for example a cheap
   `standard` profile and a stronger `hard` one, each with a `when` text that describes
   its issues in terms an issue shows (the parts of the system it touches). Set
   `default_profile` to the cheapest one. A triage call then picks the profile at every
   `/nexkit plan`, the plan comment shows it, and a collaborator changes it with
   `/nexkit plan <request>`. `nexkit init` does not write profiles; add them to
   `.nexkit/config.json` after step 4.
   If the repository is public, say that anyone signed in to GitHub can read NexKit's
   logs and artifacts. NexKit redacts known secrets, but offer `"transcript": false` in
   the config to stop storing Claude's transcripts.

4. **Write the files.**
   `nexkit init --check "test=<command>" [--check "lint=<command>"] [--setup "<command>"] --model <model>`
   Show the generated `.nexkit/config.json` and `.github/workflows/nexkit.yml`.

5. **Add the Claude credential** as a repository secret. With a Claude Pro or Max
   subscription, the user runs `claude setup-token` in their own terminal (in this
   session they can type `! claude setup-token`), then
   `gh secret set CLAUDE_CODE_OAUTH_TOKEN`. With an API key instead:
   `gh secret set ANTHROPIC_API_KEY`. Never ask the user to paste a token into the chat.

6. **Allow Actions to open pull requests.** After confirmation, run
   `gh api -X PUT repos/OWNER/REPO/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=true`.

7. **Optional: let the repository's own CI run on NexKit pull requests.** Pull requests
   opened with the default Actions token do not trigger other workflows. NexKit runs the
   configured checks itself, so this is optional. To also trigger the repository's CI,
   store a fine-grained token with Contents and Pull requests write access as
   `NEXKIT_PUSH_TOKEN`.

   If the user wants `"auto_merge": true`, say that branch protection still applies and
   that a merge by NexKit starts no `push` CI on the base branch. Offer to list those CI
   workflows in `after_merge_workflows` (for example `["ci.yml"]`) so NexKit starts them
   after each merge; each one needs a `workflow_dispatch` trigger, which is a change the
   user makes to that workflow file.

8. **Commit to the default branch.** Comment events only run workflows from the default
   branch. Commit both files (directly or through a pull request, as the user prefers).

9. **Verify.** Run `nexkit doctor` and fix every ✗ line. Then suggest a first trial:
   open a small, well-defined issue and comment `/nexkit plan`.

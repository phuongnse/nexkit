# Agents and profiles

This page covers the agents that run each stage and the profiles that choose them. It follows [How it works](how-it-works.md), which holds the jobs of a run and what you see during one.

## Agents

Each stage runs on an agent: a runtime, which is the CLI that NexKit starts, paired with a
provider, which serves the model (see [Agents](configuration.md#agents)). NexKit builds
the prompt, starts the CLI with it, reads the CLI's event stream for the log and the
transcript, and checks the structured result against the stage's schema itself before
anything is published. Everything specific to one CLI lives in `nexkit/runtimes/`, and no other
code branches on a runtime's name.

Claude Code is the one runtime that NexKit supports. The reasons are in
[D7](decisions/d7-one-tested-runtime-claude-code.md).

**What a stage may affect** is the same for every runtime:

| Stage | Access | Claude Code |
|---|---|---|
| triage | none: an empty working directory | no tools |
| plan, review | read the checkout, never change it | `Read`, `Grep`, `Glob` |
| implement, fix | anything on the disposable runner, which holds only a read-only GitHub token | every tool, permissions bypassed, background tasks off |

The levels define what an agent can affect, not a set of tools: a runtime added later
meets each level in its own way and documents how here. A run ends when the agent ends its
turn, so Claude Code runs with background tasks turned off: a check started in the
background would end the run without a result. When Claude Code accepted a result but does
not repeat it at the end, NexKit uses the result it accepted.

**What an agent reads of an issue.** The prompt has the issue's body whole, up to the
65,536 characters GitHub keeps in a body or a comment, and each comment of collaborators
whole too: in the discussion, the notes on `/nexkit` commands and, for a fix round, the
review comments on the current commit. The latest 30 comments of a discussion are in the
prompt, and each block of them, such as the discussion of the issue or of the pull
request, holds at most 80,000 characters, latest first. An older comment past that keeps
its first 1,000 characters in the prompt, with a note that says how many it has and where
the rest is. Comments older than the latest 30 are left out, with a note at the start of
the discussion that says how many and where they are. NexKit writes each whole text it
shortens, and the comments it leaves out of each discussion together, before the agent
starts, to `texts/` beside `context.json`, outside the working tree. The prompt's section
*Texts this prompt shortened* names each one, and the agent reads them rather than ask a
person for them. The run's comment says which texts were shortened or left out, so a
person who answers the agent knows why it asks. Triage has no tools to read a file with,
so it keeps the start of each such text alone, and of the comments it leaves out only
how many.

**What an agent reads of the texts an issue links to.** The agent holds no GitHub token,
so it cannot open a link. NexKit reads for it, before the agent starts, what the issue
refers to, and the plan, implement, fix and review prompts show it in their section
*Texts the issue links to*:

- **The issues a part was split from.** A part of a split gets the text and the
  collaborators' discussion of the issue it was split from, and of each issue above that
  one, up to the root, root first: its parent on GitHub, the parent's parent, and so on.
  The rules and the context the part refers to often live there; where they differ, the
  nearer issue decides, and the part's own text decides over all. Each step counts when
  the sub-issue is a part NexKit's App filed of that parent, or an issue that someone who
  can write opened, such as a part a person split by hand past the depth autopilot splits
  to (see [limits on parts](work-without-a-command.md#autopilot)) or an issue a writer put under an epic. An issue
  someone else opened, and one whose text only claims to be a part, reads no issue above,
  and its prompt says so. The text of an issue above counts when NexKit or someone who
  can write wrote it, or a writer vouched for it; a round that NexKit started stops when
  someone without write access edited it after the vouch, as for the issue itself.
- **Linked comments.** Each comment of this repository that the issue's body, the issues
  it was split from, or a collaborator's comment on any of them links to: a link to a
  comment on an issue or pull request, such as
  `https://github.com/<owner>/<repo>/issues/12#issuecomment-345`, or a bare anchor such as
  `(#issuecomment-345)`, which names a comment by its id. A comment that the issue's own
  discussion shows is not repeated, apart from the fix prompt, which shows no discussion;
  a comment of an issue above is shown once, as linked. A part's text links the plan that
  split it, so a part reads the plan of each issue above it.
- **Linked issues and pull requests.** The text of each issue or pull request of this
  repository that those texts link to, such as `#12` or
  `https://github.com/<owner>/<repo>/issues/12`. Only its text: a comment of its
  discussion counts only when it is linked itself.

Links count outside code only, as GitHub links them, on the server the workflow runs on.
NexKit tries at most 20 linked comments and 10 linked issues or pull requests, in the
order they were linked, each once. Only texts of collaborators count, as in a discussion,
and NexKit's own plans and issues, which the agents read without NexKit's markers and the
records for people. Every link NexKit did not read is listed under *Links NexKit did not
read* with why, up to 30 of them and then how many more: another repository, a review
comment or a discussion, which NexKit does not read, a text a person outside the
collaborators wrote, past the limits, or one that could not be read. When the work
depends on one of them and nothing the agent has settles it, the plan asks a question
with no recommended option, and an implement or fix round stops blocked with one, so a
person answers and autopilot does not; the review checks the change against the rules
the texts set, and flags a listed text that may hold one. The round's comment says what
NexKit read for the agents (*Read for the agent*) and what it did not (*Linked but not
read*), and so do the log and the run summary. The linked texts keep at most 80,000
characters, the first linked whole first. The texts and discussions of the issues above
share another 80,000, the nearest first, each text at most 65,536; past it each text and
comment keeps its start. Every text kept only in part is whole in `texts/`. Triage does
not read them.

**What an agent reads of a failing check's output.** NexKit keeps the last 12,000
characters of each check's output, and the first 20 lines before them that report a
failure (they contain `fail`, `error`, `✘` or `✗`, in any case, as for a
[failure signature](failure-handling.md#failure-handling)), each cut at 300 characters, so a failure that a
long teardown follows is not lost. The fix prompt shows the last 6,000 characters of a
failing check's output, and the review prompt the last 3,000, from the start of a line, so
the summary that test runners print at the end is there. Before that end, each shows the
lines that report a failure, those kept from before the 12,000 characters first: at most
20 in the fix prompt and 10 in the review prompt. When the prompt does not show all that
NexKit kept, NexKit writes it to `texts/` too, and the check's section names the file and
says whether it holds the whole output or only what NexKit kept, which is less when the
state comment had to shorten it to fit GitHub's limit (see [State](how-it-works.md#state)). The run's
comment does not list these files: no person wrote them.

**A result that does not fit.** An agent may finish the work and still give no result
that fits the stage's schema, such as one that leaves out a field or that Claude Code
turned down as often as it allows (`error_max_structured_output_retries`). A field whose
empty value means the same as leaving it out, such as `questions`, `blocker`,
`outside_plan`, `reviewer_notes`, `protected_paths` or `no_change`, takes that empty value
instead. Otherwise NexKit asks the same session once for its result alone, with what was
wrong, and leaves the working tree as the agent left it, so an implement or fix round
keeps its change. That request gets at most 10 minutes of the time the stage has left, and
of `max_budget_usd` what the first run left; the log and `result-again/` in the agent's
artifact show it. A result that fits then goes on as usual. When the session gives none
either, runs out of that time or budget, or the runtime cannot continue a session, the
stage stops as after a transient error, never as work that ran out of time: NexKit runs the
command again by itself after the waits of `retry.wait_minutes`, and a stop after the last
waits for a person (see [Failure handling](failure-handling.md#failure-handling)). Claude Code keeps each session for this, under an id NexKit
gives it: on the runner's disk, or on a person's machine in Claude Code's own session store
when NexKit runs there, such as the smoke test.

**How secrets reach an agent.** The repository's workflow passes the pipeline
`secrets: inherit`. `route` works out which agents each job may run and names their
secrets: for the `agent` job those of its stage's agent and its fallback agents (for a
plan, triage and the plan of every profile, since triage chooses one in the job), for the
`review` job those of the review's agent and its fallback agents. The job reads each
named secret into one of 8 slots, and NexKit gives it back its name. Names that start with
`GITHUB_` or `NEXKIT_` are never read. Within the job, each CLI process gets an
environment without any agent secret, plus the ones its provider names, and setup and
check commands get none. Keys are never put on a command line or in a file.

This filtering limits what each process inherits. It is not a wall inside a job: an agent
that can run commands can read other processes on the runner, so it may see every secret
its job holds. A job holds another agent's secret only when that agent may run in the same
job, such as a plan whose triage runs on another agent, or a fallback agent of the stage.

## Profiles

With `profiles` in the configuration (see the
[configuration reference](configuration.md#profiles)), each issue runs on one profile,
which sets the models and limits of its stages.

**Triage chooses the profile.** Every `/nexkit plan` starts with a triage call in the
`agent` job: one agent session with no access to the repository, run in an empty
directory so the repository's agent settings do not load. It reads the issue, the collaborators'
discussion including the notes on earlier `/nexkit` commands, the note on this command,
each profile with its `when` text, and the previous plan's profile. It returns a profile
name and a reason. It decides in this order:

1. A collaborator's request in the discussion or the note wins; the latest one counts. A
   request in the issue itself does not, because anyone can write the issue.
2. Otherwise a re-plan keeps the previous plan's profile, so the profile does not change
   unless someone asks.
3. Otherwise triage picks the profile whose `when` text fits the issue.

The plan then runs with the profile's settings. Triage has its own run summary, with its
own tokens.

**Triage never stops the plan.** After an error, a timeout or an unknown profile name, the
plan uses the previous plan's profile, or `default_profile` when there is none, and the
plan comment says that triage failed. A profile chosen only as that default is not kept
on the next re-plan: triage chooses again.

**The plan comment shows the profile.** Below the summary: the profile, the models it uses
for plan, implement and review, the reason, and how to change it. To change it, say which
profile in a comment and comment `/nexkit plan`, or comment `/nexkit plan <request>`, for
example `/nexkit plan Use the hard profile`. There is no other way to change a profile, so
the plan comment always shows the profile its rounds run on.

**Rounds use the latest plan's profile.** `route` reads it when the run starts and passes
it, with the profile's settings, to every job of the run. `/nexkit go`, fix rounds,
automatic rounds and `/nexkit review` use it; with no plan, or a plan from before profiles
were configured, they use `default_profile`. `/nexkit go` without a plan does not run
triage. When the plan names a profile that is no longer in the configuration, the round
stops before any job runs and asks for a re-plan; it never falls back to another profile.
A re-plan while the pull request is open applies from the next round.

The job timeouts follow the profile. A stage may run for its `timeout_minutes` once for
each agent of its fallback list, at most 340 minutes; the `agent` job adds
`limits.setup_timeout_minutes`, and `review` adds time for its own steps. For a plan,
`route` does not know the profile yet, so the `agent` job allows for the triage call and
the longest plan of all profiles.

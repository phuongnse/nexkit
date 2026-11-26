# Writing the docs

The docs say what NexKit is, how it behaves and why it is built the way it is. They are written for
people: someone who sets NexKit up on a repository, the people who work with it there, and whoever
works on NexKit itself. Agents read the same docs, inside the pipeline and in a session on this
repository, and a page that a person reads easily is also the best input for an agent. This page says
what goes where, how to write it, and how the docs stay true while NexKit changes.

## What goes where

Each kind of content has one home. Write a fact in its home and link to it from everywhere else.

| Content | Home |
| --- | --- |
| What NexKit is, what it adds around an agent, how to install it | The [README](../README.md) |
| How NexKit behaves on a repository: commands, jobs, stages, state, failure handling | [How it works](how-it-works.md) and the pages it links: [stages](stages.md), [work without a command](work-without-a-command.md), [agents and profiles](agents-and-profiles.md) and [failure handling](failure-handling.md) |
| Every setting, its default and what it does | The [configuration reference](configuration.md) and its pages for [checks](checks.md), [autopilot](autopilot.md) and [merges and waits](merges-and-waits.md) |
| Why a run stopped or waits, and what to do | [Troubleshooting](troubleshooting.md) |
| The engine, its protocol and its hosts | [The engine](engine.md) and its pages of [states, events and actions](engine-states.md) and [runners, policies and hosts](engine-hosts.md) |
| How NexKit is built: modules, workflows and jobs, tokens and secrets | [Architecture](architecture.md) |
| What NexKit trusts, and the rule that contains each input it does not | [Security](security.md) |
| A choice between real alternatives, with its reasons | A [decision](#decisions), listed in the [index of decisions](decisions.md) |
| How the work on NexKit is done: issues, pull requests, labels, milestones, releases | [Delivery](delivery.md) |
| How NexKit is tested | [Testing](testing.md) |
| How an agent applies a rule, step by step | A skill, which links the rule and does not restate it: the plugin's skills under `skills/` for people who use NexKit, and the maintenance skills under `.claude/skills/` for work on NexKit |
| Each feature and its status | The [roadmap](roadmap.md) |
| What a release changed, and how to upgrade to it | The [changelog](../CHANGELOG.md) |
| Who builds what, and its progress | GitHub issues and pull requests |
| Values that the code owns: pins, defaults, limits | The code. A doc names the setting or the constant, and a test compares any list that a doc must repeat |

So no doc says that something "is built" or "is not built yet", other than by the
[planned marker](#planned-work), and no doc holds dates of discussions, who asked for what, issue or
pull request numbers, or how a rule used to read. Links that track work are the exception: the
roadmap's links to milestones, the issue that a planned marker names, and the changelog, which may
cite the issue of each change.

## Decisions

A decision records a choice that had real alternatives and is costly to undo: an architectural
choice, a contract with the repositories that use NexKit, a security property, a way of working. A
default value or the wording of a comment is not a decision: it is a rule in the page that owns it.

A decision is a file `decisions/d<n>-<slug>.md` with a row in the index, `decisions.md`, and it
reads like this:

```markdown
# D<n>. <title that states the choice>

Status: **Proposed**

## Context

What forces the choice, in a few sentences.

## Decision

What was chosen, as a few rules. The detail lives in the pages that this links.

## Consequences

What becomes easier, what becomes harder, and what it costs.

## Alternatives

Each option not taken, and why. Optional.
```

A decision holds only the choice and its reasons, so it stays true while the pages around it change.
It never holds progress, dates, names or issue numbers.

- **Proposed** is a working default that can still change, and it may be edited freely.
- **Agreed** means the owner has accepted it and work may rely on it. A wording or a link may still be
  fixed, but a change to the choice is a new decision.
- **A new decision that changes an agreed one replaces it whole.** It restates the parts that still
  hold, so the old file is never half valid. In the same change the old file is deleted, its row in
  the index says `Superseded by D<n>` with a link to its last text in git, and every page, link and
  comment in the code that cited it follows the new decision. A number is never used again.

A change needs a decision when it makes a choice that had real alternatives and is costly to undo,
or when it changes the choice of an Agreed decision. Such a change adds a **Proposed** decision in
the same pull request, and links it from the rule or page that the choice shapes.

A new decision takes the next number that no decision has used, superseded ones included. Its slug
is its title in kebab-case, so the title must state the choice, as `D1. State lives only on GitHub`
does. Its row in the index links the file, and repeats the title without the `D<n>. ` prefix and
the status, as in this row:

```markdown
| [D1](decisions/d1-state-lives-only-on-github.md) | State lives only on GitHub | Agreed |
```

## Reference pages

A reference page describes how NexKit behaves, as it is, and the agreed work that is not built yet.
[How it works](how-it-works.md) and the pages split from it ([stages](stages.md), [work without a
command](work-without-a-command.md), [agents and profiles](agents-and-profiles.md) and [failure
handling](failure-handling.md)), the [configuration reference](configuration.md) and the pages split
from it ([checks](checks.md), [autopilot](autopilot.md) and [merges and
waits](merges-and-waits.md)), [troubleshooting](troubleshooting.md), [the engine](engine.md) and the
pages split from it ([states, events and actions](engine-states.md) and [runners, policies and
hosts](engine-hosts.md)) and [security](security.md) are reference pages.

- **Write the current rule, never the change.** When a rule changes, rewrite it where it lives and
  delete the old text. A page does not say "no longer", "now" or "this replaces". What changed between
  releases, and how to upgrade, is the [changelog](../CHANGELOG.md)'s, and the configuration
  reference's *Upgrading from earlier releases*.
- **One home for each rule.** Other pages link to it. A page may sum up another page's rule in a
  sentence that links it, but never restates its detail.
- **Give the reason** when a reader would ask why, in a sentence next to the rule. A longer argument
  is a decision, which the page links.
- **State a rule's scope by kind.** A rule says what it covers by what those things are, such as
  "every job that runs an agent", never by a list of the files, jobs or settings that hold them
  today. A new one then falls under the rule without a change.
- **Leave values to the code.** Name the pin, the setting or the constant, not its value, unless a
  test compares the page with the code.
- **Describe NexKit for any repository.** The docs name no product or repository that uses NexKit,
  except the README's *Built with NexKit* list and the changelog, which may cite a case.

### Planned work

Work that is agreed but not built yet carries the marker `_(planned, #<n>)_`, which names the open
issue that builds it. The marker sits on a line of its own under the heading of a section that is
planned as a whole, right after the bold lead-in of a single planned rule in a list, or in the row of
a table whose rows are items of their own. It never sits in the middle of a sentence. The pull request
that builds the work removes its marker.

A page that describes a whole milestone's design ahead of it, as [the engine](engine.md) does, says
under its title what is built so far and links the section that tracks the rest. That note goes when
the milestone ends.

## References in the code

Comments in the code point at places that still hold, so a reader can find the rule and its reason.

- **A comment that says what the code must do links the doc section that states the rule,** as
  `docs/<page>.md#<section>`. The section holds the rule, so the comment stays true when it changes.
- **A comment that says why a choice was made cites the decision in force,** as `D<n>`.
- **The code names no milestone, issue or pull request.** Those are progress and history, which move
  as work does.

## Style

Write for a reader who knows software and GitHub but not NexKit.

- **Open with the purpose.** Each page, and each `##` section, starts with a sentence or two that say
  what it covers and why it matters.
- **Write whole sentences.** Join related facts with "and", "because" or "so". A sentence that must be
  decoded is too short, and one that needs a second reading is too long.
- **Use NexKit's words** as [how it works](how-it-works.md), the pages it links and [the engine](engine.md#words)
  define them, such as round, stage, sweep and work item, and name the role that acts: a person with write
  access, the owner, an agent, NexKit's App.
- **Pick the form that fits.** A table for things with the same attributes, a numbered list for steps,
  a bulleted list for rules a reader scans, prose for an explanation. Use a bold lead-in only in a
  list of rules.
- **Show an example** when a rule is abstract: a snippet of configuration, a command and its comment,
  the words a status shows.
- **Write in English,** like the code, the commits, the issues and the pull requests.

Some pages are read from start to end by a person who wants to understand NexKit, rather than
searched: the [README](../README.md), the opening sections of [how it works](how-it-works.md), the
architecture page and any guide. They also say what and why first, give an example before the rule,
use everyday words, define a term where it first appears, and link a decision at the end of a section
rather than in its sentences.

## Structure

The docs are split so that a reader, or an agent, reads only what a change needs, and so that two
pull requests rarely edit the same lines.

- **One kind of page.** A page is a reference page, which says how NexKit behaves; an explanation,
  which says why and how the parts fit, such as the architecture and the decisions; a how-to guide,
  the steps of one task for one reader; or a tutorial. These are the kinds of Diátaxis, and a page
  does not mix them.
- **One file for each topic, and one map.** [The map](README.md) opens with a reading path by what
  the reader wants, then lists every file under `docs/` with what it holds and when to read it,
  grouped for people who use NexKit and people who work on it. A new file gets its row in the same
  change, and a test fails when a file has none. A new topic that needs more than a few sections
  starts in a file of its own. The map lists the decisions through their [index](decisions.md): a
  decision file gets a row in the index, not in the map.
- **Small files.** A doc under `docs/` holds at most 12,000 words. A change that grows a doc past
  10,000 words splits it by topic, moving whole `##` sections unchanged to a new file, and leaves each
  page at or under 8,000 words so that the next changes do not meet the limit again. A change near
  the limit never shortens, merges or deletes text only to fit; text leaves a page only for its own
  reason, which the pull request names. A doc already past the limit is split by an issue of its own.
- **Short agent instructions.** [AGENTS.md](../AGENTS.md) holds only where to look, the rules an
  agent must never break, and the commands. A line belongs in it only when almost every session needs
  it, an agent would get it wrong without it, and it is a sentence that links the doc holding its
  detail. It holds at most 800 words, and a rule at most 60. `.claude/CLAUDE.md` only imports it.
- **Short skills.** A skill holds the steps of one role's workflow and links the rules it applies.
  Its `description` says what it does, when to use it and when not to. `SKILL.md` routes to the parts
  of the workflow, a part that few runs need is a file of its own beside it, and `SKILL.md` holds at
  most 1,000 words.
- **Stable links.** Link to a file, or to a heading in it, never to text that changes with status.
  Renaming or moving a heading changes its anchor, so the same change fixes every link to it.

## Diagrams

A diagram shows what text says slowly: how the parts fit, or how a flow runs. Diagrams follow the C4
model and are written in Mermaid inside the Markdown, which GitHub draws, so a review reads their diff
and an agent can change them.

- **Next to its text.** A diagram sits in the section that it illustrates, and the text says the same
  thing in words, so a reader who cannot see the diagram loses nothing.
- **One view.** A diagram shows one view at one level: the jobs of a run, the parts of the engine and
  a host, the flow of a round. One that needs a legend, or more than about fifteen boxes, is split.
- **Names from the docs and the code,** so a reader can search for them.
- **No image files** under `docs/`.
- **True like any statement.** A change that makes a diagram wrong fixes it, as it fixes the text.

## How the docs stay correct

Docs drift when a change adds a rule and leaves the old one beside it, when progress is written into
the text, when one fact lives in two places, when work is built and its page still says planned, and
when a list of what the code defines misses an entry. These rules close each of them.

1. **Every pull request states its docs.** The item of this repository's [review
   checklist](configuration.md#review-checklist) that points at this page asks for them, so the plan's
   answer to that item, and the review's, name the doc sections the change touches, or say why it
   touches none. The pull request rewrites the section that describes what it changes and deletes what
   it replaces, removes the planned marker of what it builds, and searches the docs and the skills for
   every name it renames or removes, such as a setting, a command, a label or a status. A pull request
   that makes any doc statement false fixes it. The review checks the docs diff against the code diff.
2. **A setting, a command or a prompt contract changes together with its docs and skills,** in the
   same pull request, as its tests do.
3. **A changed decision is a new decision,** which replaces the old one as [Decisions](#decisions)
   says.
4. **Status lives in the roadmap and on GitHub.** The [roadmap](roadmap.md) records each feature and
   its status. The docs mark only planned work.
5. **The security page follows new trust boundaries.** A change that adds a kind of input NexKit does
   not trust, a token, a secret or a job that holds one updates the security page in the same change.
6. **A list of what the code defines has a test, or is not written.** A doc that lists settings,
   commands, statuses, labels, jobs or anything else the code defines has a test that reads the list
   from the doc and from the code and compares them. Without such a test, the doc names the file that
   holds the list rather than copying it.
7. **A doc names a setting of this repository's `.nexkit/config.json`, never its value,** so the
   owner tunes it with a change to that file alone. The configuration reference's defaults are
   NexKit's own and are not such values.
8. **An audit reads the docs against the code every few merges.** An agent reads a share of the
   docs, the skills, the README and AGENTS.md against the code each time, so every page is read
   every few audits, and its findings become issues that NexKit takes: a statement the code
   contradicts, planned work already built, behaviour no page describes, a rule the code does not
   follow, a list with no test, a break of this standard, a doc past its size, and a milestone
   whose close on GitHub and roadmap row disagree. It also points each broken link of an open issue
   at its new home. The [docs audit skill](../.claude/skills/nexkit-docs-audit/SKILL.md) says how a
   page is read and what each issue holds.
9. **Checks catch what a machine can.** `tests/test_docs.py` checks that [the map](README.md) lists
   every doc, with the decisions listed through [the index](decisions.md), and that its list of the
   plugin's skills matches the skills under `skills/`; that the links of every
   page under `docs/`, of the README, of [AGENTS.md](../AGENTS.md), of the plugin's skills under
   `skills/` and of the maintenance skills under `.claude/skills/` resolve; that each link to how it
   works and the pages split from it resolves; that each task of the `nexkit-maintain` skill links a
   section of [Delivery](delivery.md); that AGENTS.md keeps within its word limits; that the job and
   module tables of the architecture page match the workflows and `nexkit/`; that the job table of
   [how it works](how-it-works.md#one-run-ten-jobs) matches the jobs of `pipeline.yml`, the
   permissions its rows name, and the number in its heading; that the plan and review layouts of
   [stages](stages.md) list the sections the code writes, and that each value the pages repeat
   matches its constant; that each test the security page names exists; that the outcome table of
   [how it works](how-it-works.md) lists each outcome the code defines and no other; and that the
   index matches the decision files, each of which follows the
   template and is linked from a rule or a page. A docs check is added only once the whole tree
   passes it: there are no lists of exceptions.

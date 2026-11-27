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
  fixed, but a change to the choice is a new decision. A narrower or wider scope, an exception added
  or removed, a case it now allows or forbids and a new title each change the choice; a wording fix
  leaves what the decision allows and forbids as it was. Only the owner sets **Agreed**: a new
  decision stays **Proposed** until the owner agrees to it.
- **A new decision that changes an agreed one replaces it whole.** It restates the parts that still
  hold, so the old file is never half valid. In the same change the old file is deleted, its row in
  the index says `Superseded by D<n>` with a link to its last text in git, and every page, link and
  comment in the code that cited it follows the new decision. A number is never used again.

A change needs a decision when it makes a choice that had real alternatives and is costly to undo,
or when it changes the choice of an Agreed decision. Such a change adds a **Proposed** decision in
the same pull request, and links it from the rule or page that the choice shapes.

A change follows the decisions and rules in force; none is widened to let a change pass. When a
review or a check finds that a decision or a rule forbids the change, the change is made to follow
it, or it adds a **Proposed** decision that weighs the alternatives and goes ahead only once the
owner agrees to it. A plan that would edit an Agreed decision's choice proposes such a decision
instead.

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
9. **Checks catch what a machine can.** `tests/test_docs.py` makes these checks, one test each, and
   a test compares this list with the file. A docs check is added only once the whole tree passes
   it: there are no lists of exceptions.
   - `test_rule_9_lists_each_check_of_this_file_and_no_other`: this list names each test method of
     `tests/test_docs.py` once, and no other.
   - `test_the_map_lists_every_doc`: [the map](README.md) lists every doc, with the decisions listed
     through [the index](decisions.md).
   - `test_the_map_names_each_skill_of_the_plugin`: the map's list of the plugin's skills matches
     the skills under `skills/`.
   - `test_the_architecture_page_names_each_skill_of_the_plugin`: the list of the plugin's skills on
     [the architecture page](architecture.md) matches the skills under `skills/`.
   - `test_the_links_of_the_docs_the_readme_agents_md_and_the_skills_resolve`: the links of every
     page under `docs/`, of the README, of [AGENTS.md](../AGENTS.md) and of the plugin's skills
     under `skills/` resolve.
   - `test_each_link_to_how_it_works_and_its_pages_resolves`: each link to [how it
     works](how-it-works.md) and to the pages split from it resolves, from any of those pages.
   - `test_each_test_the_page_names_exists_in_the_file_it_names`: each test that [the security
     page](security.md) names exists in the file it names.
   - `test_each_untrusted_input_names_a_rule_and_a_test`: each untrusted input in the table of [the
     security page](security.md) links a rule of that page and names a test.
   - `test_agents_md_holds_at_most_800_words`: [AGENTS.md](../AGENTS.md) stays within 800 words.
   - `test_each_rule_holds_at_most_60_words`: each rule of [AGENTS.md](../AGENTS.md) stays within 60
     words.
   - `test_each_rule_links_a_doc_section`: each rule of [AGENTS.md](../AGENTS.md) links a section of
     a page under `docs/`.
   - `test_the_supported_versions_table_gives_the_pins_of_the_code`: the supported versions table of
     [the README](../README.md#supported-versions) matches the pins in `nexkit/__init__.py`.
   - `test_every_runner_the_readme_names_is_the_pinned_runner`: each `ubuntu-NN.NN` in [the
     README](../README.md) is the pinned runner.
   - `test_the_release_list_names_each_file_that_holds_the_version`: the files that
     [Releases](delivery.md#releases) lists as holding the version are those that hold it, and no
     other.
   - `test_the_readme_installs_the_tag_of_the_version`: the `pipx install` line of [the
     README](../README.md) installs the tag of the version.
   - `test_no_doc_repeats_a_pinned_version`: no doc, the README, AGENTS.md or a skill of the plugin
     under `skills/` repeats a pinned version: the version of each agent runtime or the digest of the
     image's base that `nexkit/__init__.py` pins, as [D9](decisions/d9-pins-live-in-one-place.md)
     says, or a lint tool's version from `scripts/lint.sh`.
   - `test_the_configuration_example_is_what_nexkit_init_writes`: the configuration example of [the
     README](../README.md#configuration) is what `nexkit init` writes for the README's own setup
     command.
   - `test_the_local_commands_table_lists_each_command_for_people_and_no_other`: the local commands
     table of [the README](../README.md#local-commands) lists each command `nexkit/cli.py` defines
     for people, and no other.
   - `test_each_nexkit_option_the_readme_names_exists`: each option that the README names with a
     `nexkit` command exists for that command.
   - `test_the_outcome_table_lists_each_outcome_the_code_defines_and_no_other`: the outcome table of
     [how it works](how-it-works.md) lists each outcome the code defines and no other.
   - `test_the_label_table_lists_each_label_the_code_creates_and_no_other`: the label table of [the
     delivery page](delivery.md#labels-and-milestones) lists each label the code creates and,
     besides the labels of a kind, no other.
   - `test_the_mention_table_lists_each_outcome_that_needs_a_person_and_no_other`: the mention table
     of [troubleshooting](troubleshooting.md#what-a-mention-says) lists each outcome that needs a
     person and no other.
   - `test_each_row_says_what_the_code_writes_in_the_mention`: each row of that mention table gives
     the code's text for what to do and why.
   - `test_the_readme_lists_each_command_the_code_accepts_and_no_other`: the command table of [the
     README](../README.md#use-it) lists each comment command that `nexkit/route.py` accepts, where
     it accepts it, and no other.
   - `test_the_permissions_table_lists_each_permission_the_app_asks_for_at_its_level`: the table of
     the App's permissions in [configuration](configuration.md#the-github-app) lists each permission
     `nexkit/app.py` asks for, at its level.
   - `test_the_runtime_table_lists_each_runtime_the_code_defines_and_no_other`: the runtime table of
     [the configuration reference](configuration.md) lists each runtime the code defines and no
     other, with its providers and effort levels.
   - `test_the_provider_table_lists_each_provider_the_code_defines_and_no_other`: the provider table
     of [the configuration reference](configuration.md) lists each provider the code defines and no
     other, with its settings and secret fields.
   - `test_the_page_names_each_runtime_that_reports_usage_limits`: [the configuration
     reference](configuration.md) names each runtime that reports usage limits.
   - `test_the_key_tables_list_each_key_the_code_defines_and_no_other`: the key tables of [the
     configuration reference](configuration.md) list each key the code defines and no other.
   - `test_the_stage_table_lists_each_stage_key_the_code_defines_and_no_other`: the table of a
     stage's keys in [the configuration reference](configuration.md#stages) lists each key
     `nexkit/config.py` accepts and no other.
   - `test_the_cache_field_list_names_each_cache_key_the_code_defines_and_no_other`: the list of a
     cache entry's fields in [the configuration reference](configuration.md#caches) names each field
     `nexkit/config.py` accepts and no other.
   - `test_the_pages_list_each_secret_an_older_workflow_can_pass_and_no_other`: the configuration
     reference and [troubleshooting](troubleshooting.md) list each secret an older workflow can
     pass, and no other.
   - `test_each_key_row_gives_the_code_default_and_range`: each key row of [the configuration
     reference](configuration.md) gives the default and range the code has.
   - `test_the_complete_configuration_lists_each_key_the_code_defines_and_no_other`: the complete
     configuration in [the configuration reference](configuration.md) lists each key the code
     defines and no other.
   - `test_the_complete_configuration_holds_each_default_it_says_it_does`: the complete
     configuration in [the configuration reference](configuration.md) holds each key's default from
     the code, except `setup` and `checks`.
   - `test_the_workflow_file_table_lists_each_trigger_of_the_template`: the trigger table of [the
     workflow file](configuration.md#workflow-file) lists each trigger of
     `nexkit/templates/workflow.yml` and no other.
   - `test_the_app_upgrade_lists_each_workflow_permission_of_the_template`: step 5 of the App
     upgrade in [the configuration reference](configuration.md) lists each workflow permission of
     the template, with its level.
   - `test_the_page_lists_each_refusal_of_the_code_and_no_other`: the refusals of `unaffected_by`
     that [the checks page](checks.md#checks-that-a-change-cannot-affect) lists are each refusal of
     the code and no other.
   - `test_the_code_refuses_each_listed_pattern`: the code refuses each refusal that the checks page
     lists.
   - `test_the_field_list_names_each_check_key_the_code_defines_and_no_other`: the list of a check's
     fields on [the checks page](checks.md#checks) lists each key the code defines and no other.
   - `test_the_page_names_the_limits_of_check_heads`: the list under `NEXKIT_CHECK_VERIFIED_SHA` on
     [the checks page](checks.md#skipping-work-that-a-change-cannot-affect) names the limits of 50
     checks and 3 commands that the code sets.
   - `test_the_settings_block_lists_each_autopilot_key_the_code_defines_and_no_other`: the autopilot
     settings block on [the autopilot page](autopilot.md) lists each key the code defines and no
     other.
   - `test_the_settings_block_holds_each_default_but_enabled`: the same block holds each default the
     code has, except `enabled`, which the block turns on.
   - `test_the_page_has_a_section_for_each_part`: [the architecture page](architecture.md) has a
     section for each part: the package, the workflows and their jobs, tokens and secrets, state,
     the engine and its hosts, and the plugin.
   - `test_each_job_has_a_row_with_its_tokens_and_secrets`: the job table of [the architecture
     page](architecture.md) has a row for each job of the three pipeline workflows, with its tokens
     and secrets.
   - `test_each_module_has_a_row`: the module table of [the architecture page](architecture.md) has
     a row for each module of `nexkit/`.
   - `test_the_job_diagram_shows_each_job_of_the_pipeline_and_the_order_it_waits_in`: the job
     diagram of [the architecture page](architecture.md) shows each job of `pipeline.yml`, and each
     arrow is a `needs:` of the job after it.
   - `test_the_run_table_has_a_row_for_each_job_of_the_pipeline`: the job table of [how it
     works](how-it-works.md#one-run-ten-jobs) has a row for each job of `pipeline.yml`, with the
     permissions its rows name and the number in its heading.
   - `test_each_decision_file_follows_the_template`: each file under `decisions/` follows the
     template.
   - `test_the_index_has_one_row_for_each_decision_file`: the [index](decisions.md) has one row for
     each decision file, and each row matches its file.
   - `test_each_decision_is_linked_from_a_rule_or_a_page`: a rule or a page links each decision.
   - `test_the_links_of_the_maintenance_skills_resolve`: the links of the maintenance skills under
     `.claude/skills/` resolve.
   - `test_each_task_of_nexkit_maintain_links_a_delivery_section`: each task of the
     `nexkit-maintain` skill links a section of [Delivery](delivery.md).
   - `test_the_plan_layout_lists_the_sections_the_code_writes`: the plan layout of
     [stages](stages.md) lists the sections that `publish.render_plan` writes.
   - `test_the_review_layout_lists_the_parts_the_code_writes`: the review layout of
     [stages](stages.md) lists the parts that `report.review_body` writes.
   - `test_each_value_a_page_repeats_is_its_constant`: each value that [agents and
     profiles](agents-and-profiles.md), [how it works](how-it-works.md), [work without a
     command](work-without-a-command.md) and [failure handling](failure-handling.md) repeat equals
     its constant in the code.
   - `test_the_page_lists_the_secret_words_and_names_the_code_has`: the words and names of the
     redaction list in [how it works](how-it-works.md#what-is-redacted) are those of
     `SECRET_WORDS` and `SECRET_NAMES` in `nexkit/redact.py`, and no other.
   - `test_the_page_lists_each_token_shape_the_code_redacts_and_no_other`: the token shapes that
     the same list names each have a sample that the test redacts, and no other shape is named.
   - `test_the_code_redacts_each_listed_shape_and_each_pattern_has_one`: `nexkit/redact.py`
     redacts a sample of each shape the list names, and each of its patterns redacts one of them.
   - `test_each_page_lists_the_words_of_failure_line_and_no_other`: the words that [agents and
     profiles](agents-and-profiles.md) and [failure handling](failure-handling.md#failure-handling)
     list as marking a failing line are those of `FAILURE_LINE`.
   - `test_the_page_lists_the_check_jobs_and_no_other`: the jobs in which
     [autopilot](work-without-a-command.md#autopilot) stops a round are those of `CHECK_JOBS`, and
     no other.
   - `test_the_failure_table_lists_the_retry_delays_the_code_has`: the retry delays that [failure
     handling](failure-handling.md#failure-handling) give for `publish` and for every other GitHub
     API call are those of `publish.WAITS` and `github.GitHub.waits`.
   - `test_the_failure_table_counts_the_tries_the_code_makes`: the counts of tries that the same
     table gives for those calls are the code's.
   - `test_the_access_table_lists_the_read_only_tools_and_no_other`: the tools that [agents and
     profiles](agents-and-profiles.md#agents) give the plan and review stages are those of
     `READ_ONLY_TOOLS`.
   - `test_the_skill_lists_each_issue_state_the_code_sets_and_no_other`: the issue states that [the
     status skill](../skills/status/SKILL.md) lists are those that `issue_item` in
     `nexkit/status.py` sets.
   - `test_the_skill_lists_each_autopilot_state_the_code_gives_and_no_other`: the autopilot states
     that the status skill lists are those that autopilot's steps take in `nexkit/autopilot.py` and
     `nexkit/host.py`.
   - `test_the_skill_lists_each_pull_request_state_the_code_gives_and_no_other`: the pull request
     states that the status skill lists are those that `pull_state` and `pull_item` give.
   - `test_the_page_lists_each_outcome_an_approval_merges_after_and_no_other`: the outcomes after
     which a writer's approval merges, which [work without a
     command](work-without-a-command.md#work-without-a-command) lists, are those of `APPROVABLE` in
     `nexkit/maintain.py`.
   - `test_the_title_table_lists_each_title_the_code_writes_and_no_other`: the table of CI issue
     titles on [the autopilot page](autopilot.md) shows the titles that `ci_title` and `red_title`
     write, for each kind and form, and no other.

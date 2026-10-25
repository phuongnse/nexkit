---
name: request
description: Turn an idea into small, well-scoped GitHub issues that the NexKit pipeline can plan and implement, with the sub-issues, blocked-by relationships and milestone that NexKit's autopilot reads. Use when the user wants to request a change, file work for NexKit, break a large feature or a milestone into issues, or check whether work is ready to start.
---

# Request a change through NexKit

NexKit implements one issue per agent session, so issue size decides whether a run
succeeds. Help the user write issues that one session can finish and test.

1. **Understand the change.** Ask what should behave differently and how they would check
   it. Read the relevant code so the issue names real files, commands and behaviour.

2. **Size it.** A good issue changes one behaviour, touches a handful of files and can be
   verified by the project's tests. If the idea is bigger, split it into parts, each useful
   on its own. Prefer several small issues over one large one. Order the parts so that
   each one leaves the default branch working when it merges alone, because each part
   merges as soon as it is done. The order you list them in is not a dependency; step 5
   says when one part waits for another.

3. **Write each issue** with:
   - A title that states the outcome ("Reject empty passwords at sign-up").
   - **Context**: what exists today, with file paths when known. Name the parts of the
     system and the concepts the change touches in the issue's own words, even when you
     link the docs that describe them: with profiles, triage reads only the issue and its
     discussion, never the links, to choose the issue's models.
   - **Expected behaviour**: concrete and observable.
   - **Acceptance criteria**: two to five checks that a test in the change or one of the
     project's checks can show. NexKit's review reads the code and the check results and
     runs nothing, so steps such as "passes five runs in a row" or a manual probe are not
     criteria. For a flaky failure, ask for a test that reproduces the cause every time.
     An issue that covers every case of something, such as every edit or every widget,
     has a criterion whose test finds the cases from the code itself, not from a fixed
     list, and fails on a case that is not covered. So a case that a later issue adds
     cannot be left out unnoticed.
     When the work may turn out to be unneeded, such as a failure that may be fixed
     already, say when to stop with no change ("If a commit already fixed it, close the
     issue with that evidence"): NexKit then closes the issue with its finding.
     Only code behaviour; never process steps such as approvals or PR text.
   - **Out of scope**, when helpful.

4. **Check autopilot.** Read `.nexkit/config.json`. When `autopilot.enabled` is true,
   NexKit takes the open issues of the earliest open milestone (`autopilot.scope`
   `"milestone"`, the default), or every open issue (`null`), by milestone in due-date
   order, then those without one. Give each issue its milestone; ask the user which
   milestone when that is not clear
   (`gh api repos/{owner}/{repo}/milestones --jq '.[].title'`). With `null`, an issue
   starts as soon as it is open and nothing open blocks it, so open only the issues that
   may start, and give each the *blocked by* relationships it needs. With or without
   autopilot, the pull request NexKit opens for an issue gets that issue's labels and
   milestone, so give each issue the ones its change should carry.

5. **Decide what waits for what.** Mark an issue *blocked by* another open issue when, and
   only when:
   - its acceptance criteria check behaviour that the other issue delivers, for example
     each step of a journey and the step before it; or
   - it needs something new that the other issue adds: code, an API, a schema, a test
     helper, or a lint rule that its checks will meet. When two open issues need the same
     new thing, one builds it and the other waits for it, so it is built once.

   Touching the same files, or a tidier order, is no reason to wait: NexKit merges the
   base branch and resolves conflicts by itself. Nor is adding a new case of something
   that another issue covers in every case: that issue's test finds the new case, so
   whichever of the two merges last fails its checks until it covers it. Keep the marks
   this narrow because a mark that is not needed only slows the work, while a missing one
   lets two issues build the same thing without any conflict to show it.

   Check each new issue against every open issue in autopilot's scope, or every open
   issue without autopilot, not only against the other parts of this request
   (`gh issue list --state open --limit 500`). Give each mark its own line in the issue
   text, `Depends on #N: <what this issue uses from #N>`, so a reader can check every mark
   against its reason. A mark that no line explains is not made. An issue that needs
   something new that no open issue adds, and that the code does not have yet, is not
   opened until an issue for that thing exists; with autopilot, opening an issue starts
   it. The marks autopilot makes when it splits a plan need no line.

6. **Check that the work is ready,** when you break down a milestone or a large request.
   Before creating any issue, sort each thing the work needs into one of three:
   - already in the code;
   - being built by an open issue, which the new issue waits for;
   - built by nobody, which this work must include or wait for.

   Report a verdict to the user before creating anything: *ready*; *ready with waits*, and
   which issues wait for which; or *not ready*, and what has to exist first.

7. **Show the drafts** with the structure you will create: the parent, its parts in order,
   which issue is blocked by which and why, and the milestone or labels. Adjust them with
   the user.

8. **Create the issues and their structure.** NexKit and its autopilot read GitHub's own
   relationships, never text such as "Depends on #12", so create the real ones:
   - Each issue: `gh issue create --title ... --body-file ... [--milestone ...] [--label ...]`.
   - For a split, a parent issue that states the whole outcome, then each part added to it
     as a sub-issue, in order. The API takes the part's database id, not its number:

     ```sh
     id=$(gh api repos/{owner}/{repo}/issues/<part> --jq .id)
     gh api -X POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id="$id"
     ```

   - A *blocked by* relationship for each mark that step 5 allows, again by the blocking
     issue's database id. Issues without one can run at the same time. With autopilot,
     opening an issue starts a sweep, which plans an issue that nothing blocks, so add an
     issue's relationships right after you create it:

     ```sh
     id=$(gh api repos/{owner}/{repo}/issues/<blocking issue> --jq .id)
     gh api -X POST repos/{owner}/{repo}/issues/<issue>/dependencies/blocked_by -F issue_id="$id"
     ```

   If either endpoint returns 404, the `gh` version is too old for it: upgrade `gh`.

   Write the `Depends on #N: …` line next to the relationship, never in place of it:
   NexKit reads only the relationship. Do not block a part only because it comes later
   in the list. These are guidelines for good issues; NexKit does not check how issues
   are written.

9. **Start the pipeline.** With autopilot on, do nothing: autopilot plans each issue as
   soon as nothing blocks it, starts `/nexkit go` on a plan that needs nobody, and the
   user does not comment `/nexkit plan`. Without autopilot, and only if the user wants to,
   comment `/nexkit plan` on each issue that nothing blocks
   (`gh issue comment <n> --body "/nexkit plan"`). The plan appears as a comment; a
   collaborator reviews it and comments `/nexkit go` to implement it.

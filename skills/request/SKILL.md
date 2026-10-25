---
name: request
description: Turn an idea into small, well-scoped GitHub issues that the NexKit pipeline can plan and implement, with the sub-issues, blocked-by relationships and milestone that NexKit's autopilot reads. Use when the user wants to request a change, file work for NexKit, or break a large feature into issues.
---

# Request a change through NexKit

NexKit implements one issue per agent session, so issue size decides whether a run
succeeds. Help the user write issues that one session can finish and test.

1. **Understand the change.** Ask what should behave differently and how they would check
   it. Read the relevant code so the issue names real files, commands and behaviour.

2. **Size it.** A good issue changes one behaviour, touches a handful of files and can be
   verified by the project's tests. If the idea is bigger, split it into parts, each useful
   on its own. Prefer several small issues over one large one. For each part, note which
   other parts it really needs first: a part needs another only when it builds on that
   part's code or data. The order you list them in is not a dependency.

3. **Write each issue** with:
   - A title that states the outcome ("Reject empty passwords at sign-up").
   - **Context**: what exists today, with file paths when known.
   - **Expected behaviour**: concrete and observable.
   - **Acceptance criteria**: two to five checks that a test in the change or one of the
     project's checks can show. NexKit's review reads the code and the check results and
     runs nothing, so steps such as "passes five runs in a row" or a manual probe are not
     criteria. For a flaky failure, ask for a test that reproduces the cause every time.
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

5. **Show the drafts** with the structure you will create: the parent, its parts in order,
   which part is blocked by which, and the milestone or labels. Adjust them with the user.

6. **Create the issues and their structure.** NexKit and its autopilot read GitHub's own
   relationships, never text such as "Depends on #12", so create the real ones:
   - Each issue: `gh issue create --title ... --body-file ... [--milestone ...] [--label ...]`.
   - For a split, a parent issue that states the whole outcome, then each part added to it
     as a sub-issue, in order. The API takes the part's database id, not its number:

     ```sh
     id=$(gh api repos/{owner}/{repo}/issues/<part> --jq .id)
     gh api -X POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id="$id"
     ```

   - A *blocked by* relationship only where a part really needs another part first, again
     by the blocking issue's database id. Parts without one can run at the same time:

     ```sh
     id=$(gh api repos/{owner}/{repo}/issues/<needed part> --jq .id)
     gh api -X POST repos/{owner}/{repo}/issues/<part>/dependencies/blocked_by -F issue_id="$id"
     ```

   Do not write "Depends on #x" in place of a relationship, and do not block a part only
   because it comes later in the list. These are guidelines for good issues; NexKit does
   not check how issues are written.

7. **Start the pipeline.** With autopilot on, do nothing: autopilot plans each issue as
   soon as nothing blocks it, starts `/nexkit go` on a plan that needs nobody, and the
   user does not comment `/nexkit plan`. Without autopilot, and only if the user wants to,
   comment `/nexkit plan` on each issue that nothing blocks
   (`gh issue comment <n> --body "/nexkit plan"`). The plan appears as a comment; a
   collaborator reviews it and comments `/nexkit go` to implement it.

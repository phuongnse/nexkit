---
name: request
description: Turn an idea into small, well-scoped GitHub issues that the NexKit pipeline can plan and implement. Use when the user wants to request a change, file work for NexKit, or break a large feature into issues.
---

# Request a change through NexKit

NexKit implements one issue per agent session, so issue size decides whether a run
succeeds. Help the user write issues that one session can finish and test.

1. **Understand the change.** Ask what should behave differently and how they would check
   it. Read the relevant code so the issue names real files, commands and behaviour.

2. **Size it.** A good issue changes one behaviour, touches a handful of files and can be
   verified by the project's tests. If the idea is bigger, split it into an ordered list
   of issues, each useful on its own. Prefer several small issues over one large one.

3. **Write each issue** with:
   - A title that states the outcome ("Reject empty passwords at sign-up").
   - **Context**: what exists today, with file paths when known.
   - **Expected behaviour**: concrete and observable.
   - **Acceptance criteria**: two to five checks that a test can verify. Only code
     behaviour; never process steps such as approvals or PR text.
   - **Out of scope**, when helpful.

4. **Show the drafts** and adjust them with the user. Then create them with
   `gh issue create --title ... --body-file ...`.

5. **Start the pipeline** only if the user wants to: comment `/nexkit plan` on the issue
   (`gh issue comment <n> --body "/nexkit plan"`). The plan appears as a comment;
   a collaborator reviews it and comments `/nexkit go` to implement it.

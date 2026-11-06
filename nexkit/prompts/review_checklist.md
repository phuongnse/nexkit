
- `checklist`: one entry for each item of the repository's checklist below, every one of
  them:
  - `number`: the item's number.
  - `answer`: `met` when the diff or the check results show that the change meets it;
    `not_met` when they show that it does not; `not_applicable` when the item does not
    concern this change.
  - `evidence`: what in the diff or the check results shows it, such as `file:line`
    references.

  Return `request_changes` when an item is `not_met`. NexKit turns each one into a blocking
  finding, so do not raise it as a finding as well.

The repository's checklist, which every pull request must meet:

$items

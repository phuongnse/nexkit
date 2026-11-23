## Merge of `$base`

The checks failed, and `$base` has moved since this branch last merged it. NexKit has
merged `$base` (commit $base_commit) into the branch, without committing it. It merged
without conflicts. The base branch may already fix some of the failing checks above, for
example a flaky test, so judge every failure on the merged code.

- Run the failing checks first and fix only what still fails. Of a slow check, run only
  the tests that failed.
- When nothing fails any more, change no files and say so in `summary`. NexKit then
  publishes the merge alone, and the next checks run on it.
- Leave the merge in place: do not run `git merge --abort`, `git commit` or `git reset`.
  NexKit publishes the merge with your changes.

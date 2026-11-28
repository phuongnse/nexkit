## Merge of `$base`

The checks failed, and `$base` has moved since $last_merged. NexKit has
merged `$base` ($version_kind $base_commit) $merge_into. It merged
without conflicts. The $base_kind may already fix some of the failing checks above, for
example a flaky test, so judge every failure on the merged code.

- Run the failing checks first and fix only what still fails. Of a slow check, run only
  the tests that failed.
- When nothing fails any more, change no files and say so in `summary`. NexKit then
  publishes the merge alone, and the next checks run on it.
- Leave the merge in place: $merge_undo
  NexKit publishes the merge with your changes.

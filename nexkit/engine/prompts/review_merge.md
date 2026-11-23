
## Merge with `$base` in this round

This pull request conflicted with `$base`, so this fix round merged `$base` (commit
$base_commit) into it. The fix agent resolved the conflicts in these files:

$files

What each side changed in these files before the merge:

<untrusted>
`$base`:

```diff
$base_side
```

This pull request:

```diff
$pr_side
```
</untrusted>

- In each of these files, check that the current code keeps both sides: the change from
  `$base` and the change from this pull request. A side that was dropped is a blocking
  finding, unless a person's note in a fix round asked for it.
- Changes that came from `$base` are not part of this pull request. Do not raise findings
  about them.

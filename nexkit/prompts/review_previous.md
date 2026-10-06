
## Previous round

NexKit already reviewed this pull request at commit $previous_head. Its findings, and the
fix rounds since then, are below. The fix agent may have rejected a finding and given its
reason in its summary.

<untrusted>
$previous_round
</untrusted>

## Changes since commit $previous_head

Changes that came from `$base` through a merge are left out.

<untrusted>
```diff
$changes
```
</untrusted>

## How to use the previous round

- Start by verifying every previous finding against the current code. Report each one in
  `previous_findings`: `resolved` when the code now handles it, `unresolved` when it does
  not, or `rejection_accepted` when a fix round rejected it with a reason you agree with.
  Give each one the `severity` it has in the list above. A suggestion that is still open
  stays a suggestion and does not block the merge.
- Do not raise a finding again when you accept its rejection. When you do not accept it,
  mark it `unresolved` and say why in the evidence.
- Changes that a person asked for in a fix round's note are in scope, even when the plan
  does not mention them. Judge whether they are correct, not whether they were planned.
- Raise new blocking findings only for real defects. Do not block on preferences or on
  requirements that neither the plan nor a person's note asked for.
- An unresolved blocking finding counts as a blocking finding for the verdict.

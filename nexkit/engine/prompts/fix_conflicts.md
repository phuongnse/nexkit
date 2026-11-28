## Conflicts with `$base`

This $change_kind no longer merges cleanly into `$base`. NexKit has started merging
`$base` ($version_kind $base_commit) $merge_into. These files have
conflicts:

$files

Resolve them in this round, together with the feedback above. When there is no other
feedback and no note, only resolve the conflicts.

- Keep the intent of both sides. $side_history
- First decide, for each conflict, whether it has one right answer or needs a choice.
  - One right answer: both sides added different things that can live together (keep
    both), or one side renamed something that the other side uses (use the new name in
    the other side's code). Resolve these yourself.
  - A choice: both sides changed the same value or behaviour in different ways (for
    example, one side sets a timeout to 10 seconds and the other to 60), or one side
    deleted code that the other side changed. Whatever you pick drops the intent of one
    side.
- For a choice, look for the answer in the plan, the $item_kind, the feedback and the note
  above. $side_note When none of them settles the
  choice, do not pick a side and do not guess: return `status: blocked` and list every
  such conflict in `open_conflicts`. NexKit then publishes nothing and asks a person to
  decide.
- A note from the person who requested this round settles the choice it describes.
  Follow it, and resolve the other conflicts yourself.
- Remove every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`). A file that one side
  deleted and the other side changed has no markers: keep it or delete it.
- Leave the merge in place: $merge_undo
  NexKit publishes the merge with your changes.

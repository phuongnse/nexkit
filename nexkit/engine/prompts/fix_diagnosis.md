## Diagnosis round

These checks failed the same way before and after the last fix round, so that round's
change did not reach the cause:

<untrusted>
$repeated
</untrusted>

This round finds the cause before it changes any code, then fixes that cause.

1. Find the cause first. Read the evidence: the check output above, the result files of
   the failing checks when there are any, and the code the failure points at. Run a
   failing check again only when the evidence does not show the cause. When it still
   does not, add logging or narrow the test in the working tree yourself and run it;
   remove what you added for this before you finish.
2. Write the cause in `cause`: what goes wrong and why, followed by the evidence that
   shows it, such as the trace, log or output lines, each with the file it comes from.
3. Then fix that cause in this session, and say in `summary` how the change addresses it.
   Do not try another variation of an earlier round's change without a cause that
   explains why it failed.
4. When you cannot find the cause, change no files, leave `cause` empty and return
   `status: blocked`. Say in `blocker` what the evidence shows and what is still unknown,
   and ask in `questions` only what neither the evidence nor a run of your own can show,
   such as which of two behaviours is intended.

NexKit rejects a result of this round that changes code without a cause.


## Diagnosis round

These checks failed the same way before and after the last fix round, so that round's
change did not reach the cause:

<untrusted>
$repeated
</untrusted>

This round finds the cause before it changes any code, then fixes that cause.

1. Find the cause first. Read the evidence: the check output above, the result files of
   the failing checks when there are any, and the code the failure points at. Run a
   failing check again only when the evidence does not show the cause.
2. Write the cause in `cause`: what goes wrong and why, followed by the evidence that
   shows it, such as the trace, log or output lines, each with the file it comes from.
3. Then fix that cause in this session, and say in `summary` how the change addresses it.
   Do not try another variation of an earlier round's change without a cause that
   explains why it failed.
4. When you cannot find the cause, change no files, leave `cause` empty and return
   `status: blocked`. Say in `blocker` what the evidence shows and what is still unknown,
   and ask in `questions` what a person must decide or provide, such as more logging or
   a smaller test that shows the failure.

NexKit rejects a result of this round that changes code without a cause.


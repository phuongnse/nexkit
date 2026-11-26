# List what waits for the owner

What waits for the owner is the work that NexKit does not carry on by itself, as
[NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) and
[Labels and milestones](../../../docs/delivery.md#labels-and-milestones) say. Change nothing: report
one list in four groups.

1. **Open issues with no label:** `gh issue list --state open --search 'no:label'`.
2. **Open issues with no milestone:** `gh issue list --state open --search 'no:milestone'`. An issue
   that no roadmap row names correctly has none, so ask the owner to check each against the
   milestone test of Delivery, and do not report one as wrong when it has its kind label.
3. **Issues that a hold stops.** Read the labels of `autopilot.labels` in `.nexkit/config.json`
   that are marked as holds, and list the open issues of each
   (`gh issue list --state open --label <label>`).
4. **Runs that wait for a person.** Run `bin/nexkit status --json` from the checkout and report
   its `needs_person` items with their reasons. Report too each issue of its `autopilot` list whose
   state is `held` or `waits`, with its reason. The [status skill](../../../skills/status/SKILL.md)
   says what each state means.

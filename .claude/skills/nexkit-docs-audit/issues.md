# The issues of the docs audit

The `plan` step of the [docs audit](SKILL.md) writes them, so this is what to expect, not what to
do. It opens them in this order:

- One issue per milestone whose close and roadmap row disagree, labelled `documentation`.
- One issue for each `unbuilt` finding, labelled `bug` and `documentation`, which asks the code to
  follow the rule, and one for each `untested-list` finding, labelled `documentation`, which asks
  for a test that compares the list with the code.
- One issue per page for its `wrong`, `built`, `missing` and `standard` findings, labelled
  `documentation`, with each finding as an acceptance criterion.
- One issue per doc past 10,000 words, labelled `documentation`, which asks to split it by topic
  into pages of at most 8,000 words. An open issue whose title names the page and a split counts
  as one, also when the audit did not open it.
- Each also carries `docs-audit` and no milestone, as
  [labels and milestones](../../../docs/delivery.md#labels-and-milestones) says, and NexKit's
  marker of the run that opened it on its first line, so autopilot takes it, since
  `autopilot.trusted_workflows` lists `docs-audit.yml`. Its last line is a hidden `docs-audit`
  marker of what it holds: the milestone, the page, or the finding with its statement normalised,
  so a later run opens no second issue while it is open.
- The agent's text in an issue mentions nobody and opens no HTML comment.

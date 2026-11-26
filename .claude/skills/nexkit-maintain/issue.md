# Write an issue

An issue for NexKit's own repository follows
[Labels and milestones](../../../docs/delivery.md#labels-and-milestones) and
[Issues and pull requests](../../../docs/delivery.md#issues-and-pull-requests). The plugin's
[request skill](../../../skills/request/SKILL.md) says how to size and write the text, which issues
it is *blocked by*, and how to create the sub-issues and the *blocked by* relationships with
`gh api`. Follow it, and add these steps.

1. **Pick the milestone** by the roadmap row whose *What it delivers* names what the issue builds
   (see [the roadmap](../../../docs/roadmap.md)). When no row names it, give no milestone, as the
   test in Delivery says.
2. **Pick the labels.** Give the label of the issue's kind, and every other label whose
   description fits it (`gh label list`).
3. **Decide whether NexKit may take it.** Autopilot on this repository takes every open issue the
   moment it opens, unless a label that `autopilot.labels` marks as a hold stops it. When NexKit
   must not take the issue, such as a change under `.github/` or `.nexkit/`, give it that hold
   label in the create command (`gh issue create --label ...`), never afterwards, and add its
   *blocked by* relationships right after.

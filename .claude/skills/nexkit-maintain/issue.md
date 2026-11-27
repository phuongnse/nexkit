# Write an issue

An issue for NexKit's own repository follows
[Issues and pull requests](../../../docs/delivery.md#issues-and-pull-requests). The plugin's
[request skill](../../../skills/request/SKILL.md) says how to size and write the text, which issues
it is *blocked by*, and how to create the sub-issues and the *blocked by* relationships with
`gh api`. Follow it, and add these steps.

1. **Give it its milestone and labels** as
   [Labels and milestones](../../../docs/delivery.md#labels-and-milestones) says. Take the rows of
   [the roadmap](../../../docs/roadmap.md) and the labels of `gh label list`.
2. **Decide whether NexKit may take it.** Autopilot on this repository takes every open issue the
   moment it opens, but not those with a hold label, as
   [NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) says. When
   NexKit must not take the issue, such as a change under `.github/` or `.nexkit/`, label it
   `hand-made` in the create command (`gh issue create --label hand-made ...`), never afterwards,
   since autopilot may take it as it opens. Then add its *blocked by* relationships right after.
   A session takes it as [Make a hand-made change](hand-made.md) says.

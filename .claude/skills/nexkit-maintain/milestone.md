# Break a milestone down

A milestone is broken down into the issues that its roadmap row delivers, each with the milestone
that [Labels and milestones](../../../docs/delivery.md#labels-and-milestones) gives it: a part of a
split keeps its parent's.

1. Read the milestone's row in [the roadmap](../../../docs/roadmap.md), then its open issues
   (`gh issue list --milestone <title> --state open`), so no part repeats one that exists.
2. Check that the work is ready, and decide what waits for what, as steps 5 and 6 of the
   [request skill](../../../skills/request/SKILL.md) say.
3. Show the owner the drafts: the parts in order, which part is blocked by which and why. Create
   nothing until the owner agrees.
4. Create each part as [Write an issue](issue.md) says, with its *blocked by* relationships right
   after it.

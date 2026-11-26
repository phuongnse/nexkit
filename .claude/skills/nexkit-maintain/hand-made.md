# Make a hand-made change

A hand-made change is one that NexKit's agents may not make, such as a change under `.github/` or
`.nexkit/`. A session outside NexKit takes only `hand-made` issues, as
[NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) says.

1. **Take the issue.** Take the next one as
   [Issues and pull requests](../../../docs/delivery.md#issues-and-pull-requests) says, unless the
   owner names one.
2. **Branch and make the change.** Run the commands of *Verify* in
   [AGENTS.md](../../../AGENTS.md) before you open the pull request.
3. **Open the pull request** with one closing keyword for the issue. Name the doc sections the change
   touches, or say why it touches none, as
   [How the docs stay correct](../../../docs/writing-docs.md#how-the-docs-stay-correct) says. Give it
   the issue's labels except the holds, and its milestone, as
   [Labels and milestones](../../../docs/delivery.md#labels-and-milestones) says.
4. **Never merge.** Leave the pull request to the owner.

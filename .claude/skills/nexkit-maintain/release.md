# Release a version

A release follows [Releases](../../../docs/delivery.md#releases). The session prepares the release
pull request and stops there. It never merges, pushes a tag or runs `gh release create`, because an
agent does the work and [NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository)
leaves those steps to the owner.

1. Open the release pull request with every item that
   [Releases](../../../docs/delivery.md#releases) lists for it. Go through the whole list, since some
   items apply only to some releases.
2. Stop. Give the owner the steps that remain, as a command to run after the merge, with the
   release notes written to a file:

   ```sh
   gh release create vX.Y.Z --target <merge commit> --title 'NexKit X.Y.Z' --notes-file <file>
   ```

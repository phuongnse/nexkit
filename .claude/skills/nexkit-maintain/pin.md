# Move the pin

NexKit's own workflow is pinned to a release, and only the owner moves the pin, once the release
runs well elsewhere, as
[NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) says. The change
is under `.github/`, so a session makes it as a hand-made change.

1. On the owner's word that the release runs well, open the pull request that changes the
   `uses:` ref of `pipeline.yml` in `.github/workflows/nexkit.yml` to the release's tag. Follow
   [Make a hand-made change](hand-made.md).
2. Apply each `### Upgrade` step of `CHANGELOG.md` between the old and new tags to
   `.nexkit/config.json` and the workflow.
3. Never merge, push a tag or run `gh release create`. Hand the merge to the owner.

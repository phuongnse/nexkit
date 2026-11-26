# Move the pin

NexKit's own workflow is pinned to a release, and only the owner moves the pin, once the release
runs well elsewhere, as
[NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) says. The change
is under `.github/`, so a session makes it as a hand-made change.

1. On the owner's word that the release runs well, follow
   [Make a hand-made change](hand-made.md) and upgrade `.github/workflows/nexkit.yml` to the
   release's tag, as
   [Upgrading from earlier releases](../../../docs/configuration.md#upgrading-from-earlier-releases)
   says. Pin every ref it names, `nexkit_ref` among them. Carry out the upgrade steps of each
   release after the pinned one, wherever they act. Then run `nexkit doctor`.
2. List in the pull request each upgrade step that a session cannot carry out, such as accepting
   an App permission on the installation or stopping a scheduled session, for the owner.
3. Hand the merge to the owner, as
   [NexKit on this repository](../../../docs/delivery.md#nexkit-on-this-repository) says.

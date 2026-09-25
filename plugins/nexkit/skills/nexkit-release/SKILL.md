---
name: nexkit-release
description: Prepare a specific NexKit release candidate and request the authorized human's release decision, separately from feature delivery.
---

# NexKit release

Read this consumer's release configuration and merged changes. Prepare version
source changes through normal approved delivery before selecting the candidate;
never add a version commit after its release approval. Draft concise release notes
from actual changes and inspect the candidate's checks. Do not treat default-branch
HEAD at some future execution time as the chosen candidate.

Run `nexkit release --commit <full-sha> --version <version> --notes-file <file>`.
This queues an immutable commit, version, notes and configuration digest for a
release issue. Use the returned `nexkit intake-status --operation release --key
<key>` to find it, then `nexkit approval <issue>` to get the exact command. Show
the human that issue and its `/nexkit release <hash>` command.
Only the authorized human posts that approval; never post it for them.

After that single decision, Actions verifies/builds the approved source, records
artifact hashes, creates the matching tag and publishes by the configured flow.
On a partial failure, inspect state/assets and resume the same candidate; never
move a released tag, overwrite differing bytes or silently select another commit.
Release does not grant production deployment authority.

# D17. The state document changes in place until the engine is stable

Status: **Proposed**

## Context

The engine's state document has a schema version of its own. Each time it changed, the change
needed a rule of its own: an in-place edit with a tolerance for the old shape, or a new schema
version with a step that moves an old document to it. Every such step adds code, golden documents
and conformance edits that live until the engine is stable, and no release of the engine holds the
documents that they serve. The only stored documents are those of NexKit 6, on the repositories
that run it, and a host can build the state of an item again from NexKit 6's markers.

## Decision

- Until NexKit 7.0, the state document has one schema version, which changes in place. The engine
  reads a stored document as it is and refuses one its schema refuses, a newer one included.
- A host builds again the state of an item whose stored document no longer fits. The GitHub host
  imports the item from NexKit 6's markers, posts a new state comment, leaves the old one and reads
  the new one from then on. A document of a later schema version, one that does not decode and
  a comment that someone else changed still stop the item.
- The release of 7.0 ends this rule. It removes the rule, and every note about NexKit 6 or the time
  before 7.0, from the docs, the decisions and the code. From then on, each change to the state
  document raises its schema version, with a step from the one before.

The detail is in [Versions](../engine.md#versions) and [State](../how-it-works.md#state).

## Consequences

- A change to the state document adds no upgrade code, and no golden for an old version.
- An item whose state comment no longer fits loses that comment's cursor. The import takes what
  came after the new comment, as on a first import.
- A stored document that the schema refuses is never repaired in place. A rolled-back engine finds
  a newer document and stops, as before.
- At 7.0, the rule's code and notes are removed, and the first stable schema is the base of every
  later step.

## Alternatives

- **A new schema version and an upgrade step for each change.** Each version is read for as long as
  its documents may exist, so each step stays in the code, with goldens and conformance cases,
  although no release holds those documents.
- **A tolerance for each old shape in the reader.** The reader keeps every field that was ever
  removed or renamed, and the schema no longer says what a document holds.

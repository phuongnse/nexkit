# D10. An engine decides, hosts carry out

Status: **Agreed**

## Context

The same loop runs wherever an agent changes something for a person: a plan, a change, checks, a
review and an accept. Two copies of that loop would drift apart, and each fix would have to be
made twice.

## Decision

- The engine decides what happens next to a work item.
- A host decides where the work lives and how each step is carried out. It stores the work, sends
  events to the engine, carries out the engine's answers and tells people.
- The engine's answers follow the JSON Schemas of its protocol, and conformance cases check them.

The detail is in [The engine: Why](../engine.md#why) and
[The engine and its hosts](../architecture.md#the-engine-and-its-hosts).

## Consequences

- A rule of the loop is changed once, and every host follows it.
- A host must send the events that the engine needs. What exists only on its platform reaches the
  engine as an event or a result.
- Each new host needs its own tests of how it carries out the engine's answers.

## Alternatives

- Each host has its own loop. Each host then fits its platform exactly, but the loops drift apart.

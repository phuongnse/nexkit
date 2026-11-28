# D15. The engine assumes no host and no kind of change

Status: **Agreed**

## Context

[D10](d10-an-engine-decides-hosts-carry-out.md) splits the loop into an engine that decides and
hosts that carry its decisions out. Splitting the decisions is not enough while the engine still
speaks one host's language. A contract whose fields, prompts and explanations assume issues, pull
requests, git and CI holds every other host to that shape. Take a host whose work item is a request
in an application, and whose change is records in the application's database that an agent edits
through the application's own tools. It then fits the protocol only by faking what the protocol
asks for. Once the protocol is 1.0, a field that assumes one kind of change can change only in a
new major version.

The engine was also built by moving decisions out of an earlier implementation, and its code and
pages explained each rule by pointing at that implementation. Those pointers explain nothing once
that implementation is gone.

## Decision

- The engine, its protocol and its pages use the engine's own words, as
  [Words](../engine.md#words) and the pages it leads to define them: work item, change, version,
  stage, round, check, review, accept, person and host. They name no host, no product that uses
  the engine and no implementation that the engine was built from.
- The protocol assumes no kind of change. A change may be files in a repository or records in an
  application's database. Where a finding points, how a change is shown and what an agent may not
  change are the host's terms, which the engine keeps and passes back without reading them.
- What belongs to one host reaches the engine as the host's input: its words for the work item
  and the change, and how an agent sees and edits the change in a session. The engine's prompts
  hold each stage's contract: what the stage must do, the rules it judges by and the result it
  gives.
- Each rule of the engine is explained by its own reason, never by what an earlier implementation
  did. What served only to build the engine goes when the engine is done, and the engine's own
  checks then guarantee what it guaranteed.
- A host's pages say how its platform maps onto the engine's words.

The detail is in [The engine](../engine.md), [Hosts](../engine-hosts.md#hosts) and
[Writing the docs](../writing-docs.md#reference-pages).

## Consequences

- A host whose change is not files, such as an application that edits its own records, uses the
  protocol as it is, with no field to fake.
- A second host in the tests, whose change is records, carries work items through the engine,
  prompts included. It fails where the engine assumes files, git or one platform, so the tests see
  the engine as such a host does.
- The GitHub host gives its own words and workspace to every prompt, so the prompts that its
  agents read stay as they are.
- A stage whose agent needs a platform's detail, such as a branch or a CI run, gets it from the
  host's input. A new kind of evidence therefore needs a new input, not a new sentence in a
  template.
- A rule's reason must be written out in the engine's terms, which takes more words than a pointer
  to the code it came from.

## Alternatives

- **A list of words that a test forbids in the engine.** It catches words, not assumptions: a
  field named `file` passes it, while an ordinary word such as "issued" fails it and asks for an
  exception. When a host's terms reach the engine only as the host's input, no such list is
  needed.
- **A protocol for code changes now, and one for other changes in a later major version.** Hosts
  would build on a contract known to be too narrow, and then move once more.
- **Prompts written wholly by each host.** Each host could word its prompts freely, but each stage's
  rules and result contract would be copied into every host and drift apart, which D10 rules out.

# D12. A host runs the engine as a program or over HTTP

Status: **Agreed**

## Context

[D10](d10-an-engine-decides-hosts-carry-out.md) says the engine decides and a host carries out
the decisions. A host can be written in any language and run on its own platform. How a host
reaches the engine is therefore hard to change once hosts depend on it.

## Decision

- A host never imports the engine's code.
- A host runs `nexkit-engine <call>` with one JSON request on standard input. The engine writes
  one JSON answer on standard output.
- Or a host runs `nexkit-engine serve` and sends the same JSON requests over HTTP. It gets the
  same JSON answers.
- A host in any language can run the engine. The engine could be written again in another
  language without a change to any host, as long as the conformance suite passes.
- Both ways use only Python's standard library, as [D8](d8-standard-library-only.md) requires.

The detail is in [The parts](../engine.md#the-parts).

## Consequences

- `serve` does not check who calls it. A host must put it where only that host can reach it.
- A `prompt` request over the HTTP size limit goes to `nexkit-engine prompt` on standard input,
  which has no size limit. Other calls over the limit are refused.
- On standard input, a host starts one process for each call. `serve` avoids that cost, so a
  host that makes many calls can run it instead.

## Alternatives

- **A library imported into the host's process.** It ties every host to Python. A failure in the
  engine then becomes a failure of the host.
- **gRPC.** It needs protobuf, generated code and a library, and D8 rules those out. The
  protocol's JSON Schemas already give the contract. The calls are short requests and answers,
  with nothing to stream.
- **A message broker.** The engine keeps no state. Queueing, locking and durability stay with the
  host, so a broker adds infrastructure and does no work the host needs.
- **WebAssembly.** Python compiled to WebAssembly is heavy. An engine written in another language
  could add it later, since the protocol names no language.
- **A Unix socket.** It works, but TCP on a private network is simpler in containers and
  Kubernetes.

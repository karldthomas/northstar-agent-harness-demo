# ADR 0002: Keep model orchestration in managed Harness

## Decision

Northstar will call the managed AgentCore Harness through an adapter. The
application owns simulator state, authorization, approvals, idempotency,
events, and verification, but it will not implement a model/tool/model loop.

The Phase 0 Harness boundary is a small, local request contract and dry-run
check. The deployed smoke command remains an explicit lifecycle hook in the
existing deployment orchestrator and will be wired to the selected Harness
CLI/SDK after the target account, region, and supported API are confirmed.

## Consequences

The project can test the contract locally without pretending that a cloud
Harness is available. The first cloud gate must prove managed orchestration,
tool invocation, and trace visibility before later simulator or UI phases are
called complete.

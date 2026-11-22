# Northstar Gateway target

`northstar-tools.openapi.json` is the source contract for the first
AgentCore Gateway target. It exposes only `GET` operations against the
run-scoped simulator API; no mutation endpoint is included in this phase.

AgentCore Gateway derives the callable name from the target name and
`operationId`, so the Harness allow-list uses names such as
`northstar-simulator___check_service_health`. The local OpenAPI server URL is
only a contract placeholder. The cloud deployment must replace it with the
authenticated HTTPS endpoint produced by the simulator stack and bind the
target with the deployment context before the Harness smoke test runs.

The backend remains authoritative for run ownership, validation, and state.
Gateway exposure alone is not an authorization grant.

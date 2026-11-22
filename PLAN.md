# Northstar IT Operations

## Technical Implementation Plan and Delivery Phases

**Version:** 1.6  
**Date:** 2026-11-21  
**Status:** In progress  
**Companion:** Northstar IT Operations Business Requirements Document v1.0

## 1. Purpose and architectural decision

Build a web-based demonstration of an autonomous IT operations agent working against a **fully synthetic, stateful enterprise environment**. Real AWS AgentCore Harness execution and tool calls will operate against simulated devices, services, deployments, incidents, and approvals. No production infrastructure will be modified.

**Non-negotiable distinction from LedgerFlow:** use **Amazon Bedrock AgentCore Harness** as the managed model/tool reasoning loop. Do not recreate a custom `while model -> tool -> model` loop in the application. Application services still own authentication, authorization, synthetic state, approvals, idempotency, event delivery, and independent outcome evaluation.

### 1.1 Target architecture

```text
React / TypeScript web app
  |-- Scenario library and fault injection controls
  |-- Agent chat and execution-stage timeline
  |-- Device fleet / service dependency digital twin
  |-- Approval center and evaluation dashboard
  |
  +--> Application API (Python FastAPI)
         |-- Auth/session/tenant and scenario-run ownership
         |-- InvokeHarness adapter + response streaming
         |-- Scenario loader / reset / fault scheduler
         |-- Approval service + policy enforcement
         |-- Simulator tools + authoritative state mutations
         |-- Event stream (SSE initially; WebSocket optional)
         |-- Evaluation runner / reporting
         |
         +--> Amazon Bedrock AgentCore Harness
         |      |-- Configured model, instructions, limits
         |      |-- AgentCore Memory and Agent Skills
         |      |-- Gateway tools to simulator API
         |      +-- Lifecycle hooks for policy / telemetry
         |
         +--> DynamoDB (run-scoped simulator state, operations, approvals)
         +--> S3 (scenario fixtures, KB documents, skill packages, artifacts)
         +--> CloudWatch / AgentCore Observability
```

**Important:** this is a conceptual component layout, not a requirement that every component be deployed on day one. Start locally for the simulator and frontend; add AWS integration incrementally. Never let the browser invoke AWS control-plane credentials or perform authoritative mutations directly.

### 1.2 Responsibility boundaries

| Owner             | Responsibilities                                                                | Explicitly not responsible for                                  |
| ----------------- | ------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| AgentCore Harness | Model reasoning loop, tool selection, context handling, agent session execution | Business authorization or source-of-truth state                 |
| AgentCore Gateway | Exposing approved simulator tools                                               | Deciding whether a specific user may mutate a specific resource |
| Application API   | Authentication, run/session routing, approvals, action validation, event stream | Reimplementing the model's reasoning loop                       |
| Simulator         | Deterministic operational rules, authoritative synthetic state, fault injection | Generating claims about successful resolution                   |
| Evaluation runner | Comparing actual states/actions with scenario ground truth                      | Trusting the agent's self-assessment                            |
| Web UI            | Rendering real server events, approval controls, replay, diagnostics            | Fabricating successful tool or state transitions                |

## 2. Proposed stack

- **Agent orchestration:** Amazon Bedrock AgentCore Harness, configured through AgentCore CLI or SDK.
- **Model:** Bedrock-supported model selected and pinned per harness version. Do not depend on a changing service default.
- **Tools:** AgentCore Gateway with explicitly typed tool contracts; API-backed synthetic operations.
- **Agent context:** Harness session memory; episodic/semantic memory introduced after baseline. Durable business state stays outside agent memory.
- **Agent skills:** `vpn-diagnostics`, `incident-triage`, `safe-remediation`, `post-change-verification`, each as a versioned `SKILL.md` package.
- **Backend:** Python 3.12+ / FastAPI / Pydantic.
- **Frontend:** React + TypeScript; React Flow for service topology; simple grid for fleet health; charting for evaluation.
- **Storage:** DynamoDB in AWS; local in-memory/SQLite adapter for early development. S3 for immutable fixtures and documents.
- **Events:** Server-Sent Events initially; event log with monotonically increasing per-run sequence numbers. WebSocket only if bidirectional streaming requires it.
- **Telemetry:** CloudWatch, AgentCore traces, application correlation IDs, structured audit events.
- **Deployment:** Manifest-driven CloudFormation/SAM using the `agent-poc` deployment pattern, with separate dev/demo environments.
- **Testing:** pytest, API contract tests, frontend tests, Playwright end-to-end tests, deterministic scenario evaluator.

### 2.1 Deployment pattern inherited from `agent-poc`

Northstar is a new implementation. It should copy the deployment mechanics,
not LedgerFlow's application code or CloudFormation resources. The deployment
contract is:

- `deploy-all.sh` remains a thin entry point to a Python deployment orchestrator.
- `cloudformation/deployment.yaml` owns stack order, dependencies, stable naming,
  and allow-listed lifecycle steps.
- Each Northstar stack has a target-specific template and matching parameter
  configuration. The manifest is the source of stack truth.
- Stack outputs flow to later stacks through a deployment-context JSON file using
  `CF_OUT_<OutputKey>` values. Do not use nested stacks or CloudFormation
  exports/imports as the application integration contract.
- Preflight lifecycle steps run tests, validate stack contracts, and check AWS
  identity before any stack is changed.
- Runtime/API images are built and pushed to ECR, then deployed by immutable
  digest rather than a mutable tag.
- `--plan`, one-stack deployment, context hydration, stable environment names,
  and resumable retries are first-class developer workflows.
- Lifecycle hooks are Northstar-specific: build application artifacts, seed
  simulator fixtures, smoke-test Harness, and verify the API and web console.
- The deployment context is a cache, not the source of truth; a one-stack retry
  can rebuild predecessor outputs from CloudFormation.

These mechanics must remain usable locally without AWS for repository
validation and plan generation. Actual Harness availability, model access,
region support, and cost limits are verified by an explicit cloud smoke test.

The normal lifecycle is: run tests -> validate contracts -> check AWS identity
-> deploy foundation -> build/push artifacts -> deploy state -> seed fixtures
-> deploy simulator -> deploy Harness/Gateway -> run Harness smoke test ->
deploy application and web -> deploy observability -> run hosted smoke tests.
Each step must be explicit, observable, and safely repeatable or intentionally
skippable for a controlled follow-up deployment.

### 2.2 Northstar logical stack graph

The stack boundaries below are an initial deployment design, not copied
templates. Each stack will be implemented only when its phase requires it.

| Stack | Purpose | Depends on |
| --- | --- | --- |
| `00-foundation` | Artifact storage, ECR, KMS, and deployment prerequisites | — |
| `01-state` | DynamoDB/S3 simulator state, fixtures, and durable event data | `00-foundation` |
| `02-simulator` | Synthetic environment API and typed Gateway tool backends | `01-state` |
| `03-harness` | Harness configuration, Gateway, Memory, skills, policy, and hooks | `00-foundation`, `02-simulator` |
| `04-application` | FastAPI sessions, approvals, SSE, evaluation, and auth integration | `01-state`, `03-harness` |
| `05-web` | Frontend hosting and browser-facing configuration | `04-application` |
| `06-observability` | Logs, traces, metrics, dashboards, and evaluation artifacts | `01-state`, `03-harness`, `04-application` |

The graph may be reduced for local development or expanded when a service
boundary becomes operationally meaningful. Stack count is not the goal; clear
ownership, resumable deployment, and independently testable contracts are.

## 3. Core data model

All mutable records are scoped to `scenario_run_id` to prevent cross-run contamination.

| Entity            | Key fields                                                                             |
| ----------------- | -------------------------------------------------------------------------------------- |
| Employee          | employee_id, department, office, role, assigned_device_ids                             |
| Device            | device_id, employee_id, office, OS, software_versions, health, vpn_connected, revision |
| Service           | service_id, health, dependencies, revision                                             |
| Deployment        | deployment_id, version, target_devices, status, timestamp                              |
| Incident          | incident_id, impacted_entities, severity, status, evidence, revision                   |
| Knowledge article | article_id, revision, issue_signature, steps, provenance                               |
| Scenario          | scenario_id, seed, initial_fixture, faults, expected_outcomes                          |
| Scenario run      | scenario_run_id, scenario_id, status, harness_session_id, started_at                   |
| Approval          | approval_id, actor, action_type, exact_target_set, parameters_hash, expiry, decision   |
| Operation         | operation_id, idempotency_key, target_set, action, status, result, verification        |
| State event       | event_id, sequence, run_id, entity_id, old_state, new_state, operation_id, timestamp   |
| Evaluation        | evaluation_id, run_id, assertions, results, metrics                                    |

Use optimistic concurrency on mutable records. Persist an operation record and its state transition atomically where possible; otherwise use a durable outbox/reconciliation process. The UI must distinguish **requested**, **accepted**, **executed**, and **verified**.

## 4. Tool contracts (initial catalog)

| Tool                      | Type     | Behavior                                                                       |
| ------------------------- | -------- | ------------------------------------------------------------------------------ |
| `get_employee`            | Read     | Return synthetic employee and permitted device associations                    |
| `list_affected_devices`   | Read     | Filter devices by incident/deployment/office                                   |
| `get_device_diagnostics`  | Read     | Return state, telemetry, and diagnostic codes                                  |
| `check_service_health`    | Read     | Return synthetic service health                                                |
| `list_recent_deployments` | Read     | Return relevant software changes                                               |
| `search_knowledge_base`   | Read     | Return cited, versioned troubleshooting articles                               |
| `get_incident`            | Read     | Retrieve incident and history                                                  |
| `propose_remediation`     | Plan     | Store a proposed action and exact target set; no mutation                      |
| `request_approval`        | Workflow | Create approval record for immutable action parameters                         |
| `get_approval_status`     | Read     | Return approval status and expiry                                              |
| `execute_remediation`     | Write    | Enforce authorization, preconditions, idempotency, and apply simulated changes |
| `verify_remediation`      | Read     | Query authoritative post-action state and report unmet conditions              |
| `update_incident`         | Write    | Record final or partial outcome after validation                               |

Every tool returns a structured result with `run_id`, `operation_id` where applicable, `status`, `error_code`, `retryable`, and references to affected entities. Tool descriptions must clearly specify side effects and required approval. Never treat model-supplied identity, approval IDs, or target lists as trusted without server-side validation.

## 5. Agent loop and security design

1. Web API creates a scenario run and maps the authenticated demo actor to a harness actor/session.
2. Backend invokes Harness with objective, run context, and available tools.
3. Harness selects tools and receives results. The application **does not** implement its own reasoning loop.
4. `before_tool_call` Lambda hook may deny disallowed actions. The simulator API independently enforces authorization and preconditions on every mutation.
5. For approval-required operations, the tool creates an approval request and returns a pending result. The application ends or suspends the current user-facing activity and resumes via a subsequent Harness invocation after approval; no assumption of an indefinitely running session.
6. The simulator commits state transitions and emits durable events. The UI subscribes to the authoritative event feed.
7. Harness requests verification tools; independent evaluator checks actual final state and required constraints.
8. The final response distinguishes verified success, partial completion, escalation, and failure.

### 5.1 Permissions and approval invariants

- Default deny for writes; read access is scoped by scenario and actor.
- Approvals bind to action type, exact resource IDs, parameters hash, run ID, approver, and expiry.
- An approval cannot be expanded from 10 devices to 11 devices.
- Changed deployment version, target revision, or operation scope requires revalidation.
- Retries use stable idempotency keys and inspect authoritative operation status after timeouts.
- Agent text, retrieved KB content, and tool output are untrusted instructions.
- Denied operations are visible in the trace but cause **no state mutation**.

### 5.2 Memory and context invariants

- Session memory supports conversation continuity; long-term memory may retain investigation patterns.
- Authoritative operation state, approvals, and incident status are always rehydrated from the simulator, not inferred from memory.
- Context compaction must preserve objective, run ID, pending tasks, unresolved failures, approval constraints, and verified outcomes.
- Long-term memories are scoped by actor and cannot cross scenario-run or tenant boundaries without an explicit policy.
- The UI displays **observable execution stages** inferred from tool/lifecycle events, not private chain-of-thought.

## 6. Frontend experience and event contract

### 6.1 Pages

1. **Overview:** Fleet health, active incidents, pending approvals, current run.
2. **Scenario Catalog:** Select scenario, fault profile, random seed, reset/replay.
3. **Agent Console:** Chat, task status, tool activity, verification summary.
4. **Digital Twin:** Device grid, service topology, filters, before/after view.
5. **Approval Center:** Immutable proposal details, approve/reject, audit history.
6. **Execution Timeline:** Correlated lifecycle events, tool calls, failures, state changes.
7. **Evaluation:** Pass/fail assertions, duration, tool calls, policy compliance, comparison.

### 6.2 Live event schema

```json
{
  "event_id": "EVT-0091",
  "sequence": 91,
  "scenario_run_id": "RUN-104",
  "event_type": "entity.state_changed",
  "entity_type": "device",
  "entity_id": "DEV-1001",
  "operation_id": "OP-220",
  "previous_state": "degraded",
  "new_state": "healthy",
  "verification_status": "pending",
  "occurred_at": "2026-11-20T18:00:00Z"
}
```

Event types: `run.started`, `agent.activity`, `tool.requested`, `tool.completed`, `tool.failed`, `approval.requested`, `approval.decided`, `operation.started`, `entity.state_changed`, `verification.completed`, `run.completed`, `evaluation.completed`.

**Important:** `entity.state_changed` is an observed simulator mutation; `verification.completed` is a separate assertion. The frontend replays events by sequence and fetches a snapshot on reconnect or when gaps are detected. Do not use best-effort Harness lifecycle notifications as the sole source of truth for simulator state.

## 7. Synthetic scenarios and evaluation

### 7.1 Initial dataset

- 100 employees, 150 devices, 15 applications, 500 tickets, 25 KB articles, 30 deployments, 50 incidents.
- 20 deterministic evaluation scenarios, including the 8 named scenarios in the BRD.
- Seeded generator, referential-integrity checks, chronological consistency checks, and fixture versioning.
- Expand only after the initial 20 scenarios pass reliably.

### 7.2 Flagship scenario: VPN rollback

**Initial state:** 40 London devices degraded after `DEP-0088`; no valid rollback approval.

**Expected progression:** diagnose -> identify deployment -> propose exact device set -> request approval -> approve -> execute synthetic rollback -> read device states -> investigate five failures -> retry only where permitted -> verify -> update incident.

**Fault profiles:** full success; 35/40 success; timeout after 24/40 mutations; denied approval; stale approval; KB prompt injection; concurrent state change; API unavailable.

**Evaluation assertions:** correct root cause; approval required and scoped; no unauthorized mutation; no duplicate rollback; final device counts match state; unresolved devices accurately reported; incident status matches verified outcomes.

### 7.3 Scoring

- **Hard gates:** zero unauthorized writes, no cross-run leakage, no false verified-success claims, idempotent retries.
- **Quality metrics:** task success rate, verified remediation rate, recovery success rate, escalation accuracy, tool-call count, latency, token usage, and cost per verified success.
- **Ground truth:** scenario fixture and authoritative event/state store, never the agent's final prose.
- **Comparisons:** baseline Harness, skill-enhanced Harness, and optimized Harness on the same fixture seeds.

### 7.4 Tutorial concept-to-proof matrix

The tutorial is an engineering exercise plan, not only background reading.
Each concept below must produce an observable demo and an automated assertion.

| Tutorial concept | Northstar exercise | Proof |
| --- | --- | --- |
| Managed Harness boundary | Start with `check_service_health` and `get_device_diagnostics` as two read-only tools | Harness selects tools adaptively; application code contains no model-to-tool loop |
| Tool results as observations | Return typed success, failure, retryability, and entity references from every tool | The next agent action and UI trace use the returned observation, not a scripted sequence |
| Stateful synthetic environment | Investigate connected employee, device, service, deployment, and incident records | Repeated reads are consistent; reset and replay reproduce the same seed |
| State-changing tools | Propose, execute, and verify a synthetic rollback | The authoritative state changes only after execution and later reads show the change |
| Human approval and policy | Approve, reject, expire, and scope-change the same operation | Backend policy rejects unauthorized or stale actions even when the model requests them |
| Memory and context engineering | Interrupt a long run, compact context, and resume with a transient Memory failure | Objective, run ID, constraints, unresolved work, and verified outcomes survive; state is rehydrated from the simulator |
| Failure recovery and idempotency | Inject partial success, timeout-after-commit, unavailable API, and conflicting state | Reconciliation follows authoritative state and retries do not duplicate mutations |
| Digital twin and observability | Render the fleet, topology, timeline, and before/after state from events | Every visual transition maps to a durable state event; sequence gaps trigger replay |
| Evaluation and comparison | Run fixed scenarios against baseline, skill-enhanced, and optimized Harness configurations | Reports include independent success, policy, recovery, latency, token, and cost results |
| Deployment engineering | Deploy the same target-specific stack graph through the manifest runner | Plan-only mode is non-mutating; a later stack can be retried using output context and an image digest |

## 8. Phased implementation

Each phase is independently demonstrable. Do not begin the next phase until the exit criteria are satisfied.

### Phase 0 — Repository, scope, and architecture baseline

**Goal:** Establish a repeatable project and prove the AWS Harness service is available in the target account/region.

**Implementation status (2026-11-21):** The local Phase 0 baseline and the
first managed cloud gate are in place.
The deployment runner, manifest/context/SAM mechanics, and thin
`deploy-all.sh` entry point have been generalized from `agent-poc`; no
LedgerFlow CloudFormation resources or application handlers remain in the
Northstar scaffold. Northstar now has seven target-specific contract
skeletons, a FastAPI health boundary, versioned skill placeholders, a local
Harness request contract, and deployment-contract tests. The live cloud proof
uses AWS account `832443555706`, the configured AWS credentials in region `us-east-1`,
and the pinned inference profile `us.amazon.nova-2-lite-v1:0`.

Verified locally:

- `python3 -m pytest -q` — 81 tests passed in the current repository.
- `python3 cloudformation/validate.py` — 7 stack contracts validated.
- `python3 cloudformation/deploy.py --environment test --plan` — non-mutating
  seven-stack plan produced without AWS credentials.
- `python3 -m harness.smoke_test --dry-run` — managed-orchestration contract
  produced without implementing a custom model/tool loop.
- `./deploy-all.sh --help` — existing deployment entry point resolves to the
  generalized orchestrator.

Verified in AWS:

- `northstar-dev-00-foundation` and `northstar-dev-02-simulator` are
  `UPDATE_COMPLETE` under the configured AWS credentials.
- The simulator Lambda has an explicit
  `bedrock-agentcore.amazonaws.com` invoke permission and the Gateway
  execution role can invoke it.
- The live Gateway target is `IOXUE1ZVQY`, type `LAMBDA`, and `READY`; the
  direct MCP call returned an authoritative `check_service_health` observation.
- Harness `northstar_dev_harness-YV5BCvNyWH`, version `5`, is `READY` with
  managed memory, four S3 skills, and the bounded `@northstar-simulator`
  selector.
- The managed smoke for `demo-vpn` / `INC-0001` completed with `errors: []`,
  `stop_reason: end_turn`, 416 normalized events, seven real simulator tool
  calls, and a grounded report that preserved an unresolved deployment
  conflict.
- A current direct managed invocation completed with `errors: []`,
  `stop_reason: end_turn`, 162 normalized events, six named read-only Gateway
  tool calls, 3,637 total model tokens, and 1,898 ms reported latency. The
  adapter also normalizes an event-stream `runtimeClientError` into a
  correlated structured event, covered by a unit test.
- The bounded Phase 0 smoke objective was tightened to two read-only tools and
  a concise stop condition. Two consecutive live turns on the same managed
  runtime session then succeeded with `errors: []` and `end_turn`: 50 events / 2
  Gateway calls / 2,443 tokens followed by 45 events / 2 Gateway calls / 3,201
  tokens. Full managed compaction and broader recovery playbooks remain open.

**Tasks**

- [x] Create repository, environment configuration, and IaC skeleton.
- [x] Select AWS region, model, profile, and deployment strategy; keep the
  remaining cost-budget/cleanup decision explicit for the hosted demo.
- [x] Create an actual config-based Harness; invoke a simple prompt and inspect
  its managed trace.
- [x] Document separation of Harness reasoning from simulator/business control.
- [x] Define canonical IDs, API conventions, and run-scoped data model; the
  simulator envelopes, operation/approval records, API schemas, and
  `scenario_run_id` isolation tests are the authoritative implementation.
- [x] Generalize the copied `agent-poc` deployment runner for Northstar; retain the
  manifest, context, validation, SAM, and lifecycle patterns without copying
  LedgerFlow templates or domain-specific handlers.
- [x] Define the Northstar stack graph, stack output contract, parameter/configuration
  format, environment naming, artifact retention, and cleanup policy.
- [x] Add deployment-contract tests for manifest ordering, matching template/config
  files, output dependencies, forbidden stack coupling, and context resolution.
- [x] Prove that repository validation and `--plan` work without AWS credentials;
  keep the actual Harness smoke test as an explicit cloud-only gate.
- [x] Pin the Python, AWS CLI, SAM CLI, Docker/Buildx, Harness SDK/CLI, model, and
  region versions used by the demo. `toolchain.lock.json` records the verified
  profile, account, region, model, CLI/tool versions, and the current SAM/Lambda
  runtime boundary; `requirements.txt` and `pyproject.toml` pin the Lambda,
  local, and development dependencies. AgentCore Harness is accessed through
  the pinned boto3 clients rather than a separate Harness SDK or CLI, and the
  current runtime does not use container images.

**Deliverables:** ADRs, working Harness smoke test, repository structure,
deployment manifest skeleton, deployment-contract tests, budget/cleanup
checklist, and a documented local/cloud execution matrix.

**Exit criteria:** Harness invocation works end to end; no custom model/tool
loop exists in application code; repository validation and a non-mutating plan
run succeed from a clean checkout; the deployment design has no inherited
LedgerFlow resource assumptions.

### Phase 1 — Synthetic enterprise and deterministic simulator

**Goal:** Create a believable, mutable enterprise with no AI dependency.

**Implementation status (2026-11-20):** The deterministic simulator foundation
is implemented locally. The fixture generator creates 100 employees, 150
devices, 15 applications, 6 services, 30 deployments, 50 incidents, and 25
knowledge articles with connected relationships. The original five scenario
definitions plus fifteen recovery/evaluation variants are checked in with
separate expected-outcome records. A thread-safe run store supports
actor-owned runs, reset, eight deterministic fault classes, event history,
checkpointed context, and observation capture. Typed read-only tools expose
incident lookup, service health, affected devices, recent deployments,
diagnostics, and knowledge retrieval. Approval-gated mutations enforce an
optimistic state-revision precondition and safe reconciliation.

Verified locally:

- `python3 -m pytest -q` — 81 tests passed in the current repository (the
  Phase 1 checkpoint had 25 tests before the later Harness/API slices).
- Fixture relationship validation passes for the seeded VPN scenario.
- Run isolation, reset, fault scoping, typed tool envelopes, and idempotency
  conflict behavior are covered by unit tests.

**Tasks**

- [x] Build seeded fixture generator and validators.
- [x] Implement device, deployment, incident, and approval data models.
- [x] Implement read-only simulator APIs and typed tool-result envelopes before any
  Harness integration or state-changing tool is introduced.
- [x] Add the operation-ledger and idempotency foundation plus optimistic
  state-revision preconditions for state-changing tools.
- [x] Add reset, fault profiles, event history, authenticated replay, observation
  capture, actor-owned runs, and per-run isolation.
- [x] Implement the five core scenario definitions plus fifteen deterministic
  recovery/evaluation variants.
- [x] Exercise the tutorial's first synthetic-environment milestone with connected
  employee, device, service, deployment, and incident records.

**Deliverables:** Simulator API, fixtures, test suite, scenario runner.

**Exit criteria:** All state transitions are reproducible, resettable, and testable without an agent; timeouts cannot duplicate a mutation.

### Phase 2 — Managed Harness and tool integration

**Goal:** Let Harness investigate synthetic incidents with real tool calls.

**Implementation status (2026-11-21):** The local managed-Harness contract and
first read-only vertical slice are implemented. The repository now contains a
declarative Harness configuration with explicit model/instruction/limit
fields, a six-operation read-only Gateway contract translated to the live
Lambda target, versioned knowledge retrieval, Agent Skill frontmatter, and a
thin `InvokeHarness` SDK adapter. The adapter normalizes streamed Harness
events and carries `scenario_run_id`, session, request, actor, and trace
identifiers without implementing a model/tool loop. FastAPI exposes completed
and SSE streaming invocation boundaries, while the simulator remains
authoritative and read-only in this phase.

Verified locally:

- `python3 -m pytest -q` — 81 tests passed, including fake Harness stream,
  configuration safety, Gateway operation, API-boundary, knowledge, and
  simulator assertions.
- `python3 -m harness.smoke_test --dry-run` — existing smoke helper validates
  the managed-orchestration contract; `--invoke` is the explicit cloud gate.
- `python3 cloudformation/validate.py --skip-cfn-lint` — 7 independent stack
  contracts validated.
- `python3 cloudformation/deploy.py --environment test --plan` — the existing
  manifest-driven, non-mutating deployment plan remains valid.

The first cloud gate is complete. The final live path uses the reusable
`agent-poc` deployment mechanics and its Gateway-to-Lambda pattern, adapted to
Northstar's simulator without copying LedgerFlow resources or handlers. The
earlier OpenAPI-to-IAM-API-Gateway experiment is retained only as the checked-in
tool contract; the deployed target is Lambda because the live OpenAPI target
returned downstream 500s without reaching the simulator.

The five-scenario cloud gate is also complete. The existing smoke helper
created each run through the IAM-protected simulator API and invoked the same
managed Harness (`northstar_dev_harness-YV5BCvNyWH`, version `5`, model
`us.amazon.nova-2-lite-v1:0`). Every run ended with `errors: []` and
`stop_reason: end_turn`; each produced real `northstar-simulator___*` Gateway
tool evidence. The suite uses a bounded read-only prompt and a 4,096-token
Harness response budget so scenario objectives that mention remediation cannot
turn into write attempts or unbounded model output.

| Scenario | Cloud run ID | Trace ID | Events | Gateway calls |
| --- | --- | --- | ---: | ---: |
| `vpn-deployment-failure` | `cloud-vpn-deployment-failure-d4aaf6b57b26` | `62c8aba1206b423b9dc5b246ac81c913` | 132 | 4 |
| `partial-remediation-failure` | `cloud-partial-remediation-failure-4b5015c86844` | `cac3d6b342e6441294560c9512d1e0a7` | 134 | 7 |
| `service-outage` | `cloud-service-outage-32923750b9ae` | `689ecbe8df214c9a94c803c1dfb074d7` | 149 | 3 |
| `unauthorized-access-request` | `cloud-unauthorized-access-request-20633b2899e4` | `4dcc1d02d4e14bcbae68783bbcbf5bd4` | 115 | 3 |
| `interrupted-investigation` | `cloud-interrupted-investigation-fbdc96f36c47` | `6c249641bfdc400cb83f9cc2f9ffb964` | 158 | 4 |

The suite therefore covers one VPN control profile plus four alternate
root-cause/context profiles while preserving run-scoped IDs and traces. The
simulator remains read-only; no mutation or approval path was exercised yet.

**Tasks**

- [x] Configure Harness model, instructions, iteration/token/time limits in a
  declarative, environment-resolved contract.
- [x] Define the six-operation read-only Gateway contract with explicit
  operation IDs and prefixed names; deploy it as a Lambda target backed by the
  simulator function and a bounded Harness selector.
- [x] Add versioned knowledge articles and a deterministic retrieval tool.
- [x] Implement the thin backend Harness invocation adapter and streaming SSE
  response boundary without an application-owned reasoning loop.
- [x] Correlate Harness session, scenario run, request, actor, trace, tool,
  and simulator observation identifiers in normalized events.
- [x] Complete the local first vertical slice contract with
  `check_service_health` and `get_device_diagnostics`.
- [x] Create the real Gateway/Harness resources and capture a managed cloud
  trace for the first vertical slice.
- [x] Exercise five seeded scenario definitions through the real API-backed
  simulator and managed Harness, including four alternate root-cause/context
  profiles; require Gateway tool evidence for every run.

**Deliverables:** Local managed-Harness invocation boundary, Lambda-backed
Gateway target contract, versioned read-only observations, correlated stream
events, and a cloud smoke-test entry point. The first functional managed cloud
investigation and five-scenario grounded cloud suite are complete; later
mutation/approval phases remain.

**Exit criteria:** Agent can investigate and select tools adaptively; no frontend or backend scripted tool sequence masquerades as agent decisions.

### Phase 3 — Live website and digital twin

**Goal:** Make every authoritative state change visible.

**Implementation status (2026-11-21):** The digital-twin website
slice is implemented as a bundled React/TypeScript application. It calls the
simulator Lambda Function URL directly, uses Cognito ID tokens for cloud HTTP
routes, and exposes user creation only to the `northstar-admin` Cognito group.
The browser does not load React or any other library from a CDN. The API
provides snapshot, topology, comparison, entity, and sequence-replay
endpoints; the UI polls the authenticated replay endpoint because native
`EventSource` cannot send a bearer header. The deployed `01-state` DynamoDB
table is now the shared run repository for the Function URL and Gateway Lambda
target; local development retains the in-memory adapter. Normalized Harness
markers (`agent_turn_started`, `agent_tool_call_requested`,
`agent_turn_completed`, invocation lifecycle, metadata, and errors) and
Gateway `observation_recorded` events share the same per-run sequence, so the
console can show managed turn/tool progress while the invocation is running.
The UI polls that sequence every 1.2 seconds. Model text deltas and hidden
chain-of-thought are intentionally not persisted. The Harness boundary
regression test also verifies that raw tool inputs are excluded while named
tool progress and usage metadata remain visible. Phase 4 now adds a separate,
approval-gated mutation API and UI while the managed Harness/Gateway selector
remains read-only. The console also exposes an authenticated independent
evaluation scorecard for the selected run. The scenario catalog now carries a
business-facing description and investigation goal for every scenario; the
selected description is shown in a dedicated panel beneath the dropdown. The
primary control is labeled “Start investigation,” while technical Harness/run
identifiers remain implementation details rather than user-facing copy.

Verified locally and in the target AWS environment in `us-east-1`:

- `npm run build` in `apps/web` — TypeScript check and Vite production bundle passed.
- `sam validate --template-file cloudformation/stacks/02-simulator.yaml --lint` — valid SAM template.
- `sam build` for `02-simulator` — Lambda package includes the Cognito JWT dependency.
- `northstar-dev-02-simulator` — `UPDATE_COMPLETE`, with Function URL,
  Cognito pool, public app client, and `northstar-admin` group.
- The full manifest was deployed through `deploy-all.sh` with the existing
  configured AWS credentials as release `20261121-full-deployment`; the
  `northstar-dev-02-simulator` Lambda environment points at
  `northstar-dev-scenario-state` and its role is
  scoped to `GetItem`, `PutItem`, and `Scan` on that table.
- The complete manifest processed stacks `00-foundation` through
  `06-observability`; hosted `/healthz` returned 200 and the Function URL
  served the deployed React JavaScript and CSS assets.
- A read-only Gateway-style Lambda probe against synthetic run
  `cloud-service-outage-observability-20261121` returned a typed observation;
  a direct DynamoDB read then confirmed the shared run contained
  `scenario_created`, `observation_recorded`, and one persisted observation.
  No fault was injected and no AWS resource was deleted.
- Direct Function URL smoke: `/healthz` returns 200, unauthenticated
  `/simulator/scenarios` returns 401, an admin token can invite a user, and a
  non-admin token receives 403 from `POST /admin/users`.
- Hosted console smoke after the manifest deployment: the same Lambda Function
  URL returns the bundled React shell and JavaScript asset with 200 responses;
  a Cognito challenge-completing user can access `/auth/me` and the scenario
  catalog, while a non-admin invitation attempt remains 403.
- Local browser verification confirms that changing the business-situation
  dropdown updates the description panel and investigation goal without
  exposing internal scenario or run identifiers; the refreshed dashboard
  screenshot is stored at `docs/screenshots/dashboard.jpg`.
- The full scenario catalog was rewritten in plain business language with
  concrete employee, department, and application impact. Examples include
  “40 employees lose access after a workplace software update” and “Payments
  portal unavailable to Finance and Sales”; scenario IDs and evaluation
  behavior remain unchanged.
- Post-recovery deployment smoke: the authenticated Function URL created an
  actor-owned `memory-unavailable-resume` run, saved and compacted a
  checkpoint, injected a memory fault, returned authoritative fallback state,
  and served a 200 independent evaluation scorecard.

**Tasks**

- [x] Build bundled React/TypeScript shell, Cognito login, scenario catalog,
  Harness action panel, and device fleet grid.
- [x] Add event feed with authenticated replay polling/reconnect and run
  snapshot endpoints.
- [x] Persist deployed run state and event sequences through the existing
  `01-state` DynamoDB table so API and Gateway Lambda environments share
  browser-created runs; retain the in-memory adapter for local work.
- [x] Correlate normalized managed Harness turn/tool lifecycle markers and
  Gateway observation results into the polled run timeline without persisting
  model text deltas or hidden reasoning.
- [x] Add service dependency visualization and entity detail drawer.
- [x] Build agent activity timeline from observed lifecycle/tool events.
- [x] Add before/after state comparison and reset controls.
- [x] Add business-facing scenario descriptions beneath the catalog dropdown;
  replace visible “Invoke Harness” language with “Start investigation” and
  keep internal identifiers out of the primary user flow.

**Deliverables:** Three synchronized panels: agent, environment, timeline;
direct Lambda Function URL; Cognito login; and admin-only user invitations.

**Exit criteria:** The read-only slice reflects simulator events without manual
refresh, replays from a sequence cursor, and never marks an observation as a
verified mutation. The approval-gated mutation slice is separately covered by
Phase 4 and does not widen the read-only managed-tool selector.

### Phase 4 — Authorization and controlled remediation

**Goal:** Demonstrate approved synthetic actions and hard policy boundaries.

**Implementation status (2026-11-21):** The first controlled-remediation slice
is implemented locally and in the bundled React console. A proposal records an
immutable action, exact target set, parameter fingerprint, requester, and
expiry. A Cognito-admin-only decision endpoint approves or rejects it; an
authenticated operator executes it once; the simulator verifies authoritative
post-state and emits approval, operation, and verification audit events.
Rollback, service restart, and incident update are supported by the API-level
simulator operation service. The managed Harness/Gateway contract remains
intentionally read-only: a live `before_tool_call` Lambda guard denies tools
outside the six-operation allow-list or calls without a scenario `run_id`.
After the API executes an approved operation, the React console resumes the
same managed Harness runtime session with a read-only verification prompt.

Verified locally:

- `pytest -q` — 81 tests passed, including denied, expired, scope-conflict,
  idempotent, partial-timeout, API-route, and authoritative verification cases.
- `npm run build` in `apps/web` — approval center, admin decision controls, and
  operator execution controls compile into the local React bundle.
- Direct local lifecycle — requested -> approved/rejected -> executed ->
  verified/unverified, with no state mutation before approval.
- Live target-environment / `us-east-1` smoke — Function URL health 200,
  unauthenticated simulator 401, proposal 201, non-admin decision 403, admin
  decision 200, execution `verified`, and idempotent replay `replayed=true`.
- Managed Harness version 5 — `northstar-read-only-guard` emitted an
  `allow` decision for a real `before_tool_call` event; the invocation ended
  with `end_turn` and no errors.
- Live Function URL resumption smoke — Cognito admin identity 200, initial
  managed Harness invocation 200 with `end_turn`/no errors, proposal 201,
  admin decision 200, execution `verified`, and a second invocation on the
  same runtime session 200 with `end_turn`/no errors.

**Tasks**

- [x] Implement proposal/approval workflow and operator approval UI through the
  authenticated direct Function URL.
- [x] Bind approvals to exact operation parameters, target set, and expiry;
  reject target expansion and idempotency-key reuse with changed parameters.
- [x] Add the managed `before_tool_call` authorization hook; it denies unknown
  tools and missing run IDs with failure mode `deny`, while the API backend
  enforces approval and Cognito-admin decision boundaries.
- [x] Add API-level synthetic rollback, service restart, and incident update
  operations with post-action authoritative verification.
- [x] Resume agent execution after approval using the same logical investigation
  context; the API owns the write and the managed selector re-reads authoritative
  state through read-only tools on the same runtime session.
- [x] Add denied, expired, and scope-conflict cases with deterministic tests.
- [x] Persist and display the requested -> approved -> executed -> verified
  transition for every state-changing operation, including rejected and partial
  outcomes.

**Deliverables:** Direct API/React approval-to-state-change demo, policy audit
events, exact-scope/idempotency enforcement, bounded same-session Harness
resumption, and a clearly bounded list of remaining recovery/evaluation work.

**Exit criteria:** The local/API slice satisfies the authorization, approval,
idempotency, and verification invariants. The authorization hook is live and
the post-approval Harness resumption boundary is proven in the deployed
Function URL path. Managed write tools remain intentionally outside the
selector; the next phase covers recovery, memory, and skills.

### Phase 5 — Failure recovery, memory, and skills

**Goal:** Demonstrate adaptive, resilient execution beyond LedgerFlow's read-only investigation.

**Implementation status (2026-11-21):** The recovery and continuity slice is
implemented locally. It covers partial success, timeout before commit,
timeout after commit, transient simulator unavailability, stale approvals,
conflicting evidence, actor-scoped checkpoints, context compaction, and
authoritative-state fallback when synthetic memory is unavailable. A retry of
an executed/unverified immutable operation emits `operation_reconciled` and
does not duplicate a previously changed target. Managed Harness memory and
four versioned S3 skills remain configured for the deployed Harness, while the
local checkpoint contract is the independently testable seam for compaction
and memory recovery.

Verified locally:

- `pytest -q` — 81 tests passed, including authoritative partial-timeout and
  timeout-after-commit reconciliation without duplicate target changes,
  stale-state invalidation, actor isolation, compaction, memory fallback, and
  prompt-injection evidence handling.
- `npm run build` in `apps/web` — the approval center includes `Retry & verify`
  and the independent evaluation panel.
- `python3 -m evaluation.runner --seed 20261120` — 20 deterministic reports
  emitted from one fixed seed; the suite records independent pass/fail,
  hard-gate, recovery, policy, latency, tool-call, token, and cost fields.
- Live target-environment / `us-east-1` post-deploy Harness smoke — 335 managed
  events, four real Gateway tool calls, `errors: []`, and `stop_reason:
  end_turn` after the recovery/evaluation code was deployed.
- A subsequent direct `--invoke` smoke completed with 162 normalized events,
  six named read-only Gateway tool calls, `errors: []`, `stop_reason:
  end_turn`, and a grounded report; an event-stream max-token error path is
  now normalized and tested rather than escaping as a traceback.
- The bounded Phase 0 smoke now also proves two consecutive read-only turns on
  one stable runtime session ID: both returned `errors: []`, `end_turn`, and two
  named Gateway calls, with 50 and 45 normalized events respectively.

**Tasks**

- [x] Add failure injection: timeout after commit, partial success, stale state,
  transient API unavailability, and conflicting evidence.
- [x] Implement bounded operation reconciliation and safe retries for an
  executed/unverified approval; broaden the failure catalog next.
- [x] Configure Harness memory and prove same-session read-only resumption through
  the deployed Function URL.
- [x] Test actor isolation, interruption/compaction, and transient memory
  unavailability with actor-scoped checkpoints and authoritative fallback.
- [x] Add and deploy versioned Agent Skills for diagnostics, remediation, and
  verification.
- [x] Ensure rehydration from authoritative state after interruption.
- [x] Force local context compaction and transient Memory unavailability; verify
  that critical invariants survive and that authoritative state remains the
  fallback source.
- [x] Include prompt-injection content in synthetic knowledge articles and prove
  that retrieved text remains untrusted data and cannot authorize a mutation.
- [x] Expand to 20 deterministic scenarios (including the original five).
- [ ] Run full managed-service compaction and broader recovery playbooks against
  the deployed Harness. The local checkpoint/memory seams are complete; this
  cloud-hardening run remains gated on bounded cloud-run approval and budget.
  The bounded direct `interrupted-investigation` probe intentionally failed
  without a checkpoint event, confirming that the direct read-only runner does
  not substitute for a real managed recovery/compaction exercise.

**Deliverables:** Recovery playbooks, skills, actor-scoped checkpoints,
compaction/memory tests, partial-failure reconciliation, and visible recovery
events in the timeline/evaluation scorecard.

**Exit criteria:** The local/API recovery seam can recover from ambiguous tool
outcomes without duplicate changes; critical invariants survive checkpoint
compaction and memory fallback; failures remain visible. Full managed-service
compaction and broader recovery playbooks remain a Phase 6/7 cloud-hardening
follow-up.

### Phase 6 — Automated evaluation and comparative harness runs

**Goal:** Turn the demo into an evidence-based harness engineering showcase.

**Implementation status (2026-11-21):** An independent evaluator now scores
authoritative state, durable events, and typed observations without trusting
agent prose or implementing a model loop. `evaluation/runner.py` emits a
versioned per-run scorecard; `/evaluation/runs/{run_id}` and the React console
expose it for a selected live run. The catalog contains 20 deterministic
scenario definitions and 20 separate expected-outcome records, and the fixed
seed suite records hard-gate failures, policy violations, recovery events,
tool-call counts, latency, token, and cost fields. A scorecard comparison
contract and trace-to-event evidence links are implemented. A bounded live
comparison of the three deployed Harness profiles is now complete for the
canonical `service-outage` fixture; the full managed 20-scenario regression
remains open.

The cloud scenario-suite runner can optionally preserve a combined managed
Harness trace summary and authenticated independent evaluator scorecard for
each run with `--artifact-dir`; this makes the remaining full-catalog live
comparative execution reproducible without changing the default cloud gate. The persisted
artifact schema is `northstar.live-harness-artifact.v1`.

The existing smoke helper now also exposes a guarded
`--direct-scenario-suite` mode. It runs the real managed Harness/Gateway
boundary directly, requires `--skip-runtime-binding`, supports
`--all-scenarios`, and writes the same artifact schema without requiring an
out-of-band Cognito token. A bounded live `service-outage` proof completed on
the default Harness with four Gateway calls, `end_turn`, hard gates passing,
and normalized latency/token fields in
`artifacts/live-direct-suite-20261121/`. This is useful managed-Harness
evidence, but it deliberately does not close the authenticated Function URL
scenario-suite gate. The `--all-scenarios` form remains a 20-invocation,
spend-gated operation and now requires the explicit
`--acknowledge-cloud-spend` flag; bounded `--scenario-ids` subsets do not.

A second bounded direct run covered `vpn-deployment-failure`,
`interrupted-investigation`, and `unauthorized-access-request`. The
unauthorized-access scenario passed its independent scorecard at 100% with
hard gates and zero policy violations. The VPN scenario gathered all required
read-only evidence with hard gates passing but, as expected, did not reach its
write-dependent final state. The interruption scenario correctly failed the
`required_observation:checkpoint` hard gate because this direct read-only path
does not fabricate a checkpoint or compaction event. Those artifacts remain in
`artifacts/live-direct-suite-20261121/` and make the managed recovery gap
explicit rather than treating an uncheckpointed run as proof of recovery.

Verified live comparison evidence (target environment / us-east-1, fixed seed
`20261120`) was collected without rebinding the browser Lambda:

- `baseline` — Harness `northstar_dev_baseline_harness-fnOGWcFTk6`, version 1.
- `skills-memory` — the deployed default Harness
  `northstar_dev_harness-YV5BCvNyWH`, version 5.
- `optimized` — Harness `northstar_dev_optimized_harness-gWWh4ETgvR`, version 1.

Each invocation completed with `end_turn`, 11 managed turns, five typed
read-only observations, no policy violations, and all hard gates passing.
The overall scorecard is `failed` at 81.82% for each run only because the
read-only objective intentionally did not resolve or escalate the open
incident; those final-state assertions are soft gates. The artifacts are
preserved at `artifacts/live-variants-20261121/` and compare with zero score,
tool-count, or policy-violation delta between profiles. The optimized profile
initially exposed a missing IAM resource pattern for its managed memory; the
foundation stack was updated through the normal deployment runner and the
retry then completed successfully.

Verified locally:

- `python3 -m evaluation.runner --seed 20261120` — 20 reports emitted from a
  fixed seed with reproducible scenario IDs and independent scorecards. The
  read-only baseline reports 12 passing and 8 expected non-mutating failures
  for scenarios whose ground truth requires an unperformed write; no policy
  violations are reported.
- `python3 -m evaluation.compare --baseline ... --candidate ...` — same-seed
  comparison reports score, tool-call, recovery, policy, latency, and cost
  deltas across 20 scenarios.
- `GET /evaluation/runs/{run_id}` — authenticated state-based report boundary.
- `npm run build` in `apps/web` — evaluation panel and evidence-to-timeline
  links compile into the bundle.
- Live target-environment / `us-east-1` Function URL probe — a disposable
  Cognito user was authenticated, a non-admin `POST /admin/users` attempt
  returned 403, checkpoint compaction and authoritative memory fallback
  succeeded, all three required read-only observations returned 200, and the
  authenticated scorecard passed at 100% with hard gates and three
  event-sequence evidence links.

**Tasks**

- [x] Expand to 20 deterministic scenarios and separate ground-truth assertions.
- [x] Build evaluation runner and per-run scorecard.
- [x] Record tool calls, cost, latency, success, recovery, and policy-violation
  fields; live Harness trace values can be supplied to the same scorer.
- [x] Compare fixed-seed baseline, skills/memory, and optimized scorecard
  artifacts with a shared-catalog comparison contract; the bounded live
  `service-outage` comparison now has three separate deployed Harness
  artifacts with the same seed and objective.
- [x] Run the local regression suite repeatedly with fixed seeds and compare
  the resulting artifacts; repeated managed cloud regression remains open.
- [x] Add optional `--artifact-dir` persistence for combined live Harness and
  independent scorecard artifacts; direct non-binding comparison invocations
  use the same artifact schema.
- [x] Add a guarded direct managed-Harness catalog runner for bounded
  credential-free comparison evidence; it refuses to run unless browser
  runtime binding is explicitly skipped.
- [x] Execute the baseline, skills/memory, and optimized variants against
  separate deployed Harness versions and preserve the live scorecard
  artifacts for the canonical service-outage fixture.
- [ ] Repeat the complete fixed-seed regression through the authenticated
  managed cloud deployment's scenario catalog; the direct runner and bounded
  one-scenario proof above do not substitute for this 20-scenario Function URL
  and Cognito live proof.
- [x] Add trace-to-state-event evidence links in the UI; clicking an evaluated
  assertion scrolls to its durable timeline sequence.
- [x] Store expected outcomes separately from agent-visible context and make
  policy violations hard evaluation failures.

**Deliverables:** Evaluation dashboard, run comparisons, regression report.

**Exit criteria:** Every run has independent pass/fail results; policy violations are hard failures; comparisons are reproducible.

### Phase 7 — Deployment, hardening, and presentation

**Goal:** Produce a reliable portfolio-quality public demo.

**Implementation status (2026-11-21):** The manifest-driven AWS foundation is
deployed in the target environment / `us-east-1`: stacks `00-foundation` through
`06-observability` exist with successful CloudFormation status, and the
`02-simulator` stack has been updated through the deployment orchestrator with
the recovery/evaluation code. The managed Harness binding was refreshed to
version 5 after the update. The direct Function URL, Cognito boundary, API,
simulator, Gateway, Harness, and observability paths have live smoke evidence.
The manifest now builds the React bundle before SAM packaging, serves it from
the same Lambda Function URL, and publishes a private versioned release
artifact through `05-web`. The simulator now has a process-local request
limiter, reserved concurrency, an invocation-budget alarm, and both state and
artifact buckets explicitly block public access. The hosted React login shell
has been checked at desktop and mobile widths for labels, image alternatives,
horizontal overflow, and root visibility. Remaining Phase 7 work is limited
to an actual CloudFormation-resource failure proof and a real spend-budget
choice; larger distributed load/security testing is explicitly out of scope
for this demo. The disposable cleanup proof is represented by a plan-only run.
A disposable `cleanup-proof` environment was
deployed and its cleanup plan enumerated only the seven manifest-derived stack
names; deletion was intentionally not executed without explicit authorization.
The unused broad API Gateway invoke
grant was removed from the live Gateway execution role, and the Harness role
now scopes Gateway invocation and skill reads to the Northstar environment;
its deployed target is Lambda-only.
The follow-up `20261121-observability-auth` one-stack deployment added the
shared DynamoDB run repository, persisted Harness/tool timeline markers, and
Harness run-ownership enforcement without changing the read-only Gateway
selector. A live read-only Gateway-target probe after that deployment returned
typed observations and confirmed the same run sequence in DynamoDB; the
authenticated Harness/browser run remains credential-dependent rather than
being fabricated by a bypass.

Final local verification for this deployment slice is `pytest -rA` with 81
tests passing and four opt-in hosted checks skipped by default, seven
independent stack contracts validated, the simulator SAM template linting
successfully, the production React bundle compiling with Vite, and the
Lambda dependency/toolchain contract passing against the exact pins in
`requirements.txt`, `pyproject.toml`, and `toolchain.lock.json`; a full
`pip install --dry-run --ignore-installed -e '.[dev]'` resolution also
completed successfully. The
required hosted smoke passed during the full manifest deployment,
the real `--stack 05-web` retry after predecessor output hydration, and the
`20261121-guardrails`, `20261121-iam`, and `20261121-iam-scope` deployments. A
controlled `02-simulator` SAM-command failure was followed by a normal retry;
the stack recovered to `UPDATE_COMPLETE`, preserved the published web release
ID, and recorded `20261121-retry-proof` as the deployment attempt. Live checks
also confirmed reserved concurrency 10, an `OK` 5,000-invocation/hour alarm,
private-bucket controls, and a `401` protected API response carrying the
configured rate-limit header. The exact-model/memory IAM update deployed as
`20261121-iam-model-memory`, and a managed read-only Harness smoke succeeded
afterward. The later `20261121-harness-variants` foundation update widened
only the environment-scoped Harness memory ARN pattern so the optimized
comparison profile could use its own managed memory. The default browser
binding was restored and verified as `northstar_dev_harness-YV5BCvNyWH`; the
variant runs explicitly skipped runtime binding. The bounded hosted smoke then
passed the unauthenticated `401`,
malformed-token `401`, 32 concurrent authenticated catalog reads, and
regular-operator `403` admin-route checks.
The newly created regular operator also received `403` from the live admin-user
route, confirming the Cognito group boundary. The cloud scenario-suite path was
also corrected to create runs through the direct Function URL with a Cognito ID
token; a live `service-outage` run passed with four real read-only Gateway tool
calls and no errors. The bounded hosted smoke also passed a malformed-token
`401` probe.

The hosted-load tooling now also includes the standalone, bounded,
multi-process read-only probe at `tests/load/hosted_multiprocess.py`. The
latest unauthenticated run used the maximum 128 requests across eight spawned
worker processes and returned 128 `401` responses with no transport or server
errors. The probe accepts an authenticated ID token only from
`NORTHSTAR_ID_TOKEN`, never from the command line. Authenticated distributed
capacity is not claimed and is outside the current demo scope.

A fresh read-only verification pass on 2026-11-21 reconfirmed the AWS caller
as account `832443555706` through the configured AWS credentials, found all seven
`northstar-dev-*` stacks and all seven intentionally retained
`northstar-cleanup-proof-*` stacks in a successful CloudFormation state, and
ran the hosted unauthenticated/malformed-token smoke against the deployed
Function URL: 2 passed and 2 credential-dependent checks skipped. No stack,
bucket, object, user, or other AWS resource was deleted or failure-injected by
this verification pass. A subsequent read-only URL check returned `200` for
`/healthz` and the bundled React shell and `401` for the protected scenario
catalog. No `NORTHSTAR_ID_TOKEN` is present in the current session, so the
authenticated 20-scenario and distributed-load gates were not attempted.

The same read-only audit confirmed the live Lambda URL is `AuthType: NONE` for
the public React/login shell while protected API routes remain Cognito-gated,
reserved concurrency is `10` via `aws lambda get-function-concurrency`, the
generated simulator invocation alarm is `OK` with a `5,000` threshold, and
both the fixture and web artifact buckets have all four public-access-block
settings enabled. The deployed Harness policy is scoped to the Northstar Nova
2 Lite profile, `northstar-dev-*` Gateway/skill resources,
environment-scoped `northstar_dev*_harness-*` memory resources, and the named
 hook; the Gateway policy permits invocation only of
 `northstar-dev-simulator`.

**Tasks**

- [x] Implement the target-specific CloudFormation/SAM templates and matching JSON
  configuration for the Northstar stack graph; do not import LedgerFlow stack
  resources.
- [x] Deploy the bundled frontend, API, simulator, Harness, Gateway, and observability through
  the manifest-driven orchestrator.
- [x] Exercise `--plan`, full deployment, one-stack update, and predecessor-output
  hydration through the deployment context.
- [x] Expose `--stack` and `--from` on the top-level orchestrator so a later-stack
  retry can reuse hydrated predecessor outputs without rebuilding earlier stacks.
- [x] Exercise retry after a controlled later-stack deployment-command failure
  and capture the recovery proof. The `02-simulator` SAM command was forced to
  fail before CloudFormation, then retried normally to `UPDATE_COMPLETE`; the
  web artifact release remained `20261121-iam-scope` and the retry attempt was
  `20261121-retry-proof`.
- [ ] Exercise an actual CloudFormation resource failure in a disposable
  later-stack environment, if a safe deterministic injection point is added.
  No safe resource-level injection point is currently implemented; the
  controlled pre-CloudFormation deployment-command failure is the non-destructive
  recovery proof.
- [x] Evaluate the containerized application/runtime option. It is not
  applicable to this implementation: the target-appropriate runtime is
  SAM/Lambda, with the web release tracked by release ID in a versioned,
  private artifact bucket. If a container runtime is introduced later, its
  image must be pinned by immutable digest.
- [x] Seed the canonical scenarios after state resources exist and run a post-deploy
  Harness/API/web smoke test against the hosted environment.
- [x] Repair and verify the cloud scenario-suite run-creation path after enabling
  Cognito protection: it now uses the direct Function URL with an out-of-band
  Cognito ID token and passed a live `service-outage` managed-Harness gate.
- [x] Record the release ID and published web artifact location in the
  deployment context.
- [x] Add a built-in required web smoke that verifies the hosted React shell
  and its bundled JavaScript asset when no external browser command is set.
- [x] Add request-rate and usage guardrails: the API has configurable
  process-local fixed-window limits, the simulator has reserved concurrency 10,
  and CloudWatch alarms at 5,000 invocations/hour; both versioned buckets
  explicitly block public access. The Cognito boundary and private artifact
  bucket are also implemented.
- [x] Add a plan-only-by-default cleanup utility that discovers only
  manifest-owned versioned buckets, empties their object versions/markers, and
  requires an exact environment confirmation before deleting stacks.
- [x] Remove the unused broad `execute-api:Invoke` permission from the Gateway
  role after verifying the deployed Gateway target is the named simulator
  Lambda.
- [x] Narrow the remaining environment-specific Harness permissions for Gateway
  invocation and versioned skill-package reads, then tighten model and managed
  memory resources to the live Nova 2 Lite inference profile and the
  environment-scoped `northstar_<environment>*_harness-*` memory ARN pattern
  required by the comparison profiles.
- [x] Preserve the published web artifact release ID across stack-only retries
  while recording the separate deployment-attempt ID; verified with a live
  `05-web` publish followed by a live `00-foundation` retry.
- [ ] Decide whether an account-spend budget is appropriate for the demo
  account; no billing resource was created without explicit authorization.
- [x] Run the non-destructive cleanup proof in a disposable environment. The
  plan-only utility enumerated the seven manifest-derived
  `northstar-cleanup-proof-*` stacks in reverse dependency order and confirmed
  that no AWS resources were changed. Destructive bucket purge and stack
  deletion were intentionally not executed without explicit authorization.
- [x] Add demo reset, preflight checks, fallback scenario, and seeded sample
  runs; the required preflight and hosted smoke are part of the manifest gate.
- [x] Run the hosted React shell smoke plus accessibility/browser checks at
  desktop and 390px mobile widths; confirm labels, image alternatives, root
  visibility, and no horizontal overflow.
- [x] Run representative local load/security checks: 32 concurrent read-only
  catalog requests, protected-route/auth/admin tests, `npm audit`, and the
  deployment contract suite.
- [x] Add and run bounded hosted load/security checks: unauthenticated `401`
  plus rate-limit header, malformed-bearer `401`, 32 concurrent authenticated
  read-only catalog requests, and regular-operator `403` on the admin route.
  The reproducible opt-in test is `tests/load/test_hosted_api_smoke.py`.
- [x] Keep larger distributed/hosted load and security testing out of the
  current demo scope. The bounded hosted checks and the credential-free
  maximum 128-request protected-catalog burst are sufficient for this
  implementation; no distributed authenticated-capacity claim is made.
- [x] Create the five-minute scripted walkthrough and architecture README.
- [x] Validate that the managed Gateway selector has no real-system mutation path;
  all writes remain synthetic and application-authorized.

**Deliverables:** Hosted demo, architecture diagram, deployment guide,
operator guide, deployment-context/recovery procedure, recorded walkthrough,
technical case study, and cleanup checklist.

**Exit criteria:** Flagship scenario can be replayed reliably; website accurately
reflects actual simulator state; a clean checkout can plan, deploy, smoke-test,
and report the release; a failed later stack can be retried without rebuilding
earlier stacks; any introduced container images are digest-pinned; all AWS resources can be cleaned up;
and no real-system mutation path exists.

### Phase 8 — Managed prompt lifecycle and administration

**Goal:** Move runtime prompt prose into Amazon Bedrock Prompt Management and
make prompt changes observable, versioned, and restricted to Cognito admins.

**Implementation status (2026-11-21/22):** Complete and deployed. The Python
adapter now renders investigation, approval-resumption, and evaluation prompts
through `harness/prompt_management.py`; prompt prose is stored in native Bedrock
Prompt Management resources and the local seed is used only for offline tests.
The AgentCore Harness safety contract remains in its declarative configuration.
The `agent-poc` implementation is the reference for Prompt resources,
immutable versions, DynamoDB-selected active versions, and the admin Prompt
Library; Northstar reuses those mechanics without importing LedgerFlow
resources or handlers.

**Prompt contract**

| Prompt key | Current source | Runtime purpose | Required input |
| --- | --- | --- | --- |
| `investigation` | Bedrock Prompt Management | Initial evidence-gathering turn | objective, scenario run ID, optional incident ID |
| `approval_resumption` | Bedrock Prompt Management | Read-only rehydration after an API-owned approved operation | objective, run ID, approval ID, operation ID, action |
| `evaluation` | Bedrock Prompt Management | Bounded read-only smoke/evaluation invocation | objective, run ID, required tools |

The managed templates retain `{{input}}` as the stable Prompt Management
variable, following the `agent-poc` runtime contract. Python will serialize
only the per-run context into that variable; prompt instructions will live in
the Bedrock prompt draft/version. Agent Skills remain versioned `SKILL.md`
packages because they are Harness context packages, not prompt versions.

**Tasks**

- [x] Add native `AWS::Bedrock::Prompt` and `AWS::Bedrock::PromptVersion`
  resources to `03-harness`, with outputs for each base and initial version ARN.
- [x] Add the prompt-selection DynamoDB table to `01-state`; use it only for
  operator-selected active immutable versions, with the initial CloudFormation
  version as the fallback.
- [x] Grant the existing simulator/API Lambda only the Prompt Management and
  selection-table permissions required by the admin UI and runtime resolver.
- [x] Replace Python prompt prose with a Bedrock-backed resolver that reads the
  active immutable version, renders `{{input}}`, and records prompt key/version
  metadata without recording full prompt text.
- [x] Add Cognito-admin-only prompt APIs for draft editing, version creation,
  version selection, version history, and safe preview rendering.
- [x] Add the React Prompt Library using the `agent-poc` presentation pattern:
  draft editor, required-variable display, immutable version history, active
  version selection, and an explicit draft-versus-runtime warning.
- [x] Add local deterministic prompt seed fixtures for tests only; cloud
  runtime behavior must fail closed to Bedrock when `NORTHSTAR_PROMPT_SOURCE`
  is `bedrock`.
- [x] Test admin authorization, active-version selection, draft isolation,
  renderer contracts, deployment outputs, and the existing Harness/API flows.
- [x] Deploy through the existing `deploy-all.sh` manifest flow with the
  configured AWS credentials; do not add a deployment script, failure injection,
  load test, or destructive cleanup step. The `dev` deployment completed in
  `us-east-1`; the simulator/API update was resumed from `02-simulator`, the
  prompt stack was updated independently, and the provision-only smoke refreshed
  the AgentCore binding without invoking a model.

**Phase 8 verification**

- `python3 -m pytest -q` — 89 passed, 4 skipped.
- `python3 cloudformation/validate.py` — 7 independent stack contracts
  validated.
- `npm run build` in `apps/web` — TypeScript and Vite production build passed.
- `deploy-all.sh --from 02-simulator --resolve-s3` and the independent
  `--stack 03-harness --resolve-s3` update completed successfully.
- Bedrock contains the three native prompt resources and their initial
  CloudFormation versions; the active evaluation selection resolves to its
  validated immutable version.
- A live `PromptManager` render using the deployed AWS profile resolved
  `evaluation` version 3 from DynamoDB and substituted the required tool list
  and scenario run ID.
- The hosted admin route returns `401` without a Cognito ID token, and the
  local API tests cover the admin-only `403` boundary and successful admin
  operations.
- `harness.smoke_test --provision` completed successfully and refreshed the
  deployed Harness/Gateway binding without a load test or model invocation.
- `README.md`, `TUTORIAL.md`, and `DEPLOYMENT.md` now describe the managed
  prompt lifecycle and the existing manifest deployment pattern.

**Exit criteria:** Met for the implementation and hosted boundary. No runtime
prompt prose remains in Python; the admin UI/API supports draft editing,
immutable version creation, activation, and rollback; the deployed resolver
uses the selected version; and non-admin users receive `403`. An authenticated
browser walkthrough remains an operator-level check when a Cognito admin token
is available.

## 9. Phase-to-business-requirements traceability

| Business requirement group                  | Implementation phases      |
| ------------------------------------------- | -------------------------- |
| BR-001–006: Interaction and planning        | 2, 3, 5                    |
| BR-007–012: Synthetic environment and reset | 1                          |
| BR-013–019: Visual environment              | 3                          |
| BR-020–025: Agent loop and timeline         | 2, 3                       |
| BR-026–032: Approval and policy             | 4                          |
| BR-033–039: Failure and recovery            | 1, 5                       |
| BR-040–045: Memory and continuity           | 5                          |
| BR-046–051: Scenario management             | 1, 3, 6                    |
| BR-052–060: Evaluation                      | 6                          |
| NFRs: Reliability, safety, auditability     | All phases; hardening in 7 |

## 10. Suggested repository structure

```text
northstar-agentcore-harness-demo/
  README.md
  deploy-all.sh
  Dockerfile
  docs/
    business-requirements.md
    technical-plan.md
    architecture.md
    deployment.md
    adr/
  cloudformation/
    deployment.yaml
    stacks/
      <northstar-stack>.yaml
      <northstar-stack>.json
    scripts/
    build/
  apps/
    web/
    api/
  harness/
    config/
    skills/
      vpn-diagnostics/SKILL.md
      incident-triage/SKILL.md
      safe-remediation/SKILL.md
      post-change-verification/SKILL.md
  simulator/
    models/
    services/
    tools/
    state_engine/
    fault_injection/
    generators/
    fixtures/
  scenarios/
    definitions/
    expected_outcomes/
  evaluation/
    assertions/
    runner/
    reports/
  tests/
    unit/
    contract/
    deployment/
    integration/
    end_to_end/
```

## 11. Critical risks and mitigations

| Risk                                                | Mitigation                                                                            |
| --------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Demo is only a scripted animation                   | Require tool-generated, persisted mutations and independent state assertions          |
| Harness loop is accidentally rebuilt in custom code | Architecture review and integration test proving Harness handles model/tool iteration |
| Model claims success after an API acknowledgment    | Require post-action authoritative verification                                        |
| Approval replay or scope escalation                 | Bind approval to exact parameters, run, actor, target set, and expiry                 |
| Duplicate mutations after ambiguous timeouts        | Idempotency keys, operation ledger, reconciliation                                    |
| Lost/misordered live events                         | Durable sequence numbers, snapshot + replay, reconnect logic                          |
| Cross-run memory or state leakage                   | Run-scoped state and actor/session isolation tests                                    |
| Prompt injection in synthetic KB/tickets            | Treat retrieved content as data; enforce policy outside the model                     |
| Unpredictable model costs or latency                | Fixed scenario budgets, iteration limits, usage telemetry, cleanup                    |
| Deployment scaffold retains LedgerFlow assumptions  | Rename handlers, context keys, stack IDs, and image paths; add Northstar contract tests before cloud deployment |
| Stack drift or hidden cross-stack coupling          | Require manifest ordering, matching template/config validation, output-context checks, and no exports/nested stacks |
| Tutorial concepts remain prose only                 | Maintain the tutorial concept-to-proof matrix; every row needs a demo and automated assertion |
| Local and cloud behavior diverge                    | Run the same deterministic scenarios locally and after deployment, with explicit Harness-only cloud gates |
| AWS capability/configuration drift                  | Pin CLI/SDK versions, check region support, verify API contracts during Phase 0       |

## 12. Final definition of done

The demonstration is complete when an operator can select a synthetic incident, issue a natural-language objective, observe **real managed Harness reasoning/tool execution**, approve an exact remediation, watch the simulated environment change, inject a partial failure, see the agent recover or escalate, and review an independent state-based evaluation.

The essential proof is the chain:

**Business objective -> managed agent loop -> authorized tool call -> persisted synthetic state transition -> independent verification -> visible outcome.**

The delivery is also complete only when:

- Every tutorial phase has a visible demonstration and an automated assertion.
- A clean checkout can run tests, validate contracts, produce a non-mutating plan,
  deploy the target-specific stack graph, and run a hosted smoke test.
- A later stack can be retried from persisted CloudFormation outputs and an
  immutable image digest without redeploying unrelated earlier stacks.
- The deployment context, release ID, scenario seed, Harness session, operation
  IDs, and evaluation report can be correlated for a single run.
- Cleanup is documented and tested, with no path from the demo to real-system
  infrastructure or credentials.

## 13. Official AWS references

- [AgentCore Harness overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [Harness getting started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-get-started.html)
- [Harness lifecycle hooks](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-lifecycle-hooks.html)
- [Harness memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)
- [Harness skills](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-skills.html)
- [Harness tools and allowed tool names](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html)
- [Gateway OpenAPI schema](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-schema-openapi.html)
- [Gateway Lambda function targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)
- [Gateway tool naming](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html)

_Implementation note: exact CLI/SDK configuration and service availability should be validated against the current AWS documentation in Phase 0._

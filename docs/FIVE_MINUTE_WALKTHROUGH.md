# Northstar Five-Minute Walkthrough

This script demonstrates the managed Harness boundary, the synthetic digital
twin, authorization, recovery, and independent evaluation. It assumes the
`dev` environment is already deployed and that an administrator and a regular
synthetic operator exist in Cognito.

## 0:00–0:45 — Explain the boundary

Open the `WebUrl` output from `northstar-dev-05-web`. Point out that the page
is a bundled React application served directly by the simulator Lambda Function
URL. No browser AWS credentials or CDN-hosted JavaScript are involved.

Show the architecture diagram in [`ARCHITECTURE.md`](ARCHITECTURE.md), then
state the key rule: AgentCore Harness chooses among read-only observations;
the FastAPI boundary owns identity, approvals, mutations, and verification.

## 0:45–1:30 — Sign in and show the digital twin

Sign in as the regular synthetic operator. Open the scenario catalog and select
`vpn-deployment-failure`. Start a run and show:

- the connected device and service state;
- the event/timeline view;
- the scenario run ID and authoritative snapshot;
- the evaluation panel, which is separate from the agent's prose.

The browser calls the Lambda Function URL directly. Protected routes require a
Cognito ID token.

## 1:30–2:30 — Show managed observation

Start the investigation. Explain that the application starts the invocation
but does not run a local model/tool/model loop. The managed Harness selects
Gateway operations such as `get_incident`, `get_device_diagnostics`, and
`search_knowledge_base`.

Open the activity timeline and point out the run-scoped tool observations. A
successful final sentence without a Gateway observation is not sufficient for
the cloud gate.

## 2:30–3:30 — Show approval and admin separation

Use the operator account to propose a synthetic rollback for the exact
affected-device set. The proposal is pending and cannot execute yet.

Switch to the administrator account and approve it. Return to the operator
view, execute the operation once, and show the transition:

```text
requested -> approved -> executed -> verified
```

If useful, show that a regular operator cannot approve the proposal and cannot
invite a Cognito user. Only the `northstar-admin` group may call those admin
routes.

## 3:30–4:15 — Show recovery instead of a happy path

Reset the run and enable `timeout-after-commit` or
`memory-unavailable-resume`. Explain that a timeout after a state mutation is
reported as `executed`/`unverified`; the system does not claim success. Clear
the fault and retry the same immutable operation. Reconciliation limits the
retry to unresolved targets and the authoritative verification can then reach
`verified`.

For the memory fault, show that compaction preserves the objective, run ID,
approval constraint, pending work, unresolved targets, and verified outcomes,
then falls back to the simulator's authoritative summary.

## 4:15–5:00 — Show evaluation and deployment evidence

Open the scorecard. Highlight hard gates, policy compliance, recovery count,
durable event evidence, and the trace-to-timeline links. Explain that the
evaluator reads state/events/typed observations rather than trusting the final
answer.

If demonstrating deployment mechanics, show the deployment context and the
required web smoke, then explain that `--stack 05-web` can retry a later stack
by rehydrating predecessor outputs. The current release also has a private
versioned web artifact, reserved Lambda concurrency, an invocation budget
alarm, and S3 public-access blocks.

## Reset after the demo

Use the UI reset control or the authenticated reset endpoint for the synthetic
run. Do not delete AWS resources during the walkthrough. Full cleanup is a
separate operator action described in the Phase 7 checklist in [`PLAN.md`](../PLAN.md).

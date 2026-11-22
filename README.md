# Northstar IT Operations

This case study and its implementation were created with substantial
assistance from AI. AI generated much of the code and helped develop the
accompanying materials, while I directed the architecture, made the design
decisions, and reviewed the results. This project is intended as a tutorial.

## Overview

Northstar represents the internal IT operations and service-management team of
an enterprise. That team supports employees and business departments by
keeping company devices, access, business applications, and shared services
working.

Northstar is a web application for demonstrating how an AI operations agent
works through one of that team's incidents from investigation to verified
resolution.

The system gives the agent an incident and access to connected records from a
fictional company. The agent gathers evidence with tools, explains what it
found, recommends the next action, pauses for human approval when a change is
required, performs the approved change, and verifies the resulting state.

The web console makes the process visible in real time: users can follow the
agent's tool calls, observations, decisions, approvals, changes, recovery
steps, and final evaluation. The goal is to show a controlled agent workflow,
not simply generate a text answer.

All company data and infrastructure are simulated. Northstar is a safe test
environment for exploring Amazon Bedrock AgentCore Harness concepts such as
tool use, durable runs, approval gates, recovery, and independent evaluation;
it does not change real infrastructure.

## Features

- Choose from concrete workplace situations, such as employees losing access
  after a software update, Finance and Sales being unable to use the payments
  portal, or an employee requesting temporary access to a restricted system.
- Read a plain-language explanation of who is affected, what is blocked, and
  what decision the agent must make before starting an investigation.
- Let the agent investigate using read-only tools against the digital twin.
- Watch tool calls, observations, decisions, and run state in the React console.
- Require approval before the agent performs a synthetic remediation.
- Verify the result with an independent evaluator.
- Store runs and event history in DynamoDB and monitor them with polling.
- Run the console from a Lambda Function URL with Cognito authentication; only
  administrators can invite users.
- Let Cognito administrators edit Bedrock Prompt Management drafts, preview
  rendered prompts, create immutable versions, and select the version used by
  future investigations.
- Exercise AgentCore Harness behavior including checkpoints, compaction,
  recovery, and fault handling.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the manifest-driven deployment pattern,
prerequisites, stack-only retries, prompt version updates, and verification
commands.

## Screenshots and walkthrough

The hosted console starts with Cognito sign-in:

![Northstar Cognito sign-in](docs/screenshots/login.jpg)

After authentication, select an incident scenario and create an isolated run:

![Northstar operations dashboard](docs/screenshots/dashboard.jpg)

The console then shows the authoritative environment, investigation activity,
tool observations, and the replayable event timeline:

![Northstar evidence timeline](docs/screenshots/evidence-timeline.jpg)

### Walkthrough

1. Sign in with a Cognito user invited by an administrator.
2. Select an incident scenario from the catalog.
3. Create a run to load the deterministic digital twin.
4. Start the investigation and watch its progress, system checks, and typed
   evidence appear in the timeline.
5. Propose and approve a synthetic remediation when the scenario requires it.
6. Execute the change and use the independent evaluator to verify the outcome.

## AgentCore services

| Service or feature | How Northstar uses it |
| --- | --- |
| AgentCore Harness | Runs the model-driven agent loop: tool selection, iteration, managed memory, and streamed results. |
| AgentCore Gateway | Exposes six read-only synthetic IT operations backed by the simulator Lambda. |
| AgentCore Memory | Provides managed Harness continuity; DynamoDB remains the authoritative business state. |
| Harness Skills | Supplies versioned diagnostic, triage, remediation-safety, and verification guidance from S3. |
| AgentCore Runtime | Not used; the API and React console run in Lambda behind a Function URL. |
| AgentCore Identity | Not used; Cognito handles user authentication and administrator membership. |
| AgentCore Policy | Not used as a separate service; application authorization and a Harness lifecycle hook enforce the safety boundary. |
| AgentCore Observability | Not separately provisioned; the application records normalized run events in DynamoDB and logs. |
| AgentCore Evaluations | Not used; the repository’s independent evaluator scores the deterministic simulator state and expected outcomes. |
| Bedrock Prompt Management | Supporting service used to store, version, preview, and activate the prompts sent to Harness. |

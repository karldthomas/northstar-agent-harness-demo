# Using Amazon Bedrock AgentCore in Northstar

This tutorial explains only how Amazon Bedrock AgentCore is used in the
Northstar demo. Northstar is a synthetic IT operations environment: an agent
investigates an operational problem, gathers evidence from a simulator, and
produces a business-facing recommendation. A separate application boundary
owns authoritative state changes and approval.

## The central idea

**AgentCore Harness owns model reasoning, tool selection, iteration, and stop
conditions.**

Northstar starts a managed Harness invocation and records the resulting stream.
It does not implement a Python model/tool/model loop, choose the next tool, or
decide when the agent should stop.

The demo intentionally makes that boundary visible:

| Responsibility | Northstar | AgentCore |
| --- | --- | --- |
| Start an invocation | Creates the run, session, and prompt context |  |
| Reason about the objective |  | Harness |
| Select the next tool |  | Harness |
| Repeat tool calls and model turns |  | Harness |
| Stop the investigation |  | Harness |
| Execute a bounded simulator operation | Supplies the Gateway target | Gateway |
| Enforce the read-only tool boundary | Supplies the lifecycle hook and backend checks | Harness invokes the hook |
| Record authoritative simulator state | Simulator store |  |
| Score the result | Independent evaluator |  |

## 1. AgentCore's role in the demo

Northstar uses these AgentCore capabilities:

| Capability | How Northstar uses it |
| --- | --- |
| **AgentCore Harness** | Runs the model-driven investigation, chooses tools, iterates over observations, and returns a streamed result. |
| **AgentCore Gateway** | Presents six read-only simulator operations to the Harness as callable tools. |
| **Harness lifecycle hook** | Runs before a tool call and rejects tools outside the allow-list or calls without a scenario run ID. |
| **Harness managed memory** | Provides summarization and episodic continuity for a Harness session. |
| **Harness Skills** | Provides versioned operating guidance for diagnosis, triage, safe remediation, and verification. |

The demo also uses Amazon Bedrock Prompt Management as a supporting service.
It stores versioned investigation, approval-resumption, and evaluation prompt
definitions that are rendered before a Harness invocation.

### End-to-end AgentCore flow

~~~mermaid
flowchart LR
    Request["Objective + scenario_run_id"] --> Boundary["Northstar invocation boundary"]
    Boundary --> Prompt["Selected Prompt Management version"]
    Prompt --> Harness["AgentCore Harness"]
    Skills["Harness Skills"] -.-> Harness
    Memory["Managed Harness Memory"] -.-> Harness
    Harness --> Model["Model reasoning"]
    Model --> Hook["before_tool_call hook"]
    Hook --> Gateway["AgentCore Gateway<br/>northstar-simulator"]
    Gateway --> Target["Lambda Gateway target"]
    Target --> Simulator["Run-scoped simulator"]
    Simulator --> Observation["Typed observation"]
    Observation --> Harness
    Harness --> Stream["Harness event stream"]
    Stream --> Records["Northstar event records"]
~~~

The important direction is that observations go back to Harness. Northstar
does not receive one model response and then run its own orchestration loop.

## 2. The Harness invocation boundary

The application-facing code is deliberately thin:

1. Create an <code>InvocationContext</code> containing the scenario run ID,
   session ID, request ID, and trace ID.
2. Resolve the selected managed prompt and substitute its runtime variables.
3. Send one <code>invoke_harness</code> request.
4. Normalize the returned event stream into text, tool-use records, usage,
   metrics, stop reason, and errors.
5. Persist lifecycle events and return the result.

The boundary is implemented in:

- [harness/api.py](harness/api.py), which creates the context, resolves the
  prompt, invokes Harness, and records events.
- [harness/agentcore.py](harness/agentcore.py), which is the small boto3
  adapter around the managed <code>invoke_harness</code> data-plane API.

<code>harness/agentcore.py</code> parses events; it does not dispatch tools or
call the model again after receiving a tool result. A <code>turn_count</code>
is calculated from the stream for reporting, but counting turns is not the
same as implementing the loop.

### What the adapter does not contain

There is no application code equivalent to:

~~~text
while not finished:
    response = model(...)
    tool = choose_tool(response)
    result = call_tool(tool)
    messages.append(result)
~~~

That orchestration is the managed Harness responsibility.

## 3. Harness configuration

The Harness contract is represented by
[harness/config/northstar-harness.yaml](harness/config/northstar-harness.yaml).
Its AgentCore-relevant settings are:

| Setting | Northstar value or purpose |
| --- | --- |
| Model | Bedrock model configured through <code>HARNESS_MODEL_ID</code>. |
| System prompt | Evidence-first investigation instructions and a fixed recommendation format. |
| Gateway tool source | <code>northstar-simulator</code>. |
| Allowed tool selector | <code>@northstar-simulator</code>, the bounded Gateway target selector. |
| Skills | Four versioned skill packages under <code>harness/skills</code>. |
| Memory | <code>SUMMARIZATION</code> and <code>EPISODIC</code> strategies with a seven-day event expiry. |
| Iteration bound | Twelve managed Harness iterations. |
| Token bound | 4,096 configured maximum output tokens. |
| Time bound | 180 seconds. |

The bounds are Harness configuration, not a Python retry or loop. The model can
stop earlier when it has enough evidence, or Harness can stop it when a bound
is reached.

## 4. Gateway tools

The <code>northstar-simulator</code> Gateway target exposes only read-only
operations. The source contract is
[harness/gateway/northstar-tools.openapi.json](harness/gateway/northstar-tools.openapi.json).

| Gateway operation | What the agent learns |
| --- | --- |
| <code>check_service_health</code> | Whether a business service is healthy and which dependencies are affected. |
| <code>get_device_diagnostics</code> | Diagnostics for a device associated with the incident. |
| <code>get_incident</code> | Incident status, impact, timeline, and known evidence. |
| <code>find_affected_devices</code> | Which devices or users are in the affected scope. |
| <code>list_recent_deployments</code> | Recent changes that may correlate with the incident. |
| <code>search_knowledge</code> | Relevant operational guidance from the synthetic knowledge base. |

The Harness sees the Gateway-qualified names, for example:

~~~text
northstar-simulator___check_service_health
northstar-simulator___get_incident
~~~

The <code>@northstar-simulator</code> selector keeps the Harness contract at
the Gateway target boundary. Gateway exposure is not by itself an
authorization grant: the target validates the run-scoped request, and the
lifecycle hook enforces the same read-only boundary before the operation runs.

### Tool-call sequence

~~~mermaid
sequenceDiagram
    participant H as AgentCore Harness
    participant L as before_tool_call hook
    participant G as AgentCore Gateway
    participant T as Simulator target
    participant S as Authoritative simulator state

    H->>L: Request a Gateway tool with run_id
    L->>L: Check tool name and run_id
    alt accepted
        L-->>H: allow
        H->>G: Invoke selected operation
        G->>T: Forward bounded request
        T->>S: Read run-scoped state
        S-->>T: Typed observation
        T-->>G: Tool result
        G-->>H: Observation
    else rejected
        L-->>H: deny
    end
~~~

The model receives observations, not direct access to the simulator store.

## 5. Lifecycle hooks as a Harness safety boundary

[apps/api/hook_handler.py](apps/api/hook_handler.py) implements the
<code>before_tool_call</code> lifecycle hook.

It applies two checks:

1. The requested tool must be one of the six read-only Gateway operations.
2. The tool input must contain a non-empty <code>run_id</code>.

The hook fails closed for invalid tool calls. It does not decide which tool is
useful; Harness has already selected the tool. It only decides whether the
selected call is inside the demo's safety contract.

State-changing operations are not exposed through this read-only Gateway
selector. The application-owned approval boundary performs a synthetic change
when one is approved, after which a new or resumed Harness invocation can
perform read-only verification.

## 6. Skills provide context, not authority

The Harness attaches four skill packages:

| Skill package | Purpose in the investigation |
| --- | --- |
| <code>vpn-diagnostics</code> | Structured connectivity and device-diagnostic guidance. |
| <code>incident-triage</code> | Evidence collection, impact assessment, and uncertainty handling. |
| <code>safe-remediation</code> | Guidance for describing a bounded, approval-required change. |
| <code>post-change-verification</code> | Guidance for checking authoritative state after a change. |

Skills help Harness reason consistently about the domain. They do not add
tools, grant permissions, or change the lifecycle-hook decision. A skill can
describe a safe remediation, but the agent still cannot perform a mutation
through the read-only Gateway contract.

## 7. Managed memory versus authoritative state

Northstar enables Harness managed memory with:

- <code>SUMMARIZATION</code>, so long conversations can be compressed into
  useful context.
- <code>EPISODIC</code>, so relevant prior interaction context can be retained.
- A seven-day event expiry for the demo.

Memory is useful for continuity; it is not the system of record. The
run-scoped simulator and its durable records remain authoritative for:

- current service, incident, device, and deployment state;
- whether an observation was actually returned;
- whether a synthetic change was approved and executed;
- whether post-change verification passed.

This distinction prevents the agent from treating remembered text as proof that
an operational state changed.

## 8. Prompt Management feeds Harness

Northstar keeps prompt content in native Bedrock Prompt Management resources
when the managed source is enabled. The fixed prompt catalog contains:

| Prompt | AgentCore use |
| --- | --- |
| <code>investigation</code> | Starts an evidence-first read-only investigation. |
| <code>approval_resumption</code> | Rehydrates context after an application-owned approved operation and asks Harness to verify the result. |
| <code>evaluation</code> | Gives bounded evaluation runs a consistent evidence contract. |

[harness/prompt_management.py](harness/prompt_management.py) resolves the
operator-selected immutable version, substitutes variables such as
<code>scenario_run_id</code>, and returns the rendered text to the Harness
adapter. Runtime event metadata records the prompt key, version, ARN, and
source without putting the entire prompt body into every event.

Prompt Management changes the instructions supplied to Harness; it does not
replace Harness orchestration or add permissions.

## 9. What AgentCore owns and what Northstar owns

The boundary can be summarized as follows:

| AgentCore Harness owns | Northstar owns |
| --- | --- |
| Model reasoning | Invocation context and run identity |
| Tool selection | Prompt version resolution |
| Iteration over tool observations | Authoritative simulator state |
| Stop conditions | Approval and state-changing application actions |
| Harness memory behavior | Durable lifecycle and observation records |
| Streaming Harness events | Independent evaluation |

The Gateway owns the translation from a selected operation to the bounded
simulator target. The lifecycle hook is the final pre-tool-call check.

## 10. Determinism in the demo

The demo is deterministic where the evaluator needs repeatable evidence:

- the simulator is seeded and run-scoped;
- Gateway tool behavior and synthetic state transitions are controlled;
- scenario definitions and expected outcomes are versioned;
- the evaluator reads state and typed observations rather than judging prose.

The agent itself is still model-driven. These values can vary:

- which useful tool Harness selects first;
- how many managed turns are needed;
- the wording of the report;
- latency and token usage.

So the precise statement is:

> The simulator and evaluator are deterministic; the Harness agent is
> model-driven.

## 11. Evaluation outside the Harness

[evaluation/runner.py](evaluation/runner.py) intentionally sits outside the
Harness invocation. It does not call a model and does not implement another
agent loop.

It combines:

- authoritative simulator state;
- durable lifecycle events;
- typed tool observations;
- versioned expected outcomes;
- usage and latency fields.

This allows the demo to evaluate whether the agent:

- gathered the required evidence;
- stayed inside the read-only tool boundary;
- handled recovery and checkpoint conditions;
- produced a recommendation consistent with observed state;
- avoided claiming that an unexecuted change was completed.

The final model report is useful to a person, but it is not the sole source of
truth for evaluation.

## 12. Example investigation

Consider a business service that employees cannot use.

1. Northstar creates an isolated <code>scenario_run_id</code> and sends the
   objective to the managed Harness.
2. Harness reasons from the objective and selects a read-only Gateway tool,
   such as <code>get_incident</code> or <code>check_service_health</code>.
3. The lifecycle hook checks the tool name and run ID.
4. Gateway invokes the simulator target and returns a typed observation.
5. Harness decides whether another observation is needed, without Northstar
   choosing the next tool.
6. Harness stops when it has enough evidence or reaches its configured bound.
7. The application records the stream and exposes the report, including the
   required recommendation fields.
8. If a state-changing recommendation needs approval, that action remains
   outside the read-only Harness selector. A later Harness invocation verifies
   the authoritative result.

This is the AgentCore lesson in the demo: the managed Harness performs the
agentic work, while the surrounding application supplies bounded tools,
authoritative state, and verification.

## 13. AgentCore capabilities not separately used

These capabilities are intentionally outside the current Northstar AgentCore
boundary:

| Capability | Status in this demo |
| --- | --- |
| AgentCore Runtime | Not used as a separate Northstar component; the application invokes the managed Harness directly. |
| AgentCore Identity | Not used as the tool authorization mechanism. The demo uses its application identity boundary and IAM-controlled service calls. |
| AgentCore Policy | Not configured as a separate policy engine. The current boundary uses the Gateway allow-list, lifecycle hook, backend validation, and IAM. |
| AgentCore Observability | The demo records normalized Harness events and run records directly; it does not depend on a separate AgentCore Observability workflow. |
| AgentCore Evaluations | The independent evaluator is repository code over deterministic simulator evidence, not the separate AgentCore Evaluations service. |

These omissions are deliberate: the tutorial isolates the Harness, Gateway,
Memory, Skills, and lifecycle-hook concepts that the demo exercises.

## 14. Code map

| File | AgentCore concept |
| --- | --- |
| [harness/agentcore.py](harness/agentcore.py) | Thin <code>invoke_harness</code> adapter and event normalization. |
| [harness/api.py](harness/api.py) | Invocation context, prompt resolution, Harness invocation, and event recording. |
| [harness/config/northstar-harness.yaml](harness/config/northstar-harness.yaml) | Harness model, Gateway, Skills, Memory, and execution bounds. |
| [harness/gateway/northstar-tools.openapi.json](harness/gateway/northstar-tools.openapi.json) | Read-only Gateway tool contract. |
| [apps/api/gateway_handler.py](apps/api/gateway_handler.py) | Gateway target dispatch into simulator read operations. |
| [apps/api/hook_handler.py](apps/api/hook_handler.py) | <code>before_tool_call</code> allow/deny decision. |
| [harness/prompt_management.py](harness/prompt_management.py) | Prompt Management version resolution and variable rendering. |
| [evaluation/runner.py](evaluation/runner.py) | Independent evidence-based evaluation. |

## References

- [Amazon Bedrock AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [Get started with AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-get-started.html)
- [Use tools with AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html)
- [Add a Lambda target to AgentCore Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html)
- [Use memory with AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)
- [Use lifecycle hooks with AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-lifecycle-hooks.html)
- [AgentCore Policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
- [Amazon Bedrock Prompt Management](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-view.html)

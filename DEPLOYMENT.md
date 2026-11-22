# Northstar Deployment

This is the operator guide for deploying the Northstar synthetic IT operations
demo. Northstar uses the deployment mechanics copied from `agent-poc`, adapted
for this repository's resources. There is one deployment entry point:
`deploy-all.sh`.

## What the deployment does

The deployment is manifest-driven and keeps the infrastructure stacks
independent. `deploy-all.sh` calls `cloudformation/orchestrate.py`, which reads
`cloudformation/deployment.yaml`, runs the declared lifecycle checks, and
delegates each stack to `cloudformation/sam_deploy.py`.

Stack outputs are written to a deployment-context JSON file and become
`CF_OUT_*` inputs for downstream stacks. The context is a cache for retries;
when a later stack is selected, the deployer rehydrates predecessor outputs
from CloudFormation instead of relying on stale local state.

| Stack | Responsibility |
| --- | --- |
| `00-foundation` | Least-privilege AgentCore Harness and Gateway roles |
| `01-state` | Durable simulator state, fixture storage, and active prompt selections |
| `02-simulator` | Synthetic API, Lambda Function URL, Cognito boundary, and Harness hook |
| `03-harness` | Bedrock Prompt Management resources and initial immutable versions |
| `04-application` | Application capability metadata |
| `05-web` | Private versioned web artifact bucket and direct console URL |
| `06-observability` | CloudWatch log group |

The React application is built locally, uploaded as a private web artifact,
and served through the deployed Lambda Function URL. The browser does not use
AWS credentials or a CDN.

## Prerequisites

Install or make available:

- Python 3.12 or later with the project dependencies from `requirements.txt`
  and `cloudformation/requirements.txt`.
- AWS CLI with credentials for the target account.
- AWS SAM CLI, because the simulator and web stacks use the SAM transform.
- Node.js and npm for the bundled React build.

Confirm the intended identity and region before deploying:

```bash
export AWS_PROFILE=YOUR_AWS_PROFILE
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

The profile must be allowed to deploy CloudFormation/SAM resources, create and
update IAM roles, publish the web artifact, manage Cognito resources, and
create/update Bedrock Prompt Management resources. Bedrock model availability
and account access must also be enabled for the `PromptModelId` configured in
`cloudformation/stacks/03-harness.json`.

## Preflight

Run the checks directly when iterating locally:

```bash
python3 -m pytest -q
python3 cloudformation/validate.py
(cd apps/web && npm run build)
python3 cloudformation/deploy.py --environment dev --plan
```

The final command is non-mutating. It resolves the manifest and configuration
contracts without calling CloudFormation.

## Full deployment

Use a unique release ID for each intentional deployment attempt:

```bash
AWS_PROFILE=YOUR_AWS_PROFILE AWS_REGION=us-east-1 \
  ./deploy-all.sh \
    --environment dev \
    --release-id 20261122-demo \
    --resolve-s3
```

`--resolve-s3` lets SAM create or reuse its managed staging bucket when the
foundation stack does not provide a shared `ArtifactBucketName` output. The
orchestrator still publishes the final React bundle to the private bucket
created by `05-web`.

The normal lifecycle runs tests, validates stack contracts, checks AWS
identity, builds the web bundle, deploys stacks in manifest order, publishes
the web release, and writes the resulting context to:

```text
cloudformation/build/deployment-context-dev.json
```

The command prints the final web URL. It can also be read from CloudFormation:

```bash
aws cloudformation describe-stacks \
  --stack-name northstar-dev-05-web \
  --query 'Stacks[0].Outputs[?OutputKey==`WebUrl`].OutputValue' \
  --output text
```

## Targeted updates and retries

Deploy only one stack when a change is scoped to that stack. Predecessor
outputs are hydrated from AWS automatically:

```bash
AWS_PROFILE=YOUR_AWS_PROFILE AWS_REGION=us-east-1 \
  ./deploy-all.sh --environment dev --stack 03-harness \
  --release-id 20261122-prompt-update --resolve-s3
```

Deploy a stack and all of its downstream dependents with `--from`:

```bash
AWS_PROFILE=YOUR_AWS_PROFILE AWS_REGION=us-east-1 \
  ./deploy-all.sh --environment dev --from 02-simulator \
  --release-id 20261122-api-update --resolve-s3
```

This is the supported recovery path after a later stack needs to be retried.
Do not reset the deployment context or manually recreate CloudFormation
resources. Inspect the failing stack, correct the implementation, and rerun
the smallest valid `--stack` or `--from` scope.

## Prompt Management rollout

`03-harness` creates three native Bedrock Prompt Management resources:

- `investigation`
- `approval-resumption`
- `evaluation`

CloudFormation creates the initial immutable version for each prompt. Runtime
resolution is set to `NORTHSTAR_PROMPT_SOURCE=bedrock`; the API Lambda reads
the active immutable version from the prompt-selection table and falls back to
the initial CloudFormation version when no selection exists.

After signing in as a Cognito user in the `northstar-admin` group, use the
Prompt Library in the React console to:

1. Edit and save a draft.
2. Preview its variables without changing runtime behavior.
3. Create an immutable version.
4. Select the version used by future invocations.

Saving a draft alone is intentionally not a production change. The selection
step is the runtime promotion boundary, and selecting an earlier immutable
version is the rollback path.

For a stack-only prompt-resource update, use `--stack 03-harness`. For a
runtime/API or web change, use `--from 02-simulator` so the downstream context
and bundle are refreshed as well.

## AgentCore provisioning

CloudFormation supplies the simulator outputs and IAM roles. The reusable
provisioner then creates or reuses the AgentCore Gateway target and managed
Harness, and binds the simulator Lambda to the resulting identifiers:

```bash
AWS_PROFILE=YOUR_AWS_PROFILE AWS_REGION=us-east-1 \
  python3 -m harness.smoke_test \
    --provision \
    --profile YOUR_AWS_PROFILE \
    --region us-east-1 \
    --environment dev \
    --context-file cloudformation/build/deployment-context-dev.json
```

`--provision` does not invoke the model. It is the safe post-deployment way to
refresh the control-plane binding. Use `--provision-and-invoke` or an explicit
smoke command only when a model invocation is intended and the resulting AWS
spend is acceptable.

## Verification

For a deployment-only check, verify the stack states and hosted boundary:

```bash
aws cloudformation describe-stacks \
  --stack-name northstar-dev-03-harness \
  --query 'Stacks[0].StackStatus' \
  --output text

curl -i "$(aws cloudformation describe-stacks \
  --stack-name northstar-dev-05-web \
  --query 'Stacks[0].Outputs[?OutputKey==`WebUrl`].OutputValue' \
  --output text)"
```

The expected unauthenticated behavior is a public React shell and a protected
API boundary: `/healthz` is public, while simulator and admin API routes
require a Cognito ID token. Only users in `northstar-admin` can invite users
or manage prompts.

The deployment manifest includes a built-in web smoke. Enable it when desired:

```bash
AWS_PROFILE=YOUR_AWS_PROFILE AWS_REGION=us-east-1 \
  ./deploy-all.sh --environment dev --from 05-web \
  --release-id 20261122-web-verification \
  --resolve-s3 --require-web-smoke
```

There is no load test or failure-injection step in the deployment. The
repository's bounded tests and the stack-only retry path are the intended
verification and recovery mechanisms for this demonstration.

## Reference documentation

- [Deployment manifest](cloudformation/deployment.yaml)
- [Deployment orchestrator](cloudformation/orchestrate.py)
- [SAM/CloudFormation adapter](cloudformation/sam_deploy.py)
- [Tutorial and architecture explanation](TUTORIAL.md)
- [AWS Bedrock Prompt Management](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-management-view.html)
- [`AWS::Bedrock::Prompt`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrock-prompt.html)
- [`AWS::Bedrock::PromptVersion`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrock-promptversion.html)

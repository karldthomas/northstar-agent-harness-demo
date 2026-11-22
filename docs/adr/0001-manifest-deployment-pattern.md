# ADR 0001: Reuse the manifest-driven deployment pattern

## Decision

Northstar uses the deployment mechanics from `agent-poc`:

- `deploy-all.sh` is a thin shell entry point.
- `cloudformation/orchestrate.py` owns lifecycle ordering and delegates stack
  writes to `cloudformation/sam_deploy.py`.
- `cloudformation/deployment.yaml` is the source of stack order and
  dependencies.
- Each stack has a Northstar-owned CloudFormation template and matching JSON
  configuration file.
- Ordinary CloudFormation outputs are hydrated into a deployment context using
  `CF_OUT_<OutputKey>` placeholders.

Northstar does not copy LedgerFlow application resources, templates, or domain
handlers. The initial templates are contract skeletons and will be replaced by
Northstar resources as implementation phases make each boundary real.

## Consequences

Local validation and non-mutating plans work without AWS. Cloud deployment can
resume from an individual stack while reconstructing predecessor outputs. The
deployment flow remains recognizable to the author of `agent-poc`, while the
application and stack graph remain specific to Northstar.

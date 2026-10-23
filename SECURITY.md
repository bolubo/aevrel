# Security Policy

## Supported versions

Security fixes target `main` and the latest published release line. Pre-release
candidates are supported on a best-effort basis until superseded. Older lines
receive fixes only when the issue is reproducible there and the fix is safe.

## Reporting a vulnerability

Do not open a public issue for token, authentication, path traversal,
write-boundary, denial-of-service, supply-chain, or data-exposure issues.

Use GitHub's private vulnerability reporting / Security Advisories for this
repository. Include:

- affected version or commit;
- the smallest reproducible configuration;
- the impact and whether source or target data can be modified, read, or lost;
- required privileges and exposure conditions;
- sanitized logs or proof.

Never include live tokens, private keys, kubeconfigs, checkpoint contents, or
customer data.

## What to expect

Maintainers will acknowledge a report on a best-effort basis, validate it in
private, coordinate a fix and disclosure, and credit the reporter if requested.
Do not disclose the issue publicly until the fix and upgrade path are ready.

There is no bug bounty or guaranteed response SLA.

## Security scope

Examples of in-scope issues include:

- source checkpoint modification or malformed target commit;
- authentication or authorization bypass in agent, gateway, or controller;
- path traversal or unintended filesystem access;
- secret, token, or proof leakage;
- unsafe Helm defaults or privilege escalation;
- remote denial of service that is not merely expected resource exhaustion;
- supply-chain or release-artifact integrity issues.

## Deployment boundary

shardferry agents can read source checkpoints and write target checkpoints.
Run them only on a trusted network, keep `SHARDFERRY_AGENT_TOKEN` in a Secret,
and use mTLS when agents cross a network boundary. Do not expose the artifact
server, gateway, or agent ports directly to the public Internet.

The source checkpoint is treated as read-only by the implementation. If you
find a path where source data can be modified, treat it as a security issue.

## Safe harbor

Good-faith research that follows this policy will not be pursued or reported as
abuse. Do not access data that is not yours, do not disrupt a running training
job, and stop testing once you have enough evidence to report the issue.

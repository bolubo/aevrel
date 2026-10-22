# Security Policy

## Supported versions

Security fixes target `main` and the latest `0.2.x` candidate line. The `0.1.x`
line is legacy and will receive fixes only when the issue is reproducible there.

## Reporting a vulnerability

Please do not open a public issue for token, authentication, path traversal,
write-boundary, or denial-of-service vulnerabilities.

Use GitHub's private vulnerability reporting / Security Advisories for this
repository. Include:

- affected version or commit;
- the smallest reproducible configuration;
- the impact and whether source or target data can be modified;
- any sanitized logs. Never include live tokens, private keys, or checkpoint
  contents.

## Deployment boundary

`shardferry` agents can read source checkpoints and write target checkpoints.
Run them only on a trusted network, keep `SHARDFERRY_AGENT_TOKEN` in a Secret,
and use mTLS when agents cross a network boundary. Do not expose the artifact
server or agent ports directly to the public Internet.

The source checkpoint is treated as read-only by the implementation. If you
find a path where source data can be modified, treat it as a security issue.

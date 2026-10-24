# Support

Collect a redacted support bundle before filing a non-security issue:

```bash
aevrel-support-bundle --namespace aevrel-control
```

The bundle is written as a compressed archive in the current directory. Use
`--output` to select a path.

## What the bundle contains

- Aevrel version and configuration summary;
- Kubernetes object status for Aevrel resources;
- controller, agent, gateway, and worker logs;
- selected events and recent warnings;
- sanitized environment and dependency information.

Secret values and checkpoint contents are excluded by default. Do not add real
credentials or model data to a public issue.

## Include with a report

- the exact command that failed;
- the expected result;
- the observed result and exit code;
- whether the source checkpoint changed;
- relevant sanitized logs;
- Kubernetes and Aevrel versions.

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY](../SECURITY.md).

## Operational checklist

1. Confirm the source checkpoint is unchanged.
2. Check `CheckpointRecord` and `CheckpointReadiness` status.
3. Inspect the latest `RestoreDrill` report.
4. Check target Job/Pod termination messages.
5. Collect a support bundle and attach only sanitized evidence.

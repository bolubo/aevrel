# Support

## Where to ask

- **Usage and troubleshooting:** open an issue with the question template.
- **Reproducible bug:** use the bug report template with version, command,
  environment, and sanitized logs.
- **Feature or integration request:** use the feature or integration template
  and describe the real user workflow.
- **Security issue:** follow [SECURITY.md](SECURITY.md) and do not open a
  public issue.
- **Product documentation:** start at [docs/INDEX.zh-CN.md](docs/INDEX.zh-CN.md)
  and [docs/SUPPORT.zh-CN.md](docs/SUPPORT.zh-CN.md).

## What to include

Include the release or commit, operating system, Python/Go/PyTorch versions,
storage and Kubernetes/Kueue versions, the exact command, expected behavior,
actual behavior, and sanitized evidence.

Never include tokens, private keys, credentials, checkpoint contents, customer
data, or unredacted kubeconfigs.

## Support boundary

The project is currently an alpha/control-plane candidate. Support is best
effort and does not imply a production SLA. `main` and the latest published
release are the primary support targets; older lines receive fixes only when
the issue is reproducible and the fix is safe.

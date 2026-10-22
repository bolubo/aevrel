# Contributing

## Scope

shardferry is a checkpoint readiness and mobility control plane. Keep changes
focused on observable readiness, safe recovery, source immutability, explicit
provider boundaries, and operator/user experience. LRS and Kueue are adapters;
they must not become alternate sources of truth for core status.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
go test ./...
helm lint deploy/helm/shardferry
```

For real DCP work, install the CPU PyTorch extra:

```bash
pip install -e ".[dcp]"
```

## Validation

Before opening a pull request:

```bash
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/shardferry
helm template shardferry deploy/helm/shardferry --include-crds >/tmp/shardferry.yaml
```

Use `scripts/product_acceptance.py` for the local product path. Physical
multi-node and GPU checks are environment-dependent and should be recorded as
evidence, not silently skipped. Optional components should also render with
both enabled:

```bash
helm template shardferry deploy/helm/shardferry --include-crds \
  --set liveWorker.enabled=true --set kueueAdapter.enabled=true \
  >/tmp/shardferry-all-components.yaml
```

## Safety rules

- Never modify the source checkpoint during migration.
- Never commit tokens, private keys, credentials, real checkpoint data, or
  internal machine paths.
- Keep target staging separate from committed target data.
- A failed rank must not be reported as a successful migration.
- Add a regression test for every write-boundary, retry, or recovery bug.

By contributing, you agree that your contribution is licensed under the
project's Apache-2.0 license.

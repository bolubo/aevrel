# Contributing

Thanks for helping improve Aevrel. By participating, you agree to follow
the [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- reproduce and triage issues;
- improve documentation, examples, and operator guidance;
- add tests for recovery, commit boundaries, and failure behavior;
- implement provider, scheduler, or runtime adapters;
- review changes in an area where you have practical experience.

Start with the [good first issue label](https://github.com/bolubo/aevrel/labels/good%20first%20issue)
for bounded tasks with explicit validation.

For a concrete first path, read
[First contribution](docs/FIRST-CONTRIBUTION.md) and the
[development map](docs/DEVELOPMENT-MAP.md).

## Claiming an issue

Comment `/assign` on the issue you want. A bot assigns you within a few seconds,
so you do not have to wait for a maintainer to be awake. Comment `/unassign` to
drop it again — that is always better than going quiet.

Check the assignee field before you start. An issue with a label, or with a
maintainer comment on it, is not thereby taken: only the assignee is. If it is
already someone else's, pick another one — most issues are unclaimed.

If a claimed issue has no pull request after about a week, a maintainer may
release it so it does not sit idle. Being slow is fine; say so and it stays
yours.

Start with an issue for design changes, new providers, scheduler integrations,
public contract changes, or anything that affects recovery or safety.
Use the [RFC guide](docs/RFC-GUIDE.md) for design decisions rather than
opening an implementation PR first.

## Issue and discussion forms

- Use the `task` form for a bounded engineering task with an expected outcome.
- Use the `refactor` form for behavior-preserving changes.
- Use the feature, bug, question, and integration forms for their matching
  workflows.
- State whether you plan to implement the issue; this helps maintainers avoid
  duplicating scope.
- Use the design/idea discussion forms for open-ended proposals when Discussions
  is enabled. Do not start a large implementation PR before the design is
  resolved.

## Scope

Aevrel is a scheduler-neutral training-state control plane. Checkpoint
readiness is the first complete vertical; transfer is one action.

Keep core changes focused on:

- observable state and readiness semantics;
- source immutability and fail-closed behavior;
- explicit provider boundaries;
- portable contracts and proof;
- user and operator experience.

Kueue, LRS, storage backends, and training frameworks are adapters or
providers. They must not become alternate sources of truth for core status.

## Development setup

```bash
make dev
```

That creates `.venv`, installs the editable package with the `dev` and `dcp`
extras, and builds the Go binaries. The equivalent manual steps:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
go build ./...
```

Use `python -m pip install -e ".[dev]"` instead if you do not need the
PyTorch-backed DCP paths. `make dev-native` also builds the Rust hot path.

The repository is split deliberately:

- Go owns the Kubernetes/control-plane authority;
- Python owns training-runtime and data-plane adapters;
- cross-language contracts live under `spec/`;
- public product docs live under `docs/`; internal research and worklogs do not.

## Validation

Run the relevant checks before opening a pull request. `make check` runs the
repository hygiene check, the release check, the Python suite, and the Go
suite; the remaining gates need extra tooling:

```bash
make check
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/aevrel
helm template aevrel deploy/helm/aevrel --include-crds \
  --set liveWorker.enabled=true --set kueueAdapter.enabled=true \
  >/tmp/aevrel-all-components.yaml
python scripts/product_acceptance.py
```

Physical multi-node and GPU checks are environment-dependent. Record them as
evidence when they actually run; never describe them as validated when they
were skipped.

## Pull requests

- Keep the change focused and explain the behavior change, not only the files.
- Link the issue or user workflow that motivates the change.
- Add a regression test for write boundaries, retry, recovery, and failure
  paths.
- Update public docs when the user-visible contract changes.
- Add a `CHANGELOG.md` entry for user-visible changes.
- Call out breaking changes and required upgrade steps explicitly.
- Use a short, plain subject such as `Add readiness report validation` or
  `Fix target commit recovery`. A `type:` prefix is optional; avoid emojis,
  novelty formatting, and generated-looking titles.
- Rebase or update the branch so CI runs against `main`; do not force-push
  someone else's branch.

## Review and merge

Normal changes use lazy consensus and require maintainer approval. New
providers, scheduler integrations, public contracts, and breaking changes need
a `design/rfc` issue first. See [GOVERNANCE.md](GOVERNANCE.md).

Pull requests are never merged solely because they are old. Maintainers may
close a change that is unsafe, unmaintainable, out of scope, or abandoned.

## Safety rules

- Never modify the source checkpoint during migration.
- Never commit tokens, private keys, credentials, real checkpoint data, or
  internal machine paths.
- Keep target staging separate from committed target data.
- A failed rank must not be reported as a successful migration.
- Do not silently weaken readiness, quorum, coverage, or target-load checks.

## Security and conduct

Report vulnerabilities through [SECURITY.md](SECURITY.md), not a public issue.
Report conduct concerns through the channel in
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

By contributing, you agree that your contribution is licensed under the
project's Apache-2.0 license.

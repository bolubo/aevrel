# Releasing

## Versioning

aevrel uses SemVer. Pre-release versions such as `X.Y.Z-rc.N` are labeled
as pre-releases and must not be described as production-ready.

## Before tagging

1. Add or update `CHANGELOG.md`.
2. Add `docs/RELEASE-NOTES-<version>.md`.
3. Update the version in `pyproject.toml`, `src/aevrel/__init__.py`,
   `deploy/helm/aevrel/Chart.yaml`, and the image tags in
   `deploy/helm/aevrel/values.yaml`.
4. Run the repository gate:

```bash
python scripts/check_repo.py
python scripts/check_release.py
python -m pytest -q
go test ./...
go vet ./...
go build ./cmd/controller ./cmd/ctl ./cmd/gateway ./cmd/kueue-adapter
helm lint deploy/helm/aevrel
helm template aevrel deploy/helm/aevrel --include-crds \
  --set liveWorker.enabled=true --set kueueAdapter.enabled=true \
  >/tmp/aevrel-all-components.yaml
python scripts/product_acceptance.py
```

Physical GPU or multi-cluster checks are release evidence only when the
environment actually exists. Never write "validated" for a check that was not
run.

## Tag and publish

```bash
git tag -a vX.Y.Z -m "aevrel vX.Y.Z"
git push origin vX.Y.Z
```

The tag-triggered release workflow builds and publishes container images, the
Helm chart, and the wheel. Create the GitHub Release from the matching
`docs/RELEASE-NOTES-<version>.md` and mark pre-release candidates accordingly.

## Post-release

- Verify the published image tags, chart digest, wheel install, and release
  notes.
- Record any unexpected result in the next patch release.
- If a release is unsafe, publish a patch and mark the old release as
  deprecated; do not silently rewrite history.

## Security releases

Coordinate security fixes privately under [SECURITY.md](SECURITY.md). Publish
only after a fix and upgrade path are available.

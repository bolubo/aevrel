## Summary

<!-- Describe the behavior change, not only the files changed. -->

Fixes #

## Why

<!-- Link the issue, user workflow, or design/RFC. -->

## Process

Pick one:

- [ ] Small, self-contained fix — direct pull request is fine (see
      CONTRIBUTING, "Do you need an issue first?")
- [ ] Issue discussed first: #
- [ ] Design/RFC discussed first

## Scope and compatibility

- [ ] No public contract change
- [ ] Public contract or CRD change
- [ ] Breaking change; upgrade notes included
- [ ] Adapter/provider-only change

## Validation

- [ ] `python scripts/check_repo.py`
- [ ] `python scripts/check_release.py`
- [ ] Python tests
- [ ] Go tests / vet / build
- [ ] Helm lint/template
- [ ] Optional liveWorker/Kueue Helm render
- [ ] New or changed container image build
- [ ] Real DCP, Kubernetes, GPU, or multi-node evidence when applicable
- [ ] Failure, retry, or recovery path

## Safety

- [ ] Source remains read-only.
- [ ] No secrets, credentials, or checkpoint data are included.
- [ ] A failed rank cannot be reported as success.
- [ ] Readiness, quorum, coverage, and target-load checks were not weakened.

## Docs and release

- [ ] Public docs updated
- [ ] `CHANGELOG.md` updated for a user-visible change
- [ ] Release notes/upgrade impact described

## First-time contributors

If this is your first contribution, include the exact validation command and
result in the pull request. Ask for scope clarification before expanding the
change beyond the linked issue.

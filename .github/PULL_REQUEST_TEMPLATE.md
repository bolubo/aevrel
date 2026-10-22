## What changed

<!-- Describe the behavior change, not only the files changed. -->

## Why

<!-- Link the issue or describe the user workflow. -->

## Validation

- [ ] Python tests
- [ ] Go tests / vet / build
- [ ] Helm lint/template
- [ ] Real DCP test, if applicable
- [ ] Failure or recovery path, if applicable

## Safety

- [ ] Source remains read-only.
- [ ] No secrets or checkpoint data are included.
- [ ] A failed rank cannot be reported as success.

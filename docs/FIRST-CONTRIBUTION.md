# First Contribution

This path takes about 15–30 minutes and uses the existing DCP demo. It does not
require a GPU or Kubernetes cluster.

## 1. Run the first-run acceptance path

```bash
git clone https://github.com/bolubo/aevrel.git
cd aevrel
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev,dcp]"
python -m aevrel demo --dcp --work-dir /tmp/aevrel-first-run --json
```

The expected result includes `verification.passed=true`, source and target
readiness `READY`, and `target_load=ok`.

The same path is checked automatically:

```bash
python scripts/first_run_acceptance.py
```

If this fails, stop and report the exact command and output before taking
another issue.

## 2. Pick a starter issue

The active starter batch is unassigned until a contributor claims an item:

- [#11 restore-drill failure paths](https://github.com/bolubo/aevrel/issues/11)
- [#14 UI state fixtures](https://github.com/bolubo/aevrel/issues/14)
- [#20 target-load failure paths](https://github.com/bolubo/aevrel/issues/20)
- [#21 Helm optional components](https://github.com/bolubo/aevrel/issues/21)
- [#23 capability metadata source](https://github.com/bolubo/aevrel/issues/23)

An unassigned issue is not owned by someone else. Do not assume it is already
taken because it has a label.

## 3. Claim an issue

Comment with:

```text
I'd like to take this. I plan to:
1. ...
2. ...
I will run: <validation command>
```

A maintainer will assign it after the claim is confirmed. Take one starter
issue at a time.

## 4. Find the code

Use the [development map](DEVELOPMENT-MAP.md) to locate the first file and the
smallest relevant test.

Run the narrow test first, then the full checks:

```bash
python -m pytest -q
go test ./...
python scripts/check_repo.py
python scripts/check_release.py
helm lint deploy/helm/aevrel
```

Starter changes should include a regression test or an explicit documentation
validation. Do not describe skipped GPU, multi-node, or Kubernetes checks as
passed.

## 5. Open a pull request

- keep the change inside the issue boundary;
- link the issue;
- include the exact validation commands and results;
- update public docs and the changelog for user-visible changes;
- never commit secrets, real checkpoint data, or internal machine paths.

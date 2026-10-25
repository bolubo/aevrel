# First Contribution

This path takes about 15–30 minutes and uses the existing DCP demo. It does not
require a GPU or Kubernetes cluster.

## 1. Set up and run the first-run acceptance path

```bash
git clone https://github.com/bolubo/aevrel.git
cd aevrel
make dev
. .venv/bin/activate
aevrel demo --dcp --work-dir /tmp/aevrel-first-run --json
```

`make dev` creates `.venv`, installs the editable package with the `dev` and
`dcp` extras, and builds the Go binaries. Add `make dev-native` if you want
the native hot-path tests enabled as well.

The expected result includes `verification.passed=true`, source and target
readiness `READY`, and `target_load=ok`.

The same path is checked automatically:

```bash
python scripts/first_run_acceptance.py
```

If this fails, stop and report the exact command and output before taking
another issue.

## 2. Pick a starter issue

The pinned [Start here](https://github.com/bolubo/aevrel/issues/53) issue maps
the three roadmap tracks onto the open work. For a first task, use the label
query rather than a list copied into this page, so the set cannot go stale:

<https://github.com/bolubo/aevrel/issues?q=is%3Aopen+label%3A%22good+first+issue%22>

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
smallest relevant test. The map also states the language boundary for each
area.

Run the narrow test first, then the full checks:

```bash
make check
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

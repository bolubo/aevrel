# RFC Guide

Use an RFC when a change needs a shared decision before implementation:

- public APIs, CRDs, schemas, or CLI behavior;
- provider or runtime boundaries;
- readiness, restore, or migration semantics;
- scheduler integration contracts;
- security, compatibility, or fail-closed behavior.

Use a normal issue for a bounded bug fix, test, documentation update, or
isolated refactor.

## Evidence markers

Separate evidence from assumptions:

- **FACT**: verified from the current repository or a controlled test;
- **REPORT**: a linked upstream issue, discussion, or public report;
- **ASSUMPTION**: a design belief that still needs testing;
- **OPEN**: a question that needs a maintainer or upstream decision.

Do not present a report or assumption as a fact.

## RFC sections

An RFC should contain:

1. Problem and affected users.
2. Current behavior and boundaries.
3. Evidence, with links and evidence markers.
4. Goals and non-goals.
5. Proposed contract or design.
6. Alternatives and tradeoffs.
7. Failure semantics and security impact.
8. Compatibility and migration impact.
9. Acceptance criteria.
10. Open questions.

## Decision process

1. Open a `design/rfc` issue.
2. Allow at least 48 hours for review.
3. Resolve blocking questions in the issue.
4. Record the accepted scope and non-goals.
5. Split implementation into bounded tasks.
6. Link the implementation tasks back to the RFC.

An RFC must not claim community agreement without a public reference. A
maintainer may close an RFC that is out of scope, unsafe, or not actionable.

## Implementation rule

Do not start a large implementation pull request against an unresolved public
contract. Small experiments may proceed in a branch, but they do not become a
public contract until the RFC is accepted.

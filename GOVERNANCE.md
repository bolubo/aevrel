# Governance

## Model

Aevrel is maintainer-led. The current maintainer and final decision maker
is listed in [MAINTAINERS.md](MAINTAINERS.md).

The project intentionally keeps governance small until there is a sustained
contributor community. Adding process that nobody uses is not a goal.

## Principles

1. User-visible correctness comes before feature count.
2. Source checkpoints are read-only; failed work must never look successful.
3. The control plane owns portable state semantics; implementations and
   integrations are adapters.
4. Provider and scheduler boundaries must remain replaceable.
5. Changes that affect contracts, safety, or release behavior need explicit
   review.

## Roles

Roles and promotion criteria are described in
[CONTRIBUTOR_LADDER.md](CONTRIBUTOR_LADDER.md).

## Decisions

- Normal changes use lazy consensus on a pull request and require maintainer
  approval before merge.
- New providers, schedulers, public contracts, breaking changes, and
  cross-cutting architecture changes start with an issue labeled `design/rfc`.
  Maintainers make the final call after the discussion has had a reasonable
  review window.
- Security reports are handled privately under [SECURITY.md](SECURITY.md).
- Release decisions follow [RELEASING.md](RELEASING.md).
- Maintainers may close issues or pull requests that are off-topic, unsafe,
  unmaintainable, or outside the project's stated scope.

## Maintainers

Maintainer candidates should demonstrate sustained, high-quality contributions,
good review judgment, and comfort with the project's safety rules. Existing
maintainers review the candidate; the current owner makes the final decision.

Inactive maintainers may be moved to emeritus status after six months without
project activity. This is a status change, not a judgment of past work.

## Changes to governance

Governance changes use the same pull-request process. Changes to the decision
model, maintainer roster, or release authority require explicit maintainer
approval and should be recorded in this file.

## Automation boundary

Codex-created automations (heartbeats, assistant cron jobs, or similar
assistant-side scheduled work) are not used for this repository. That is a
constraint on the assistant runtime, not on GitHub.

GitHub-native scheduled workflows and Dependabot are allowed when they have a
clear maintenance purpose. They must not make maintainer decisions, auto-merge
changes, or auto-close issues without human review.

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "claim.yml"


def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def step_script() -> str:
    spec = yaml.safe_load(text())
    return spec["jobs"]["claim"]["steps"][0]["run"]


def commands() -> str:
    """The step with its comment lines dropped.

    The comments deliberately name the call that does not work, so that the
    next reader learns why; an assertion about what the step *does* must not
    trip over that explanation.
    """
    lines = step_script().splitlines()
    return "\n".join(
        line for line in lines if not line.lstrip().startswith("#")
    )


def test_claim_workflow_runs_on_issue_comments_only() -> None:
    """It must not fire on pull requests: a PR comment is not a claim."""
    spec = yaml.safe_load(text())
    assert spec[True]["issue_comment"]["types"] == ["created"]
    assert "github.event.issue.pull_request == null" in spec["jobs"]["claim"]["if"]


def test_claim_workflow_assigns_through_the_api_not_gh_issue_edit() -> None:
    """`gh issue edit --add-assignee` cannot assign an outside contributor.

    The CLI checks the login against `/repos/{owner}/{repo}/assignees` first,
    and that list holds collaborators only, so it refuses every non-collaborator
    before a request is sent — measured against this repository, where the only
    assignable user the API reports is the owner. The assignees endpoint itself
    accepts any user who has commented on the issue, which is exactly the case
    that matters here: the claimant has just commented.

    Pinned as a test because the shorter CLI call looks like an obvious
    simplification and silently breaks every claim.
    """
    script = commands()
    assert "gh issue edit" not in script
    assert 'gh api -X POST "repos/$REPO/issues/$ISSUE/assignees"' in script


def test_claim_workflow_releases_through_the_api_and_verifies_it() -> None:
    script = commands()
    assert 'gh api -X DELETE "repos/$REPO/issues/$ISSUE/assignees"' in script
    # Releasing must not claim success it did not observe.
    assert "Could not release this issue automatically" in script


def test_claim_workflow_ignores_the_command_when_it_is_not_the_first_word() -> None:
    """`/assign` in the middle of a sentence is prose, not a claim."""
    script = step_script()
    assert "firstline=${BODY%%$'\\n'*}" in script
    assert "read -r first _ <<<" in script


def test_claim_workflow_refuses_issues_that_are_waiting_on_a_decision() -> None:
    script = step_script()
    for label in ("blocked", "needs-design", "design/rfc"):
        assert label in script

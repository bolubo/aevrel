from __future__ import annotations

import pytest

from tests.mutation.harness import (
    check_plan,
    drop_checksum,
    drop_chunk,
    duplicate_chunk,
    mark_full_gather,
    shift_offset,
    valid_plan,
    wrong_target_rank,
    wrong_source_rank,
)


def test_valid_plan_has_no_errors(toy_spec) -> None:
    assert check_plan(valid_plan(), toy_spec) == []


@pytest.mark.parametrize(
    "mutate",
    [
        drop_chunk,
        duplicate_chunk,
        shift_offset,
        wrong_target_rank,
        wrong_source_rank,
        drop_checksum,
        mark_full_gather,
    ],
)
def test_mutation_is_detected(toy_spec, mutate) -> None:
    plan = valid_plan()
    mutate(plan)
    assert check_plan(plan, toy_spec), f"mutation {mutate.__name__} was not detected"

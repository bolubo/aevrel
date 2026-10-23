from __future__ import annotations

from copy import deepcopy

from hypothesis import given, settings, strategies as st

from shardferry.planning import plan, validate_plan
from tests.fixtures.generated import generate_sharded_spec
from tests.oracle.reference import (
    assert_bitwise_equal,
    materialize_plan,
    reference_reshard,
)


@st.composite
def tensor_and_meshes(draw):
    tensor_rank = draw(st.integers(min_value=1, max_value=3))
    tensor_shape = tuple(
        draw(st.integers(min_value=2, max_value=7))
        for _ in range(tensor_rank)
    )

    def draw_mesh():
        mesh_rank = draw(st.integers(min_value=1, max_value=2))
        mesh_shape = tuple(
            draw(st.integers(min_value=1, max_value=2))
            for _ in range(mesh_rank)
        )
        placements = []
        for _ in range(mesh_rank):
            placements.append(
                draw(
                    st.one_of(
                        st.just("Replicate()"),
                        st.integers(min_value=0, max_value=tensor_rank - 1).map(
                            lambda dim: f"Shard({dim})"
                        ),
                    )
                )
            )
        return mesh_shape, tuple(placements)

    source_mesh = draw_mesh()
    target_mesh = draw_mesh()
    return tensor_shape, source_mesh, target_mesh


@settings(max_examples=200, deadline=None)
@given(tensor_and_meshes())
def test_plan_matches_independent_oracle(case) -> None:
    tensor_shape, source_mesh, target_mesh = case
    source_shape, source_placements = source_mesh
    target_shape, target_placements = target_mesh
    spec, source_arrays, tensor = generate_sharded_spec(
        tensor_shape,
        source_shape,
        source_placements,
        target_shape,
        target_placements,
    )

    result = plan(spec)
    assert result == plan(deepcopy(spec))
    validate_plan(result, spec)

    produced = materialize_plan(result, spec, source_arrays)
    expected = reference_reshard(tensor, target_shape, target_placements)
    for rank, (_, expected_array) in expected.items():
        assert_bitwise_equal(produced[("w", rank)], expected_array)

    minimum = sum(array.nbytes for _, array in expected.values())
    assert result["total_bytes"] == minimum
    assert result["theoretical_min_bytes"] == minimum

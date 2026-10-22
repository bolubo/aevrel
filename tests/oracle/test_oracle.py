from __future__ import annotations

import numpy as np

from tests.fixtures.toy import make_tensor
from tests.oracle.reference import assert_bitwise_equal, gather_to_global, reshard_1d


def test_oracle_4to2_shard_roundtrip() -> None:
    tensor = make_tensor((8, 4))
    chunks = reshard_1d(tensor, 4, "Shard(0)")

    shards = []
    offset = 0
    for chunk in chunks:
        shards.append(
            {
                "offset": [offset, 0],
                "size": list(chunk.shape),
                "data": chunk,
            }
        )
        offset += chunk.shape[0]

    rebuilt = gather_to_global(shards, tensor.shape)
    assert_bitwise_equal(rebuilt, tensor)

    target_chunks = reshard_1d(rebuilt, 2, "Shard(0)")
    target = np.concatenate(target_chunks, axis=0)
    assert_bitwise_equal(target, tensor)

from __future__ import annotations

from aevrel.lrs.rng_reshard import (
    RNGStream,
    RNGStreamKey,
    RNGStreamRegistry,
)


def test_rng_stream_reshard_follows_data_and_layer() -> None:
    registry = RNGStreamRegistry()
    data = RNGStream(RNGStreamKey("data", 0), seed=1)
    layer = RNGStream(RNGStreamKey("layer", 1, layer=2), seed=2)
    registry.add(data)
    registry.add(layer)
    data.draw(3)
    layer.draw(2)

    moved = registry.reshard(
        {
            RNGStreamKey("data", 0): RNGStreamKey("data", 2),
            RNGStreamKey("layer", 1, layer=2): RNGStreamKey(
                "layer", 2, layer=2
            ),
        }
    )
    assert moved.get(RNGStreamKey("data", 2)).draw(2) == data.draw(2)
    assert (
        moved.get(RNGStreamKey("layer", 2, layer=2)).draw(2)
        == layer.draw(2)
    )


def test_rng_stream_registry_roundtrip() -> None:
    registry = RNGStreamRegistry()
    stream = RNGStream(RNGStreamKey("microbatch", 0, microbatch=3), seed=9)
    registry.add(stream)
    stream.draw(5)
    states = registry.capture()
    expected = stream.draw(4)

    restored = RNGStreamRegistry()
    restored.restore(states)
    assert (
        restored.get(
            RNGStreamKey("microbatch", 0, microbatch=3)
        ).draw(4)
        == expected
    )

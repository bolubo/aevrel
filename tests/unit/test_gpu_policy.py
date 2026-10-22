from __future__ import annotations

import pytest

from shardferry.gpu_policy import decide_codec


def test_policy_rejects_incompressible_data_even_on_slow_wan() -> None:
    decision = decide_codec(
        raw_bytes=128 * 1024 * 1024,
        encoded_bytes=125 * 1024 * 1024,
        compress_seconds=0.28,
        decompress_seconds=0.2,
        network_gbps=1.0,
    )
    assert decision.enabled is False
    assert "gain" in decision.reason or "ratio" in decision.reason


def test_policy_enables_compressible_data_on_slow_wan() -> None:
    decision = decide_codec(
        raw_bytes=128 * 1024 * 1024,
        encoded_bytes=10 * 1024 * 1024,
        compress_seconds=0.3,
        decompress_seconds=0.15,
        network_gbps=1.0,
    )
    assert decision.enabled is True
    assert decision.projected_gain_seconds > 0.05


def test_policy_disables_compressible_data_on_fast_lan() -> None:
    decision = decide_codec(
        raw_bytes=128 * 1024 * 1024,
        encoded_bytes=10 * 1024 * 1024,
        compress_seconds=0.3,
        decompress_seconds=0.15,
        network_gbps=10.0,
    )
    assert decision.enabled is False
    assert "gain" in decision.reason


def test_policy_requires_known_network_bandwidth() -> None:
    decision = decide_codec(
        raw_bytes=1024,
        encoded_bytes=512,
        compress_seconds=0.01,
        decompress_seconds=0.01,
        network_gbps=None,
    )
    assert decision.enabled is False
    assert "bandwidth" in decision.reason


def test_policy_rejects_negative_measurements() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        decide_codec(
            raw_bytes=1024,
            encoded_bytes=512,
            compress_seconds=-1.0,
            decompress_seconds=0.0,
            network_gbps=1.0,
        )


def test_policy_decision_is_json_friendly() -> None:
    decision = decide_codec(
        raw_bytes=1024,
        encoded_bytes=128,
        compress_seconds=0.001,
        decompress_seconds=0.001,
        network_gbps=1.0,
    )
    payload = decision.to_dict()
    assert isinstance(payload["enabled"], bool)
    assert isinstance(payload["ratio"], float)

from __future__ import annotations

import threading

import torch
from torch import nn

from aevrel.lrs.handoff import HandoffServer, local_ip, send_state
from aevrel.lrs.state_closure import capture_state


def test_socket_handoff_roundtrip() -> None:
    model = nn.Linear(4, 2)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    state = capture_state(
        model,
        optimizer,
        step=3,
        consumed_samples=12,
        data_loader={"offset": 12},
    )
    server = HandoffServer(port=0)
    received = []

    def receive() -> None:
        received.append(server.receive(timeout=30.0))

    thread = threading.Thread(target=receive)
    thread.start()
    try:
        send_state(local_ip(), server.port, state, timeout=30.0)
    finally:
        thread.join(timeout=30.0)
        server.close()

    assert len(received) == 1
    restored = received[0]
    assert restored.step == 3
    assert restored.consumed_samples == 12
    assert restored.data_loader == {"offset": 12}
    for name, tensor in state.model.items():
        assert torch.equal(restored.model[name], tensor)

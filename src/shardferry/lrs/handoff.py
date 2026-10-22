"""Two-process training-state handoff over a length-prefixed TCP link."""

from __future__ import annotations

import socket
import struct

from .state_closure import TrainingState


_MAX_STATE_BYTES = 8 * 1024**3


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ConnectionError("handoff connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


class HandoffServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(1)
        bound_host, bound_port = self._server.getsockname()[:2]
        self.host = str(bound_host)
        self.port = int(bound_port)

    def receive(self, timeout: float = 300.0) -> TrainingState:
        self._server.settimeout(timeout)
        connection, _ = self._server.accept()
        with connection:
            connection.settimeout(timeout)
            header = _recv_exact(connection, 8)
            length = struct.unpack("<Q", header)[0]
            if length < 1 or length > _MAX_STATE_BYTES:
                raise ValueError("invalid handoff payload length")
            payload = _recv_exact(connection, length)
            connection.sendall(b"ok")
        return TrainingState.from_bytes(payload)

    def close(self) -> None:
        self._server.close()


def send_state(
    host: str,
    port: int,
    state: TrainingState,
    *,
    timeout: float = 300.0,
) -> None:
    payload = state.to_bytes()
    if len(payload) > _MAX_STATE_BYTES:
        raise ValueError("handoff payload is too large")
    with socket.create_connection(
        (host, port),
        timeout=timeout,
    ) as connection:
        connection.sendall(struct.pack("<Q", len(payload)) + payload)
        acknowledgement = _recv_exact(connection, 2)
        if acknowledgement != b"ok":
            raise RuntimeError("handoff was not acknowledged")

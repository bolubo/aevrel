from __future__ import annotations

import pytest

from aevrel.tls import client_context, server_context


def test_tls_is_optional_without_files(monkeypatch) -> None:
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    assert client_context() is None
    assert server_context() is None


def test_server_context_requires_cert_and_key(monkeypatch) -> None:
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError):
        server_context(cert="server.crt")

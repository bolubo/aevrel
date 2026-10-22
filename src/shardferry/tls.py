from __future__ import annotations

import os
import ssl


def client_context(
    ca: str | None = None,
    cert: str | None = None,
    key: str | None = None,
) -> ssl.SSLContext | None:
    ca = ca or os.environ.get("SHARDFERRY_TLS_CA")
    cert = cert or os.environ.get("SHARDFERRY_TLS_CERT")
    key = key or os.environ.get("SHARDFERRY_TLS_KEY")
    if not any((ca, cert, key)):
        return None
    if (cert is None) != (key is None):
        raise ValueError("TLS cert and key must be provided together")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if ca:
        context.load_verify_locations(ca)
    if cert and key:
        context.load_cert_chain(cert, key)
    return context


def server_context(
    cert: str | None = None,
    key: str | None = None,
    ca: str | None = None,
) -> ssl.SSLContext | None:
    cert = cert or os.environ.get("SHARDFERRY_TLS_CERT")
    key = key or os.environ.get("SHARDFERRY_TLS_KEY")
    ca = ca or os.environ.get("SHARDFERRY_TLS_CA")
    if not any((cert, key, ca)):
        return None
    if not cert or not key:
        raise ValueError("TLS server cert and key are required together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    if ca:
        context.load_verify_locations(ca)
        context.verify_mode = ssl.CERT_REQUIRED
    return context

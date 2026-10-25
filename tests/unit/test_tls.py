from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID

from aevrel.tls import client_context, server_context


@pytest.fixture
def cert_key_pair(tmp_path):
    """Generate a self-signed certificate and matching private key in tmp_path."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(Encoding.PEM))
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
    return str(cert_path), str(key_path)


@pytest.fixture
def other_key(tmp_path):
    """Generate an unmatched private key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "other_key.pem"
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
    return str(key_path)


def test_tls_is_optional_without_files(monkeypatch) -> None:
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    assert client_context() is None
    assert server_context() is None


def test_client_context_cert_without_key_raises(monkeypatch, cert_key_pair) -> None:
    cert_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="TLS cert and key must be provided together"):
        client_context(cert=cert_path)


def test_client_context_key_without_cert_raises(monkeypatch, cert_key_pair) -> None:
    _, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="TLS cert and key must be provided together"):
        client_context(key=key_path)


def test_client_context_valid_ca_only(monkeypatch, cert_key_pair) -> None:
    ca_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    ctx = client_context(ca=ca_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_client_context_valid_cert_and_key(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    ctx = client_context(cert=cert_path, key=key_path)
    assert isinstance(ctx, ssl.SSLContext)


def test_client_context_valid_full_bundle(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    ctx = client_context(ca=cert_path, cert=cert_path, key=key_path)
    assert isinstance(ctx, ssl.SSLContext)


def test_client_context_env_var_fallback(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.setenv("AEVREL_TLS_CA", cert_path)
    monkeypatch.setenv("AEVREL_TLS_CERT", cert_path)
    monkeypatch.setenv("AEVREL_TLS_KEY", key_path)
    ctx = client_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_client_context_invalid_ca_raises(monkeypatch, tmp_path) -> None:
    bad_ca = tmp_path / "bad_ca.pem"
    bad_ca.write_text("NOT A VALID CERTIFICATE")
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ssl.SSLError):
        client_context(ca=str(bad_ca))


def test_client_context_mismatched_key_raises(monkeypatch, cert_key_pair, other_key) -> None:
    cert_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ssl.SSLError):
        client_context(cert=cert_path, key=other_key)


def test_server_context_requires_cert_and_key(monkeypatch, cert_key_pair) -> None:
    cert_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="TLS server cert and key are required together"):
        server_context(cert=cert_path)


def test_server_context_key_only_raises(monkeypatch, cert_key_pair) -> None:
    _, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="TLS server cert and key are required together"):
        server_context(key=key_path)


def test_server_context_ca_only_raises(monkeypatch, cert_key_pair) -> None:
    ca_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="TLS server cert and key are required together"):
        server_context(ca=ca_path)


def test_server_context_valid_cert_and_key(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    ctx = server_context(cert=cert_path, key=key_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE


def test_server_context_valid_with_client_auth_ca(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    ctx = server_context(cert=cert_path, key=key_path, ca=cert_path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_server_context_env_var_fallback(monkeypatch, cert_key_pair) -> None:
    cert_path, key_path = cert_key_pair
    monkeypatch.setenv("AEVREL_TLS_CERT", cert_path)
    monkeypatch.setenv("AEVREL_TLS_KEY", key_path)
    monkeypatch.setenv("AEVREL_TLS_CA", cert_path)
    ctx = server_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_server_context_mismatched_key_raises(monkeypatch, cert_key_pair, other_key) -> None:
    cert_path, _ = cert_key_pair
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ssl.SSLError):
        server_context(cert=cert_path, key=other_key)


def test_server_context_invalid_ca_raises(monkeypatch, cert_key_pair, tmp_path) -> None:
    cert_path, key_path = cert_key_pair
    bad_ca = tmp_path / "bad_ca.pem"
    bad_ca.write_text("NOT A VALID CERTIFICATE")
    monkeypatch.delenv("AEVREL_TLS_CA", raising=False)
    monkeypatch.delenv("AEVREL_TLS_CERT", raising=False)
    monkeypatch.delenv("AEVREL_TLS_KEY", raising=False)
    with pytest.raises(ssl.SSLError):
        server_context(cert=cert_path, key=key_path, ca=str(bad_ca))

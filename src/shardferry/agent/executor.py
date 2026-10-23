from __future__ import annotations

import json
import os
import ssl
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .state import TaskRecord
from ..dcp import export_dcp_local
from ..executor import apply, commit_staged
from ..manifest import load_payload
from ..verification import build_proof
from ..verification import DcpTargetReady


class ArtifactError(ValueError):
    """Raised when a task artifact reference cannot be resolved."""


class ArtifactResolver(Protocol):
    def resolve(self, ref: str) -> dict[str, Any]:
        ...


class LocalArtifactResolver:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def resolve(self, ref: str) -> dict[str, Any]:
        candidate = Path(ref)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactError(f"artifact escapes root: {ref}") from exc
        return load_payload(candidate)


class HttpArtifactResolver:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        use_proxy: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        handlers = []
        context = _tls_context_from_env()
        if context is not None:
            handlers.append(HTTPSHandler(context=context))
        if not use_proxy:
            handlers.append(ProxyHandler({}))
        self._opener = build_opener(*handlers) if handlers else None

    def resolve(self, ref: str) -> dict[str, Any]:
        url = (
            ref
            if ref.startswith(("http://", "https://"))
            else self.base_url + "/v1/artifacts/" + quote(ref, safe=":")
        )
        if not (
            url == self.base_url
            or url.startswith(self.base_url + "/")
        ):
            raise ArtifactError(f"artifact URL escapes base: {ref}")
        request = Request(url)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            if self._opener is not None:
                response = self._opener.open(request, timeout=self.timeout)
            else:
                from urllib.request import urlopen

                response = urlopen(request, timeout=self.timeout)
            with response:
                data = response.read()
        except HTTPError as exc:
            raise ArtifactError(f"artifact HTTP {exc.code}: {url}") from exc
        except (URLError, OSError) as exc:
            raise ArtifactError(f"artifact request failed: {url}: {exc}") from exc
        try:
            payload = json.loads(data)
        except ValueError as exc:
            raise ArtifactError(f"artifact is not valid JSON: {url}") from exc
        if not isinstance(payload, dict):
            raise ArtifactError(f"artifact must be a JSON object: {url}")
        return payload


def _tls_context_from_env() -> ssl.SSLContext | None:
    ca = os.environ.get("SHARDFERRY_TLS_CA", "")
    cert = os.environ.get("SHARDFERRY_TLS_CERT", "")
    key = os.environ.get("SHARDFERRY_TLS_KEY", "")
    if not (ca or cert or key):
        return None
    if bool(cert) != bool(key):
        raise ArtifactError("TLS cert and key are required together")
    context = ssl.create_default_context(cafile=ca or None)
    if cert:
        context.load_cert_chain(cert, key)
    return context


class TargetTaskExecutor:
    def __init__(
        self,
        resolver: ArtifactResolver,
        source_token: str | None = None,
        *,
        proof_enabled: bool = False,
        load_smoke: bool = False,
    ) -> None:
        self.resolver = resolver
        self.source_token = source_token
        self.proof_enabled = proof_enabled
        self.load_smoke = load_smoke

    def __call__(self, task: TaskRecord) -> dict[str, Any]:
        spec = self.resolver.resolve(task.spec_ref)
        plan = self.resolver.resolve(task.plan_ref)
        return apply(
            plan,
            spec,
            target_rank=task.rank,
            target_root=task.target_root,
            commit=False,
            token=self.source_token,
        )

    def commit(self, task: TaskRecord) -> dict[str, Any]:
        spec = self.resolver.resolve(task.spec_ref)
        plan = self.resolver.resolve(task.plan_ref)
        report = commit_staged(
            plan,
            spec,
            target_rank=task.rank,
            target_root=task.target_root,
            token=self.source_token,
        )
        if spec["checkpoint"]["format"] == "dcp":
            dcp_root = export_dcp_local(
                spec,
                task.target_root,
                raw_root=task.target_root,
                ranks=(task.rank,),
                force=True,
            )
            if self.load_smoke:
                DcpTargetReady(
                    task.target_root,
                    rank=task.rank,
                )(task.migration_uid)
            report = dict(report)
            report["dcp_root"] = str(dcp_root)
        if self.proof_enabled:
            proof = build_proof(
                spec,
                plan,
                task.target_root,
                token=self.source_token,
                target_rank=task.rank,
            )
            result = dict(report)
            result["proof"] = proof
            return result
        return report

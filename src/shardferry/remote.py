from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, build_opener

import yaml

from .identity import source_id_for_shard
from .manifest import validate_spec


def rewrite_source_uris(
    spec: dict,
    base_url: str,
    target_storage: str | None = None,
) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    result = deepcopy(spec)
    rank_endpoints = {
        int(entry["rank"]): entry["endpoint"]
        for entry in result["source"]["mesh"].get("rank_map", [])
    }
    for endpoint in rank_endpoints.values():
        endpoint_parsed = urlparse(endpoint)
        if endpoint_parsed.scheme not in {"http", "https"} or not endpoint_parsed.netloc:
            raise ValueError(f"rank_map endpoint is not an http(s) URL: {endpoint}")
    for shard in result["source"]["shards"]:
        tensor = quote(shard["tensor"], safe="")
        endpoint = rank_endpoints.get(shard["rank"], base_url)
        identifier = quote(source_id_for_shard(shard), safe=":")
        shard["uri"] = (
            f"{endpoint.rstrip('/')}/shard/{tensor}/{shard['rank']}/{identifier}"
        )
        shard["id"] = identifier
    if target_storage is not None:
        result["target"]["storage"]["uri"] = Path(target_storage).resolve().as_uri()
    validate_spec(result)
    return result


def fetch_spec(
    server_url: str,
    target_storage: str,
    out: str,
) -> dict:
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server URL must be an absolute http(s) URL")
    manifest_url = server_url.rstrip("/") + "/manifest"
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(manifest_url, timeout=30) as response:
            payload = yaml.safe_load(response.read().decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"could not fetch manifest from {manifest_url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("server manifest is not a mapping")
    result = rewrite_source_uris(payload, server_url, target_storage)
    Path(out).write_text(
        yaml.safe_dump(result, sort_keys=False),
        encoding="utf-8",
    )
    return result

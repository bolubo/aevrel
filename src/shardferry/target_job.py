"""Kubernetes target-Job adapter for online handoff."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .verification.target_ready import TargetReadyError


REPORT_PATH = "/dev/termination-log"


@dataclass(frozen=True)
class TargetJobConfig:
    template: Path
    namespace: str
    world_size: int
    steps: int
    job_id: str
    checkpoint_template: str
    state_template: str | None = None
    state_providers: str = "python.random,numpy.random,torch.cpu.rng"
    require_state: bool = False
    timeout: float = 900.0
    kubectl: str = "kubectl"

    def __post_init__(self) -> None:
        if isinstance(self.template, str):
            object.__setattr__(self, "template", Path(self.template))
        if self.world_size < 1:
            raise ValueError("target world size must be positive")
        if self.steps < 1:
            raise ValueError("target validation steps must be positive")
        if self.timeout <= 0:
            raise ValueError("target job timeout must be positive")
        if not self.job_id:
            raise ValueError("handoff job id must not be empty")
        if self.require_state and not self.state_template:
            raise ValueError("require_state needs state_template")
        if not self.state_providers.strip():
            raise ValueError("state_providers must not be empty")


class KubernetesTargetReady:
    """Run an Indexed Job and validate all per-rank termination reports."""

    def __init__(self, config: TargetJobConfig) -> None:
        self.config = config

    def __call__(self, version_id: str) -> None:
        job = self.render(version_id)
        name = job["metadata"]["name"]
        self._run(
            ["apply", "-f", "-"],
            input_text=yaml.safe_dump(job, sort_keys=False),
        )
        try:
            self._run(
                [
                    "wait",
                    "--for=condition=complete",
                    f"job/{name}",
                    f"--timeout={int(self.config.timeout)}s",
                ],
            )
        except TargetReadyError as exc:
            raise TargetReadyError(
                f"target validation Job {name} did not complete: {exc}"
            ) from exc
        reports = self._reports(name)
        evaluate_target_reports(
            reports,
            world_size=self.config.world_size,
            version=version_id,
            job_id=self.config.job_id,
            steps=self.config.steps,
            require_state=self.config.require_state,
        )

    def render(self, version_id: str) -> dict[str, Any]:
        payload = yaml.safe_load(
            self.config.template.read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict):
            raise TargetReadyError("target Job template must be a YAML object")
        if payload.get("apiVersion") != "batch/v1" or payload.get("kind") != "Job":
            raise TargetReadyError("target Job template must be batch/v1 Job")
        metadata = payload.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise TargetReadyError("target Job metadata must be an object")
        metadata["namespace"] = self.config.namespace
        metadata.pop("generateName", None)
        metadata.setdefault("name", self._job_name(version_id))
        if not metadata["name"]:
            metadata["name"] = self._job_name(version_id)
        spec = payload.setdefault("spec", {})
        if not isinstance(spec, dict):
            raise TargetReadyError("target Job spec must be an object")
        spec["completionMode"] = "Indexed"
        spec["completions"] = self.config.world_size
        spec["parallelism"] = self.config.world_size
        spec["backoffLimit"] = 0
        spec["activeDeadlineSeconds"] = int(self.config.timeout)
        template = spec.setdefault("template", {})
        if not isinstance(template, dict):
            raise TargetReadyError("target Job pod template must be an object")
        pod_spec = template.setdefault("spec", {})
        if not isinstance(pod_spec, dict):
            raise TargetReadyError("target Job pod spec must be an object")
        pod_spec["restartPolicy"] = "Never"
        containers = pod_spec.get("containers")
        if not isinstance(containers, list) or not containers:
            raise TargetReadyError("target Job has no containers")
        checkpoint_template = self.config.checkpoint_template.replace(
            "{version_id}",
            version_id,
        )
        env = [
            {"name": "SHARDFERRY_TARGET_READY", "value": "1"},
            {"name": "SHARDFERRY_REPORT_PATH", "value": REPORT_PATH},
            {
                "name": "SHARDFERRY_MIGRATION_UID",
                "value": self.config.job_id,
            },
            {"name": "SHARDFERRY_VERSION", "value": version_id},
            {
                "name": "SHARDFERRY_TARGET_WORLD_SIZE",
                "value": str(self.config.world_size),
            },
            {
                "name": "SHARDFERRY_VALIDATION_STEPS",
                "value": str(self.config.steps),
            },
            {
                "name": "SHARDFERRY_CHECKPOINT_TEMPLATE",
                "value": checkpoint_template,
            },
        ]
        if self.config.state_template:
            env.extend(
                [
                    {
                        "name": "SHARDFERRY_STATE_TEMPLATE",
                        "value": self.config.state_template.replace(
                            "{version_id}",
                            version_id,
                        ),
                    },
                    {
                        "name": "SHARDFERRY_STATE_PROVIDERS",
                        "value": self.config.state_providers,
                    },
                    {
                        "name": "SHARDFERRY_REQUIRE_STATE",
                        "value": (
                            "1" if self.config.require_state else "0"
                        ),
                    },
                ]
            )
        for container in containers:
            if not isinstance(container, dict):
                raise TargetReadyError("target Job container must be an object")
            existing = container.setdefault("env", [])
            if not isinstance(existing, list):
                raise TargetReadyError("target Job container env must be a list")
            merged = {
                item.get("name"): item
                for item in existing
                if isinstance(item, dict) and item.get("name")
            }
            for item in env:
                merged[item["name"]] = item
            container["env"] = list(merged.values())
            container["terminationMessagePath"] = REPORT_PATH
            container["terminationMessagePolicy"] = "File"
        metadata.setdefault("labels", {})
        if not isinstance(metadata["labels"], dict):
            raise TargetReadyError("target Job labels must be an object")
        metadata["labels"]["shardferry.io/handoff-target"] = "true"
        return payload

    def _job_name(self, version_id: str) -> str:
        suffix = _dns_label(version_id)[:42]
        base = _dns_label(self.config.job_id)[:16]
        return f"shardferry-{base}-{suffix}"[:63].rstrip("-.")

    def _reports(self, job_name: str) -> list[dict[str, Any]]:
        output = self._run(
            [
                "get",
                "pods",
                "-l",
                f"batch.kubernetes.io/job-name={job_name}",
                "-o",
                "json",
            ],
            capture=True,
        )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise TargetReadyError("kubectl returned invalid pod JSON") from exc
        reports: list[dict[str, Any]] = []
        for pod in payload.get("items", []):
            metadata = pod.get("metadata", {})
            annotations = metadata.get("annotations", {})
            rank = _rank(annotations)
            messages: list[str] = []
            for status in pod.get("status", {}).get("containerStatuses", []):
                terminated = status.get("state", {}).get("terminated", {})
                message = str(terminated.get("message", "")).strip()
                if message:
                    messages.append(message)
            if not messages:
                continue
            decoded: list[dict[str, Any]] = []
            for message in messages:
                try:
                    report = json.loads(message)
                except json.JSONDecodeError as exc:
                    raise TargetReadyError(
                        f"pod {metadata.get('name')} has invalid target report"
                    ) from exc
                if not isinstance(report, dict):
                    raise TargetReadyError(
                        f"pod {metadata.get('name')} target report is not an object"
                    )
                if "rank" not in report:
                    report["rank"] = rank
                decoded.append(report)
            if any(report != decoded[0] for report in decoded[1:]):
                raise TargetReadyError(
                    f"pod {metadata.get('name')} has conflicting target reports"
                )
            reports.append(decoded[0])
        return reports

    def _run(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
        capture: bool = False,
    ) -> str:
        command = [
            self.config.kubectl,
            "--namespace",
            self.config.namespace,
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.config.timeout + 30,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TargetReadyError(
                f"kubectl not found: {self.config.kubectl}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TargetReadyError(
                f"kubectl timed out after {self.config.timeout + 30}s"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise TargetReadyError(
                f"kubectl {' '.join(args)} exited {result.returncode}: {detail}"
            )
        return result.stdout if capture else ""


def evaluate_target_reports(
    reports: list[dict[str, Any]],
    *,
    world_size: int,
    version: str,
    job_id: str,
    steps: int,
    require_state: bool = False,
) -> None:
    expected = set(range(world_size))
    seen: dict[int, dict[str, Any]] = {}
    for report in reports:
        rank = report.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
            raise TargetReadyError(f"target report has invalid rank: {rank!r}")
        if rank not in expected:
            raise TargetReadyError(f"target report has unexpected rank {rank}")
        if report.get("schemaVersion") != "1":
            raise TargetReadyError(f"rank {rank} has unsupported report schema")
        if report.get("migrationUID") != job_id:
            raise TargetReadyError(f"rank {rank} has wrong migrationUID")
        if report.get("version") != version:
            raise TargetReadyError(f"rank {rank} has wrong version")
        if not report.get("ok"):
            raise TargetReadyError(
                f"rank {rank} reported failure: {report.get('error', '')}"
            )
        if not report.get("modelLoaded") or not report.get("optimizerLoaded"):
            raise TargetReadyError(f"rank {rank} did not load model/optimizer")
        ran = report.get("stepsRan")
        if not isinstance(ran, int) or isinstance(ran, bool) or ran < steps:
            raise TargetReadyError(
                f"rank {rank} ran {ran!r} steps, need {steps}"
            )
        if require_state and not report.get("stateComplete"):
            raise TargetReadyError(
                f"rank {rank} did not restore rank-local state"
            )
        if rank in seen and seen[rank] != report:
            raise TargetReadyError(f"conflicting reports for rank {rank}")
        seen[rank] = report
    missing = sorted(expected - set(seen))
    if missing:
        raise TargetReadyError(f"missing target reports for ranks {missing}")


def _rank(annotations: dict[str, Any]) -> int:
    value = annotations.get("batch.kubernetes.io/job-completion-index")
    if value is None:
        value = annotations.get("job-completion-index", 0)
    try:
        rank = int(value)
    except (TypeError, ValueError) as exc:
        raise TargetReadyError(
            f"invalid job completion index: {value!r}"
        ) from exc
    if rank < 0:
        raise TargetReadyError(f"job completion index must be non-negative: {rank}")
    return rank


def _dns_label(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return normalized or "target"

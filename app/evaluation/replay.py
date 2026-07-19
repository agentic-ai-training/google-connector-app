"""No-network Google Workspace mutation simulator for workflow regression tests.

This module intentionally models observable contracts (IDs, URLs, destinations,
idempotency and dependency failures), not Google's client libraries. Production
credentials are never accepted, which makes mutation trajectories safe to replay
in CI.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ReplayStepResult:
    step_id: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    compensated: bool = False


@dataclass
class ReplayResult:
    case_id: str
    status: str
    technical_completion: float
    functional_completion: float
    first_breaking_point: str | None
    steps: list[ReplayStepResult]
    artifacts: dict[str, dict[str, Any]]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class SimulatedGoogleWorkspace:
    """An in-memory, deterministic replacement for mutating Google APIs."""

    def __init__(self, fail_once: set[str] | None = None):
        self.artifacts: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, dict[str, Any]] = {}
        self._counters: dict[str, int] = {}
        self._fail_once = set(fail_once or set())

    def _identifier(self, service: str) -> str:
        self._counters[service] = self._counters.get(service, 0) + 1
        return f"sim-{service}-{self._counters[service]:04d}"

    def execute(
        self, service: str, operation: str, arguments: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        if idempotency_key in self._idempotency:
            return copy.deepcopy(self._idempotency[idempotency_key])
        failure_key = f"{service}.{operation}"
        if failure_key in self._fail_once:
            self._fail_once.remove(failure_key)
            raise RuntimeError(f"injected transient failure: {failure_key}")

        external_id = self._identifier(service)
        artifact = {
            "service": service,
            "operation": operation,
            "external_id": external_id,
            "url": f"https://simulated.invalid/{service}/{external_id}",
            "arguments": copy.deepcopy(arguments),
            "verified": True,
            "deleted": operation in {"delete", "trash", "cancel"},
        }
        if service == "gmail":
            artifact.update(message_id=external_id, recipient=arguments.get("recipient"))
        elif service == "sheets":
            rows = arguments.get("rows", [])
            artifact.update(spreadsheetId=external_id, spreadsheetUrl=artifact["url"],
                            row_count=len(rows))
        elif service == "chat":
            artifact.update(name=f"spaces/{arguments.get('space')}/messages/{external_id}")
        elif service == "calendar":
            artifact.update(event_id=external_id, meet_url=f"https://meet.invalid/{external_id}")
        elif service in {"drive", "docs"}:
            artifact.update(file_id=external_id)
        elif service == "tasks":
            artifact.update(task_id=external_id)

        self.artifacts[external_id] = artifact
        self._idempotency[idempotency_key] = copy.deepcopy(artifact)
        return copy.deepcopy(artifact)

    def compensate(self, external_id: str) -> bool:
        artifact = self.artifacts.get(external_id)
        if not artifact or artifact["deleted"]:
            return False
        artifact["deleted"] = True
        artifact["compensated"] = True
        return True


_REFERENCE = re.compile(r"^\$\{([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_]+)\}$")


def _resolve(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = _REFERENCE.match(value)
        if match:
            step_id, field_name = match.groups()
            if step_id not in outputs or field_name not in outputs[step_id]:
                raise ValueError(f"unresolved replay reference: {value}")
            return outputs[step_id][field_name]
        return value
    if isinstance(value, list):
        return [_resolve(item, outputs) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, outputs) for key, item in value.items()}
    return value


def replay_case(case: dict[str, Any]) -> ReplayResult:
    """Execute a fixture with deterministic retry, verification and compensation."""
    workspace = SimulatedGoogleWorkspace(set(case.get("fail_once", [])))
    results: list[ReplayStepResult] = []
    outputs: dict[str, dict[str, Any]] = {}
    successful_ids: list[str] = []
    failed = False
    first_breaking_point = None

    for step in case["steps"]:
        dependencies = step.get("dependencies", [])
        if failed or any(dependency not in outputs for dependency in dependencies):
            results.append(ReplayStepResult(step["id"], "skipped", error="dependency failed"))
            continue
        try:
            arguments = _resolve(step.get("arguments", {}), outputs)
            attempts = 1 + int(step.get("retries", 0))
            output = None
            for attempt in range(attempts):
                try:
                    output = workspace.execute(
                        step["service"], step["operation"], arguments,
                        step.get("idempotency_key", f"{case['id']}:{step['id']}"),
                    )
                    break
                except RuntimeError:
                    if attempt + 1 == attempts:
                        raise
            if not output or not output.get("external_id") or not output.get("verified"):
                raise RuntimeError("simulated postcondition verification failed")
            outputs[step["id"]] = output
            successful_ids.append(output["external_id"])
            results.append(ReplayStepResult(step["id"], "completed", output=output))
        except (RuntimeError, ValueError) as exc:
            failed = True
            first_breaking_point = step["id"]
            results.append(ReplayStepResult(step["id"], "failed", error=str(exc)))

    if failed and case.get("compensate_on_failure", False):
        for result in reversed(results):
            external_id = result.output.get("external_id")
            if external_id and workspace.compensate(external_id):
                result.compensated = True

    completed = sum(result.status == "completed" for result in results)
    total = max(1, len(results))
    actual_status = "failed" if failed else "completed"
    expectations = case.get("expect", {})
    expectation_checks = [actual_status == case.get("expected_status", "completed")]
    if "artifact_count" in expectations:
        expectation_checks.append(len(workspace.artifacts) == expectations["artifact_count"])
    if "first_breaking_point" in expectations:
        expectation_checks.append(first_breaking_point == expectations["first_breaking_point"])
    if "compensated_steps" in expectations:
        actual = {result.step_id for result in results if result.compensated}
        expectation_checks.append(actual == set(expectations["compensated_steps"]))
    for left_step, right_step in expectations.get("same_external_id", []):
        expectation_checks.append(
            outputs.get(left_step, {}).get("external_id")
            == outputs.get(right_step, {}).get("external_id")
        )
    return ReplayResult(
        case_id=case["id"], status=actual_status,
        technical_completion=round(completed * 100 / total, 2),
        functional_completion=100.0 if all(expectation_checks) else 0.0,
        first_breaking_point=first_breaking_point,
        steps=results,
        artifacts={key: copy.deepcopy(value) for key, value in workspace.artifacts.items()},
    )

"""Typed execution failures that retain safe structured evidence."""

from __future__ import annotations

from typing import Any


class ExecutionFailure(RuntimeError):
    def __init__(
        self, message: str, *, category: str = "execution",
        component: str = "durable_worker", boundary: str = "execution",
        provider_code: str | None = None, provider_param: str | None = None,
        evidence: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.component = component
        self.boundary = boundary
        self.provider_code = provider_code
        self.provider_param = provider_param
        self.evidence = evidence or {}


def is_provider_context_length_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return (
        "reduce the length of the messages or completion" in text
        or ("invalid_request_error" in text and "'param': 'messages'" in text)
        or ("context" in text and any(term in text for term in (
            "length", "window", "too long", "maximum",
        )))
    )


class ModelContextLengthFailure(ExecutionFailure):
    def __init__(
        self, message: str, *, boundary: str,
        evidence: dict[str, Any] | None = None,
    ):
        super().__init__(
            message,
            category="model_context_length",
            component="executor_context_builder",
            boundary=boundary,
            provider_code="invalid_request_error",
            provider_param="messages",
            evidence=evidence,
        )


class ToolSelectionFailure(ExecutionFailure):
    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(
            message, category="tool_selection", component="service_agent",
            boundary="write_tool_selection", evidence=evidence,
        )


class ToolExecutionFailure(ExecutionFailure):
    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(
            message, category="tool_failure", component="service_agent",
            boundary="write_tool_execution", evidence=evidence,
        )


class PostconditionFailure(ExecutionFailure):
    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None):
        super().__init__(
            message, category="postcondition_failure", component="step_verifier",
            boundary="postcondition_verification", evidence=evidence,
        )

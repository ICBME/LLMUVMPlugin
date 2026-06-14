"""Generic LLM backend interfaces for generation pipelines."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Protocol


class LLMBackendError(RuntimeError):
    """Raised when an LLM backend cannot complete a request."""


@dataclass(frozen=True)
class LLMRequest:
    """Provider-neutral LLM request used by generation stages."""

    prompt: dict[str, Any]
    model: str | None = None
    system_prompt: str = ""
    run_name: str = "spec2ir_llm_request"
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    response_format: str = "json_object"


@dataclass(frozen=True)
class LLMResponse:
    """Provider-neutral LLM response.

    ``parsed_json`` is optional because some backends only expose text.  Callers
    remain responsible for normalizing the response into their target artifact.
    """

    content: str
    raw: Any = None
    parsed_json: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMBackend(Protocol):
    """Protocol implemented by pluggable LLM backends."""

    name: str

    def invoke(self, request: LLMRequest) -> LLMResponse | None:
        """Return a response, or ``None`` when the backend is unavailable."""


LegacyLLMCallable = Callable[[dict[str, Any], str | None], dict[str, Any] | None]


class CallableLLMBackend:
    """Adapter for tests and legacy prompt/model callables."""

    name = "callable"

    def __init__(self, func: LegacyLLMCallable):
        self._func = func

    def invoke(self, request: LLMRequest) -> LLMResponse | None:
        value = self._func(request.prompt, request.model)
        if value is None:
            return None
        if isinstance(value, LLMResponse):
            return value
        if isinstance(value, dict):
            return LLMResponse(
                content=json.dumps(value, sort_keys=True),
                raw=value,
                parsed_json=value,
                metadata={"backend": self.name},
            )
        return LLMResponse(
            content=str(value),
            raw=value,
            metadata={"backend": self.name},
        )


def message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("content") or item))
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    return str(content)

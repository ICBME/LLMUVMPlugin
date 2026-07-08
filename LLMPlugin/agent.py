"""Provider-neutral LLM agent loop for harness-driven workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from .base import LLMBackend, LLMBackendError, LLMRequest, LLMResponse


class LLMAgentHarness(Protocol):
    """Minimal harness protocol consumed by ``LLMAgentRunner``."""

    def start(self) -> dict[str, Any]:
        ...

    def observe(self) -> dict[str, Any]:
        ...

    def apply(self, action: dict[str, Any]) -> dict[str, Any]:
        ...

    def is_done(self) -> bool:
        ...

    def result(self) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LLMAgentConfig:
    model: str | None = None
    max_attempts: int = 2
    temperature: float = 0.0
    run_name: str = "llm_agent"
    tags: tuple[str, ...] = ()
    response_format: str = "json_object"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", max(0, int(self.max_attempts)))
        object.__setattr__(self, "tags", tuple(str(item) for item in self.tags))
        object.__setattr__(self, "metadata", dict(self.metadata))


class LLMAgentRunner:
    """Run an LLM-controlled JSON action loop against a harness."""

    def __init__(
        self,
        harness: LLMAgentHarness,
        *,
        backend: LLMBackend,
        config: LLMAgentConfig | None = None,
    ) -> None:
        self.harness = harness
        self.backend = backend
        self.config = config or LLMAgentConfig()
        self.attempts: list[dict[str, Any]] = []
        self._started = False
        self._start_observation: dict[str, Any] = {}

    def run(self) -> dict[str, Any]:
        self._start_observation = self.harness.start()
        self._started = True
        if self.harness.is_done() or self.config.max_attempts == 0:
            return self._result()

        for attempt_index in range(1, self.config.max_attempts + 1):
            observation = self.harness.observe()
            messages = build_agent_messages(observation, self.attempts)
            request = LLMRequest(
                prompt={
                    "workflow": observation.get("workflow", self.config.run_name),
                    "observation": observation,
                    "attempt_history": list(self.attempts),
                    "messages": messages,
                },
                model=self.config.model,
                system_prompt=str(observation.get("agent_instructions") or ""),
                run_name=self.config.run_name,
                tags=self.config.tags,
                metadata={
                    **self.config.metadata,
                    "attempt_index": attempt_index,
                    "messages": messages,
                    "temperature": self.config.temperature,
                },
                response_format=self.config.response_format,
            )
            try:
                response = self.backend.invoke(request)
            except LLMBackendError as exc:
                self.attempts.append(
                    {
                        "attempt": attempt_index,
                        "status": "llm_unavailable",
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    }
                )
                break
            if response is None:
                self.attempts.append(
                    {
                        "attempt": attempt_index,
                        "status": "llm_unavailable",
                        "error": {"type": "LLMUnavailable", "message": "backend returned no response"},
                    }
                )
                break

            action, error = response_to_action(response)
            if error is not None:
                self.attempts.append(
                    {
                        "attempt": attempt_index,
                        "status": "llm_invalid_response",
                        "error": error,
                        "response": compact_response(response),
                    }
                )
                continue

            apply_result = self.harness.apply(action)
            self.attempts.append(
                {
                    "attempt": attempt_index,
                    "status": str(apply_result.get("status") or "applied"),
                    "action": compact_action(action),
                    "harness_result": apply_result,
                    "response": compact_response(response),
                }
            )
            if self.harness.is_done():
                break
        return self._result()

    def _result(self) -> dict[str, Any]:
        result = dict(self.harness.result())
        if result.get("status") in {None, "running", "pending"}:
            statuses = [str(item.get("status") or "") for item in self.attempts]
            if any(status == "llm_unavailable" for status in statuses):
                result["status"] = "llm_unavailable"
            elif statuses and all(status == "llm_invalid_response" for status in statuses):
                result["status"] = "llm_invalid_response"
            else:
                result["status"] = "repair_failed"
        result["attempts"] = list(self.attempts)
        result.setdefault(
            "llm_provenance",
            {
                "backend": getattr(self.backend, "name", type(self.backend).__name__),
                "model": self.config.model,
                "run_name": self.config.run_name,
                "tags": list(self.config.tags),
                "attempt_count": len(self.attempts),
            },
        )
        result["llm_provenance"].setdefault("attempt_count", len(self.attempts))
        result["llm_provenance"].setdefault("backend", getattr(self.backend, "name", type(self.backend).__name__))
        result["llm_provenance"].setdefault("model", self.config.model)
        return result


def build_agent_messages(
    observation: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> list[dict[str, str]]:
    instructions = str(observation.get("agent_instructions") or "Return one JSON action object.")
    user_payload = {
        "observation": observation,
        "attempt_history": attempts,
    }
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(user_payload, indent=2, sort_keys=True)},
    ]


def response_to_action(response: LLMResponse) -> tuple[dict[str, Any], dict[str, str] | None]:
    try:
        payload = response.parsed_json if response.parsed_json is not None else json.loads(response.content)
    except Exception as exc:  # noqa: BLE001 - returned to the agent as repair context
        return {}, {"type": type(exc).__name__, "message": f"LLM response is not JSON: {exc}"}
    if not isinstance(payload, dict):
        return {}, {"type": "InvalidAction", "message": f"LLM response must be an object, got {type(payload).__name__}"}
    action = payload.get("action")
    if isinstance(action, dict):
        payload = action
    if not isinstance(payload.get("action"), str):
        return {}, {"type": "InvalidAction", "message": "LLM action must define string field 'action'"}
    return payload, None


def compact_response(response: LLMResponse) -> dict[str, Any]:
    content = response.content
    return {
        "content": content[:4000],
        "truncated": len(content) > 4000,
        "metadata": dict(response.metadata),
    }


def compact_action(action: dict[str, Any]) -> dict[str, Any]:
    compact = dict(action)
    candidate = compact.get("semantic_spec_ir")
    if isinstance(candidate, dict):
        compact["semantic_spec_ir"] = {
            "schema_version": candidate.get("schema_version"),
            "target": candidate.get("target"),
            "semantic_element_count": len(candidate.get("semantic_elements", []))
            if isinstance(candidate.get("semantic_elements"), list)
            else None,
        }
    return compact


__all__ = [
    "LLMAgentConfig",
    "LLMAgentHarness",
    "LLMAgentRunner",
    "build_agent_messages",
    "response_to_action",
]

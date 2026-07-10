"""Provider-neutral LangGraph agent runtime for harness-driven workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol
from uuid import uuid4

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


class LLMAgentToolHarness(LLMAgentHarness, Protocol):
    """Optional harness extension for runtime-dispatched tools."""

    def tool_specs(self) -> list[dict[str, Any]]:
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LLMAgentConfig:
    model: str | None = None
    max_attempts: int = 2
    temperature: float = 0.0
    run_name: str = "llm_agent"
    tags: tuple[str, ...] = ()
    response_format: str = "json_object"
    history_window: int = 4
    event_window: int = 8
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", max(0, int(self.max_attempts)))
        object.__setattr__(self, "history_window", max(0, int(self.history_window)))
        object.__setattr__(self, "event_window", max(0, int(self.event_window)))
        object.__setattr__(self, "tags", tuple(str(item) for item in self.tags))
        object.__setattr__(self, "metadata", dict(self.metadata))


class LLMAgentRunner:
    """Run an LLM-controlled JSON step graph against a harness."""

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
        self.events: list[dict[str, Any]] = []
        self.last_state: dict[str, Any] = {}
        self._started = False
        self._start_observation: dict[str, Any] = {}
        self._thread_id = str(
            self.config.metadata.get("thread_id")
            or self.config.metadata.get("checkpoint_thread_id")
            or f"{self.config.run_name}-{uuid4().hex}"
        )
        self.checkpointer = create_memory_checkpointer()
        self._graph = self._compile_graph()

    def run(self) -> dict[str, Any]:
        state = {
            "attempts": [],
            "events": [],
            "messages": [],
            "observation": {},
            "last_error": None,
            "last_response": None,
            "last_step": None,
            "result": None,
            "llm_attempt_count": 0,
            "route": "start",
        }
        self.last_state = self._graph.invoke(
            state,
            config={"configurable": {"thread_id": self._thread_id}},
        )
        self.attempts = list(self.last_state.get("attempts") or [])
        self.events = list(self.last_state.get("events") or [])
        result = self.last_state.get("result")
        if not isinstance(result, dict):
            result = self._result(self.last_state)
            self.last_state["result"] = result
        return result

    def _compile_graph(self):
        try:
            from langgraph.graph import END, StateGraph
        except ModuleNotFoundError as exc:
            raise LLMBackendError("LLM agent runtime requires langgraph.") from exc

        graph = StateGraph(dict)
        graph.add_node("start_harness", self._node_start_harness)
        graph.add_node("build_context", self._node_build_context)
        graph.add_node("call_model", self._node_call_model)
        graph.add_node("parse_step", self._node_parse_step)
        graph.add_node("apply_action", self._node_apply_action)
        graph.add_node("execute_tool", self._node_execute_tool)
        graph.add_node("observe", self._node_observe)
        graph.add_node("finalize", self._node_finalize)
        graph.set_entry_point("start_harness")
        graph.add_conditional_edges(
            "start_harness",
            self._route_after_observe,
            {"continue": "build_context", "finalize": "finalize"},
        )
        graph.add_edge("build_context", "call_model")
        graph.add_edge("call_model", "parse_step")
        graph.add_conditional_edges(
            "parse_step",
            self._route_after_parse,
            {
                "harness_action": "apply_action",
                "tool_call": "execute_tool",
                "continue": "observe",
                "finalize": "finalize",
            },
        )
        graph.add_edge("apply_action", "observe")
        graph.add_edge("execute_tool", "observe")
        graph.add_conditional_edges(
            "observe",
            self._route_after_observe,
            {"continue": "build_context", "finalize": "finalize"},
        )
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _node_start_harness(self, state: dict[str, Any]) -> dict[str, Any]:
        observation = self.harness.start()
        self._started = True
        self._start_observation = dict(observation)
        events = append_event(
            state,
            "observation",
            {"phase": "start", "observation": observation},
        )
        return merge_state(state, observation=observation, events=events)

    def _node_build_context(self, state: dict[str, Any]) -> dict[str, Any]:
        observation = dict(state.get("observation") or self.harness.observe())
        attempts = list(state.get("attempts") or [])
        events = list(state.get("events") or [])
        messages = build_agent_messages(
            observation,
            attempts,
            events=events,
            last_error=state.get("last_error"),
            tool_specs=self._tool_specs(),
            history_window=self.config.history_window,
            event_window=self.config.event_window,
            current_attempt=int(state.get("llm_attempt_count") or 0) + 1,
            max_attempts=self.config.max_attempts,
        )
        return merge_state(state, messages=messages)

    def _node_call_model(self, state: dict[str, Any]) -> dict[str, Any]:
        attempt_index = int(state.get("llm_attempt_count") or 0) + 1
        observation = dict(state.get("observation") or {})
        messages = list(state.get("messages") or [])
        attempts = list(state.get("attempts") or [])
        attempt_history = (
            attempts[-self.config.history_window :]
            if self.config.history_window
            else []
        )
        request = LLMRequest(
            prompt={
                "workflow": observation.get("workflow", self.config.run_name),
                "observation": observation,
                "attempt_history": attempt_history,
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
                "checkpoint_thread_id": self._thread_id,
            },
            response_format=self.config.response_format,
        )
        try:
            response = self.backend.invoke(request)
        except LLMBackendError as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            attempt = {"attempt": attempt_index, "status": "llm_unavailable", "error": error}
            return merge_state(
                state,
                llm_attempt_count=attempt_index,
                attempts=[*list(state.get("attempts") or []), attempt],
                events=append_event(state, "error", {"phase": "call_model", "error": error}),
                last_error=error,
                last_response=None,
                route="finalize",
            )
        if response is None:
            error = {"type": "LLMUnavailable", "message": "backend returned no response"}
            attempt = {"attempt": attempt_index, "status": "llm_unavailable", "error": error}
            return merge_state(
                state,
                llm_attempt_count=attempt_index,
                attempts=[*list(state.get("attempts") or []), attempt],
                events=append_event(state, "error", {"phase": "call_model", "error": error}),
                last_error=error,
                last_response=None,
                route="finalize",
            )
        return merge_state(
            state,
            llm_attempt_count=attempt_index,
            last_response=response,
            events=append_event(
                state,
                "assistant_message",
                {"attempt": attempt_index, "response": compact_response(response)},
            ),
        )

    def _node_parse_step(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("route") == "finalize":
            return merge_state(state, route="finalize")
        response = state.get("last_response")
        attempt_index = int(state.get("llm_attempt_count") or 0)
        if not isinstance(response, LLMResponse):
            error = {"type": "InvalidAgentState", "message": "missing LLM response"}
            return self._record_invalid_attempt(state, attempt_index, error)
        step, error = response_to_agent_step(response)
        if error is not None:
            return self._record_invalid_attempt(
                state,
                attempt_index,
                error,
                response=response,
            )
        step_type = str(step.get("type") or "")
        if step_type == "final":
            return merge_state(
                state,
                last_step=step,
                events=append_event(state, "final_step", {"attempt": attempt_index, "step": compact_action(step)}),
                route="finalize",
            )
        return merge_state(
            state,
            last_step=step,
            events=append_event(state, "agent_step", {"attempt": attempt_index, "step": compact_action(step)}),
            route=step_type,
        )

    def _node_apply_action(self, state: dict[str, Any]) -> dict[str, Any]:
        step = dict(state.get("last_step") or {})
        attempt_index = int(state.get("llm_attempt_count") or 0)
        action = agent_step_to_harness_action(step)
        apply_result = self.harness.apply(action)
        attempt = {
            "attempt": attempt_index,
            "status": str(apply_result.get("status") or "applied"),
            "action": compact_action(action),
            "harness_result": apply_result,
        }
        response = state.get("last_response")
        if isinstance(response, LLMResponse):
            attempt["response"] = compact_response(response)
        return merge_state(
            state,
            attempts=[*list(state.get("attempts") or []), attempt],
            events=append_event(
                state,
                "harness_action",
                {"attempt": attempt_index, "action": compact_action(action), "result": apply_result},
            ),
            last_error=apply_result.get("error"),
            route="observe",
        )

    def _node_execute_tool(self, state: dict[str, Any]) -> dict[str, Any]:
        step = dict(state.get("last_step") or {})
        attempt_index = int(state.get("llm_attempt_count") or 0)
        tool_name = str(step.get("tool") or "")
        arguments = step.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        result = self._call_tool(tool_name, arguments)
        status = str(result.get("status") or "tool_result")
        attempt = {
            "attempt": attempt_index,
            "status": status,
            "tool_call": {"tool": tool_name, "arguments": compact_tool_arguments(arguments)},
            "tool_result": compact_tool_result(result),
        }
        response = state.get("last_response")
        if isinstance(response, LLMResponse):
            attempt["response"] = compact_response(response)
        error = result.get("error") if status == "tool_error" else None
        return merge_state(
            state,
            attempts=[*list(state.get("attempts") or []), attempt],
            events=append_event(
                state,
                "tool_result",
                {"attempt": attempt_index, "tool": tool_name, "result": compact_tool_result(result)},
            ),
            last_error=error,
            route="observe",
        )

    def _node_observe(self, state: dict[str, Any]) -> dict[str, Any]:
        observation = self.harness.observe()
        return merge_state(
            state,
            observation=observation,
            events=append_event(
                state,
                "observation",
                {"phase": "loop", "observation": observation},
            ),
        )

    def _node_finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        result = self._result(state)
        return merge_state(
            state,
            result=result,
            events=append_event(state, "final_result", {"result": compact_result(result)}),
        )

    def _route_after_observe(self, state: dict[str, Any]) -> str:
        if self.harness.is_done() or self.config.max_attempts == 0:
            return "finalize"
        if int(state.get("llm_attempt_count") or 0) >= self.config.max_attempts:
            return "finalize"
        return "continue"

    def _route_after_parse(self, state: dict[str, Any]) -> str:
        route = str(state.get("route") or "")
        if route == "finalize":
            return "finalize"
        if route in {"harness_action", "tool_call"}:
            return route
        if int(state.get("llm_attempt_count") or 0) >= self.config.max_attempts:
            return "finalize"
        return "continue"

    def _record_invalid_attempt(
        self,
        state: dict[str, Any],
        attempt_index: int,
        error: dict[str, str],
        *,
        response: LLMResponse | None = None,
    ) -> dict[str, Any]:
        attempt: dict[str, Any] = {
            "attempt": attempt_index,
            "status": "llm_invalid_response",
            "error": error,
        }
        if response is not None:
            attempt["response"] = compact_response(response)
        return merge_state(
            state,
            attempts=[*list(state.get("attempts") or []), attempt],
            events=append_event(
                state,
                "error",
                {"phase": "parse_step", "attempt": attempt_index, "error": error},
            ),
            last_error=error,
            route="continue",
        )

    def _result(self, state: dict[str, Any]) -> dict[str, Any]:
        attempts = list(state.get("attempts") or [])
        result = dict(self.harness.result())
        if result.get("status") in {None, "running", "pending"}:
            statuses = [str(item.get("status") or "") for item in attempts]
            if any(status == "llm_unavailable" for status in statuses):
                result["status"] = "llm_unavailable"
            elif statuses and all(status == "llm_invalid_response" for status in statuses):
                result["status"] = "llm_invalid_response"
            else:
                result["status"] = "repair_failed"
        result["attempts"] = attempts
        result["events"] = list(state.get("events") or [])
        result.setdefault(
            "llm_provenance",
            {
                "backend": getattr(self.backend, "name", type(self.backend).__name__),
                "model": self.config.model,
                "run_name": self.config.run_name,
                "tags": list(self.config.tags),
                "attempt_count": len(attempts),
                "runtime": "langgraph",
                "checkpoint": {"type": "memory", "thread_id": self._thread_id},
                "context_window": {
                    "attempts": self.config.history_window,
                    "events": self.config.event_window,
                },
            },
        )
        result["llm_provenance"].setdefault("attempt_count", len(attempts))
        result["llm_provenance"].setdefault("backend", getattr(self.backend, "name", type(self.backend).__name__))
        result["llm_provenance"].setdefault("model", self.config.model)
        result["llm_provenance"].setdefault("runtime", "langgraph")
        result["llm_provenance"].setdefault(
            "checkpoint",
            {"type": "memory", "thread_id": self._thread_id},
        )
        result["llm_provenance"].setdefault(
            "context_window",
            {
                "attempts": self.config.history_window,
                "events": self.config.event_window,
            },
        )
        return result

    def _tool_specs(self) -> list[dict[str, Any]]:
        tool_specs = getattr(self.harness, "tool_specs", None)
        if not callable(tool_specs):
            return []
        value = tool_specs()
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not name:
            return tool_error("InvalidToolCall", "tool_call requires non-empty string field 'tool'")
        specs = self._tool_specs()
        known = {str(item.get("name") or "") for item in specs}
        if specs and name not in known:
            return tool_error("UnknownTool", f"unsupported tool {name!r}")
        call_tool = getattr(self.harness, "call_tool", None)
        if not callable(call_tool):
            return tool_error("ToolUnavailable", "harness does not expose call_tool")
        try:
            result = call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 - surfaced as repair context
            return tool_error(type(exc).__name__, str(exc))
        if not isinstance(result, dict):
            return tool_error("InvalidToolResult", f"tool returned {type(result).__name__}")
        result.setdefault("status", "tool_result")
        return result


def create_memory_checkpointer() -> Any:
    try:
        from langgraph.checkpoint.memory import MemorySaver
    except ModuleNotFoundError as exc:
        raise LLMBackendError("LLM agent runtime requires langgraph checkpoint memory support.") from exc
    return MemorySaver()


def merge_state(state: dict[str, Any], **updates: Any) -> dict[str, Any]:
    merged = dict(state)
    merged.update(updates)
    return merged


def build_agent_messages(
    observation: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    events: list[dict[str, Any]] | None = None,
    last_error: dict[str, Any] | None = None,
    tool_specs: list[dict[str, Any]] | None = None,
    history_window: int = 4,
    event_window: int = 8,
    current_attempt: int = 1,
    max_attempts: int | None = None,
) -> list[dict[str, str]]:
    instructions = str(observation.get("agent_instructions") or "Return one JSON action object.")
    runtime_instructions = (
        "Treat the latest observation and validator feedback as authoritative. "
        "Use prior assistant steps only as history: do not repeat a failed step unchanged. "
        "Return exactly one JSON step and no surrounding prose. Tool calls consume one model call, "
        "so call a tool only when its result is needed to choose or validate the next action."
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": f"{instructions}\n\n{runtime_instructions}"}
    ]
    recent_attempts = attempts[-max(0, history_window) :] if history_window else []
    for attempt in recent_attempts:
        response = attempt.get("response")
        if isinstance(response, dict) and isinstance(response.get("content"), str):
            messages.append({"role": "assistant", "content": response["content"]})
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "kind": "agent_step_feedback",
                        "attempt": attempt.get("attempt"),
                        "feedback": attempt_feedback(attempt),
                    },
                    indent=2,
                    sort_keys=True,
                ),
            }
        )

    remaining_attempts = None
    if max_attempts is not None:
        remaining_attempts = max(0, int(max_attempts) - int(current_attempt) + 1)
    user_payload = {
        "kind": "current_agent_observation",
        "observation": observation,
        "recent_events": compact_events(events or [], limit=event_window),
        "last_error": last_error or {},
        "tool_specs": tool_specs or [],
        "budget": {
            "current_attempt": current_attempt,
            "max_attempts": max_attempts,
            "remaining_model_calls_including_this_one": remaining_attempts,
        },
        "response_contract": {
            "legacy_harness_action": {"action": "submit_semantic_spec_ir | mark_human_required | request_finalize"},
            "modern_harness_action": {"type": "harness_action", "action": "action name"},
            "tool_call": {"type": "tool_call", "tool": "tool name", "arguments": {}},
            "final": {"type": "final"},
        },
    }
    messages.append({"role": "user", "content": json.dumps(user_payload, indent=2, sort_keys=True)})
    return messages


def attempt_feedback(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return the actionable part of one prior step without duplicating its response."""

    return {
        key: value
        for key, value in attempt.items()
        if key not in {"attempt", "response"}
    }


def response_to_action(response: LLMResponse) -> tuple[dict[str, Any], dict[str, str] | None]:
    step, error = response_to_agent_step(response)
    if error is not None:
        return {}, error
    if step.get("type") != "harness_action":
        return {}, {"type": "InvalidAction", "message": "LLM response must be a harness action"}
    return agent_step_to_harness_action(step), None


def response_to_agent_step(response: LLMResponse) -> tuple[dict[str, Any], dict[str, str] | None]:
    try:
        payload = response.parsed_json if response.parsed_json is not None else json.loads(response.content)
    except Exception as exc:  # noqa: BLE001 - returned to the agent as repair context
        return {}, {"type": type(exc).__name__, "message": f"LLM response is not JSON: {exc}"}
    if not isinstance(payload, dict):
        return {}, {"type": "InvalidStep", "message": f"LLM response must be an object, got {type(payload).__name__}"}
    if isinstance(payload.get("action"), dict):
        payload = payload["action"]
        if not isinstance(payload, dict):
            return {}, {"type": "InvalidStep", "message": "nested action must be an object"}

    step_type = str(payload.get("type") or "")
    if not step_type and isinstance(payload.get("action"), str):
        step = {"type": "harness_action", **payload}
        return step, None
    if step_type == "harness_action":
        if not isinstance(payload.get("action"), str):
            return {}, {"type": "InvalidAction", "message": "harness_action must define string field 'action'"}
        return dict(payload), None
    if step_type == "tool_call":
        if not isinstance(payload.get("tool"), str) or not payload.get("tool"):
            return {}, {"type": "InvalidToolCall", "message": "tool_call must define non-empty string field 'tool'"}
        arguments = payload.get("arguments")
        if arguments is None:
            payload = {**payload, "arguments": {}}
        elif not isinstance(arguments, dict):
            return {}, {"type": "InvalidToolCall", "message": "tool_call arguments must be an object"}
        return dict(payload), None
    if step_type == "final":
        return {"type": "final", **payload}, None
    return {}, {"type": "InvalidStep", "message": "LLM step must define type 'harness_action', 'tool_call', or 'final'"}


def agent_step_to_harness_action(step: dict[str, Any]) -> dict[str, Any]:
    action = {key: value for key, value in step.items() if key != "type"}
    if "action" not in action and isinstance(step.get("name"), str):
        action["action"] = step["name"]
    return action


def append_event(state: dict[str, Any], event_type: str, content: dict[str, Any]) -> list[dict[str, Any]]:
    events = list(state.get("events") or [])
    events.append(
        {
            "index": len(events) + 1,
            "type": event_type,
            "content": content,
        }
    )
    return events


def compact_events(events: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    return [compact_event(event) for event in events[-limit:]]


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    compact = dict(event)
    content = compact.get("content")
    if isinstance(content, dict) and "observation" in content:
        observation = content.get("observation")
        if isinstance(observation, dict):
            content = dict(content)
            content["observation"] = {
                "workflow": observation.get("workflow"),
                "status": observation.get("status"),
                "target": observation.get("target"),
                "last_error": observation.get("last_error"),
                "review_status": (
                    observation.get("review_report", {}).get("status")
                    if isinstance(observation.get("review_report"), dict)
                    else None
                ),
            }
            compact["content"] = content
    return compact


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


def compact_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return compact_action(arguments)


def compact_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    for key in ("semantic_ir", "normalized_semantic_ir", "candidate"):
        value = compact.get(key)
        if isinstance(value, dict):
            compact[key] = {
                "schema_version": value.get("schema_version"),
                "target": value.get("target"),
                "semantic_element_count": len(value.get("semantic_elements", []))
                if isinstance(value.get("semantic_elements"), list)
                else None,
            }
    return compact


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "attempt_count": result.get("attempt_count"),
        "review_status": result.get("review", {}).get("status") if isinstance(result.get("review"), dict) else None,
    }


def tool_error(error_type: str, message: str) -> dict[str, Any]:
    return {"status": "tool_error", "error": {"type": error_type, "message": message}}


__all__ = [
    "LLMAgentConfig",
    "LLMAgentHarness",
    "LLMAgentRunner",
    "LLMAgentToolHarness",
    "build_agent_messages",
    "response_to_action",
    "response_to_agent_step",
]

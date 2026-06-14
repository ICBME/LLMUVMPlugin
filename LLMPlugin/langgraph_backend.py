"""LangGraph orchestration wrapper for LLM backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import LLMBackend, LLMBackendError, LLMRequest, LLMResponse
from .langchain_backend import create_langchain_backend


@dataclass(frozen=True)
class LangGraphBackendConfig:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2


class LangGraphLLMBackend:
    """LangGraph-backed backend that keeps provider invocation pluggable.

    The graph is intentionally one node for now.  It gives the spec-to-IR flow a
    stable place to add later nodes such as retry, repair, validation feedback,
    or human review routing without changing Spec2IR.
    """

    name = "langgraph"

    def __init__(self, delegate: LLMBackend):
        self.delegate = delegate
        self._graph = self._compile_graph()

    def invoke(self, request: LLMRequest) -> LLMResponse | None:
        if self._graph is None:
            return self.delegate.invoke(request)
        try:
            result = self._graph.invoke({"request": request})
        except LLMBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize graph/provider failures
            raise LLMBackendError(f"LangGraph backend invocation failed: {exc}") from exc
        response = result.get("response")
        if response is None or isinstance(response, LLMResponse):
            return response
        raise LLMBackendError(f"LangGraph backend returned invalid response {type(response).__name__}")

    def _compile_graph(self):
        try:
            from langgraph.graph import END, StateGraph
        except ModuleNotFoundError as exc:
            raise LLMBackendError("LangGraph backend requires langgraph.") from exc

        def invoke_model(state: dict[str, Any]) -> dict[str, Any]:
            return {"response": self.delegate.invoke(state["request"])}

        graph = StateGraph(dict)
        graph.add_node("invoke_model", invoke_model)
        graph.set_entry_point("invoke_model")
        graph.add_edge("invoke_model", END)
        return graph.compile()


def create_langgraph_backend(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
    timeout: int = 60,
    max_retries: int = 2,
    delegate: LLMBackend | None = None,
    **_kwargs,
) -> LangGraphLLMBackend | None:
    resolved_delegate = delegate or create_langchain_backend(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
    )
    if resolved_delegate is None:
        return None
    return LangGraphLLMBackend(resolved_delegate)

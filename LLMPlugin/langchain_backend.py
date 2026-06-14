"""LangChain-backed LLM backend plugin."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any

from .base import LLMBackendError, LLMRequest, LLMResponse, message_content_to_text


@dataclass(frozen=True)
class LangChainBackendConfig:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2


class LangChainLLMBackend:
    """OpenAI-compatible LangChain backend for JSON-oriented spec extraction."""

    name = "langchain"

    def __init__(self, config: LangChainBackendConfig | None = None):
        self.config = config or LangChainBackendConfig()

    def invoke(self, request: LLMRequest) -> LLMResponse | None:
        api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        model = request.model or self.config.model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        base_url = (
            self.config.base_url
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:
            raise LLMBackendError(
                "LangChain backend requires langchain-openai and langchain-core."
            ) from exc

        model_kwargs: dict[str, Any] = {}
        if request.response_format == "json_object":
            model_kwargs["response_format"] = {"type": "json_object"}

        llm = ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=self.config.temperature,
            timeout=self.config.timeout,
            max_retries=self.config.max_retries,
            model_kwargs=model_kwargs,
        )
        response = llm.invoke(
            [
                SystemMessage(content=request.system_prompt),
                HumanMessage(content=json.dumps(request.prompt, sort_keys=True)),
            ],
            config={
                "run_name": request.run_name,
                "tags": list(request.tags),
                "metadata": {
                    **request.metadata,
                    "model": model,
                    "provider": self.name,
                },
            },
        )
        content = message_content_to_text(response.content)
        return LLMResponse(
            content=content,
            raw=response,
            metadata={
                **request.metadata,
                "backend": self.name,
                "model": model,
                "base_url": base_url,
            },
        )


def create_langchain_backend(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.1,
    timeout: int = 60,
    max_retries: int = 2,
    **_kwargs,
) -> LangChainLLMBackend | None:
    if api_key is None and not os.getenv("OPENAI_API_KEY"):
        return None
    return LangChainLLMBackend(
        LangChainBackendConfig(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
        )
    )

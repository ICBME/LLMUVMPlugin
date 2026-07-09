"""LangChain-backed LLM backend plugin."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .base import LLMBackendError, LLMRequest, LLMResponse, message_content_to_text

_DOTENV_LOADED = False


@dataclass(frozen=True)
class LangChainBackendConfig:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    user_agent: str | None = None
    temperature: float = 0.1
    timeout: int = 60
    max_retries: int = 2


class LangChainLLMBackend:
    """OpenAI-compatible LangChain backend for JSON-oriented spec extraction."""

    name = "langchain"

    def __init__(self, config: LangChainBackendConfig | None = None):
        self.config = config or LangChainBackendConfig()

    def invoke(self, request: LLMRequest) -> LLMResponse | None:
        load_local_dotenv()
        api_key = self.config.api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        model = request.model or self.config.model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        base_url = normalize_openai_base_url(
            self.config.base_url
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        user_agent = self.config.user_agent or os.getenv("OPENAI_USER_AGENT") or "RTLAgent/Spec2IR"

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            try:
                from langchain_core.messages import AIMessage
            except ImportError:
                AIMessage = HumanMessage
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
            default_headers={"User-Agent": user_agent},
        )
        try:
            response = llm.invoke(
                request_messages(request, SystemMessage=SystemMessage, HumanMessage=HumanMessage, AIMessage=AIMessage),
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
        except Exception as exc:  # noqa: BLE001 - normalize provider errors for callers
            raise LLMBackendError(f"LangChain backend invocation failed: {exc}") from exc
        content = message_content_to_text(response.content)
        return LLMResponse(
            content=content,
            raw=response,
            metadata={
                **request.metadata,
                "backend": self.name,
                "model": model,
                "base_url": base_url,
                "user_agent": user_agent,
            },
        )


def create_langchain_backend(
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    user_agent: str | None = None,
    temperature: float = 0.1,
    timeout: int = 60,
    max_retries: int = 2,
    **_kwargs,
) -> LangChainLLMBackend | None:
    load_local_dotenv()
    if api_key is None and not os.getenv("OPENAI_API_KEY"):
        return None
    return LangChainLLMBackend(
        LangChainBackendConfig(
            model=model,
            api_key=api_key,
            base_url=base_url,
            user_agent=user_agent,
            temperature=temperature,
            timeout=timeout,
            max_retries=max_retries,
        )
    )


def request_messages(request: LLMRequest, *, SystemMessage: Any, HumanMessage: Any, AIMessage: Any) -> list[Any]:
    messages = request.metadata.get("messages")
    if isinstance(messages, list) and messages:
        result = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            content = str(item.get("content") or "")
            if role == "system":
                result.append(SystemMessage(content=content))
            elif role == "assistant":
                result.append(AIMessage(content=content))
            else:
                result.append(HumanMessage(content=content))
        if result:
            return result
    return [
        SystemMessage(content=request.system_prompt),
        HumanMessage(content=json.dumps(request.prompt, sort_keys=True)),
    ]


def load_local_dotenv(path: str | Path = ".env") -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    dotenv_path = resolve_dotenv_path(path)
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip("\"'")
        os.environ[key] = value


def resolve_dotenv_path(path: str | Path = ".env") -> Path:
    dotenv_path = Path(path)
    if dotenv_path.is_absolute():
        return dotenv_path
    cwd_path = Path.cwd() / dotenv_path
    if cwd_path.exists():
        return cwd_path
    return Path(__file__).resolve().parent.parent / dotenv_path


def normalize_openai_base_url(value: str) -> str:
    base_url = str(value).rstrip("/")
    if base_url.endswith(("/v1", "/v1/")):
        return base_url.rstrip("/")
    if base_url.endswith(("/chat/completions", "/models")):
        return base_url.rsplit("/", 2)[0]
    return f"{base_url}/v1"

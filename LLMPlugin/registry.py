"""Backend registry for plugin-style LLM provider selection."""

from __future__ import annotations

from typing import Callable

from .base import LLMBackend


BackendFactory = Callable[..., LLMBackend | None]


class LLMBackendRegistry:
    """Small explicit registry for built-in and external LLM backends."""

    def __init__(self) -> None:
        self._factories: dict[str, BackendFactory] = {}

    def register(self, name: str, factory: BackendFactory, *, replace: bool = False) -> None:
        normalized = normalize_backend_name(name)
        if normalized in self._factories and not replace:
            raise ValueError(f"LLM backend {normalized!r} is already registered")
        self._factories[normalized] = factory

    def create(self, name: str, **kwargs) -> LLMBackend | None:
        normalized = normalize_backend_name(name)
        try:
            factory = self._factories[normalized]
        except KeyError as exc:
            raise KeyError(
                f"unknown LLM backend {normalized!r}; registered={sorted(self._factories)}"
            ) from exc
        return factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def normalize_backend_name(name: str | None) -> str:
    return (name or "langgraph").strip().lower().replace("_", "-")


GLOBAL_BACKEND_REGISTRY = LLMBackendRegistry()


def register_backend(name: str, factory: BackendFactory, *, replace: bool = False) -> None:
    GLOBAL_BACKEND_REGISTRY.register(name, factory, replace=replace)


def create_backend(name: str | None = None, **kwargs) -> LLMBackend | None:
    register_builtin_backends()
    return GLOBAL_BACKEND_REGISTRY.create(normalize_backend_name(name), **kwargs)


def registered_backend_names() -> tuple[str, ...]:
    register_builtin_backends()
    return GLOBAL_BACKEND_REGISTRY.names()


_BUILTINS_REGISTERED = False


def register_builtin_backends() -> None:
    global _BUILTINS_REGISTERED
    if _BUILTINS_REGISTERED:
        return
    from .langchain_backend import create_langchain_backend
    from .langgraph_backend import create_langgraph_backend

    register_backend("langchain", create_langchain_backend)
    register_backend("langgraph", create_langgraph_backend)
    _BUILTINS_REGISTERED = True

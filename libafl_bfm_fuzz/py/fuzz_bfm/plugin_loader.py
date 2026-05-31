from __future__ import annotations

from importlib import import_module
import inspect
from typing import Any

from .target_config import TargetConfig


def load_object(spec: str) -> Any:
    module_name, sep, object_name = spec.partition(":")
    if not sep or not module_name or not object_name:
        raise ValueError(f"plugin spec must be 'module:Object', got {spec!r}")
    module = import_module(module_name)
    obj: Any = module
    for part in object_name.split("."):
        obj = getattr(obj, part)
    return obj


def build_plugin(spec: str, **kwargs: Any) -> Any:
    return _build_loaded_plugin(load_object(spec), **kwargs)


def build_driver(config: TargetConfig, **kwargs: Any) -> Any:
    call_kwargs = dict(kwargs)
    driver_cls = load_object(config.driver)
    signature = inspect.signature(driver_cls)
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    if accepts_kwargs or "config" in parameters:
        call_kwargs["config"] = config
    return _build_loaded_plugin(driver_cls, **call_kwargs)


def _build_loaded_plugin(plugin_cls: Any, **kwargs: Any) -> Any:
    signature = inspect.signature(plugin_cls)
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    call_kwargs = dict(kwargs)
    if accepts_kwargs:
        return plugin_cls(**call_kwargs)
    call_kwargs = {name: value for name, value in call_kwargs.items() if name in parameters}
    if call_kwargs:
        return plugin_cls(**call_kwargs)
    return plugin_cls()

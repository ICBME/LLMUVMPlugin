"""Validation gates for directly generated Python plugins."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Iterable, Iterator


class ArtifactValidationError(ValueError):
    """Raised when generated artifacts fail validation."""


DISALLOWED_IMPORT_ROOTS = {
    "http",
    "os",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}

DISALLOWED_CALLS = {
    "compile",
    "eval",
    "exec",
    "input",
    "open",
    "__import__",
}

DISALLOWED_ATTRIBUTE_CALLS = {
    ("os", "popen"),
    ("os", "remove"),
    ("os", "rmdir"),
    ("os", "system"),
    ("shutil", "rmtree"),
    ("subprocess", "call"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
    ("subprocess", "Popen"),
    ("subprocess", "run"),
}


@dataclass(frozen=True)
class GoldenCase:
    """A directed ref-model check used before promotion."""

    data: dict[str, Any]
    expected: Any
    target: str
    line_no: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_target: str) -> "GoldenCase":
        case_data = data.get("data", data.get("case"))
        if not isinstance(case_data, dict):
            raise ArtifactValidationError("golden case must define data or case object")
        target = str(data.get("target", case_data.get("target", default_target)))
        if "expected" not in data:
            raise ArtifactValidationError("golden case must define expected")
        return cls(
            data=dict(case_data),
            expected=data["expected"],
            target=target,
            line_no=int(data.get("line_no", 1)),
        )

    def to_fuzz_case(self) -> Any:
        return SimpleNamespace(target=self.target, data=self.data, line_no=self.line_no)


def validate_artifact_dir(
    artifact_dir: str | Path,
    *,
    target: str,
    ref_model: str | None = None,
    scoreboard: str | None = None,
    extra_python_paths: Iterable[str | Path] = (),
    golden_cases: Iterable[GoldenCase] = (),
) -> None:
    """Run static, contract, and optional golden-case validation."""

    root = Path(artifact_dir)
    if not root.exists():
        raise FileNotFoundError(f"artifact directory does not exist: {root}")
    python_files = sorted(root.rglob("*.py"))
    if not python_files:
        raise ArtifactValidationError(f"{root}: no generated Python files found")

    for python_file in python_files:
        validate_python_file(python_file)

    paths = (
        root,
        _framework_python_path(),
        *(Path(path) for path in extra_python_paths),
    )
    with python_path(paths):
        contracts = _load_replay_contracts()
        ref_model_obj = None
        try:
            if ref_model:
                ref_model_obj = _build_plugin(ref_model, target=target, config=None)
                contracts.validate_ref_model_plugin(ref_model_obj, spec=ref_model)
            if scoreboard:
                scoreboard_obj = _build_plugin(scoreboard, target=target, config=None)
                contracts.validate_scoreboard_plugin(scoreboard_obj, spec=scoreboard)
            if ref_model_obj is not None:
                _validate_golden_cases(
                    ref_model_obj,
                    tuple(golden_cases),
                    normalize_expected=contracts.normalize_expected,
                )
        except contracts.PluginContractError as exc:
            raise ArtifactValidationError(str(exc)) from exc


def validate_python_file(path: str | Path) -> None:
    python_file = Path(path)
    source = python_file.read_text(encoding="utf-8")
    try:
        compile(source, str(python_file), "exec")
    except SyntaxError as exc:
        raise ArtifactValidationError(str(exc)) from exc

    tree = ast.parse(source, filename=str(python_file))
    _validate_static_ast(tree, python_file)


def load_golden_cases(path: str | Path, default_target: str) -> tuple[GoldenCase, ...]:
    import json

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        raw_cases = value.get("cases")
    else:
        raw_cases = value
    if not isinstance(raw_cases, list):
        raise ArtifactValidationError("golden cases JSON must be a list or contain cases[]")
    return tuple(GoldenCase.from_dict(item, default_target) for item in raw_cases)


def _validate_static_ast(tree: ast.AST, path: Path) -> None:
    imported_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in DISALLOWED_IMPORT_ROOTS:
                    raise ArtifactValidationError(f"{path}: import of {root!r} is not allowed")
                imported_aliases[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom):
            module_root = (node.module or "").split(".", 1)[0]
            if module_root in DISALLOWED_IMPORT_ROOTS:
                raise ArtifactValidationError(f"{path}: import from {module_root!r} is not allowed")
            for alias in node.names:
                imported_aliases[alias.asname or alias.name] = module_root
        elif isinstance(node, ast.Call):
            _validate_call(node, imported_aliases, path)


def _validate_call(node: ast.Call, imported_aliases: dict[str, str], path: Path) -> None:
    if isinstance(node.func, ast.Name) and node.func.id in DISALLOWED_CALLS:
        raise ArtifactValidationError(f"{path}: call to {node.func.id}() is not allowed")
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        root_name = node.func.value.id
        module_root = imported_aliases.get(root_name, root_name)
        pair = (module_root, node.func.attr)
        if pair in DISALLOWED_ATTRIBUTE_CALLS:
            raise ArtifactValidationError(
                f"{path}: call to {module_root}.{node.func.attr}() is not allowed"
            )


def _load_object(spec: str) -> Any:
    module_name, sep, object_name = spec.partition(":")
    if not sep or not module_name or not object_name:
        raise ArtifactValidationError(f"plugin spec must be 'module:Object', got {spec!r}")
    try:
        _drop_import_cache(module_name)
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - preserve import failure context
        raise ArtifactValidationError(f"failed to import plugin module {module_name}: {exc}") from exc
    obj: Any = module
    for part in object_name.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise ArtifactValidationError(f"{spec}: missing object component {part!r}") from exc
    return obj


def _drop_import_cache(module_name: str) -> None:
    parts = module_name.split(".")
    exact_names = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
    for loaded_name in list(sys.modules):
        if loaded_name in exact_names or loaded_name.startswith(f"{module_name}."):
            del sys.modules[loaded_name]


def _build_plugin(spec: str, **kwargs: Any) -> Any:
    plugin_cls = _load_object(spec)
    signature = inspect.signature(plugin_cls)
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    if accepts_kwargs:
        return plugin_cls(**kwargs)
    call_kwargs = {name: value for name, value in kwargs.items() if name in parameters}
    return plugin_cls(**call_kwargs)


def _validate_golden_cases(
    ref_model: Any,
    golden_cases: tuple[GoldenCase, ...],
    *,
    normalize_expected: Any,
) -> None:
    for golden_case in golden_cases:
        result = ref_model.predict(golden_case.to_fuzz_case())
        actual = normalize_expected(result, spec="golden case ref model").expected
        if actual != golden_case.expected:
            raise ArtifactValidationError(
                f"golden case line {golden_case.line_no}: expected "
                f"{golden_case.expected!r}, got {actual!r}"
            )


def _framework_python_path() -> Path:
    return Path(__file__).resolve().parents[2] / "libafl_bfm_fuzz" / "py"


def _load_replay_contracts() -> Any:
    try:
        from fuzz_uvm import contracts
    except Exception as exc:  # noqa: BLE001 - surface import path issues clearly
        raise ArtifactValidationError(f"failed to import fuzz_uvm contracts: {exc}") from exc
    return contracts


@contextmanager
def python_path(paths: Iterable[Path]) -> Iterator[None]:
    original = list(sys.path)
    original_dont_write_bytecode = sys.dont_write_bytecode
    for path in reversed([str(path.resolve()) for path in paths]):
        if path not in sys.path:
            sys.path.insert(0, path)
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = original_dont_write_bytecode
        sys.path[:] = original

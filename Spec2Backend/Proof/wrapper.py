"""Static wrapper-template checks for generated RefModelIR plugins."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


ALLOWED_IMPORTS = {
    ("pathlib", ("Path",)),
    ("fuzz_uvm.contracts", ("ExpectedResult",)),
    ("Spec2Backend.RefModelDSL.interpreter", ("RefModelInterpreter",)),
}

ALLOWED_CALLS = {
    "Path",
    "Path.resolve",
    "RefModelInterpreter.from_path",
    "self._interpreter.initial_state",
    "self._interpreter.step",
    "ExpectedResult",
    "outputs.get",
    "outputs.values",
    "isinstance",
    "len",
    "next",
    "iter",
}


@dataclass(frozen=True)
class WrapperCheckResult:
    ok: bool
    code: str = "wrapper_template_verified"
    message: str = "wrapper template verified"
    metadata: dict[str, Any] = field(default_factory=dict)


def check_wrapper_template(source: str) -> WrapperCheckResult:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return WrapperCheckResult(False, code="wrapper_syntax", message=str(exc))

    issues: list[str] = []
    _check_module_shape(tree, issues)
    class_node = _dsl_ref_model_class(tree)
    if class_node is None:
        issues.append("wrapper must define class DslRefModel")
    else:
        _check_class_shape(class_node, issues)
        _check_calls(class_node, issues)
        _check_init_shape(class_node, issues)
        _check_predict_shape(class_node, issues)

    if issues:
        return WrapperCheckResult(
            False,
            code="wrapper_template_mismatch",
            message="; ".join(issues),
            metadata={"issue_count": len(issues)},
        )
    return WrapperCheckResult(
        True,
        metadata={
            "implementation_boundary": "wrapper_template_only",
            "trusted_runtime": "RefModelInterpreter",
        },
    )


def _check_module_shape(tree: ast.Module, issues: list[str]) -> None:
    class_count = 0
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names)
            if (node.module or "", names) not in ALLOWED_IMPORTS:
                issues.append(f"unsupported import from {node.module}: {names}")
            continue
        if isinstance(node, ast.ClassDef):
            class_count += 1
            if node.name != "DslRefModel":
                issues.append(f"unsupported class {node.name!r}")
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        issues.append(f"unsupported top-level node {type(node).__name__}")
    if class_count != 1:
        issues.append("wrapper must define exactly one class")


def _dsl_ref_model_class(tree: ast.Module) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DslRefModel":
            return node
    return None


def _check_class_shape(class_node: ast.ClassDef, issues: list[str]) -> None:
    method_names = []
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef):
            method_names.append(node.name)
            continue
        issues.append(f"unsupported class body node {type(node).__name__}")
    if sorted(method_names) != ["__init__", "predict"]:
        issues.append("DslRefModel must define exactly __init__ and predict")


def _check_calls(class_node: ast.ClassDef, issues: list[str]) -> None:
    for node in ast.walk(class_node):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Lambda)):
            issues.append(f"unsupported node inside wrapper class: {type(node).__name__}")
        if isinstance(node, ast.Call):
            call_name = _call_name(node.func)
            if call_name not in ALLOWED_CALLS:
                issues.append(f"unsupported call {call_name or ast.dump(node.func)}")


def _check_init_shape(class_node: ast.ClassDef, issues: list[str]) -> None:
    init = next((node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"), None)
    if init is None:
        return
    _check_init_body_sequence(init, issues)
    has_interpreter_assignment = False
    has_state_assignment = False
    for node in ast.walk(init):
        if not isinstance(node, ast.Assign):
            continue
        if _is_interpreter_assignment(node):
            has_interpreter_assignment = True
        if _is_state_assignment(node):
            has_state_assignment = True
    if not has_interpreter_assignment:
        issues.append("__init__ must assign self._interpreter from RefModelInterpreter.from_path")
    if not has_state_assignment:
        issues.append("__init__ must assign self._state from RefModelInterpreter.initial_state")


def _check_init_body_sequence(init: ast.FunctionDef, issues: list[str]) -> None:
    if len(init.body) != 5:
        issues.append("__init__ must match the deterministic wrapper statement sequence")
        return
    target_stmt, config_stmt, root_stmt, interpreter_stmt, state_stmt = init.body
    if not isinstance(target_stmt, ast.Assign) or not _assigns_self_attr(target_stmt, "target"):
        issues.append("__init__ first statement must assign self.target")
    if not isinstance(config_stmt, ast.Assign) or not _assigns_self_attr(config_stmt, "config"):
        issues.append("__init__ second statement must assign self.config")
    if not isinstance(root_stmt, ast.Assign) or not _is_root_assignment(root_stmt):
        issues.append("__init__ third statement must derive root from __file__")
    if not isinstance(interpreter_stmt, ast.Assign) or not _is_interpreter_assignment(interpreter_stmt):
        issues.append("__init__ fourth statement must assign self._interpreter")
    if not isinstance(state_stmt, ast.Assign) or not _is_state_assignment(state_stmt):
        issues.append("__init__ fifth statement must assign self._state")


def _check_predict_shape(class_node: ast.ClassDef, issues: list[str]) -> None:
    predict = next((node for node in class_node.body if isinstance(node, ast.FunctionDef) and node.name == "predict"), None)
    if predict is None:
        return
    _check_predict_body_sequence(predict, issues)
    has_step_call = False
    has_expected_result_return = False
    for node in ast.walk(predict):
        if isinstance(node, ast.Call) and _valid_step_call(node):
            has_step_call = True
        if isinstance(node, ast.Return):
            if _returns_expected_result(node.value):
                has_expected_result_return = True
                if not _expected_result_uses_expected_name(node.value):
                    issues.append("ExpectedResult.expected must be the interpreter-derived expected value")
            else:
                issues.append("predict must return ExpectedResult")
        if isinstance(node, ast.Assign):
            if _is_step_assignment(node):
                continue
            unsupported_assignment = True
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "expected":
                    unsupported_assignment = False
                    if not _allowed_expected_assignment(node.value):
                        issues.append("predict may assign expected only from interpreter outputs")
            if unsupported_assignment:
                issues.append("predict may only assign interpreter outputs/state and expected")
    if not has_step_call:
        issues.append("predict must call self._interpreter.step")
    if not has_expected_result_return:
        issues.append("predict must return ExpectedResult")


def _check_predict_body_sequence(predict: ast.FunctionDef, issues: list[str]) -> None:
    if len(predict.body) != 4:
        issues.append("predict must match the deterministic wrapper statement sequence")
        return
    step_stmt, expected_stmt, fallback_stmt, return_stmt = predict.body
    if not isinstance(step_stmt, ast.Assign) or not _is_step_assignment(step_stmt):
        issues.append("predict first statement must call interpreter step")
    if not isinstance(expected_stmt, ast.Assign) or not _is_expected_assignment(expected_stmt):
        issues.append("predict second statement must derive expected from outputs")
    if not isinstance(fallback_stmt, ast.If) or not _is_expected_fallback_if(fallback_stmt):
        issues.append("predict third statement must be the single-output fallback")
    if not isinstance(return_stmt, ast.Return) or not _returns_expected_result(return_stmt.value):
        issues.append("predict final statement must return ExpectedResult")


def _is_expected_assignment(node: ast.Assign) -> bool:
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "expected"
        and _allowed_expected_assignment(node.value)
    )


def _is_expected_fallback_if(node: ast.If) -> bool:
    return (
        len(node.body) == 1
        and not node.orelse
        and isinstance(node.body[0], ast.Assign)
        and _is_expected_assignment(node.body[0])
    )


def _is_step_assignment(node: ast.Assign) -> bool:
    if len(node.targets) != 1 or not isinstance(node.value, ast.Call) or not _valid_step_call(node.value):
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Tuple) or len(target.elts) != 2:
        return False
    first, second = target.elts
    return (
        isinstance(first, ast.Name)
        and first.id == "outputs"
        and isinstance(second, ast.Attribute)
        and isinstance(second.value, ast.Name)
        and second.value.id == "self"
        and second.attr == "_state"
    )


def _valid_step_call(node: ast.Call) -> bool:
    if _call_name(node.func) != "self._interpreter.step":
        return False
    if len(node.args) != 1 or not _is_attr(node.args[0], "case", "data"):
        return False
    return any(keyword.arg == "state" and _is_attr(keyword.value, "self", "_state") for keyword in node.keywords)


def _valid_from_path_call(node: ast.Call) -> bool:
    if _call_name(node.func) != "RefModelInterpreter.from_path" or len(node.args) != 1:
        return False
    arg = node.args[0]
    return (
        isinstance(arg, ast.BinOp)
        and isinstance(arg.op, ast.Div)
        and isinstance(arg.left, ast.Name)
        and arg.left.id == "root"
        and isinstance(arg.right, ast.Constant)
        and arg.right.value == "ref_model_ir.json"
    )


def _is_root_assignment(node: ast.Assign) -> bool:
    return (
        len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "root"
        and _is_generated_root_expr(node.value)
    )


def _is_generated_root_expr(value: ast.AST) -> bool:
    if not isinstance(value, ast.Subscript) or not isinstance(value.value, ast.Attribute):
        return False
    if value.value.attr != "parents" or not isinstance(value.value.value, ast.Call):
        return False
    resolve_call = value.value.value
    if _call_name(resolve_call.func) != "Path.resolve" or resolve_call.args or resolve_call.keywords:
        return False
    receiver = resolve_call.func.value if isinstance(resolve_call.func, ast.Attribute) else None
    if not isinstance(receiver, ast.Call) or _call_name(receiver.func) != "Path":
        return False
    if len(receiver.args) != 1 or not isinstance(receiver.args[0], ast.Name) or receiver.args[0].id != "__file__":
        return False
    return isinstance(value.slice, ast.Constant) and value.slice.value == 1


def _is_interpreter_assignment(node: ast.Assign) -> bool:
    return _assigns_self_attr(node, "_interpreter") and isinstance(node.value, ast.Call) and _valid_from_path_call(node.value)


def _is_state_assignment(node: ast.Assign) -> bool:
    return (
        _assigns_self_attr(node, "_state")
        and isinstance(node.value, ast.Call)
        and _call_name(node.value.func) == "self._interpreter.initial_state"
    )


def _assigns_self_attr(node: ast.Assign, attr: str) -> bool:
    return any(_is_attr(target, "self", attr) for target in node.targets)


def _is_attr(node: ast.AST, base: str, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == base and node.attr == attr


def _returns_expected_result(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Call) and _call_name(value.func) == "ExpectedResult"


def _expected_result_uses_expected_name(value: ast.AST) -> bool:
    if not isinstance(value, ast.Call):
        return False
    for keyword in value.keywords:
        if keyword.arg == "expected":
            return isinstance(keyword.value, ast.Name) and keyword.value.id == "expected"
    return False


def _allowed_expected_assignment(value: ast.AST) -> bool:
    if isinstance(value, ast.Call) and _call_name(value.func) == "outputs.get":
        return True
    if isinstance(value, ast.Call) and _call_name(value.func) == "next":
        return True
    if isinstance(value, ast.IfExp):
        return (
            isinstance(value.body, ast.Call)
            and _call_name(value.body.func) == "outputs.get"
            and isinstance(value.orelse, ast.Name)
            and value.orelse.id == "outputs"
        )
    return False


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Subscript):
        return _call_name(node.value)
    return ""


__all__ = ["WrapperCheckResult", "check_wrapper_template"]

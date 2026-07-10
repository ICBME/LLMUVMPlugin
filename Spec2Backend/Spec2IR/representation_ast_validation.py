"""Strict validator for RepresentationAST v2."""

from __future__ import annotations

from typing import Any

from .representation_ast import (
    ALLOWED_AST_NODES,
    ALLOWED_BINARY_OPS,
    ALLOWED_CLOCK_EDGES,
    ALLOWED_COMPARE_OPS,
    ALLOWED_REDUCE_OPS,
    ALLOWED_REPRESENTATION_KINDS,
    ALLOWED_RESET_POLARITIES,
    ALLOWED_RESET_SYNCHRONIES,
    ALLOWED_SIGNAL_BINDING_ROLES,
    ALLOWED_UNARY_OPS,
    NODE_ALLOWED_KEYS,
    REPRESENTATION_AST_VERSION,
    ROOT_NODES_BY_REPRESENTATION_KIND,
)
from .schema import SemanticSpecIRIssue


def validate_representation(
    value: dict[str, Any],
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    allowed_keys = {"ast", "ast_version", "kind", "subjects", "text"}
    for key in value:
        if key not in allowed_keys:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.{key}",
                    f"is not allowed in strict RepresentationAST v{REPRESENTATION_AST_VERSION}",
                )
            )

    ast_version = value.get("ast_version")
    if ast_version != REPRESENTATION_AST_VERSION:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.ast_version",
                f"must be {REPRESENTATION_AST_VERSION}, got {ast_version!r}",
            )
        )
    representation_kind = value.get("kind")
    if representation_kind not in ALLOWED_REPRESENTATION_KINDS:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.kind",
                f"must be one of {sorted(ALLOWED_REPRESENTATION_KINDS)}, got {representation_kind!r}",
            )
        )
    text = value.get("text")
    if not isinstance(text, str) or not text.strip():
        issues.append(SemanticSpecIRIssue(f"{path}.text", "must be a non-empty string"))
    subjects = value.get("subjects", [])
    if "subjects" in value and not isinstance(subjects, list):
        issues.append(SemanticSpecIRIssue(f"{path}.subjects", "must be a list"))
    elif isinstance(subjects, list):
        for index, subject in enumerate(subjects):
            if not isinstance(subject, str):
                issues.append(SemanticSpecIRIssue(f"{path}.subjects[{index}]", "must be a string"))

    ast = value.get("ast")
    if not isinstance(ast, dict):
        issues.append(SemanticSpecIRIssue(f"{path}.ast", "must be an AST object"))
        return
    validate_ast_node(ast, f"{path}.ast", manifest_fields, issues)
    if isinstance(representation_kind, str):
        root_node = ast.get("node")
        allowed_roots = ROOT_NODES_BY_REPRESENTATION_KIND.get(representation_kind, set())
        if isinstance(root_node, str) and root_node not in allowed_roots:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.ast.node",
                    f"root node {root_node!r} is not valid for representation kind {representation_kind!r}",
                )
            )


def validate_ast_node(
    value: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, dict):
        issues.append(SemanticSpecIRIssue(path, "AST node must be an object"))
        return
    node = value.get("node")
    if node not in ALLOWED_AST_NODES:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.node",
                f"must be one of {sorted(ALLOWED_AST_NODES)}, got {node!r}",
            )
        )
        return
    allowed_keys = NODE_ALLOWED_KEYS[node]
    for key in value:
        if key not in allowed_keys:
            issues.append(SemanticSpecIRIssue(f"{path}.{key}", f"is not allowed for AST node {node!r}"))

    validator = NODE_VALIDATORS.get(node)
    if validator is not None:
        validator(value, path, manifest_fields, issues)


def validate_ref_node(value: Any, path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_ast_node(value, path, manifest_fields, issues)
    if isinstance(value, dict) and value.get("node") not in {"field_ref", "signal_ref"}:
        issues.append(SemanticSpecIRIssue(f"{path}.node", "must be field_ref or signal_ref"))


def validate_required_node(
    value: dict[str, Any],
    key: str,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if key not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.{key}", "is required"))
        return
    validate_ast_node(value[key], f"{path}.{key}", manifest_fields, issues)


def validate_optional_node(
    value: dict[str, Any],
    key: str,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if key in value:
        validate_ast_node(value[key], f"{path}.{key}", manifest_fields, issues)


def validate_required_node_kind(
    value: dict[str, Any],
    key: str,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
    allowed_nodes: set[str],
) -> None:
    if key not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.{key}", "is required"))
        return
    validate_node_kind(value[key], f"{path}.{key}", manifest_fields, issues, allowed_nodes)


def validate_optional_node_kind(
    value: dict[str, Any],
    key: str,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
    allowed_nodes: set[str],
) -> None:
    if key in value:
        validate_node_kind(value[key], f"{path}.{key}", manifest_fields, issues, allowed_nodes)


def validate_node_kind(
    value: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
    allowed_nodes: set[str],
) -> None:
    validate_ast_node(value, path, manifest_fields, issues)
    if isinstance(value, dict) and value.get("node") not in allowed_nodes:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.node",
                f"must be one of {sorted(allowed_nodes)}",
            )
        )


def validate_node_list(
    value: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
    *,
    required: bool = True,
) -> None:
    if value is None and not required:
        return
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, item in enumerate(value):
        validate_ast_node(item, f"{path}[{index}]", manifest_fields, issues)


def validate_ref_node_list(value: Any, path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, item in enumerate(value):
        validate_ref_node(item, f"{path}[{index}]", manifest_fields, issues)


def validate_string(value: dict[str, Any], key: str, path: str, issues: list[SemanticSpecIRIssue]) -> None:
    if not isinstance(value.get(key), str) or not value.get(key):
        issues.append(SemanticSpecIRIssue(f"{path}.{key}", "must be a non-empty string"))


def validate_string_list(value: Any, path: str, issues: list[SemanticSpecIRIssue], *, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            issues.append(SemanticSpecIRIssue(f"{path}[{index}]", "must be a non-empty string"))


def validate_positive_int(
    value: dict[str, Any],
    key: str,
    path: str,
    issues: list[SemanticSpecIRIssue],
    *,
    required: bool = False,
) -> None:
    if key not in value:
        if required:
            issues.append(SemanticSpecIRIssue(f"{path}.{key}", "is required"))
        return
    if not isinstance(value[key], int) or value[key] < 1:
        issues.append(SemanticSpecIRIssue(f"{path}.{key}", "must be a positive integer"))


def validate_literal(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "value" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.value", "is required"))
    if "width" in value:
        validate_positive_int(value, "width", path, issues)
    if "base" in value and not isinstance(value["base"], str):
        issues.append(SemanticSpecIRIssue(f"{path}.base", "must be a string"))


def validate_field_ref(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "name", path, issues)
    name = value.get("name")
    if isinstance(name, str) and manifest_fields and name not in manifest_fields:
        issues.append(SemanticSpecIRIssue(f"{path}.name", f"unknown manifest field {name!r}"))


def validate_named_ref(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "name", path, issues)


def validate_interface_decl(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    direction = value.get("direction")
    if direction not in {"input", "inout", "output"}:
        issues.append(SemanticSpecIRIssue(f"{path}.direction", "must be input, output, or inout"))
    validate_string(value, "name", path, issues)
    validate_positive_int(value, "width", path, issues, required=True)
    if "signed" in value and not isinstance(value["signed"], bool):
        issues.append(SemanticSpecIRIssue(f"{path}.signed", "must be a boolean"))


def validate_assignment(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "target" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.target", "is required"))
    else:
        validate_ref_node(value["target"], f"{path}.target", manifest_fields, issues)
    validate_required_node(value, "value", path, manifest_fields, issues)
    if "blocking" in value and not isinstance(value["blocking"], bool):
        issues.append(SemanticSpecIRIssue(f"{path}.blocking", "must be a boolean"))


def validate_conditional_assignment(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "condition", path, manifest_fields, issues)
    if "target" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.target", "is required"))
    else:
        validate_ref_node(value["target"], f"{path}.target", manifest_fields, issues)
    validate_required_node(value, "value", path, manifest_fields, issues)


def validate_constant_relation(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "target" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.target", "is required"))
    else:
        validate_ref_node(value["target"], f"{path}.target", manifest_fields, issues)
    validate_required_node(value, "value", path, manifest_fields, issues)


def validate_unary_op(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if value.get("op") not in ALLOWED_UNARY_OPS:
        issues.append(SemanticSpecIRIssue(f"{path}.op", f"must be one of {sorted(ALLOWED_UNARY_OPS)}"))
    validate_required_node(value, "operand", path, manifest_fields, issues)


def validate_binary_op(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if value.get("op") not in ALLOWED_BINARY_OPS:
        issues.append(SemanticSpecIRIssue(f"{path}.op", f"must be one of {sorted(ALLOWED_BINARY_OPS)}"))
    validate_required_node(value, "left", path, manifest_fields, issues)
    validate_required_node(value, "right", path, manifest_fields, issues)


def validate_compare(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if value.get("op") not in ALLOWED_COMPARE_OPS:
        issues.append(SemanticSpecIRIssue(f"{path}.op", f"must be one of {sorted(ALLOWED_COMPARE_OPS)}"))
    validate_required_node(value, "left", path, manifest_fields, issues)
    validate_required_node(value, "right", path, manifest_fields, issues)


def validate_mux(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "condition", path, manifest_fields, issues)
    validate_required_node(value, "when_true", path, manifest_fields, issues)
    validate_required_node(value, "when_false", path, manifest_fields, issues)


def validate_concat(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_node_list(value.get("parts"), f"{path}.parts", manifest_fields, issues)


def validate_slice(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "value", path, manifest_fields, issues)
    msb = value.get("msb")
    lsb = value.get("lsb")
    for key, index in (("msb", msb), ("lsb", lsb)):
        if key not in value:
            issues.append(SemanticSpecIRIssue(f"{path}.{key}", "is required"))
        elif not isinstance(index, int) or isinstance(index, bool) or index < 0:
            issues.append(SemanticSpecIRIssue(f"{path}.{key}", "must be a non-negative integer"))
    if (
        isinstance(msb, int)
        and not isinstance(msb, bool)
        and isinstance(lsb, int)
        and not isinstance(lsb, bool)
        and msb < lsb
    ):
        issues.append(SemanticSpecIRIssue(path, "must satisfy msb >= lsb"))


def validate_reduce(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if value.get("op") not in ALLOWED_REDUCE_OPS:
        issues.append(SemanticSpecIRIssue(f"{path}.op", f"must be one of {sorted(ALLOWED_REDUCE_OPS)}"))
    validate_required_node(value, "operand", path, manifest_fields, issues)


def validate_cast(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "type", path, issues)
    validate_required_node(value, "value", path, manifest_fields, issues)
    validate_positive_int(value, "width", path, issues)
    if "signed" in value and not isinstance(value["signed"], bool):
        issues.append(SemanticSpecIRIssue(f"{path}.signed", "must be a boolean"))


def validate_call(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "function", path, issues)
    validate_node_list(value.get("args", []), f"{path}.args", manifest_fields, issues)


def validate_operation_relation(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "operation", path, issues)
    validate_node_list(value.get("operands", []), f"{path}.operands", manifest_fields, issues)
    validate_optional_node(value, "result", path, manifest_fields, issues)
    if "text" in value and not isinstance(value["text"], str):
        issues.append(SemanticSpecIRIssue(f"{path}.text", "must be a string"))


def validate_text_expr(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "text", path, issues)


def validate_semantic_claim(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "text", path, issues)
    if "claim_id" in value and not isinstance(value["claim_id"], str):
        issues.append(SemanticSpecIRIssue(f"{path}.claim_id", "must be a string"))
    if "claim_kind" in value and not isinstance(value["claim_kind"], str):
        issues.append(SemanticSpecIRIssue(f"{path}.claim_kind", "must be a string"))
    validate_string_list(value.get("subjects", []), f"{path}.subjects", issues)


def validate_state_transition(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "from", path, manifest_fields, issues)
    validate_required_node(value, "to", path, manifest_fields, issues)
    for key in ("from", "to"):
        endpoint = value.get(key)
        if isinstance(endpoint, dict) and endpoint.get("node") != "state_ref":
            issues.append(SemanticSpecIRIssue(f"{path}.{key}.node", "must be state_ref"))
    validate_optional_node(value, "condition", path, manifest_fields, issues)
    validate_node_list(value.get("outputs", []), f"{path}.outputs", manifest_fields, issues)


def validate_fsm(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "state_signal" in value:
        validate_ref_node(value["state_signal"], f"{path}.state_signal", manifest_fields, issues)
    validate_string_list(value.get("states"), f"{path}.states", issues)
    if "initial_state" in value:
        validate_string(value, "initial_state", path, issues)
    validate_optional_node(value, "reset", path, manifest_fields, issues)
    validate_node_list(value.get("transitions", []), f"{path}.transitions", manifest_fields, issues)
    validate_node_list(value.get("outputs", []), f"{path}.outputs", manifest_fields, issues)
    validate_optional_node_kind(value, "context", path, manifest_fields, issues, {"clock_reset_context"})


def validate_reset_rule(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_optional_node(value, "condition", path, manifest_fields, issues)
    validate_node_list(value.get("effects"), f"{path}.effects", manifest_fields, issues)
    validate_optional_node(value, "state", path, manifest_fields, issues)
    validate_optional_node_kind(value, "context", path, manifest_fields, issues, {"clock_reset_context"})


def validate_sequential_update(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_optional_node(value, "event", path, manifest_fields, issues)
    validate_node_list(value.get("updates"), f"{path}.updates", manifest_fields, issues)
    validate_optional_node_kind(value, "context", path, manifest_fields, issues, {"clock_reset_context"})


def validate_temporal_rule(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_optional_node_kind(value, "clock", path, manifest_fields, issues, {"clock_event"})
    validate_optional_node_kind(value, "context", path, manifest_fields, issues, {"clock_reset_context"})
    validate_optional_node_kind(value, "disable", path, manifest_fields, issues, {"reset_disable"})
    validate_required_node(value, "property", path, manifest_fields, issues)


def validate_protocol_rule(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "text", path, issues)
    validate_string_list(value.get("participants", []), f"{path}.participants", issues)
    validate_optional_node_kind(value, "context", path, manifest_fields, issues, {"clock_reset_context"})
    validate_required_node(value, "property", path, manifest_fields, issues)


def validate_constraint(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "text" in value and not isinstance(value["text"], str):
        issues.append(SemanticSpecIRIssue(f"{path}.text", "must be a string"))
    validate_required_node(value, "expr", path, manifest_fields, issues)


def validate_example_trace(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    steps = value.get("steps")
    if not isinstance(steps, list):
        issues.append(SemanticSpecIRIssue(f"{path}.steps", "must be a list"))
        return
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            issues.append(SemanticSpecIRIssue(f"{path}.steps[{index}]", "must be an object"))
            continue
        for key, item in step.items():
            if key == "node":
                continue
            validate_ast_node(item, f"{path}.steps[{index}].{key}", manifest_fields, issues)


def validate_clock_event(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if value.get("edge") not in ALLOWED_CLOCK_EDGES:
        issues.append(SemanticSpecIRIssue(f"{path}.edge", f"must be one of {sorted(ALLOWED_CLOCK_EDGES)}"))
    if "signal" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.signal", "is required"))
    else:
        validate_ref_node(value["signal"], f"{path}.signal", manifest_fields, issues)


def validate_clock_reset_context(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "clock" not in value and "reset" not in value:
        issues.append(SemanticSpecIRIssue(path, "must include at least clock or reset"))
    validate_optional_node_kind(value, "clock", path, manifest_fields, issues, {"clock_event"})
    if "reset" in value:
        validate_ref_node(value["reset"], f"{path}.reset", manifest_fields, issues)
    if "reset_polarity" in value and value["reset_polarity"] not in ALLOWED_RESET_POLARITIES:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.reset_polarity",
                f"must be one of {sorted(ALLOWED_RESET_POLARITIES)}",
            )
        )
    if "reset_synchrony" in value and value["reset_synchrony"] not in ALLOWED_RESET_SYNCHRONIES:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.reset_synchrony",
                f"must be one of {sorted(ALLOWED_RESET_SYNCHRONIES)}",
            )
        )
    if "reset" not in value and ("reset_polarity" in value or "reset_synchrony" in value):
        issues.append(SemanticSpecIRIssue(f"{path}.reset", "is required when reset polarity or synchrony is provided"))


def validate_signal_property(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    if "signal" not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.signal", "is required"))
    else:
        validate_ref_node(value["signal"], f"{path}.signal", manifest_fields, issues)
    if "cycles" in value and (not isinstance(value["cycles"], int) or value["cycles"] < 1):
        issues.append(SemanticSpecIRIssue(f"{path}.cycles", "must be a positive integer"))


def validate_reset_disable(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "condition", path, manifest_fields, issues)


def validate_sequence(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_node_list(value.get("items"), f"{path}.items", manifest_fields, issues)


def validate_implication(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "antecedent", path, manifest_fields, issues)
    validate_required_node(value, "consequent", path, manifest_fields, issues)
    validate_optional_node(value, "delay", path, manifest_fields, issues)


def validate_delay_range(value: dict[str, Any], path: str, _manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    for key in ("min", "max"):
        if not isinstance(value.get(key), int) or value.get(key) < 0:
            issues.append(SemanticSpecIRIssue(f"{path}.{key}", "must be a non-negative integer"))
    if isinstance(value.get("min"), int) and isinstance(value.get("max"), int) and value["max"] < value["min"]:
        issues.append(SemanticSpecIRIssue(f"{path}.max", "must be >= min"))


def validate_latency_rule(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "trigger", path, manifest_fields, issues)
    validate_required_node(value, "response", path, manifest_fields, issues)
    validate_required_node_kind(value, "delay", path, manifest_fields, issues, {"delay_range"})


def validate_handshake_rule(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_ref_node(value, "valid", path, manifest_fields, issues)
    validate_required_ref_node(value, "ready", path, manifest_fields, issues)
    valid_name = ref_node_name(value.get("valid"))
    ready_name = ref_node_name(value.get("ready"))
    if valid_name and ready_name and valid_name == ready_name:
        issues.append(SemanticSpecIRIssue(f"{path}.ready", "valid and ready must reference different signals"))
    if "payload" in value:
        validate_ref_node_list(value["payload"], f"{path}.payload", manifest_fields, issues)
    validate_optional_node(value, "transfer", path, manifest_fields, issues)
    validate_optional_node_kind(value, "latency", path, manifest_fields, issues, {"delay_range"})


def validate_required_ref_node(
    value: dict[str, Any],
    key: str,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if key not in value:
        issues.append(SemanticSpecIRIssue(f"{path}.{key}", "is required"))
        return
    validate_ref_node(value[key], f"{path}.{key}", manifest_fields, issues)


def ref_node_name(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("node") in {"field_ref", "signal_ref"} and isinstance(value.get("name"), str):
        return value["name"]
    return None


def validate_signal_binding(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_string(value, "subject", path, issues)
    validate_required_ref_node(value, "signal", path, manifest_fields, issues)
    if "role" in value and value["role"] not in ALLOWED_SIGNAL_BINDING_ROLES:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.role",
                f"must be one of {sorted(ALLOWED_SIGNAL_BINDING_ROLES)}",
            )
        )


def validate_throughout(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "expr", path, manifest_fields, issues)
    validate_required_node(value, "sequence", path, manifest_fields, issues)


def validate_until(value: dict[str, Any], path: str, manifest_fields: set[str], issues: list[SemanticSpecIRIssue]) -> None:
    validate_required_node(value, "expr", path, manifest_fields, issues)
    validate_required_node(value, "condition", path, manifest_fields, issues)


NODE_VALIDATORS = {
    "assignment": validate_assignment,
    "binary_op": validate_binary_op,
    "call": validate_call,
    "cast": validate_cast,
    "clock_event": validate_clock_event,
    "clock_reset_context": validate_clock_reset_context,
    "compare": validate_compare,
    "concat": validate_concat,
    "conditional_assignment": validate_conditional_assignment,
    "constant_relation": validate_constant_relation,
    "constraint": validate_constraint,
    "delay_range": validate_delay_range,
    "example_trace": validate_example_trace,
    "fell": validate_signal_property,
    "field_ref": validate_field_ref,
    "fsm": validate_fsm,
    "handshake_rule": validate_handshake_rule,
    "implication": validate_implication,
    "interface_decl": validate_interface_decl,
    "literal": validate_literal,
    "latency_rule": validate_latency_rule,
    "mux": validate_mux,
    "operation_relation": validate_operation_relation,
    "past": validate_signal_property,
    "protocol_rule": validate_protocol_rule,
    "reduce": validate_reduce,
    "reset_disable": validate_reset_disable,
    "reset_rule": validate_reset_rule,
    "rose": validate_signal_property,
    "sequence": validate_sequence,
    "sequential_update": validate_sequential_update,
    "signal_binding": validate_signal_binding,
    "signal_ref": validate_named_ref,
    "slice": validate_slice,
    "stable": validate_signal_property,
    "state_ref": validate_named_ref,
    "state_transition": validate_state_transition,
    "semantic_claim": validate_semantic_claim,
    "temporal_rule": validate_temporal_rule,
    "text_expr": validate_text_expr,
    "throughout": validate_throughout,
    "unary_op": validate_unary_op,
    "until": validate_until,
}

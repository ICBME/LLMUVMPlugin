"""RepresentationAST v2 contract, construction, and traversal helpers."""

from __future__ import annotations

import re
from typing import Any, Iterable


REPRESENTATION_AST_VERSION = 2

ALLOWED_REPRESENTATION_KINDS = {
    "combinational_relation",
    "constraint",
    "example_trace",
    "interface_decl",
    "protocol_rule",
    "reset_rule",
    "sequential_update",
    "state_machine",
    "temporal_rule",
    "textual_formalization",
}

ALLOWED_AST_NODES = {
    "assignment",
    "binary_op",
    "call",
    "cast",
    "clock_event",
    "clock_reset_context",
    "compare",
    "concat",
    "conditional_assignment",
    "constant_relation",
    "constraint",
    "delay_range",
    "example_trace",
    "fell",
    "field_ref",
    "fsm",
    "implication",
    "interface_decl",
    "literal",
    "latency_rule",
    "mux",
    "operation_relation",
    "past",
    "protocol_rule",
    "handshake_rule",
    "reduce",
    "reset_disable",
    "reset_rule",
    "rose",
    "sequence",
    "sequential_update",
    "signal_binding",
    "signal_ref",
    "slice",
    "stable",
    "state_ref",
    "state_transition",
    "semantic_claim",
    "temporal_rule",
    "text_expr",
    "throughout",
    "unary_op",
    "until",
}

ROOT_NODES_BY_REPRESENTATION_KIND = {
    "combinational_relation": {
        "assignment",
        "conditional_assignment",
        "constant_relation",
        "operation_relation",
        "semantic_claim",
    },
    "constraint": {"constraint", "semantic_claim"},
    "example_trace": {"example_trace"},
    "interface_decl": {"interface_decl"},
    "protocol_rule": {"protocol_rule", "semantic_claim"},
    "reset_rule": {"reset_rule", "semantic_claim"},
    "sequential_update": {"sequential_update", "semantic_claim"},
    "state_machine": {"fsm", "state_transition", "semantic_claim"},
    "temporal_rule": {"temporal_rule", "semantic_claim"},
    "textual_formalization": {"semantic_claim"},
}

NODE_ALLOWED_KEYS = {
    "assignment": {"node", "target", "value", "blocking"},
    "binary_op": {"node", "op", "left", "right"},
    "call": {"node", "function", "args"},
    "cast": {"node", "type", "value", "width", "signed"},
    "clock_event": {"node", "edge", "signal"},
    "compare": {"node", "op", "left", "right"},
    "concat": {"node", "parts"},
    "conditional_assignment": {"node", "condition", "target", "value"},
    "constant_relation": {"node", "target", "value"},
    "constraint": {"node", "expr", "text"},
    "delay_range": {"node", "min", "max"},
    "example_trace": {"node", "steps"},
    "fell": {"node", "signal"},
    "field_ref": {"node", "name"},
    "clock_reset_context": {"node", "clock", "reset", "reset_polarity", "reset_synchrony"},
    "fsm": {"node", "state_signal", "states", "initial_state", "reset", "transitions", "outputs", "context"},
    "handshake_rule": {"node", "valid", "ready", "payload", "transfer", "latency"},
    "implication": {"node", "antecedent", "consequent", "delay"},
    "interface_decl": {"node", "direction", "name", "width", "signed"},
    "literal": {"node", "value", "width", "base"},
    "latency_rule": {"node", "trigger", "response", "delay"},
    "mux": {"node", "condition", "when_true", "when_false"},
    "operation_relation": {"node", "operation", "operands", "result", "text"},
    "past": {"node", "signal", "cycles"},
    "protocol_rule": {"node", "text", "participants", "property", "context"},
    "reduce": {"node", "op", "operand"},
    "reset_disable": {"node", "condition"},
    "reset_rule": {"node", "condition", "effects", "state", "context"},
    "rose": {"node", "signal"},
    "sequence": {"node", "items"},
    "sequential_update": {"node", "event", "updates", "context"},
    "signal_binding": {"node", "subject", "signal", "role"},
    "signal_ref": {"node", "name"},
    "slice": {"node", "value", "msb", "lsb"},
    "stable": {"node", "signal"},
    "state_ref": {"node", "name"},
    "state_transition": {"node", "from", "to", "condition", "outputs"},
    "semantic_claim": {"node", "claim_id", "claim_kind", "text", "subjects"},
    "temporal_rule": {"node", "clock", "disable", "property", "context"},
    "text_expr": {"node", "text"},
    "throughout": {"node", "expr", "sequence"},
    "unary_op": {"node", "op", "operand"},
    "until": {"node", "expr", "condition"},
}

ALLOWED_UNARY_OPS = {"bitwise_not", "logical_not", "neg", "not", "reduction_not"}
ALLOWED_BINARY_OPS = {
    "add",
    "and",
    "bitwise_and",
    "bitwise_or",
    "bitwise_xor",
    "div",
    "logical_and",
    "logical_or",
    "mod",
    "mul",
    "or",
    "shift_left",
    "shift_right",
    "sub",
    "xor",
}
ALLOWED_COMPARE_OPS = {"eq", "ge", "gt", "le", "lt", "matches", "ne"}
ALLOWED_REDUCE_OPS = {"and", "nand", "nor", "or", "xnor", "xor"}
ALLOWED_CLOCK_EDGES = {"any", "negedge", "posedge"}
ALLOWED_RESET_POLARITIES = {"active_high", "active_low", "unknown"}
ALLOWED_RESET_SYNCHRONIES = {"async", "sync", "unknown"}
ALLOWED_SIGNAL_BINDING_ROLES = {
    "clock",
    "control",
    "input",
    "internal",
    "output",
    "payload",
    "ready",
    "reset",
    "state",
    "unknown",
    "valid",
}
PLACEHOLDER_SIGNAL_NAMES = {"unspecified_target"}
PLACEHOLDER_TEXT_EXPR_VALUES = {
    "implicit clock/event from spec",
    "reset asserted",
    "reset condition from spec",
}
STRUCTURED_TEXT_FALLBACK_ROOTS = {"constraint", "protocol_rule", "temporal_rule"}


def representation_ast_contract() -> dict[str, Any]:
    return {
        "ast_version": REPRESENTATION_AST_VERSION,
        "required_representation_keys": ["ast_version", "kind", "text", "ast"],
        "allowed_representation_kinds": sorted(ALLOWED_REPRESENTATION_KINDS),
        "allowed_ast_nodes": sorted(ALLOWED_AST_NODES),
        "root_nodes_by_kind": {
            kind: sorted(nodes)
            for kind, nodes in sorted(ROOT_NODES_BY_REPRESENTATION_KIND.items())
        },
        "core_node_shapes": {
            "field_ref": {"node": "field_ref", "name": "manifest field name"},
            "signal_ref": {"node": "signal_ref", "name": "RTL/spec signal name"},
            "literal": {"node": "literal", "value": "scalar JSON value", "width": "optional positive integer"},
            "assignment": {"node": "assignment", "target": "field_ref or signal_ref", "value": "expression node"},
            "conditional_assignment": {
                "node": "conditional_assignment",
                "condition": "expression node",
                "target": "field_ref or signal_ref",
                "value": "expression node",
            },
            "fsm": {
                "node": "fsm",
                "states": ["state names"],
                "transitions": ["state_transition nodes"],
            },
            "clock_reset_context": {
                "node": "clock_reset_context",
                "clock": "clock_event node",
                "reset": "optional field_ref or signal_ref node",
                "reset_polarity": sorted(ALLOWED_RESET_POLARITIES),
                "reset_synchrony": sorted(ALLOWED_RESET_SYNCHRONIES),
            },
            "temporal_rule": {
                "node": "temporal_rule",
                "clock": "optional clock_event node",
                "context": "optional clock_reset_context node",
                "disable": "optional reset_disable node",
                "property": "SVA-like property AST node",
            },
            "latency_rule": {
                "node": "latency_rule",
                "trigger": "AST predicate or event node",
                "response": "AST predicate or event node",
                "delay": "delay_range node",
            },
            "handshake_rule": {
                "node": "handshake_rule",
                "valid": "field_ref or signal_ref node",
                "ready": "field_ref or signal_ref node",
                "payload": ["optional payload refs"],
                "latency": "optional delay_range node",
            },
        },
    }


def representation_kind_for_semantic_kind(kind: str) -> str:
    return {
        "interface": "interface_decl",
        "reset": "reset_rule",
        "state_machine": "state_machine",
        "sequential_behavior": "sequential_update",
        "temporal_behavior": "temporal_rule",
        "timing": "temporal_rule",
        "protocol": "protocol_rule",
        "constraint": "constraint",
        "combinational_behavior": "combinational_relation",
        "functional_behavior": "combinational_relation",
        "example": "example_trace",
    }.get(kind, "textual_formalization")


def semantic_representation_for_claim(
    claim: dict[str, Any],
    semantic_kind: str,
    manifest_fields: Iterable[str] = (),
) -> dict[str, Any]:
    summary = str(claim.get("summary") or claim.get("quote") or "").strip()
    fields = set(manifest_fields)
    representation_kind = representation_kind_for_semantic_kind(semantic_kind)
    ast = ast_for_claim(claim, representation_kind, fields)
    representation: dict[str, Any] = {
        "ast_version": REPRESENTATION_AST_VERSION,
        "kind": representation_kind,
        "text": summary,
        "ast": ast,
    }
    subjects = claim.get("subjects")
    if isinstance(subjects, list) and subjects:
        representation["subjects"] = [
            subject for subject in subjects if isinstance(subject, str)
        ]
    return representation


def ast_for_claim(
    claim: dict[str, Any],
    representation_kind: str,
    manifest_fields: set[str],
) -> dict[str, Any]:
    summary = str(claim.get("summary") or claim.get("quote") or "").strip()

    if representation_kind == "interface_decl":
        parsed_port = parse_port_claim(summary)
        if parsed_port:
            return {
                "node": "interface_decl",
                **parsed_port,
            }

    if representation_kind == "state_machine":
        parsed_transition = parse_transition_claim(summary)
        if parsed_transition:
            return {
                "node": "state_transition",
                "from": state_ref(parsed_transition["from"]),
                "to": state_ref(parsed_transition["to"]),
                "condition": text_expr(parsed_transition["condition"]),
                "outputs": parse_transition_outputs(parsed_transition["outputs"], manifest_fields),
            }

    if representation_kind == "combinational_relation":
        parsed_when_assignment = parse_when_assignment(summary, manifest_fields)
        if parsed_when_assignment:
            return parsed_when_assignment

        parsed_constant = parse_constant_relation(summary, manifest_fields)
        if parsed_constant:
            return parsed_constant

        parsed_operation = parse_operation_relation(summary)
        if parsed_operation:
            return parsed_operation

    if representation_kind == "reset_rule":
        parsed_reset = parse_reset_clear(summary, manifest_fields)
        if parsed_reset:
            return parsed_reset

    if representation_kind == "temporal_rule":
        parsed_latency = parse_latency_rule(summary)
        if parsed_latency:
            temporal_rule: dict[str, Any] = {
                "node": "temporal_rule",
                "property": parsed_latency,
            }
            context = parse_clock_reset_context(summary)
            if context:
                temporal_rule["context"] = context
            return temporal_rule

    if representation_kind == "protocol_rule":
        parsed_handshake = parse_handshake_rule(summary)
        if parsed_handshake:
            protocol_rule: dict[str, Any] = {
                "node": "protocol_rule",
                "text": summary,
                "participants": string_subjects(claim),
                "property": parsed_handshake,
            }
            context = parse_clock_reset_context(summary)
            if context:
                protocol_rule["context"] = context
            return protocol_rule

    if representation_kind == "constraint":
        return {
            "node": "constraint",
            "text": summary,
            "expr": text_expr(summary),
        }
    if representation_kind == "temporal_rule":
        return {
            "node": "temporal_rule",
            "property": text_expr(summary),
        }
    if representation_kind == "protocol_rule":
        return {
            "node": "protocol_rule",
            "text": summary,
            "participants": string_subjects(claim),
            "property": text_expr(summary),
        }
    if representation_kind == "sequential_update":
        return {
            "node": "sequential_update",
            "event": text_expr("implicit clock/event from spec"),
            "updates": [
                {
                    "node": "assignment",
                    "target": signal_ref("unspecified_target"),
                    "value": text_expr(summary),
                }
            ],
        }
    if representation_kind == "reset_rule":
        return {
            "node": "reset_rule",
            "condition": text_expr("reset condition from spec"),
            "effects": [
                {
                    "node": "assignment",
                    "target": signal_ref("unspecified_target"),
                    "value": text_expr(summary),
                }
            ],
        }

    return {
        "node": "semantic_claim",
        "claim_id": claim.get("id"),
        "claim_kind": claim.get("kind"),
        "text": summary,
        "subjects": string_subjects(claim),
    }


def parse_port_claim(summary: str) -> dict[str, Any] | None:
    match = re.match(
        r"^[-*]?\s*(input|output|inout)\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+\((\d+)\s+bits?\))?",
        summary.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    direction, name, width = match.groups()
    return {
        "direction": direction.lower(),
        "name": name,
        "width": int(width) if width is not None else 1,
    }


def parse_transition_claim(summary: str) -> dict[str, Any] | None:
    match = re.search(
        r"(?P<from>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s*\((?P<outputs>[^)]*)\))?\s*--(?P<condition>[^-]+)-->\s*"
        r"(?P<to>[A-Za-z_][A-Za-z0-9_]*)",
        summary,
    )
    if not match:
        return None
    return {
        "from": match.group("from"),
        "to": match.group("to"),
        "condition": match.group("condition").strip(),
        "outputs": match.group("outputs") or "",
    }


def parse_when_assignment(summary: str, manifest_fields: set[str]) -> dict[str, Any] | None:
    match = re.match(
        r"^when\s+(?P<cond_name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<cond_op>==|=|!=)\s*"
        r"(?P<cond_value>[^,.;]+)\s*,\s*"
        r"(?P<target>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<assign_op>==|=)\s*"
        r"(?P<value>[^.;]+)",
        summary.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "node": "conditional_assignment",
        "condition": {
            "node": "compare",
            "op": "ne" if match.group("cond_op") == "!=" else "eq",
            "left": ref_for_name(match.group("cond_name"), manifest_fields),
            "right": literal_from_text(match.group("cond_value")),
        },
        "target": signal_ref(match.group("target")),
        "value": literal_from_text(match.group("value")),
    }


def parse_reset_clear(summary: str, manifest_fields: set[str]) -> dict[str, Any] | None:
    lowered = summary.lower()
    if "reset" not in lowered and "rst" not in lowered:
        return None
    match = re.search(
        r"clear(?:s)?\s+(?:the\s+)?(?P<target>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+\w+){0,2}?\s+(?:to\s+)?(?P<value>zero|one|0|1)",
        summary,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "node": "reset_rule",
        "condition": text_expr("reset asserted"),
        "effects": [
            {
                "node": "assignment",
                "target": ref_for_name(match.group("target"), manifest_fields, prefer_field=False),
                "value": literal_from_text(match.group("value")),
            }
        ],
    }


def parse_constant_relation(summary: str, manifest_fields: set[str]) -> dict[str, Any] | None:
    match = re.search(
        r"(?:always\s+)?outputs?\s+(?P<value>zero|one|0|1)\b",
        summary,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "node": "constant_relation",
        "target": ref_for_name("out", manifest_fields, prefer_field=False),
        "value": literal_from_text(match.group("value")),
    }


def parse_operation_relation(summary: str) -> dict[str, Any] | None:
    text = summary.lower()
    if "not gate" in text:
        return {
            "node": "operation_relation",
            "operation": "not_gate",
            "operands": [],
            "text": summary,
        }
    sha_match = re.search(r"\bsha-?(224|256|384|512)\b", text)
    if sha_match:
        return {
            "node": "operation_relation",
            "operation": f"sha{sha_match.group(1)}",
            "operands": [],
            "text": summary,
        }
    return None


def parse_latency_rule(summary: str) -> dict[str, Any] | None:
    match = re.search(
        r"(?P<response>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+signal)?\s+must\s+pulse\s+exactly\s+"
        r"(?P<count>\d+|one|two|three|four|five)\s+cycles?\s+after\s+"
        r"(?P<trigger>[^.;]+)",
        summary,
        re.IGNORECASE,
    )
    if not match:
        return None
    cycles = word_or_int(match.group("count"))
    if cycles is None:
        return None
    return {
        "node": "latency_rule",
        "trigger": text_expr(match.group("trigger").strip()),
        "response": {
            "node": "rose",
            "signal": signal_ref(match.group("response")),
        },
        "delay": {
            "node": "delay_range",
            "min": cycles,
            "max": cycles,
        },
    }


def parse_handshake_rule(summary: str) -> dict[str, Any] | None:
    lowered = summary.lower()
    if "valid" not in lowered or "ready" not in lowered:
        return None
    valid_name = first_signal_like(summary, "valid")
    ready_name = first_signal_like(summary, "ready")
    if not valid_name or not ready_name:
        return None
    return {
        "node": "handshake_rule",
        "valid": signal_ref(valid_name),
        "ready": signal_ref(ready_name),
        "payload": [
            signal_ref(name)
            for name in sorted(set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*_payload)\b", summary)))
        ],
        "transfer": {
            "node": "binary_op",
            "op": "logical_and",
            "left": signal_ref(valid_name),
            "right": signal_ref(ready_name),
        },
    }


def parse_clock_reset_context(summary: str) -> dict[str, Any] | None:
    clock_match = re.search(
        r"\b(?P<edge>posedge|negedge)\s+(?P<clock>[A-Za-z_][A-Za-z0-9_]*)\b",
        summary,
        re.IGNORECASE,
    )
    context: dict[str, Any] = {"node": "clock_reset_context"}
    if clock_match:
        context["clock"] = {
            "node": "clock_event",
            "edge": clock_match.group("edge").lower(),
            "signal": signal_ref(clock_match.group("clock")),
        }
    reset_match = re.search(
        r"\b(?P<polarity>active[-_\s]low|active[-_\s]high)?\s*"
        r"(?P<reset>rst_n|reset_n|rst|reset)\b",
        summary,
        re.IGNORECASE,
    )
    if reset_match:
        reset_name = reset_match.group("reset")
        context["reset"] = signal_ref(reset_name)
        polarity_text = (reset_match.group("polarity") or "").lower().replace("-", "_").replace(" ", "_")
        if polarity_text in {"active_low", "active_high"}:
            context["reset_polarity"] = polarity_text
        elif reset_name.endswith("_n"):
            context["reset_polarity"] = "active_low"
        else:
            context["reset_polarity"] = "unknown"
        context["reset_synchrony"] = "unknown"
    return context if len(context) > 1 else None


def parse_transition_outputs(outputs: str, manifest_fields: set[str]) -> list[dict[str, Any]]:
    parsed = []
    for item in re.split(r"[,;]", outputs):
        text = item.strip()
        if not text:
            continue
        match = re.match(r"(?P<target>\w+)\s*=\s*(?P<value>.+)", text)
        if match:
            parsed.append(
                {
                    "node": "assignment",
                    "target": ref_for_name(match.group("target"), manifest_fields, prefer_field=False),
                    "value": literal_from_text(match.group("value")),
                }
            )
    return parsed


def first_signal_like(text: str, keyword: str) -> str | None:
    exact = re.search(rf"\b([A-Za-z_][A-Za-z0-9_]*{keyword}[A-Za-z0-9_]*)\b", text, re.IGNORECASE)
    if exact:
        return exact.group(1)
    bare = re.search(rf"\b{keyword}\b", text, re.IGNORECASE)
    return bare.group(0) if bare else None


def word_or_int(text: str) -> int | None:
    lowered = text.lower()
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
    }
    if lowered in words:
        return words[lowered]
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


def ref_for_name(name: str, manifest_fields: set[str], *, prefer_field: bool = True) -> dict[str, Any]:
    if prefer_field and name in manifest_fields:
        return field_ref(name)
    return signal_ref(name)


def field_ref(name: str) -> dict[str, Any]:
    return {"node": "field_ref", "name": name}


def signal_ref(name: str) -> dict[str, Any]:
    return {"node": "signal_ref", "name": name}


def state_ref(name: str) -> dict[str, Any]:
    return {"node": "state_ref", "name": name}


def text_expr(text: str) -> dict[str, Any]:
    return {"node": "text_expr", "text": text}


def literal_from_text(text: str) -> dict[str, Any]:
    value_text = text.strip().strip(".;")
    lowered = value_text.lower()
    if lowered == "zero":
        return {"node": "literal", "value": 0}
    if lowered == "one":
        return {"node": "literal", "value": 1}
    if re.fullmatch(r"\d+", value_text):
        return {"node": "literal", "value": int(value_text)}
    if re.fullmatch(r"'?[bB][01xzXZ_]+", value_text):
        return {"node": "literal", "value": value_text, "base": "binary"}
    if re.fullmatch(r"'?[hH][0-9a-fA-FxzXZ_]+", value_text):
        return {"node": "literal", "value": value_text, "base": "hex"}
    return {"node": "literal", "value": value_text}


def string_subjects(claim: dict[str, Any]) -> list[str]:
    subjects = claim.get("subjects", [])
    if not isinstance(subjects, list):
        return []
    return [subject for subject in subjects if isinstance(subject, str)]


def representation_requires_human_review(representation: Any) -> bool:
    if not isinstance(representation, dict):
        return True
    ast = representation.get("ast")
    if root_ast_requires_human_review(ast):
        return True
    return ast_node_requires_human_review(ast)


def root_ast_requires_human_review(expr: Any) -> bool:
    if not isinstance(expr, dict):
        return True
    node = expr.get("node")
    if node in STRUCTURED_TEXT_FALLBACK_ROOTS and root_uses_text_fallback(expr):
        return True
    if node == "temporal_rule" and not temporal_rule_has_clock(expr):
        return True
    if node == "protocol_rule" and protocol_rule_requires_clock(expr):
        return True
    return False


def root_uses_text_fallback(expr: dict[str, Any]) -> bool:
    node = expr.get("node")
    if node == "constraint":
        return isinstance(expr.get("expr"), dict) and expr["expr"].get("node") == "text_expr"
    if node in {"protocol_rule", "temporal_rule"}:
        property_expr = expr.get("property")
        return not isinstance(property_expr, dict) or property_expr.get("node") == "text_expr"
    return False


def temporal_rule_has_clock(expr: dict[str, Any]) -> bool:
    clock = expr.get("clock")
    if isinstance(clock, dict) and clock.get("node") == "clock_event":
        return True
    context = expr.get("context")
    if isinstance(context, dict):
        context_clock = context.get("clock")
        return isinstance(context_clock, dict) and context_clock.get("node") == "clock_event"
    return False


def protocol_rule_requires_clock(expr: dict[str, Any]) -> bool:
    property_expr = expr.get("property")
    if isinstance(property_expr, dict) and property_expr.get("node") == "handshake_rule":
        return not rule_has_context_clock(expr)
    return False


def rule_has_context_clock(expr: dict[str, Any]) -> bool:
    context = expr.get("context")
    if not isinstance(context, dict):
        return False
    context_clock = context.get("clock")
    return isinstance(context_clock, dict) and context_clock.get("node") == "clock_event"


def ast_node_requires_human_review(expr: Any) -> bool:
    if isinstance(expr, dict):
        node = expr.get("node")
        if node == "semantic_claim":
            return True
        if node == "latency_rule" and latency_rule_uses_text_endpoint(expr):
            return True
        if node == "signal_ref" and expr.get("name") in PLACEHOLDER_SIGNAL_NAMES:
            return True
        if node == "text_expr" and expr.get("text") in PLACEHOLDER_TEXT_EXPR_VALUES:
            return True
        if node == "clock_reset_context" and (
            expr.get("reset_polarity") == "unknown"
            or expr.get("reset_synchrony") == "unknown"
        ):
            return True
        return any(
            ast_node_requires_human_review(value)
            for key, value in expr.items()
            if key != "node"
        )
    if isinstance(expr, list):
        return any(ast_node_requires_human_review(value) for value in expr)
    return False


def latency_rule_uses_text_endpoint(expr: dict[str, Any]) -> bool:
    return contains_ast_node(expr.get("trigger"), "text_expr") or contains_ast_node(expr.get("response"), "text_expr")


def contains_ast_node(expr: Any, node: str) -> bool:
    if isinstance(expr, dict):
        if expr.get("node") == node:
            return True
        return any(contains_ast_node(value, node) for value in expr.values())
    if isinstance(expr, list):
        return any(contains_ast_node(value, node) for value in expr)
    return False


def iter_ast_field_refs(expr: Any, path: str = ""):
    if isinstance(expr, dict):
        if expr.get("node") == "field_ref" and isinstance(expr.get("name"), str):
            yield f"{path}.name", expr["name"]
        for key, value in expr.items():
            if key == "name" and expr.get("node") == "field_ref":
                continue
            yield from iter_ast_field_refs(value, f"{path}.{key}")
    elif isinstance(expr, list):
        for index, value in enumerate(expr):
            yield from iter_ast_field_refs(value, f"{path}[{index}]")

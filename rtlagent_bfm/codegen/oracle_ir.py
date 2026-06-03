"""First-pass OracleIR generation and validation.

The OracleIR is a narrow, JSON-serializable DSL for reference-model and
scoreboard generation.  It is intentionally separate from the BFM DesignIR:
DesignIR binds signals and registers, while OracleIR describes prediction and
comparison semantics inferred from a target spec.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


ORACLE_IR_SCHEMA_VERSION = 1

ALLOWED_INPUT_TYPES = {"any", "bytes", "enum", "hex_bytes", "int", "string"}
ALLOWED_COMPARE_KINDS = {"exact", "masked_hex", "numeric_tolerance", "prefix"}
ALLOWED_NORMALIZERS = {"lower_hex", "strip_0x", "upper_hex"}
ALLOWED_CALLS = {
    "binascii.crc32",
    "hashlib.sha1",
    "hashlib.sha224",
    "hashlib.sha256",
    "hashlib.sha384",
    "hashlib.sha512",
    "zlib.crc32",
}
ALLOWED_CALL_FORMATS = {"digest", "hexdigest", "hex", "int", "str"}
SHA_ALGORITHMS = ("sha1", "sha224", "sha256", "sha384", "sha512")
HEX_FIELD_PRIORITY = ("message", "msg", "data", "payload", "input", "block")


class OracleIRValidationError(ValueError):
    """Raised when an OracleIR document fails validation."""


@dataclass(frozen=True)
class ManifestField:
    name: str
    kind: str
    choices: tuple[Any, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    hex_len: int | None = None
    hex_len_by: dict[str, dict[str, int]] | None = None


@dataclass(frozen=True)
class ManifestSummary:
    target: str
    path: Path
    fields: tuple[ManifestField, ...]


@dataclass(frozen=True)
class OracleIRIssue:
    path: str
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


def generate_oracle_ir(
    *,
    manifest_path: str | Path,
    spec_paths: Iterable[str | Path] = (),
    target: str | None = None,
) -> dict[str, Any]:
    """Generate a conservative first-pass OracleIR from manifest and specs.

    This is deliberately rule-based.  LLM extraction can later target the same
    schema, while this generator provides a deterministic baseline and skeleton.
    """

    manifest = load_manifest_summary(manifest_path)
    if target is not None and manifest.target != target:
        raise OracleIRValidationError(
            f"manifest target {manifest.target!r} does not match requested target {target!r}"
        )
    specs = load_spec_documents(spec_paths)
    inputs = [input_from_manifest_field(field) for field in manifest.fields]
    evidence = collect_keyword_evidence(specs, SHA_ALGORITHMS + ("crc32", "checksum"))
    rules = infer_first_pass_rules(manifest, specs, evidence)
    unsupported = []
    if not rules:
        unsupported.append(
            {
                "reason": "No safe first-pass oracle rule inferred from manifest/spec text",
                "requires": "manual OracleIR rule authoring or LLM semantic extraction",
            }
        )

    ir: dict[str, Any] = {
        "schema_version": ORACLE_IR_SCHEMA_VERSION,
        "target": manifest.target,
        "inputs": inputs,
        "rules": rules,
        "compare": default_compare_policy(rules),
        "evidence": evidence,
        "assumptions": [],
        "unsupported": unsupported,
        "metadata": {
            "source": "rule_based_oracle_ir_generator",
            "manifest": str(manifest.path),
            "specs": [str(path) for path, _text in specs],
        },
    }
    validate_oracle_ir(ir, manifest_path=manifest.path)
    return ir


def load_oracle_ir(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_oracle_ir(path: str | Path, ir: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ir, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_oracle_ir(
    ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    target: str | None = None,
    require_rules: bool = False,
) -> None:
    issues = collect_oracle_ir_issues(
        ir,
        manifest_path=manifest_path,
        target=target,
        require_rules=require_rules,
    )
    if issues:
        formatted = "\n".join(issue.format() for issue in issues)
        raise OracleIRValidationError(f"OracleIR validation failed:\n{formatted}")


def collect_oracle_ir_issues(
    ir: Any,
    *,
    manifest_path: str | Path | None = None,
    target: str | None = None,
    require_rules: bool = False,
) -> list[OracleIRIssue]:
    issues: list[OracleIRIssue] = []
    if not isinstance(ir, dict):
        return [OracleIRIssue("$", "OracleIR must be a JSON object")]

    manifest: ManifestSummary | None = None
    if manifest_path is not None:
        try:
            manifest = load_manifest_summary(manifest_path)
        except Exception as exc:  # noqa: BLE001 - surface manifest parsing context
            issues.append(OracleIRIssue("manifest", str(exc)))

    schema_version = ir.get("schema_version")
    if schema_version != ORACLE_IR_SCHEMA_VERSION:
        issues.append(
            OracleIRIssue(
                "schema_version",
                f"must be {ORACLE_IR_SCHEMA_VERSION}, got {schema_version!r}",
            )
        )

    ir_target = ir.get("target")
    if not isinstance(ir_target, str) or not ir_target:
        issues.append(OracleIRIssue("target", "must be a non-empty string"))
    if target is not None and ir_target != target:
        issues.append(OracleIRIssue("target", f"expected {target!r}, got {ir_target!r}"))
    if manifest is not None and ir_target != manifest.target:
        issues.append(
            OracleIRIssue(
                "target",
                f"does not match manifest target {manifest.target!r}",
            )
        )

    manifest_fields = {field.name: field for field in manifest.fields} if manifest else {}
    input_names = validate_inputs(ir.get("inputs"), manifest_fields, issues)
    evidence_ids = validate_evidence(ir.get("evidence", []), issues)
    validate_rules(
        ir.get("rules"),
        input_names=input_names,
        manifest_fields=manifest_fields,
        evidence_ids=evidence_ids,
        issues=issues,
        require_rules=require_rules,
    )
    validate_compare_policy(ir.get("compare"), issues)
    validate_string_list(ir.get("assumptions", []), "assumptions", issues)

    unsupported = ir.get("unsupported", [])
    if unsupported is not None and not isinstance(unsupported, list):
        issues.append(OracleIRIssue("unsupported", "must be a list"))
    if not ir.get("rules") and not unsupported and not require_rules:
        issues.append(
            OracleIRIssue(
                "unsupported",
                "must explain why no prediction rules were generated",
            )
        )

    return issues


def load_manifest_summary(path: str | Path) -> ManifestSummary:
    manifest_path = Path(path)
    if tomllib is None:
        raise RuntimeError("tomllib/tomli is required to load target manifests")
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest does not exist: {manifest_path}")
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    target = data.get("name")
    if not isinstance(target, str) or not target:
        raise ValueError(f"{manifest_path}: manifest must define name")
    raw_fields = data.get("field", ())
    if not isinstance(raw_fields, list | tuple):
        raise ValueError(f"{manifest_path}: [[field]] entries must be a list")
    fields = tuple(manifest_field_from_dict(item) for item in raw_fields)
    return ManifestSummary(target=target, path=manifest_path, fields=fields)


def manifest_field_from_dict(data: dict[str, Any]) -> ManifestField:
    if not isinstance(data, dict):
        raise ValueError("manifest field entry must be a table")
    choices = data.get("choices", ())
    if choices is None:
        normalized_choices = ()
    elif isinstance(choices, list | tuple):
        normalized_choices = tuple(choices)
    else:
        normalized_choices = (choices,)
    hex_len_by = data.get("hex_len_by")
    normalized_hex_len_by = None
    if isinstance(hex_len_by, dict):
        normalized_hex_len_by = {
            str(selector): {str(key): int(value) for key, value in selector_map.items()}
            for selector, selector_map in hex_len_by.items()
            if isinstance(selector_map, dict)
        }
    return ManifestField(
        name=str(data["name"]),
        kind=str(data.get("kind", "any")),
        choices=normalized_choices,
        minimum=optional_int(data.get("min")),
        maximum=optional_int(data.get("max")),
        hex_len=optional_int(data.get("hex_len")),
        hex_len_by=normalized_hex_len_by,
    )


def load_spec_documents(spec_paths: Iterable[str | Path]) -> tuple[tuple[Path, str], ...]:
    documents = []
    for raw_path in spec_paths:
        path = Path(raw_path)
        documents.append((path, path.read_text(encoding="utf-8", errors="replace")))
    return tuple(documents)


def input_from_manifest_field(field: ManifestField) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": field.name,
        "type": input_type_for_manifest_kind(field.kind),
    }
    if field.choices:
        data["choices"] = list(field.choices)
    if field.minimum is not None:
        data["min"] = field.minimum
    if field.maximum is not None:
        data["max"] = field.maximum
    if field.hex_len is not None:
        data["hex_len"] = field.hex_len
    if field.hex_len_by:
        data["hex_len_by"] = field.hex_len_by
    return data


def input_type_for_manifest_kind(kind: str) -> str:
    if kind == "hex":
        return "hex_bytes"
    if kind in {"enum", "int"}:
        return kind
    if kind == "any":
        return "any"
    return "string"


def collect_keyword_evidence(
    specs: tuple[tuple[Path, str], ...],
    keywords: Iterable[str],
) -> list[dict[str, Any]]:
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    evidence: list[dict[str, Any]] = []
    for path, text in specs:
        for line_no, line in enumerate(text.splitlines(), start=1):
            normalized = normalize_algorithm_text(line)
            if not any(keyword in normalized for keyword in lowered_keywords):
                continue
            evidence.append(
                {
                    "id": f"ev{len(evidence) + 1}",
                    "source": str(path),
                    "line": line_no,
                    "quote": line.strip()[:240],
                }
            )
            if len(evidence) >= 12:
                return evidence
    return evidence


def infer_first_pass_rules(
    manifest: ManifestSummary,
    specs: tuple[tuple[Path, str], ...],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    text = normalized_context_text(manifest, specs)
    rules = infer_hashlib_rules(manifest, text, evidence)
    if rules:
        return rules
    return []


def infer_hashlib_rules(
    manifest: ManifestSummary,
    normalized_text: str,
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    message_field = select_hex_message_field(manifest.fields)
    if message_field is None:
        return []

    mode_field = select_algorithm_mode_field(manifest.fields)
    evidence_ids = [str(item["id"]) for item in evidence if "id" in item]
    rules: list[dict[str, Any]] = []
    if mode_field is not None:
        choices = {str(choice).lower() for choice in mode_field.choices}
        for algorithm in SHA_ALGORITHMS:
            if algorithm not in choices and not contains_algorithm(normalized_text, algorithm):
                continue
            rules.append(
                hashlib_rule(
                    algorithm=algorithm,
                    message_field=message_field.name,
                    mode_field=mode_field.name,
                    evidence_ids=evidence_ids,
                )
            )
        return rules

    for algorithm in SHA_ALGORITHMS:
        if contains_algorithm(normalized_text, algorithm):
            rules.append(
                hashlib_rule(
                    algorithm=algorithm,
                    message_field=message_field.name,
                    mode_field=None,
                    evidence_ids=evidence_ids,
                )
            )
            break
    return rules


def hashlib_rule(
    *,
    algorithm: str,
    message_field: str,
    mode_field: str | None,
    evidence_ids: list[str],
) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "name": f"{algorithm}_{message_field}",
        "expected": {
            "call": f"hashlib.{algorithm}",
            "args": [{"bytes_from_hex": {"field": message_field}}],
            "format": "hexdigest",
        },
    }
    if mode_field is not None:
        rule["when"] = {"eq": [{"field": mode_field}, algorithm]}
    if evidence_ids:
        rule["evidence"] = evidence_ids
    return rule


def default_compare_policy(rules: list[dict[str, Any]]) -> dict[str, Any]:
    normalizers = ["lower_hex"] if rules else []
    return {
        "kind": "exact",
        "normalize": normalizers,
    }


def select_hex_message_field(fields: tuple[ManifestField, ...]) -> ManifestField | None:
    hex_fields = [field for field in fields if field.kind == "hex"]
    if not hex_fields:
        return None
    by_name = {field.name.lower(): field for field in hex_fields}
    for preferred in HEX_FIELD_PRIORITY:
        if preferred in by_name:
            return by_name[preferred]
    return hex_fields[0]


def select_algorithm_mode_field(fields: tuple[ManifestField, ...]) -> ManifestField | None:
    for field in fields:
        choices = {str(choice).lower() for choice in field.choices}
        if choices.intersection(SHA_ALGORITHMS):
            return field
    return None


def normalized_context_text(
    manifest: ManifestSummary,
    specs: tuple[tuple[Path, str], ...],
) -> str:
    parts = [manifest.target]
    for field in manifest.fields:
        parts.append(field.name)
        parts.extend(str(choice) for choice in field.choices)
    parts.extend(text for _path, text in specs)
    return normalize_algorithm_text("\n".join(parts))


def normalize_algorithm_text(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "")


def contains_algorithm(normalized_text: str, algorithm: str) -> bool:
    return algorithm.replace("_", "").replace("-", "") in normalized_text


def validate_inputs(
    value: Any,
    manifest_fields: dict[str, ManifestField],
    issues: list[OracleIRIssue],
) -> set[str]:
    names: set[str] = set()
    if not isinstance(value, list) or not value:
        issues.append(OracleIRIssue("inputs", "must be a non-empty list"))
        return names
    for index, item in enumerate(value):
        path = f"inputs[{index}]"
        if not isinstance(item, dict):
            issues.append(OracleIRIssue(path, "must be an object"))
            continue
        name = item.get("name")
        input_type = item.get("type")
        if not isinstance(name, str) or not name:
            issues.append(OracleIRIssue(f"{path}.name", "must be a non-empty string"))
            continue
        if name in names:
            issues.append(OracleIRIssue(f"{path}.name", f"duplicate input {name!r}"))
        names.add(name)
        if manifest_fields and name not in manifest_fields:
            issues.append(OracleIRIssue(f"{path}.name", f"not present in manifest fields"))
        if input_type not in ALLOWED_INPUT_TYPES:
            issues.append(
                OracleIRIssue(
                    f"{path}.type",
                    f"must be one of {sorted(ALLOWED_INPUT_TYPES)}, got {input_type!r}",
                )
            )
        validate_input_matches_manifest(item, manifest_fields.get(name), path, issues)
    return names


def validate_input_matches_manifest(
    item: dict[str, Any],
    field: ManifestField | None,
    path: str,
    issues: list[OracleIRIssue],
) -> None:
    if field is None:
        return
    expected_type = input_type_for_manifest_kind(field.kind)
    actual_type = item.get("type")
    if actual_type != expected_type:
        issues.append(
            OracleIRIssue(
                f"{path}.type",
                f"does not match manifest field kind {field.kind!r}; expected {expected_type!r}",
            )
        )


def validate_rules(
    value: Any,
    *,
    input_names: set[str],
    manifest_fields: dict[str, ManifestField],
    evidence_ids: set[str],
    issues: list[OracleIRIssue],
    require_rules: bool,
) -> None:
    if value is None:
        value = []
    if not isinstance(value, list):
        issues.append(OracleIRIssue("rules", "must be a list"))
        return
    if require_rules and not value:
        issues.append(OracleIRIssue("rules", "must contain at least one prediction rule"))
        return
    for index, rule in enumerate(value):
        path = f"rules[{index}]"
        if not isinstance(rule, dict):
            issues.append(OracleIRIssue(path, "must be an object"))
            continue
        if "name" in rule and not isinstance(rule["name"], str):
            issues.append(OracleIRIssue(f"{path}.name", "must be a string"))
        if "expected" not in rule:
            issues.append(OracleIRIssue(f"{path}.expected", "is required"))
        else:
            validate_expr(
                rule["expected"],
                f"{path}.expected",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
        if "when" in rule:
            validate_condition_expr(
                rule["when"],
                f"{path}.when",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
        if "evidence" in rule:
            validate_string_list(rule["evidence"], f"{path}.evidence", issues)
            for evidence_index, evidence_id in enumerate(rule["evidence"]):
                if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
                    issues.append(
                        OracleIRIssue(
                            f"{path}.evidence[{evidence_index}]",
                            f"unknown evidence id {evidence_id!r}",
                        )
                    )


def validate_expr(
    expr: Any,
    path: str,
    *,
    input_names: set[str],
    manifest_fields: dict[str, ManifestField],
    issues: list[OracleIRIssue],
) -> None:
    if isinstance(expr, str | int | float | bool) or expr is None:
        return
    if isinstance(expr, list):
        for index, item in enumerate(expr):
            validate_expr(
                item,
                f"{path}[{index}]",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
        return
    if not isinstance(expr, dict):
        issues.append(OracleIRIssue(path, "expression must be a scalar, list, or object"))
        return

    if "literal" in expr:
        return
    if "field" in expr:
        field_name = expr["field"]
        if not isinstance(field_name, str) or not field_name:
            issues.append(OracleIRIssue(f"{path}.field", "must be a non-empty string"))
        elif field_name not in input_names and (
            not manifest_fields or field_name not in manifest_fields
        ):
            issues.append(OracleIRIssue(f"{path}.field", f"unknown field {field_name!r}"))
        return
    if "bytes_from_hex" in expr:
        validate_expr(
            expr["bytes_from_hex"],
            f"{path}.bytes_from_hex",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    if "call" in expr:
        validate_call_expr(
            expr,
            path,
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    if "concat" in expr:
        validate_expr(
            expr["concat"],
            f"{path}.concat",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    if "slice" in expr:
        validate_slice_expr(
            expr["slice"],
            f"{path}.slice",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    if "lower_hex" in expr:
        validate_expr(
            expr["lower_hex"],
            f"{path}.lower_hex",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    if any(key in expr for key in ("and", "eq", "not", "or")):
        validate_condition_expr(
            expr,
            path,
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    issues.append(OracleIRIssue(path, f"unknown expression operator(s): {sorted(expr)}"))


def validate_call_expr(
    expr: dict[str, Any],
    path: str,
    *,
    input_names: set[str],
    manifest_fields: dict[str, ManifestField],
    issues: list[OracleIRIssue],
) -> None:
    function = expr.get("call")
    if function not in ALLOWED_CALLS:
        issues.append(
            OracleIRIssue(
                f"{path}.call",
                f"must be one of {sorted(ALLOWED_CALLS)}, got {function!r}",
            )
        )
    args = expr.get("args", [])
    if not isinstance(args, list):
        issues.append(OracleIRIssue(f"{path}.args", "must be a list"))
    else:
        for index, arg in enumerate(args):
            validate_expr(
                arg,
                f"{path}.args[{index}]",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
    result_format = expr.get("format")
    if result_format is not None and result_format not in ALLOWED_CALL_FORMATS:
        issues.append(
            OracleIRIssue(
                f"{path}.format",
                f"must be one of {sorted(ALLOWED_CALL_FORMATS)}, got {result_format!r}",
            )
        )


def validate_slice_expr(
    value: Any,
    path: str,
    *,
    input_names: set[str],
    manifest_fields: dict[str, ManifestField],
    issues: list[OracleIRIssue],
) -> None:
    if not isinstance(value, dict):
        issues.append(OracleIRIssue(path, "must be an object"))
        return
    if "value" not in value:
        issues.append(OracleIRIssue(f"{path}.value", "is required"))
    else:
        validate_expr(
            value["value"],
            f"{path}.value",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
    for key in ("start", "end"):
        if key in value and not isinstance(value[key], int):
            issues.append(OracleIRIssue(f"{path}.{key}", "must be an integer"))


def validate_condition_expr(
    expr: Any,
    path: str,
    *,
    input_names: set[str],
    manifest_fields: dict[str, ManifestField],
    issues: list[OracleIRIssue],
) -> None:
    if isinstance(expr, bool):
        return
    if not isinstance(expr, dict):
        issues.append(OracleIRIssue(path, "condition must be a boolean or object"))
        return
    if "eq" in expr:
        pair = expr["eq"]
        if not isinstance(pair, list) or len(pair) != 2:
            issues.append(OracleIRIssue(f"{path}.eq", "must be a two-item list"))
            return
        for index, item in enumerate(pair):
            validate_expr(
                item,
                f"{path}.eq[{index}]",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
        return
    if "and" in expr or "or" in expr:
        op = "and" if "and" in expr else "or"
        values = expr[op]
        if not isinstance(values, list) or not values:
            issues.append(OracleIRIssue(f"{path}.{op}", "must be a non-empty list"))
            return
        for index, item in enumerate(values):
            validate_condition_expr(
                item,
                f"{path}.{op}[{index}]",
                input_names=input_names,
                manifest_fields=manifest_fields,
                issues=issues,
            )
        return
    if "not" in expr:
        validate_condition_expr(
            expr["not"],
            f"{path}.not",
            input_names=input_names,
            manifest_fields=manifest_fields,
            issues=issues,
        )
        return
    issues.append(OracleIRIssue(path, f"unknown condition operator(s): {sorted(expr)}"))


def validate_compare_policy(value: Any, issues: list[OracleIRIssue]) -> None:
    if not isinstance(value, dict):
        issues.append(OracleIRIssue("compare", "must be an object"))
        return
    kind = value.get("kind")
    if kind not in ALLOWED_COMPARE_KINDS:
        issues.append(
            OracleIRIssue(
                "compare.kind",
                f"must be one of {sorted(ALLOWED_COMPARE_KINDS)}, got {kind!r}",
            )
        )
    normalizers = value.get("normalize", [])
    if normalizers is None:
        normalizers = []
    if not isinstance(normalizers, list):
        issues.append(OracleIRIssue("compare.normalize", "must be a list"))
    else:
        for index, normalizer in enumerate(normalizers):
            if normalizer not in ALLOWED_NORMALIZERS:
                issues.append(
                    OracleIRIssue(
                        f"compare.normalize[{index}]",
                        f"must be one of {sorted(ALLOWED_NORMALIZERS)}, got {normalizer!r}",
                    )
                )
    if kind == "masked_hex" and "mask" not in value:
        issues.append(OracleIRIssue("compare.mask", "is required for masked_hex compare"))
    if kind == "prefix" and "length" not in value:
        issues.append(OracleIRIssue("compare.length", "is required for prefix compare"))


def validate_evidence(value: Any, issues: list[OracleIRIssue]) -> set[str]:
    seen_ids: set[str] = set()
    if value is None:
        return seen_ids
    if not isinstance(value, list):
        issues.append(OracleIRIssue("evidence", "must be a list"))
        return seen_ids
    for index, item in enumerate(value):
        path = f"evidence[{index}]"
        if not isinstance(item, dict):
            issues.append(OracleIRIssue(path, "must be an object"))
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not evidence_id:
            issues.append(OracleIRIssue(f"{path}.id", "must be a non-empty string"))
        elif evidence_id in seen_ids:
            issues.append(OracleIRIssue(f"{path}.id", f"duplicate evidence id {evidence_id!r}"))
        else:
            seen_ids.add(evidence_id)
        if "source" in item and not isinstance(item["source"], str):
            issues.append(OracleIRIssue(f"{path}.source", "must be a string"))
        if "line" in item and not isinstance(item["line"], int):
            issues.append(OracleIRIssue(f"{path}.line", "must be an integer"))
        if "quote" in item and not isinstance(item["quote"], str):
            issues.append(OracleIRIssue(f"{path}.quote", "must be a string"))
    return seen_ids


def validate_string_list(
    value: Any,
    path: str,
    issues: list[OracleIRIssue],
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        issues.append(OracleIRIssue(path, "must be a list"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(OracleIRIssue(f"{path}[{index}]", "must be a string"))


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)

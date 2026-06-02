from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any

from fuzz_bfm.target_config import FieldSpec, TargetConfig, load_target_config


RTL_GAP_PLANNER_SOURCE = "rtl_gap_planner"
RTL_GAP_HEURISTIC_SOURCE = "rtl_gap_heuristic"
LENGTH_HINTS = {"len", "length", "size", "pad", "padding", "block", "next"}
ADDRESS_HINTS = {"address", "addr", "read", "write", "we", "cs", "case"}
COMPLEX_HINTS = {"state", "round", "reset", "error"}
HEX_PATTERNS = ["zero", "ff", "increment", "alternating", "walking_one"]
REPRESENTATIVE_LENGTHS = [0, 1, 15, 16, 31, 32, 55, 56, 64]
MAX_HEURISTIC_GAPS = 12
MAX_EXPLICIT_CASES_PER_FIELD = 8
MAX_COMPLEX_GAPS = 8


def plan_mutations_from_rtl_gaps(summary: dict[str, Any]) -> dict[str, Any]:
    target = str(summary["target"])
    try:
        config = load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return {
            "source": RTL_GAP_PLANNER_SOURCE,
            "target": target,
            "heuristic_directives": [],
            "complex_gaps": compact_complex_gaps(rtl_gaps(summary)),
            "directives": [],
            "blocked": [{"reason": "target config unavailable"}],
        }

    directives: list[dict[str, Any]] = []
    complex_gaps: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for gap in rtl_gaps(summary)[:MAX_HEURISTIC_GAPS]:
        directive = heuristic_directive_for_gap(target, config, gap)
        if directive is not None:
            directives.append(directive)
            continue
        if is_complex_gap(gap):
            complex_gaps.append(compact_gap(gap))
        else:
            blocked.append(
                {
                    "gap_ids": [str(gap.get("id", ""))],
                    "reason": "no clear manifest field mapping",
                }
            )

    directives = merge_structural_directives(target, directives)
    return {
        "source": RTL_GAP_PLANNER_SOURCE,
        "target": target,
        "heuristic_directives": directives,
        "complex_gaps": complex_gaps[:MAX_COMPLEX_GAPS],
        "directives": directives,
        "blocked": blocked,
    }


def heuristic_directive_for_gap(
    target: str,
    config: TargetConfig,
    gap: dict[str, Any],
) -> dict[str, Any] | None:
    text = gap_text(gap)
    hints = hint_values(gap)
    matched_fields = [field for field in config.fields if field_matches_gap(field, text)]

    updates: dict[str, Any] = {}
    cases: list[dict[str, Any]] = []
    reasons: list[str] = []

    for field in matched_fields:
        if field.kind == "hex":
            updates[f"{field.name}_patterns"] = list(HEX_PATTERNS)
            reasons.append(f"matched hex field {field.name!r}")
        else:
            values = interesting_field_values(field)
            if values:
                updates[f"{field.name}_values"] = values
                reasons.append(f"matched field {field.name!r}")

    if hints.intersection(LENGTH_HINTS):
        variable_hex_fields = [field for field in matched_fields if is_variable_hex(field)]
        if not variable_hex_fields:
            all_variable_hex = [field for field in config.fields if is_variable_hex(field)]
            if len(all_variable_hex) == 1:
                variable_hex_fields = all_variable_hex
        for field in variable_hex_fields:
            cases.extend(length_bucket_cases(config, field))
            reasons.append(f"matched length-like RTL gap for variable hex field {field.name!r}")

    if hints.intersection(ADDRESS_HINTS) and not updates and not cases:
        address_fields = [
            field
            for field in config.fields
            if field.kind in {"int", "enum"} and field.name.lower() in text
        ]
        for field in address_fields:
            values = interesting_field_values(field)
            if values:
                updates[f"{field.name}_values"] = values
                reasons.append(f"matched address/read-write field {field.name!r}")

    if not updates and not cases:
        return None

    directive: dict[str, Any] = {
        "target": target,
        "name": f"rtl_gap_{safe_name(str(gap.get('primary_kind', 'gap')))}",
        "source": RTL_GAP_HEURISTIC_SOURCE,
        "gap_ids": [str(gap.get("id", ""))],
        "reason": "; ".join(reasons) or "clear RTL structural gap matched target schema",
        "weight": 1,
    }
    directive.update(updates)
    if cases:
        directive["cases"] = dedupe_cases(cases)
    return directive


def rtl_gaps(summary: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = summary.get("rtl_gap_summary", {}).get("top_gaps", [])
    if isinstance(gaps, list):
        return [gap for gap in gaps if isinstance(gap, dict)]
    return []


def field_matches_gap(field: FieldSpec, text: str) -> bool:
    return token_present(field.name, text)


def is_complex_gap(gap: dict[str, Any]) -> bool:
    hints = hint_values(gap)
    if hints.intersection(COMPLEX_HINTS | ADDRESS_HINTS | LENGTH_HINTS):
        return True
    return str(gap.get("primary_kind", "")) in {"branch", "expression", "fsm"}


def hint_values(gap: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for hint in gap.get("advisor_hints", []):
        if isinstance(hint, dict):
            value = hint.get("value")
            if value is not None:
                values.add(str(value).lower())
    return values


def gap_text(gap: dict[str, Any]) -> str:
    parts = [
        str(gap.get("module", "")),
        str(gap.get("code", "")),
        " ".join(str(item) for item in gap.get("objects", [])),
    ]
    for item in gap.get("context", []):
        if isinstance(item, dict):
            parts.append(str(item.get("code", "")))
    for hint in gap.get("advisor_hints", []):
        if isinstance(hint, dict):
            parts.append(str(hint.get("value", "")))
    return "\n".join(parts).lower()


def token_present(token: str, text: str) -> bool:
    pattern = r"(?<![A-Za-z0-9_$])" + re.escape(token.lower()) + r"(?![A-Za-z0-9_$])"
    return re.search(pattern, text) is not None


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip().lower()).strip("_")
    return text or "gap"


def is_variable_hex(field: FieldSpec) -> bool:
    return field.kind == "hex" and field.hex_len is None and not field.hex_len_by


def interesting_field_values(field: FieldSpec) -> list[Any]:
    if field.choices:
        return list(field.choices)
    if field.kind != "int":
        return []
    values: list[int] = []
    if field.minimum is not None:
        values.append(field.minimum)
    if field.maximum is not None:
        values.append(field.maximum)
    if (field.minimum is None or field.minimum <= 0) and (
        field.maximum is None or 0 <= field.maximum
    ):
        values.append(0)
    return sorted(set(values))


def length_bucket_cases(config: TargetConfig, field: FieldSpec) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    base = default_case_values(config, skip_field=field.name)
    for length in REPRESENTATIVE_LENGTHS[:MAX_EXPLICIT_CASES_PER_FIELD]:
        case = dict(base)
        case[field.name] = pattern_hex("increment" if length else "zero", length)
        cases.append(case)
    return cases


def default_case_values(config: TargetConfig, *, skip_field: str | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in config.fields:
        if field.name == skip_field:
            continue
        value = default_field_value(field)
        if value is not None:
            values[field.name] = value
    return values


def default_field_value(field: FieldSpec) -> Any:
    if field.choices:
        return field.choices[0]
    if field.kind == "int":
        return field.minimum if field.minimum is not None else 0
    if field.kind == "enum":
        return "default"
    if field.kind == "hex":
        length = field.hex_len
        if length is None and field.hex_len_by:
            length = max(max(choices.values()) for choices in field.hex_len_by.values())
        if length is None:
            return ""
        return pattern_hex("zero", length)
    return "default"


def pattern_hex(pattern: str, length: int) -> str:
    if pattern == "zero":
        data = bytes([0] * length)
    elif pattern == "ff":
        data = bytes([0xFF] * length)
    elif pattern == "alternating":
        data = bytes(0xAA if idx % 2 == 0 else 0x55 for idx in range(length))
    elif pattern == "walking_one":
        data = bytes(1 << (idx % 8) for idx in range(length))
    else:
        data = bytes(idx & 0xFF for idx in range(length))
    return data.hex()


def dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result = []
    for case in cases:
        key = tuple(sorted((str(name), str(value)) for name, value in case.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(case)
    return result


def merge_structural_directives(target: str, directives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not directives:
        return []

    merged: dict[str, Any] = {
        "target": target,
        "name": "rtl_gap_structural_mutation",
        "source": RTL_GAP_HEURISTIC_SOURCE,
        "reason": "Clear RTL structural gaps matched target schema.",
        "weight": 1,
        "gap_ids": [],
    }
    cases: list[dict[str, Any]] = []
    reasons = []
    for directive in directives:
        merged["gap_ids"].extend(directive.get("gap_ids", []))
        if directive.get("reason"):
            reasons.append(str(directive["reason"]))
        cases.extend(case for case in directive.get("cases", []) if isinstance(case, dict))
        for key, value in directive.items():
            if not key.endswith(("_values", "_patterns")):
                continue
            existing = list(merged.get(key, []))
            for item in value:
                if item not in existing:
                    existing.append(item)
            merged[key] = existing
    if cases:
        merged["cases"] = dedupe_cases(cases)
    if reasons:
        merged["reason"] = " ".join(reasons[:3])
    merged["gap_ids"] = sorted(set(merged["gap_ids"]))
    return [merged]


def compact_complex_gaps(gaps: list[dict[str, Any]], *, limit: int = MAX_COMPLEX_GAPS) -> list[dict[str, Any]]:
    return [compact_gap(gap) for gap in gaps[:limit]]


def compact_gap(gap: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": gap.get("id"),
        "primary_kind": gap.get("primary_kind"),
        "module": gap.get("module"),
        "file": gap.get("file"),
        "line": gap.get("line"),
        "code": gap.get("code", ""),
        "context": gap.get("context", []),
        "advisor_hints": gap.get("advisor_hints", []),
        "evidence": gap.get("evidence", {}),
    }


def build_rtl_gap_llm_prompt(
    summary: dict[str, Any],
    structural_plan: dict[str, Any],
) -> dict[str, Any]:
    target = str(summary["target"])
    return {
        "task": "Convert complex RTL coverage gaps into schema-valid mutation directives.",
        "response_contract": [
            "Return one JSON object only.",
            "The top-level object MUST contain a non-empty 'directives' array if a useful mutation exists.",
            "Each directive MUST target the requested target name.",
            "Use only fields from target_schema.",
            "Prefer explicit 'cases' when a complex RTL gap requires semantic values.",
        ],
        "target": target,
        "target_schema": target_schema(target),
        "allowed_directive_schema": allowed_directive_schema(target),
        "complex_rtl_gaps": structural_plan.get("complex_gaps", []),
        "heuristic_directives": structural_plan.get("heuristic_directives", []),
        "stimulus_summary": summary.get("stimulus_summary", {}),
        "uvm_functional_coverage": summary.get("uvm_functional_coverage", {}),
    }


def allowed_directive_schema(target: str) -> dict[str, Any]:
    try:
        config = load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return {"directives": [{"cases": []}]}
    directive: dict[str, Any] = {
        "target": target,
        "name": "short_identifier",
        "reason": "coverage gap being targeted",
        "gap_ids": ["optional rtl_gap ids"],
        "cases": [{field.name: "explicit value" for field in config.fields}],
        "weight": 1,
    }
    for field in config.fields:
        if field.kind == "hex":
            directive[f"{field.name}_patterns"] = list(HEX_PATTERNS)
        else:
            directive[f"{field.name}_values"] = ["optional values"]
    return {"directives": [directive]}


def target_schema(target: str) -> dict[str, Any]:
    try:
        config = load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return {}
    return {
        "fields": [asdict(field) for field in config.fields],
        "coverpoints": [asdict(coverpoint) for coverpoint in config.coverpoints],
        "crosses": [asdict(cross) for cross in config.crosses],
    }

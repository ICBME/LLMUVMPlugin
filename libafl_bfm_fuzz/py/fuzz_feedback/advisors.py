from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
from typing import Any

from fuzz_bfm.target_config import CoverpointSpec, FieldSpec, load_target_config


def propose_directives(summary: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    uncovered_count = int(summary.get("uncovered_line_count", 0))
    sparse_fields = sparse_field_names(summary)
    functional_updates = functional_uncovered_values(summary)
    if functional_updates:
        reason = "Functional coverage bins remain uncovered; refresh targeted schema values."
    elif sparse_fields:
        reason = f"{uncovered_count} uncovered RTL lines remain; refresh sparse schema fields."
    else:
        reason = (
            f"{uncovered_count} uncovered RTL lines remain; "
            "refresh the schema-driven seed space."
        )
    directive: dict[str, Any] = {
        "target": target,
        "name": "functional_schema_refresh" if functional_updates else "schema_refresh",
        "reason": reason,
        "weight": 1,
    }
    if sparse_fields:
        directive["focus_fields"] = sparse_fields
    directive.update(schema_refresh_values(target, sparse_fields))
    directive.update(functional_updates)
    return {"source": "generic heuristic", "directives": [directive]}


def sparse_field_names(summary: dict[str, Any]) -> list[str]:
    field_counts = summary.get("stimulus_summary", {}).get("field_counts", {})
    sparse = [
        field_name
        for field_name, counts in field_counts.items()
        if isinstance(counts, dict) and len(counts) <= 1
    ]
    return sorted(sparse)


def schema_refresh_values(target: str, focus_fields: list[str]) -> dict[str, Any]:
    try:
        config = load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return {}

    updates: dict[str, Any] = {}
    focus = set(focus_fields)
    fields = [field for field in config.fields if not focus or field.name in focus]
    for field in fields:
        values = interesting_field_values(field)
        if values:
            updates[f"{field.name}_values"] = values
        if field.kind == "hex":
            updates[f"{field.name}_patterns"] = [
                "zero",
                "ff",
                "increment",
                "alternating",
                "walking_one",
            ]
    return updates


def functional_uncovered_values(summary: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    try:
        config = load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return {}

    uncovered = summary.get("uvm_functional_coverage", {}).get("uncovered", {})
    if not isinstance(uncovered, dict):
        return {}

    updates: dict[str, Any] = {}
    field_by_name = {field.name: field for field in config.fields}
    field_uncovered = _uncovered_section(uncovered, "fields")
    for field_name, values in field_uncovered.items():
        field = field_by_name.get(field_name)
        if field is not None:
            _merge_field_updates(updates, field, values)

    coverpoint_uncovered = _uncovered_section(uncovered, "coverpoints")
    coverpoint_by_name = {coverpoint.name: coverpoint for coverpoint in config.coverpoints}
    for coverpoint_name, values in coverpoint_uncovered.items():
        coverpoint = coverpoint_by_name.get(coverpoint_name)
        if coverpoint is None:
            continue
        field = field_by_name.get(coverpoint.field)
        if field is None:
            continue
        _merge_coverpoint_updates(updates, coverpoint, field, values)
    return updates


def _uncovered_section(uncovered: dict[str, Any], section: str) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {}
    raw_section = uncovered.get(section)
    if isinstance(raw_section, dict):
        for name, missing_values in raw_section.items():
            if isinstance(missing_values, list):
                values[str(name)] = missing_values

    if section == "fields":
        for name, missing_values in uncovered.items():
            if name in {"fields", "coverpoints", "crosses"}:
                continue
            if isinstance(missing_values, list):
                values.setdefault(str(name), missing_values)
    return values


def _merge_coverpoint_updates(
    updates: dict[str, Any],
    coverpoint: CoverpointSpec,
    field: FieldSpec,
    values: list[Any],
) -> None:
    if coverpoint.patterns:
        _merge_values(updates, f"{field.name}_patterns", [str(value) for value in values])
        return
    _merge_field_updates(updates, field, values)


def _merge_field_updates(updates: dict[str, Any], field: FieldSpec, values: list[Any]) -> None:
    if field.kind == "hex":
        _merge_values(updates, f"{field.name}_patterns", [str(value) for value in values])
        return
    _merge_values(
        updates,
        f"{field.name}_values",
        [_coerce_field_value(field, value) for value in values],
    )


def _merge_values(updates: dict[str, Any], key: str, values: list[Any]) -> None:
    existing = list(updates.get(key, []))
    for value in values:
        if value not in existing:
            existing.append(value)
    if existing:
        updates[key] = existing


def _coerce_field_value(field: FieldSpec, value: Any) -> Any:
    if field.kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


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


def build_llm_prompt(summary: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    return {
        "task": (
            "Analyze Verilator RTL coverage gaps and UVM functional coverage gaps, then "
            "return mutation directives for the LibAFL corpus generator."
        ),
        "response_contract": [
            "Return one JSON object only.",
            "The top-level object MUST contain a non-empty 'directives' array.",
            "Each directive MUST target the requested target name.",
            "If the heuristic baseline is already the best option, copy it into 'directives' instead of returning an empty list.",
            "Prefer explicit 'cases' when a coverage gap requires semantic values not expressible by generic field patterns.",
        ],
        "target": summary["target"],
        "allowed_schema": allowed_schema(),
        "target_schema": target_schema(target),
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }


def write_llm_prompt(path: Path, summary: dict[str, Any], heuristic: dict[str, Any]) -> None:
    prompt = build_llm_prompt(summary, heuristic)
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n")


def allowed_schema() -> dict[str, Any]:
    return {
        "source": "llm",
        "directives": [
            {
                "target": "target_name",
                "name": "short_identifier",
                "reason": "coverage gap being targeted",
                "cases": [
                    {
                        "field_name": "explicit value matching the target manifest",
                        "hex_field": "001122",
                    }
                ],
                "field_name_values": ["optional list of values for cross-product expansion"],
                "hex_field_patterns": ["zero", "ff", "increment", "alternating", "walking_one"],
                "weight": 1,
            }
        ]
    }


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


def maybe_call_llm(prompt: dict[str, Any], model: str | None) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return call_langchain_llm(prompt, model=model, api_key=api_key, base_url=base_url)


def call_langchain_llm(
    prompt: dict[str, Any],
    *,
    model: str,
    api_key: str,
    base_url: str,
) -> dict[str, Any]:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "LangChain LLM feedback requires langchain-openai and langchain-core. "
            "Install project dependencies with `uv sync` or `uv pip install -e .`."
        ) from exc

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
        timeout=60,
        max_retries=2,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a hardware verification fuzzing assistant. "
                    "Return strict JSON that satisfies the user's response_contract."
                )
            ),
            HumanMessage(content=json.dumps(prompt, sort_keys=True)),
        ],
        config={
            "run_name": "coverage_feedback_directives",
            "tags": ["coverage-feedback", str(prompt.get("target", "unknown"))],
            "metadata": {
                "target": prompt.get("target"),
                "model": model,
                "provider": "langchain-openai",
            },
        },
    )
    raw_text = message_content_to_text(response.content)
    raw_value = json.loads(extract_json(raw_text))
    return normalize_llm_response(raw_value, target=str(prompt["target"]), model=model)


def message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                chunks.append(str(item.get("text") or item.get("content") or item))
            else:
                chunks.append(str(item))
        return "\n".join(chunks)
    return str(content)


def normalize_llm_response(value: dict[str, Any], *, target: str, model: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"LLM response JSON must be an object, got {type(value).__name__}")

    directive_value = find_directives(value)
    if directive_value is None and looks_like_directive(value):
        directive_value = [value]
    if isinstance(directive_value, dict):
        directive_value = [directive_value]

    normalized = {
        "source": f"llm:langchain:{model or 'unknown'}",
        "provider": "langchain-openai",
        "model": model,
        "raw_response": value,
        "directives": directive_value if directive_value is not None else [],
    }
    return normalized


def find_directives(value: dict[str, Any]) -> Any:
    for key in ("directives", "mutation_directives", "directive"):
        if key in value:
            return value[key]
    for key in ("result", "output", "response"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = find_directives(nested)
            if found is not None:
                return found
    return None


def looks_like_directive(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("target", "name", "reason", "cases", "weight"))


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain JSON")
    return match.group(0)


def validate_directives(target: str, value: dict[str, Any]) -> dict[str, Any]:
    directives = value.get("directives")
    if not isinstance(directives, list) or not directives:
        keys = ", ".join(sorted(str(key) for key in value.keys()))
        raise ValueError(
            "directive JSON must contain a non-empty directives list "
            f"(top-level keys: {keys or '<none>'})"
        )
    filtered = []
    for idx, directive in enumerate(directives):
        if not isinstance(directive, dict):
            continue
        directive = dict(directive)
        directive.setdefault("target", target)
        directive.setdefault("name", f"llm_directive_{idx}")
        directive.setdefault("reason", "LLM-selected coverage gap")
        if directive["target"] != target:
            continue
        filtered.append(directive)
    if not filtered:
        raise ValueError(f"no usable directives for target {target!r}")
    return {"source": value.get("source", "llm"), "directives": filtered}

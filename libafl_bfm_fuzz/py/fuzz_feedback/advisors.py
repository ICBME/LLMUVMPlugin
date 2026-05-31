from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import request

from fuzz_bfm.target_config import FieldSpec, load_target_config


def propose_directives(summary: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    uncovered_count = int(summary.get("uncovered_line_count", 0))
    sparse_fields = sparse_field_names(summary)
    reason = (
        f"{uncovered_count} uncovered RTL lines remain; refresh sparse schema fields."
        if sparse_fields
        else f"{uncovered_count} uncovered RTL lines remain; refresh the schema-driven seed space."
    )
    directive: dict[str, Any] = {
        "target": target,
        "name": "schema_refresh",
        "reason": reason,
        "weight": 1,
    }
    if sparse_fields:
        directive["focus_fields"] = sparse_fields
    directive.update(schema_refresh_values(target, sparse_fields))
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


def write_llm_prompt(path: Path, summary: dict[str, Any], heuristic: dict[str, Any]) -> None:
    prompt = {
        "task": "Analyze Verilator RTL coverage gaps and return JSON mutation directives for the LibAFL corpus generator. Return JSON only; do not emit prose.",
        "target": summary["target"],
        "allowed_schema": allowed_schema(),
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n")


def allowed_schema() -> dict[str, Any]:
    return {
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


def maybe_call_llm(prompt: dict[str, Any], model: str | None) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a hardware verification fuzzing assistant. Return strict JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode())
    return json.loads(extract_json(raw["choices"][0]["message"]["content"]))


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
        raise ValueError("directive JSON must contain a non-empty directives list")
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
        raise ValueError("no usable directives for target")
    return {"source": value.get("source", "llm"), "directives": filtered}

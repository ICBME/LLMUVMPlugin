"""Validation feedback and LLM repair loop for OracleIR documents."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from .oracle_ir import (
    ALLOWED_CALL_FORMATS,
    ALLOWED_CALLS,
    ALLOWED_COMPARE_KINDS,
    ALLOWED_INPUT_TYPES,
    ALLOWED_NORMALIZERS,
    ORACLE_IR_SCHEMA_VERSION,
    collect_oracle_ir_issues,
    load_manifest_summary,
    load_spec_documents,
)


OracleIRRepairCallable = Callable[[dict[str, Any], str | None], dict[str, Any] | None]


def build_oracle_ir_repair_prompt(
    oracle_ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    target: str | None = None,
    require_rules: bool = False,
) -> dict[str, Any]:
    """Build a strict JSON prompt for repairing an invalid OracleIR."""

    issues = collect_oracle_ir_issues(
        oracle_ir,
        manifest_path=manifest_path,
        target=target,
        require_rules=require_rules,
    )
    specs = load_spec_documents(spec_paths)
    prompt: dict[str, Any] = {
        "task": (
            "Repair the OracleIR JSON so it passes validation. Return strict JSON only."
        ),
        "workflow": "oracle_ir_validation_feedback_repair",
        "target": target or oracle_ir.get("target"),
        "current_oracle_ir": oracle_ir,
        "validation_issues": [
            {"path": issue.path, "message": issue.message}
            for issue in issues
        ],
        "constraints": [
            "Return one complete corrected OracleIR object under the top-level key oracle_ir.",
            "Do not invent manifest fields; every field reference must exist in inputs or manifest [[field]].",
            "Use only the allowed expression operations and allowlisted calls.",
            "If behavior cannot be safely expressed, keep rules empty and explain it in unsupported.",
            "Preserve useful evidence and assumptions, but remove evidence references that do not exist.",
        ],
        "oracle_ir_contract": oracle_ir_contract(),
        "response_contract": {
            "oracle_ir": "complete corrected OracleIR object",
            "assumptions": ["optional notes about repairs"],
            "changes": ["optional concise repair summary"],
        },
        "inputs": {
            "target_manifest": manifest_payload(manifest_path),
            "specs": [
                {"path": str(path), "content": text}
                for path, text in specs
            ],
        },
    }
    return prompt


def repair_oracle_ir_with_feedback(
    oracle_ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    target: str | None = None,
    require_rules: bool = False,
    llm_callable: OracleIRRepairCallable | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Validate an OracleIR and optionally ask an LLM to repair it.

    ``max_attempts`` counts LLM submissions.  A valid input returns immediately
    without invoking the LLM.
    """

    current = json_round_trip(oracle_ir)
    llm_responses: list[dict[str, Any]] = []
    prompt = build_oracle_ir_repair_prompt(
        current,
        manifest_path=manifest_path,
        spec_paths=spec_paths,
        target=target,
        require_rules=require_rules,
    )
    issues = collect_oracle_ir_issues(
        current,
        manifest_path=manifest_path,
        target=target,
        require_rules=require_rules,
    )
    if not issues:
        return repair_result("valid", current, prompt, issues, llm_responses)
    if llm_callable is None:
        return repair_result("invalid", current, prompt, issues, llm_responses)

    attempts = max(0, int(max_attempts))
    for _attempt in range(attempts):
        response = llm_callable(prompt, model)
        if response is None:
            return repair_result("llm_unavailable", current, prompt, issues, llm_responses)
        llm_responses.append(response)
        try:
            current = normalize_oracle_ir_response(response)
        except (TypeError, ValueError) as exc:
            issues = [
                *issues,
                {
                    "path": "llm_response",
                    "message": str(exc),
                },
            ]
            return repair_result("llm_invalid_response", current, prompt, issues, llm_responses)

        issues = collect_oracle_ir_issues(
            current,
            manifest_path=manifest_path,
            target=target,
            require_rules=require_rules,
        )
        prompt = build_oracle_ir_repair_prompt(
            current,
            manifest_path=manifest_path,
            spec_paths=spec_paths,
            target=target,
            require_rules=require_rules,
        )
        if not issues:
            return repair_result("repaired", current, prompt, issues, llm_responses)

    return repair_result("repair_failed", current, prompt, issues, llm_responses)


def maybe_call_oracle_ir_llm(
    prompt: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return call_langchain_oracle_ir_llm(
        prompt,
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def call_langchain_oracle_ir_llm(
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
            "OracleIR LLM repair requires langchain-openai and langchain-core. "
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
                    "You repair hardware verification OracleIR documents. "
                    "Return strict JSON satisfying the response_contract."
                )
            ),
            HumanMessage(content=json.dumps(prompt, sort_keys=True)),
        ],
        config={
            "run_name": "oracle_ir_validation_feedback_repair",
            "tags": ["oracle-ir-repair", str(prompt.get("target", "unknown"))],
            "metadata": {
                "target": prompt.get("target"),
                "model": model,
                "provider": "langchain-openai",
            },
        },
    )
    raw_text = message_content_to_text(response.content)
    raw_value = json.loads(extract_json(raw_text))
    return {
        "source": f"llm:langchain:{model}",
        "provider": "langchain-openai",
        "model": model,
        "raw_response": raw_value,
        "oracle_ir": normalize_oracle_ir_response(raw_value),
    }


def normalize_oracle_ir_response(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"LLM response JSON must be an object, got {type(value).__name__}")
    if looks_like_oracle_ir(value):
        return json_round_trip(value)
    for key in ("oracle_ir", "ir", "result", "output", "response"):
        nested = value.get(key)
        if isinstance(nested, dict):
            if looks_like_oracle_ir(nested):
                return json_round_trip(nested)
            try:
                return normalize_oracle_ir_response(nested)
            except ValueError:
                continue
    raise ValueError("LLM response did not contain an OracleIR object")


def oracle_ir_contract() -> dict[str, Any]:
    return {
        "schema_version": ORACLE_IR_SCHEMA_VERSION,
        "required_top_level_keys": [
            "schema_version",
            "target",
            "inputs",
            "rules",
            "compare",
        ],
        "allowed_input_types": sorted(ALLOWED_INPUT_TYPES),
        "allowed_compare_kinds": sorted(ALLOWED_COMPARE_KINDS),
        "allowed_normalizers": sorted(ALLOWED_NORMALIZERS),
        "allowed_calls": sorted(ALLOWED_CALLS),
        "allowed_call_formats": sorted(ALLOWED_CALL_FORMATS),
        "expression_examples": [
            {"field": "message"},
            {"bytes_from_hex": {"field": "message"}},
            {
                "call": "hashlib.sha256",
                "args": [{"bytes_from_hex": {"field": "message"}}],
                "format": "hexdigest",
            },
            {"eq": [{"field": "mode"}, "sha256"]},
        ],
    }


def manifest_payload(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    manifest_path = Path(path)
    payload: dict[str, Any] = {
        "path": str(manifest_path),
        "content": manifest_path.read_text(encoding="utf-8"),
    }
    try:
        manifest = load_manifest_summary(manifest_path)
    except Exception:
        return payload
    payload["summary"] = {
        "target": manifest.target,
        "fields": [
            {
                "name": field.name,
                "kind": field.kind,
                "choices": list(field.choices),
                "min": field.minimum,
                "max": field.maximum,
                "hex_len": field.hex_len,
                "hex_len_by": field.hex_len_by,
            }
            for field in manifest.fields
        ],
    }
    return payload


def repair_result(
    status: str,
    oracle_ir: dict[str, Any],
    prompt: dict[str, Any],
    issues: list[Any],
    llm_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "oracle_ir": oracle_ir,
        "prompt": prompt,
        "issues": normalize_issues(issues),
        "llm_responses": llm_responses,
        "attempt_count": len(llm_responses),
    }


def normalize_issues(issues: list[Any]) -> list[dict[str, str]]:
    normalized = []
    for issue in issues:
        if isinstance(issue, dict):
            normalized.append(
                {
                    "path": str(issue.get("path", "")),
                    "message": str(issue.get("message", "")),
                }
            )
        else:
            normalized.append(
                {
                    "path": str(getattr(issue, "path", "")),
                    "message": str(getattr(issue, "message", "")),
                }
            )
    return normalized


def looks_like_oracle_ir(value: dict[str, Any]) -> bool:
    return {
        "schema_version",
        "target",
        "inputs",
        "rules",
        "compare",
    }.issubset(value)


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(deepcopy(value), sort_keys=True))


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


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain JSON")
    return match.group(0)

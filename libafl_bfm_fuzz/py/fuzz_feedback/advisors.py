from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib import request


def propose_directives(summary: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    if target == "tinyalu":
        return propose_tinyalu(summary)
    if target == "aes":
        return propose_aes(summary)
    if target == "sha256":
        return propose_sha256(summary)
    return {
        "source": "generic heuristic",
        "directives": [
            {
                "target": target,
                "name": "generic_refresh",
                "reason": "No target-specific advisor is registered; refresh the configured seed space.",
                "weight": 1,
            }
        ],
    }


def propose_tinyalu(summary: dict[str, Any]) -> dict[str, Any]:
    uncovered = uncovered_text(summary)
    stimulus = summary["stimulus_summary"]
    op_counts = stimulus.get("op_counts", {})
    directives: list[dict[str, Any]] = []

    if contains_any(uncovered, "three_cycle", "mult", "done1", "done2", "done3"):
        directives.append(
            {
                "target": "tinyalu",
                "name": "cover_mul_pipeline",
                "reason": "Uncovered RTL is near the multiplier pipeline path.",
                "ops": ["MUL"],
                "operand_pairs": [{"a": "0x01", "b": "0xff"}, {"a": "0x7f", "b": "0x80"}, {"a": "0xff", "b": "0xfe"}],
                "weight": 4,
            }
        )
    if contains_any(uncovered, "3'b001", "add", "+") or op_counts.get("ADD", 0) < 8:
        directives.append(
            {
                "target": "tinyalu",
                "name": "stress_add_overflow",
                "reason": "ADD overflow and carry cases need stronger exercise.",
                "ops": ["ADD"],
                "operand_pairs": [{"a": "0xff", "b": "0xff"}, {"a": "0xff", "b": "0x01"}, {"a": "0x80", "b": "0x80"}],
                "weight": 3,
            }
        )
    if not directives:
        directives.append(
            {
                "target": "tinyalu",
                "name": "balanced_edge_refresh",
                "reason": "No obvious uncovered operation cluster; refresh edge operands.",
                "ops": ["ADD", "AND", "XOR", "MUL"],
                "operand_pairs": [{"a": "0x00", "b": "0xff"}, {"a": "0x7f", "b": "0x80"}, {"a": "0x55", "b": "0xaa"}],
                "weight": 2,
            }
        )
    return {"source": "heuristic", "directives": directives}


def propose_aes(summary: dict[str, Any]) -> dict[str, Any]:
    uncovered = uncovered_text(summary)
    stimulus = summary["stimulus_summary"]
    key_counts = stimulus.get("key_len_counts", {})
    dir_counts = stimulus.get("encdec_counts", {})
    directives: list[dict[str, Any]] = []

    if contains_any(uncovered, "decipher", "inv_", "AES_DECIPHER") or dir_counts.get("decipher", 0) < 4:
        directives.append(
            {
                "target": "aes",
                "name": "stress_decipher_paths",
                "reason": "AES decipher or inverse round logic appears under-exercised.",
                "encdecs": ["decipher"],
                "key_lens": [128, 256],
                "key_patterns": ["zero", "ff", "increment"],
                "block_patterns": ["zero", "ff", "alternating", "walking_one"],
                "weight": 4,
            }
        )
    if contains_any(uncovered, "key_mem", "keylen", "round_key") or key_counts.get("128", 0) < 4:
        directives.append(
            {
                "target": "aes",
                "name": "mix_key_schedule_lengths",
                "reason": "Key expansion and key length muxing need more directed coverage.",
                "encdecs": ["encipher", "decipher"],
                "key_lens": [128, 256],
                "key_patterns": ["increment", "decrement", "alternating"],
                "block_patterns": ["zero", "increment"],
                "weight": 3,
            }
        )
    if not directives:
        directives.append(
            {
                "target": "aes",
                "name": "aes_state_pattern_refresh",
                "reason": "Refresh AES state and key edge patterns.",
                "encdecs": ["encipher", "decipher"],
                "key_lens": [128, 256],
                "key_patterns": ["zero", "ff", "increment"],
                "block_patterns": ["zero", "ff", "alternating", "walking_one"],
                "weight": 2,
            }
        )
    return {"source": "heuristic", "directives": directives}


def propose_sha256(summary: dict[str, Any]) -> dict[str, Any]:
    uncovered = uncovered_text(summary)
    stimulus = summary["stimulus_summary"]
    mode_counts = stimulus.get("mode_counts", {})
    length_buckets = stimulus.get("message_length_buckets", {})
    directives: list[dict[str, Any]] = []

    if contains_any(uncovered, "sha224", "mode") or mode_counts.get("sha224", 0) < 4:
        directives.append(
            {
                "target": "sha256",
                "name": "balance_sha224_mode",
                "reason": "SHA-224 mode or mode control appears under-exercised.",
                "modes": ["sha224"],
                "message_lengths": [0, 1, 55, 56, 57, 64, 65, 127],
                "byte_patterns": ["zero", "ff", "increment"],
                "weight": 4,
            }
        )
    if contains_any(uncovered, "w_mem", "block", "next") or length_buckets.get("65..127", 0) < 4:
        directives.append(
            {
                "target": "sha256",
                "name": "stress_padding_and_next_blocks",
                "reason": "Message schedule and multi-block next path need boundary messages.",
                "modes": ["sha256", "sha224"],
                "message_lengths": [55, 56, 57, 63, 64, 65, 111, 112, 113, 127],
                "byte_patterns": ["zero", "ff", "alternating", "walking_one"],
                "weight": 5,
            }
        )
    if not directives:
        directives.append(
            {
                "target": "sha256",
                "name": "sha_boundary_refresh",
                "reason": "Refresh SHA padding boundaries and both modes.",
                "modes": ["sha256", "sha224"],
                "message_lengths": [0, 1, 55, 56, 57, 63, 64, 65, 127],
                "byte_patterns": ["zero", "ff", "increment", "alternating"],
                "weight": 2,
            }
        )
    return {"source": "heuristic", "directives": directives}


def uncovered_text(summary: dict[str, Any]) -> str:
    return "\n".join(f"{line['file']}:{line['line']} {line['code']}" for line in summary.get("uncovered_lines", [])).lower()


def contains_any(text: str, *patterns: str) -> bool:
    return any(pattern.lower() in text for pattern in patterns)


def write_llm_prompt(path: Path, summary: dict[str, Any], heuristic: dict[str, Any]) -> None:
    prompt = {
        "task": "Analyze Verilator RTL coverage gaps and return JSON mutation directives for the LibAFL corpus generator. Return JSON only; do not emit prose.",
        "target": summary["target"],
        "allowed_schema_by_target": allowed_schema_by_target(),
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n")


def allowed_schema_by_target() -> dict[str, Any]:
    return {
        "tinyalu": {"directives": [{"target": "tinyalu", "name": "short_identifier", "reason": "coverage gap being targeted", "ops": ["ADD", "AND", "XOR", "MUL"], "operand_pairs": [{"a": "0xff", "b": "0x01"}], "weight": 1}]},
        "aes": {"directives": [{"target": "aes", "name": "short_identifier", "reason": "coverage gap being targeted", "key_lens": [128, 256], "encdecs": ["encipher", "decipher"], "key_patterns": ["zero", "ff", "increment", "decrement", "alternating"], "block_patterns": ["zero", "ff", "increment", "alternating", "walking_one"], "weight": 1}]},
        "sha256": {"directives": [{"target": "sha256", "name": "short_identifier", "reason": "coverage gap being targeted", "modes": ["sha256", "sha224"], "message_lengths": [0, 1, 55, 56, 57, 63, 64, 65, 127], "byte_patterns": ["zero", "ff", "increment", "alternating", "walking_one"], "weight": 1}]},
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
            {"role": "system", "content": "You are a hardware verification fuzzing assistant. Return strict JSON only."},
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
        if target == "tinyalu" and not directive.get("ops"):
            directive["ops"] = ["ADD", "AND", "XOR", "MUL"]
        elif target == "aes":
            directive.setdefault("key_lens", [128, 256])
            directive.setdefault("encdecs", ["encipher", "decipher"])
            directive.setdefault("key_patterns", ["zero", "ff", "increment"])
            directive.setdefault("block_patterns", ["zero", "ff", "alternating"])
        elif target == "sha256":
            directive.setdefault("modes", ["sha256", "sha224"])
            directive.setdefault("message_lengths", [0, 1, 55, 56, 57, 64, 65, 127])
            directive.setdefault("byte_patterns", ["zero", "ff", "increment"])
        filtered.append(directive)
    if not filtered:
        raise ValueError("no usable directives for target")
    return {"source": value.get("source", "llm"), "directives": filtered}

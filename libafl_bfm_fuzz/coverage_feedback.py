"""Target-aware coverage feedback and optional LLM mutation guidance.

The script always writes a compact coverage summary, an LLM prompt, and a valid
mutation directive file. If --llm is enabled and OPENAI_API_KEY is present, it
asks a model for target-specific directives and validates the response before
using it. Otherwise it falls back to deterministic heuristics.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib import request


TARGETS = {"tinyalu", "aes", "sha256"}
OP_NAMES = {1: "ADD", 2: "AND", 3: "XOR", 4: "MUL"}


@dataclass(frozen=True)
class UncoveredLine:
    file: str
    line: int
    code: str


def parse_lcov_info(path: Path) -> tuple[list[UncoveredLine], dict[str, int]]:
    uncovered: list[UncoveredLine] = []
    file_counts: Counter[str] = Counter()
    if not path.exists():
        return uncovered, {}

    current_file: Path | None = None
    source_cache: dict[Path, list[str]] = {}
    for raw_line in path.read_text(errors="replace").splitlines():
        if raw_line.startswith("SF:"):
            current_file = Path(raw_line[3:])
            if current_file.exists():
                source_cache[current_file] = current_file.read_text(errors="replace").splitlines()
            continue
        if current_file is None or not raw_line.startswith("DA:"):
            continue

        line_no_text, count_text = raw_line[3:].split(",", 1)
        line_no = int(line_no_text)
        count = int(count_text)
        if count != 0:
            continue

        source = source_cache.get(current_file, [])
        code = source[line_no - 1].strip() if 0 < line_no <= len(source) else ""
        uncovered.append(UncoveredLine(str(current_file), line_no, code))
        file_counts[str(current_file)] += 1
    return uncovered, dict(file_counts)


def parse_corpus(path: Path, target: str) -> dict[str, Any]:
    total = 0
    origins: Counter[str] = Counter()
    summary: dict[str, Any] = {"target": target, "total_cases": 0, "origin_counts": {}}

    tiny_ops: Counter[str] = Counter()
    aes_key_lens: Counter[str] = Counter()
    aes_dirs: Counter[str] = Counter()
    sha_modes: Counter[str] = Counter()
    sha_lengths: Counter[str] = Counter()

    if not path.exists():
        return summary

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        total += 1
        origins[str(data.get("origin", "unknown"))] += 1
        case_target = str(data.get("target", "tinyalu"))
        if case_target != target:
            continue
        if target == "tinyalu":
            tiny_ops[OP_NAMES.get(int(data["op"]), str(data["op"]))] += 1
        elif target == "aes":
            aes_key_lens[str(data["key_len"])] += 1
            aes_dirs[str(data["encdec"])] += 1
        elif target == "sha256":
            sha_modes[str(data["mode"])] += 1
            msg_len = len(bytes.fromhex(str(data["message"])))
            bucket = length_bucket(msg_len)
            sha_lengths[bucket] += 1

    summary["total_cases"] = total
    summary["origin_counts"] = dict(origins)
    if target == "tinyalu":
        summary["op_counts"] = dict(tiny_ops)
    elif target == "aes":
        summary["key_len_counts"] = dict(aes_key_lens)
        summary["encdec_counts"] = dict(aes_dirs)
    elif target == "sha256":
        summary["mode_counts"] = dict(sha_modes)
        summary["message_length_buckets"] = dict(sha_lengths)
    return summary


def length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length <= 55:
        return "1..55"
    if length <= 64:
        return "56..64"
    if length <= 127:
        return "65..127"
    return "128+"


def build_summary(target: str, coverage_info: Path, corpus: Path) -> dict[str, Any]:
    uncovered, file_counts = parse_lcov_info(coverage_info)
    return {
        "target": target,
        "coverage_info": str(coverage_info),
        "corpus": str(corpus),
        "uncovered_line_count": len(uncovered),
        "uncovered_by_file": file_counts,
        "uncovered_lines": [asdict(line) for line in uncovered[:120]],
        "stimulus_summary": parse_corpus(corpus, target),
    }


def propose_directives(summary: dict[str, Any]) -> dict[str, Any]:
    target = summary["target"]
    if target == "tinyalu":
        return propose_tinyalu(summary)
    if target == "aes":
        return propose_aes(summary)
    if target == "sha256":
        return propose_sha256(summary)
    raise ValueError(f"unsupported target {target!r}")


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
                "operand_pairs": [
                    {"a": "0x01", "b": "0xff"},
                    {"a": "0x7f", "b": "0x80"},
                    {"a": "0xff", "b": "0xfe"},
                ],
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
                "operand_pairs": [
                    {"a": "0xff", "b": "0xff"},
                    {"a": "0xff", "b": "0x01"},
                    {"a": "0x80", "b": "0x80"},
                ],
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
                "operand_pairs": [
                    {"a": "0x00", "b": "0xff"},
                    {"a": "0x7f", "b": "0x80"},
                    {"a": "0x55", "b": "0xaa"},
                ],
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
    return "\n".join(
        f"{line['file']}:{line['line']} {line['code']}"
        for line in summary.get("uncovered_lines", [])
    ).lower()


def contains_any(text: str, *patterns: str) -> bool:
    return any(pattern.lower() in text for pattern in patterns)


def write_llm_prompt(path: Path, summary: dict[str, Any], heuristic: dict[str, Any]) -> None:
    prompt = {
        "task": (
            "Analyze Verilator RTL coverage gaps and return JSON mutation directives "
            "for the LibAFL corpus generator. Return JSON only; do not emit prose."
        ),
        "target": summary["target"],
        "allowed_schema_by_target": {
            "tinyalu": {
                "directives": [
                    {
                        "target": "tinyalu",
                        "name": "short_identifier",
                        "reason": "coverage gap being targeted",
                        "ops": ["ADD", "AND", "XOR", "MUL"],
                        "operand_pairs": [{"a": "0xff", "b": "0x01"}],
                        "weight": 1,
                    }
                ]
            },
            "aes": {
                "directives": [
                    {
                        "target": "aes",
                        "name": "short_identifier",
                        "reason": "coverage gap being targeted",
                        "key_lens": [128, 256],
                        "encdecs": ["encipher", "decipher"],
                        "key_patterns": ["zero", "ff", "increment", "decrement", "alternating"],
                        "block_patterns": ["zero", "ff", "increment", "alternating", "walking_one"],
                        "weight": 1,
                    }
                ]
            },
            "sha256": {
                "directives": [
                    {
                        "target": "sha256",
                        "name": "short_identifier",
                        "reason": "coverage gap being targeted",
                        "modes": ["sha256", "sha224"],
                        "message_lengths": [0, 1, 55, 56, 57, 63, 64, 65, 127],
                        "byte_patterns": ["zero", "ff", "increment", "alternating", "walking_one"],
                        "weight": 1,
                    }
                ]
            },
        },
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n")


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
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=60) as resp:
        raw = json.loads(resp.read().decode())
    content = raw["choices"][0]["message"]["content"]
    return json.loads(extract_json(content))


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--coverage-info", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    parser.add_argument("--directives-out", type=Path, required=True)
    parser.add_argument("--prompt-out", type=Path, required=True)
    parser.add_argument("--llm", action="store_true", help="Call an LLM when OPENAI_API_KEY is available")
    parser.add_argument("--llm-response-out", type=Path)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    summary = build_summary(args.target, args.coverage_info, args.corpus)
    heuristic = propose_directives(summary)
    prompt = {
        "target": args.target,
        "coverage_summary": summary,
        "heuristic_baseline": heuristic,
    }

    final_directives = heuristic
    if args.llm:
        try:
            llm_value = maybe_call_llm(prompt, args.model)
            if llm_value is not None:
                if args.llm_response_out:
                    args.llm_response_out.parent.mkdir(parents=True, exist_ok=True)
                    args.llm_response_out.write_text(json.dumps(llm_value, indent=2, sort_keys=True) + "\n")
                final_directives = validate_directives(args.target, llm_value)
                final_directives["source"] = final_directives.get("source", "llm")
            else:
                final_directives["source"] = "heuristic; OPENAI_API_KEY not set"
        except Exception as exc:  # noqa: BLE001 - keep fuzz loop moving with heuristic fallback
            final_directives = heuristic
            final_directives["source"] = f"heuristic; LLM failed: {exc}"
            print(f"warning: LLM feedback failed, using heuristic directives: {exc}", file=sys.stderr)

    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    args.directives_out.write_text(json.dumps(final_directives, indent=2, sort_keys=True) + "\n")
    write_llm_prompt(args.prompt_out, summary, heuristic)
    print(
        f"coverage feedback: target={args.target} uncovered={summary['uncovered_line_count']} "
        f"directives={len(final_directives['directives'])} source={final_directives['source']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

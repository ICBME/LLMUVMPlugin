from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from fuzz_bfm.corpus import FuzzCase
from fuzz_bfm.plugin_loader import build_plugin
from fuzz_bfm.target_config import TargetConfig, load_target_config


OP_NAMES = {1: "ADD", 2: "AND", 3: "XOR", 4: "MUL"}
TINYALU_OPS = ("ADD", "AND", "XOR", "MUL")
AES_KEY_LENS = ("128", "256")
AES_DIRECTIONS = ("encipher", "decipher")
SHA_MODES = ("sha224", "sha256")
SHA_LENGTH_BUCKETS = ("0", "1..55", "56..64", "65..127")


class FunctionalCoverageProtocol(Protocol):
    target: str

    def sample(self, case: FuzzCase) -> None:
        ...

    def to_json(self) -> dict[str, Any]:
        ...


def build_coverage_model(
    target: str,
    config: TargetConfig | None = None,
) -> FunctionalCoverageProtocol:
    config = config or _try_load_target_config(target)
    if config is not None and config.coverage_model:
        return build_plugin(config.coverage_model, target=target, config=config)
    model_cls = DEFAULT_COVERAGE_MODEL_CLASSES.get(target, GenericCoverageModel)
    return model_cls(target=target, config=config)


def FunctionalCoverageModel(
    target: str,
    config: TargetConfig | None = None,
) -> FunctionalCoverageProtocol:
    """Backward-compatible factory for the target coverage plugin."""

    return build_coverage_model(target, config=config)


def build_functional_coverage(
    target: str,
    cases: Iterable[FuzzCase],
    config: TargetConfig | None = None,
) -> dict[str, Any]:
    model = build_coverage_model(target, config=config)
    for case in cases:
        model.sample(case)
    return model.to_json()


def build_functional_coverage_from_jsonl(path: Path, target: str) -> dict[str, Any]:
    config = _try_load_target_config(target)
    cases: list[FuzzCase] = []
    if path.exists():
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            data = json.loads(line)
            case_target = str(data.get("target", "tinyalu"))
            if case_target == target:
                cases.append(FuzzCase(case_target, data, line_no))
    return build_functional_coverage(target, cases, config=config)


class CounterCoverageModel:
    def __init__(self, target: str, config: TargetConfig | None = None):
        self.target = target
        self.config = config
        self.total_cases = 0
        self.bins: dict[str, Counter[str]] = {}
        self.crosses: dict[str, Counter[str]] = {}
        self.origins: Counter[str] = Counter()

    def sample(self, case: FuzzCase) -> None:
        if case.target != self.target:
            return
        self.total_cases += 1
        self.origins[str(case.data.get("origin", "unknown"))] += 1
        self.sample_case(case.data)

    def sample_case(self, data: dict[str, Any]) -> None:
        self._hit("target", self.target)

    def to_json(self) -> dict[str, Any]:
        return {
            "domain": "uvm_functional",
            "target": self.target,
            "total_cases": self.total_cases,
            "origin_counts": dict(sorted(self.origins.items())),
            "bins": {key: dict(sorted(value.items())) for key, value in sorted(self.bins.items())},
            "crosses": {key: dict(sorted(value.items())) for key, value in sorted(self.crosses.items())},
            "uncovered": self.uncovered(),
        }

    def uncovered(self) -> dict[str, list[str]]:
        return {}

    def _hit(self, bin_name: str, value: str) -> None:
        self.bins.setdefault(bin_name, Counter())[value] += 1

    def _cross(self, cross_name: str, *values: str) -> None:
        self.crosses.setdefault(cross_name, Counter())["|".join(values)] += 1


class GenericCoverageModel(CounterCoverageModel):
    pass


class TinyAluCoverageModel(CounterCoverageModel):
    def sample_case(self, data: dict[str, Any]) -> None:
        op = OP_NAMES.get(int(data["op"]), str(data["op"]))
        a = int(data["a"])
        b = int(data["b"])
        a_bucket = operand_bucket(a)
        b_bucket = operand_bucket(b)
        pair_bucket = operand_pair_bucket(a, b)
        self._hit("op", op)
        self._hit("a_operand", a_bucket)
        self._hit("b_operand", b_bucket)
        self._hit("operand_pair", pair_bucket)
        self._cross("op_x_operand_pair", op, pair_bucket)

    def uncovered(self) -> dict[str, list[str]]:
        return {
            "op": missing(self.bins.get("op", Counter()), TINYALU_OPS),
        }


class AesCoverageModel(CounterCoverageModel):
    def sample_case(self, data: dict[str, Any]) -> None:
        key_len = str(data["key_len"])
        encdec = str(data["encdec"])
        key = bytes.fromhex(str(data["key"]))
        block = bytes.fromhex(str(data["block"]))
        key_pattern = byte_pattern(key)
        block_pattern = byte_pattern(block)
        self._hit("key_len", key_len)
        self._hit("encdec", encdec)
        self._hit("key_pattern", key_pattern)
        self._hit("block_pattern", block_pattern)
        self._cross("key_len_x_encdec", key_len, encdec)
        self._cross("encdec_x_block_pattern", encdec, block_pattern)

    def uncovered(self) -> dict[str, list[str]]:
        return {
            "key_len": missing(self.bins.get("key_len", Counter()), AES_KEY_LENS),
            "encdec": missing(self.bins.get("encdec", Counter()), AES_DIRECTIONS),
        }


class Sha256CoverageModel(CounterCoverageModel):
    def sample_case(self, data: dict[str, Any]) -> None:
        mode = str(data["mode"])
        message = bytes.fromhex(str(data["message"]))
        length = len(message)
        length_bucket_name = length_bucket(length)
        block_count = str(sha2_padded_block_count(length))
        pattern = byte_pattern(message)
        self._hit("mode", mode)
        self._hit("message_length", length_bucket_name)
        self._hit("padded_block_count", block_count)
        self._hit("byte_pattern", pattern)
        self._cross("mode_x_message_length", mode, length_bucket_name)
        self._cross("mode_x_padded_block_count", mode, block_count)

    def uncovered(self) -> dict[str, list[str]]:
        return {
            "mode": missing(self.bins.get("mode", Counter()), SHA_MODES),
            "message_length": missing(self.bins.get("message_length", Counter()), SHA_LENGTH_BUCKETS),
        }


DEFAULT_COVERAGE_MODEL_CLASSES = {
    "tinyalu": TinyAluCoverageModel,
    "aes": AesCoverageModel,
    "sha256": Sha256CoverageModel,
}


def missing(counter: Counter[str], expected: tuple[str, ...]) -> list[str]:
    return [value for value in expected if counter.get(value, 0) == 0]


def operand_bucket(value: int) -> str:
    if value == 0:
        return "zero"
    if value == 0xFF:
        return "max"
    if value in {0x7F, 0x80}:
        return "signed_edge"
    if value in {0x55, 0xAA, 0x0F, 0xF0}:
        return "alternating"
    return "other"


def operand_pair_bucket(a: int, b: int) -> str:
    if a == b:
        return "equal"
    if (a ^ b) == 0xFF:
        return "complement"
    if a in {0, 0xFF} or b in {0, 0xFF}:
        return "edge"
    if a in {0x7F, 0x80} or b in {0x7F, 0x80}:
        return "signed_edge"
    if a in {0x55, 0xAA, 0x0F, 0xF0} or b in {0x55, 0xAA, 0x0F, 0xF0}:
        return "alternating"
    return "other"


def byte_pattern(data: bytes) -> str:
    if not data:
        return "empty"
    if all(byte == 0 for byte in data):
        return "zero"
    if all(byte == 0xFF for byte in data):
        return "ff"
    if data == bytes(idx & 0xFF for idx in range(len(data))):
        return "increment"
    if data == bytes(0xFF - (idx & 0xFF) for idx in range(len(data))):
        return "decrement"
    if data in {
        bytes(0xAA if idx % 2 == 0 else 0x55 for idx in range(len(data))),
        bytes(0x55 if idx % 2 == 0 else 0xAA for idx in range(len(data))),
    }:
        return "alternating"
    if data == bytes(1 << (idx % 8) for idx in range(len(data))):
        return "walking_one"
    return "mixed"


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


def sha2_padded_block_count(length: int) -> int:
    padded_len = length + 1 + 8
    return (padded_len + 63) // 64


def _try_load_target_config(target: str) -> TargetConfig | None:
    try:
        return load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None

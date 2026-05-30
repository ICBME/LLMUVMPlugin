from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable

from fuzz_bfm.corpus import FuzzCase


OP_NAMES = {1: "ADD", 2: "AND", 3: "XOR", 4: "MUL"}
TINYALU_OPS = ("ADD", "AND", "XOR", "MUL")
AES_KEY_LENS = ("128", "256")
AES_DIRECTIONS = ("encipher", "decipher")
SHA_MODES = ("sha224", "sha256")
SHA_LENGTH_BUCKETS = ("0", "1..55", "56..64", "65..127")


def build_functional_coverage(target: str, cases: Iterable[FuzzCase]) -> dict[str, Any]:
    model = FunctionalCoverageModel(target)
    for case in cases:
        model.sample(case)
    return model.to_json()


def build_functional_coverage_from_jsonl(path: Path, target: str) -> dict[str, Any]:
    cases: list[FuzzCase] = []
    if path.exists():
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            data = json.loads(line)
            case_target = str(data.get("target", "tinyalu"))
            if case_target == target:
                cases.append(FuzzCase(case_target, data, line_no))
    return build_functional_coverage(target, cases)


class FunctionalCoverageModel:
    def __init__(self, target: str):
        self.target = target
        self.total_cases = 0
        self.bins: dict[str, Counter[str]] = {}
        self.crosses: dict[str, Counter[str]] = {}
        self.origins: Counter[str] = Counter()

    def sample(self, case: FuzzCase) -> None:
        if case.target != self.target:
            return
        self.total_cases += 1
        self.origins[str(case.data.get("origin", "unknown"))] += 1
        if self.target == "tinyalu":
            self._sample_tinyalu(case.data)
        elif self.target == "aes":
            self._sample_aes(case.data)
        elif self.target == "sha256":
            self._sample_sha256(case.data)
        else:
            self._hit("target", self.target)

    def to_json(self) -> dict[str, Any]:
        return {
            "domain": "uvm_functional",
            "target": self.target,
            "total_cases": self.total_cases,
            "origin_counts": dict(sorted(self.origins.items())),
            "bins": {key: dict(sorted(value.items())) for key, value in sorted(self.bins.items())},
            "crosses": {key: dict(sorted(value.items())) for key, value in sorted(self.crosses.items())},
            "uncovered": self._uncovered(),
        }

    def _sample_tinyalu(self, data: dict[str, Any]) -> None:
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

    def _sample_aes(self, data: dict[str, Any]) -> None:
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

    def _sample_sha256(self, data: dict[str, Any]) -> None:
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

    def _hit(self, bin_name: str, value: str) -> None:
        self.bins.setdefault(bin_name, Counter())[value] += 1

    def _cross(self, cross_name: str, *values: str) -> None:
        self.crosses.setdefault(cross_name, Counter())["|".join(values)] += 1

    def _uncovered(self) -> dict[str, list[str]]:
        if self.target == "tinyalu":
            return {
                "op": missing(self.bins.get("op", Counter()), TINYALU_OPS),
            }
        if self.target == "aes":
            return {
                "key_len": missing(self.bins.get("key_len", Counter()), AES_KEY_LENS),
                "encdec": missing(self.bins.get("encdec", Counter()), AES_DIRECTIONS),
            }
        if self.target == "sha256":
            return {
                "mode": missing(self.bins.get("mode", Counter()), SHA_MODES),
                "message_length": missing(self.bins.get("message_length", Counter()), SHA_LENGTH_BUCKETS),
            }
        return {}


def missing(counter: Counter[str], expected: tuple[str, ...]) -> list[str]:
    return [value for value in expected if counter.get(value, 0) == 0]


def operand_bucket(value: int) -> str:
    if value == 0:
        return "zero"
    if value == 0xff:
        return "max"
    if value in {0x7f, 0x80}:
        return "signed_edge"
    if value in {0x55, 0xaa, 0x0f, 0xf0}:
        return "alternating"
    return "other"


def operand_pair_bucket(a: int, b: int) -> str:
    if a == b:
        return "equal"
    if (a ^ b) == 0xff:
        return "complement"
    if a in {0, 0xff} or b in {0, 0xff}:
        return "edge"
    if a in {0x7f, 0x80} or b in {0x7f, 0x80}:
        return "signed_edge"
    if a in {0x55, 0xaa, 0x0f, 0xf0} or b in {0x55, 0xaa, 0x0f, 0xf0}:
        return "alternating"
    return "other"


def byte_pattern(data: bytes) -> str:
    if not data:
        return "empty"
    if all(byte == 0 for byte in data):
        return "zero"
    if all(byte == 0xff for byte in data):
        return "ff"
    if data == bytes(idx & 0xff for idx in range(len(data))):
        return "increment"
    if data == bytes(0xff - (idx & 0xff) for idx in range(len(data))):
        return "decrement"
    if data in {
        bytes(0xaa if idx % 2 == 0 else 0x55 for idx in range(len(data))),
        bytes(0x55 if idx % 2 == 0 else 0xaa for idx in range(len(data))),
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

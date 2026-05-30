from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .target_config import FieldSpec, TargetConfig, load_target_config


SUPPORTED_TARGETS = {"tinyalu", "aes", "sha256"}


@dataclass(frozen=True)
class FuzzCase:
    target: str
    data: dict[str, Any]
    line_no: int


def load_cases(path: Path, target: str, config: TargetConfig | None = None) -> list[FuzzCase]:
    config = config or _try_load_config(target)
    if config is None and target not in SUPPORTED_TARGETS:
        raise ValueError(f"unsupported target {target!r}; provide a target config or use one of {sorted(SUPPORTED_TARGETS)}")
    if not path.exists():
        raise FileNotFoundError(f"LibAFL corpus {path} does not exist. Run generate-corpus first.")

    cases: list[FuzzCase] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        case_target = str(data.get("target", "tinyalu"))
        if case_target != target:
            raise ValueError(f"{path}:{line_no}: expected target {target!r}, got {case_target!r}")
        validate_case(path, line_no, case_target, data, config=config)
        cases.append(FuzzCase(target=case_target, data=data, line_no=line_no))

    if not cases:
        raise ValueError(f"LibAFL corpus {path} did not contain any {target} cases")
    return cases


def validate_case(
    path: Path,
    line_no: int,
    target: str,
    data: dict[str, Any],
    config: TargetConfig | None = None,
) -> None:
    if config is not None and config.fields:
        _validate_config_fields(path, line_no, data, config.fields)
    elif target == "tinyalu":
        _validate_int(path, line_no, data, "a", 0, 0xFF)
        _validate_int(path, line_no, data, "b", 0, 0xFF)
        _validate_int(path, line_no, data, "op", 1, 4)
    elif target == "aes":
        key_len = _validate_int(path, line_no, data, "key_len", 128, 256)
        if key_len not in {128, 256}:
            raise ValueError(f"{path}:{line_no}: key_len must be 128 or 256")
        if data.get("encdec") not in {"encipher", "decipher"}:
            raise ValueError(f"{path}:{line_no}: encdec must be encipher or decipher")
        _validate_hex(path, line_no, data, "key", 16 if key_len == 128 else 32)
        _validate_hex(path, line_no, data, "block", 16)
    elif target == "sha256":
        if data.get("mode") not in {"sha224", "sha256"}:
            raise ValueError(f"{path}:{line_no}: mode must be sha224 or sha256")
        _validate_hex(path, line_no, data, "message", None)
    else:
        raise ValueError(f"{path}:{line_no}: unsupported target {target!r}")


def hex_to_bytes(text: str) -> bytes:
    return bytes.fromhex(text)


def bytes_to_words(data: bytes) -> list[int]:
    if len(data) % 4 != 0:
        raise ValueError("data length must be a multiple of four bytes")
    return [int.from_bytes(data[idx : idx + 4], "big") for idx in range(0, len(data), 4)]


def words_to_bytes(words: list[int]) -> bytes:
    return b"".join(word.to_bytes(4, "big") for word in words)


def _validate_int(
    path: Path,
    line_no: int,
    data: dict[str, Any],
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(data[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}:{line_no}: {key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{path}:{line_no}: {key}={value} outside [{minimum}, {maximum}]")
    return value


def _validate_hex(
    path: Path,
    line_no: int,
    data: dict[str, Any],
    key: str,
    expected_len: int | None,
) -> bytes:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{path}:{line_no}: {key} must be a hex string")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{path}:{line_no}: {key} is not valid hex") from exc
    if expected_len is not None and len(raw) != expected_len:
        raise ValueError(f"{path}:{line_no}: {key} must be {expected_len} bytes")
    return raw


def _validate_config_fields(
    path: Path,
    line_no: int,
    data: dict[str, Any],
    fields: tuple[FieldSpec, ...],
) -> None:
    for field in fields:
        if field.kind == "int":
            minimum = field.minimum if field.minimum is not None else -(2**63)
            maximum = field.maximum if field.maximum is not None else 2**63 - 1
            value = _validate_int(path, line_no, data, field.name, minimum, maximum)
            if field.choices and value not in {int(choice) for choice in field.choices}:
                raise ValueError(f"{path}:{line_no}: {field.name} must be one of {field.choices}")
        elif field.kind == "enum":
            if data.get(field.name) not in set(field.choices):
                raise ValueError(f"{path}:{line_no}: {field.name} must be one of {field.choices}")
        elif field.kind == "hex":
            expected_len = field.hex_len
            if field.hex_len_by:
                selector_name, selector_map = next(iter(field.hex_len_by.items()))
                expected_len = int(selector_map[str(data[selector_name])])
            _validate_hex(path, line_no, data, field.name, expected_len)
        else:
            if field.name not in data:
                raise ValueError(f"{path}:{line_no}: missing required field {field.name}")


def _try_load_config(target: str) -> TargetConfig | None:
    try:
        return load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None

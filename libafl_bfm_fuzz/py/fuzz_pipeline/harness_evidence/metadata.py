from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class HarnessMetadataExtractorProtocol(Protocol):
    def extract(self, metadata: dict[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class UvmFuzzMetadataExtractor:
    """Default metadata projection for the UVM-fuzz harness event schema."""

    def extract(self, metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_index": case_index(metadata),
            "case_id": case_id(metadata),
            "directive_id": directive_id(metadata),
            "corpus_sha256": corpus_sha256(metadata),
        }


def case_index(metadata: dict[str, Any]) -> int | None:
    for key in ("case_index", "index"):
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return None


def case_id(metadata: dict[str, Any]) -> str | None:
    for key in ("case_id", "case_hash", "case_sha256"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def directive_id(metadata: dict[str, Any]) -> str | None:
    for key in ("directive_id", "directive_name", "directive", "origin"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def corpus_sha256(metadata: dict[str, Any]) -> str | None:
    for key in ("corpus_sha256", "corpus_hash"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None

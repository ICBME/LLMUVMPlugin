from __future__ import annotations

from dataclasses import dataclass

from harness_optimization.metadata import HarnessMetadataExtractorProtocol


@dataclass(frozen=True)
class UvmFuzzMetadataExtractor:
    """Default metadata projection for the UVM-fuzz harness event schema."""

    def extract(self, metadata: dict[str, object]) -> dict[str, object]:
        return {
            "case_index": case_index(metadata),
            "case_id": case_id(metadata),
            "directive_id": directive_id(metadata),
            "corpus_sha256": corpus_sha256(metadata),
        }


def case_index(metadata: dict[str, object]) -> int | None:
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


def case_id(metadata: dict[str, object]) -> str | None:
    for key in ("case_id", "case_hash", "case_sha256"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def directive_id(metadata: dict[str, object]) -> str | None:
    for key in ("directive_id", "directive_name", "directive", "origin"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def corpus_sha256(metadata: dict[str, object]) -> str | None:
    for key in ("corpus_sha256", "corpus_hash"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


__all__ = [
    "HarnessMetadataExtractorProtocol",
    "UvmFuzzMetadataExtractor",
    "case_index",
    "case_id",
    "directive_id",
    "corpus_sha256",
]

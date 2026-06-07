from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import os
import time
import uuid


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ArtifactRef:
    role: str
    path: str
    size_bytes: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_path(
        cls,
        role: str,
        path: str | Path,
        *,
        include_sha256: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> "ArtifactRef":
        artifact_path = Path(path)
        try:
            stat = artifact_path.stat()
        except OSError:
            stat = None
        return cls(
            role=role,
            path=str(artifact_path),
            size_bytes=stat.st_size if stat is not None else None,
            mtime_ns=stat.st_mtime_ns if stat is not None else None,
            sha256=file_sha256(artifact_path) if include_sha256 and stat is not None else None,
            metadata=dict(metadata or {}),
        )

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "role": self.role,
            "path": self.path,
        }
        if self.size_bytes is not None:
            value["size_bytes"] = self.size_bytes
        if self.mtime_ns is not None:
            value["mtime_ns"] = self.mtime_ns
        if self.sha256 is not None:
            value["sha256"] = self.sha256
        if self.metadata:
            value["metadata"] = self.metadata
        return value


@dataclass(frozen=True)
class ConnectorEvent:
    event_type: str
    connector: str
    from_layer: str
    to_layer: str
    run_id: str | None = None
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp_ns: int = field(default_factory=time.time_ns)
    started_at_ns: int | None = None
    duration_ms: float | None = None
    inputs: tuple[ArtifactRef, ...] = ()
    outputs: tuple[ArtifactRef, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    error: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "connector": self.connector,
            "from_layer": self.from_layer,
            "to_layer": self.to_layer,
            "timestamp_ns": self.timestamp_ns,
            "inputs": [item.to_json() for item in self.inputs],
            "outputs": [item.to_json() for item in self.outputs],
        }
        if self.run_id is not None:
            value["run_id"] = self.run_id
        if self.started_at_ns is not None:
            value["started_at_ns"] = self.started_at_ns
        if self.duration_ms is not None:
            value["duration_ms"] = self.duration_ms
        if self.metrics:
            value["metrics"] = self.metrics
        if self.metadata:
            value["metadata"] = self.metadata
        if self.status is not None:
            value["status"] = self.status
        if self.error is not None:
            value["error"] = self.error
        return value


def normalize_artifact_refs(value: Any) -> tuple[ArtifactRef, ...]:
    if value is None:
        return ()
    if isinstance(value, ArtifactRef):
        return (value,)
    if isinstance(value, dict):
        if "path" in value:
            return (_artifact_ref_from_mapping(value),)
        result = []
        for role, item in value.items():
            if isinstance(item, dict):
                mapped = dict(item)
                mapped.setdefault("role", str(role))
                result.append(_artifact_ref_from_mapping(mapped))
            else:
                result.append(ArtifactRef.from_path(str(role), item))
        return tuple(result)
    if isinstance(value, str | os.PathLike):
        return (ArtifactRef.from_path("artifact", value),)
    result: list[ArtifactRef] = []
    for item in value:
        if isinstance(item, ArtifactRef):
            result.append(item)
        elif isinstance(item, str | os.PathLike):
            result.append(ArtifactRef.from_path("artifact", item))
        elif isinstance(item, dict):
            result.append(_artifact_ref_from_mapping(item))
        else:
            raise TypeError(f"unsupported artifact reference type: {type(item).__name__}")
    return tuple(result)


def error_payload(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_ref_from_mapping(value: dict[str, Any]) -> ArtifactRef:
    path = value.get("path")
    if not isinstance(path, str | os.PathLike):
        raise TypeError("artifact mapping must define a path")
    role = str(value.get("role", "artifact"))
    return ArtifactRef.from_path(
        role,
        path,
        include_sha256=bool(value.get("include_sha256", False)),
        metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else None,
    )

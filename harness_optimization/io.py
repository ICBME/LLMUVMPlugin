from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from ConnectGraph.schema import file_sha256


_PATH_SHA256_CACHE: dict[tuple[str, int, int], str] = {}


def read_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def path_or_none(value: object) -> Path | None:
    if not isinstance(value, str | Path) or not str(value):
        return None
    return Path(value)


def required_path(value: Any) -> Path:
    path = path_or_none(value)
    if path is None:
        raise ValueError(f"expected non-empty path, got {value!r}")
    return path


def artifact_path(
    artifacts: Mapping[str, Any],
    role: str,
    cwd: Path | None,
) -> Path | None:
    path = path_or_none(artifacts.get(role))
    if path is None:
        return None
    if path.is_absolute() or cwd is None:
        return path
    return cwd / path


def campaign_cwd(
    campaign_manifest: Mapping[str, Any],
    fallback: Path | None,
) -> Path | None:
    return path_or_none(campaign_manifest.get("cwd")) or fallback


def artifact_mapping(value: Any) -> dict[str, Path]:
    if value is None:
        return {}
    if isinstance(value, dict):
        result = {}
        for role, path in value.items():
            if isinstance(path, dict):
                item_path = path.get("path")
            else:
                item_path = path
            if item_path is not None:
                result[str(role)] = Path(item_path)
        return result
    return {}


def optional_artifact(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}


def optional_existing_artifact(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None and path.exists() else {}


def path_sha256(path: Path | str) -> str | None:
    resolved = Path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    cache_key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    if cache_key in _PATH_SHA256_CACHE:
        return _PATH_SHA256_CACHE[cache_key]
    try:
        value = file_sha256(resolved)
    except OSError:
        return None
    _PATH_SHA256_CACHE[cache_key] = value
    return value


__all__ = [
    "artifact_mapping",
    "artifact_path",
    "campaign_cwd",
    "optional_artifact",
    "optional_existing_artifact",
    "path_or_none",
    "path_sha256",
    "read_optional_json",
    "required_path",
    "write_json",
]

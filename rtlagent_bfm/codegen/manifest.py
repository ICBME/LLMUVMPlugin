"""Target manifest update helpers for generated plugin artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Mapping


PLUGIN_KEYS = ("bfm_ir", "ref_model", "scoreboard")


def update_manifest_plugins(
    manifest_path: str | Path,
    *,
    bfm_ir: str | None = None,
    ref_model: str | None = None,
    scoreboard: str | None = None,
) -> None:
    """Update top-level generated artifact references in a target manifest.

    The project intentionally avoids a TOML writer dependency. This function is
    conservative: it only rewrites or inserts top-level scalar keys before the
    first TOML table.
    """

    updates = {
        key: value
        for key, value in {
            "bfm_ir": bfm_ir,
            "ref_model": ref_model,
            "scoreboard": scoreboard,
        }.items()
        if value is not None
    }
    if not updates:
        return

    path = Path(manifest_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(update_manifest_text(text, updates), encoding="utf-8")


def update_manifest_text(text: str, updates: Mapping[str, str]) -> str:
    unknown = set(updates) - set(PLUGIN_KEYS)
    if unknown:
        raise ValueError(f"unsupported generated artifact manifest keys: {sorted(unknown)}")

    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    updated_lines: list[str] = []
    top_level = True
    key_pattern = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*).*$")

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("["):
            top_level = False
        if top_level:
            match = key_pattern.match(line)
            if match and match.group(2) in updates:
                key = match.group(2)
                updated_lines.append(f"{match.group(1)}{key}{match.group(3)}{_toml_string(updates[key])}\n")
                seen.add(key)
                continue
        updated_lines.append(line)

    missing = [key for key in PLUGIN_KEYS if key in updates and key not in seen]
    if missing:
        insert_at = _first_table_line(updated_lines)
        inserted = [f"{key} = {_toml_string(updates[key])}\n" for key in missing]
        if insert_at > 0 and updated_lines[insert_at - 1].strip():
            inserted.insert(0, "\n")
        if insert_at < len(updated_lines) and inserted[-1].strip():
            inserted.append("\n")
        updated_lines[insert_at:insert_at] = inserted

    return "".join(updated_lines)


def _first_table_line(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return index
    return len(lines)


def _toml_string(value: str) -> str:
    return json.dumps(str(value))

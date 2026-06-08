#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PY_DIR = REPO_ROOT / "libafl_bfm_fuzz" / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_pipeline.harness import close_observation, run_command, write_observation_topology  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing command after --")

    inputs = parse_artifacts(args.input)
    outputs = parse_artifacts(args.output)
    metadata = parse_metadata(args.metadata)
    if args.topology_out:
        write_observation_topology(args.topology_out)

    try:
        run_command(
            command,
            connector_name=args.connector,
            from_layer=args.from_layer,
            to_layer=args.to_layer,
            inputs=inputs,
            outputs=outputs,
            metadata=metadata,
            cwd=args.cwd,
        )
    finally:
        close_observation()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command while emitting connector observation events."
    )
    parser.add_argument("--connector", required=True)
    parser.add_argument("--from-layer", required=True)
    parser.add_argument("--to-layer", required=True)
    parser.add_argument("--input", action="append", default=[], help="Artifact as role=path.")
    parser.add_argument("--output", action="append", default=[], help="Artifact as role=path.")
    parser.add_argument("--metadata", action="append", default=[], help="Metadata as key=value.")
    parser.add_argument("--topology-out", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def parse_artifacts(items: list[str]) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for item in items:
        key, value = split_key_value(item, "--input/--output")
        if value:
            artifacts[key] = Path(value)
    return artifacts


def parse_metadata(items: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for item in items:
        key, value = split_key_value(item, "--metadata")
        metadata[key] = coerce_metadata_value(value)
    return metadata


def split_key_value(item: str, label: str) -> tuple[str, str]:
    if "=" not in item:
        raise SystemExit(f"{label} expects key=value, got {item!r}")
    key, value = item.split("=", 1)
    key = key.strip()
    if not key:
        raise SystemExit(f"{label} key must not be empty")
    return key, value


def coerce_metadata_value(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


if __name__ == "__main__":
    raise SystemExit(main())

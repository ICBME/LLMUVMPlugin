from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from harness_optimization.paths import (  # noqa: E402
    manifest_path_from_value,
    path_from_cwd,
    resolved_artifact_path,
    run_cwd,
    split_tool_command,
)


def test_path_helpers_resolve_relative_paths_from_cwd() -> None:
    cwd = Path("/tmp/project")

    assert path_from_cwd(Path("artifacts/output.json"), cwd) == Path(
        "/tmp/project/artifacts/output.json"
    )
    assert run_cwd(cwd) == Path("/tmp/project")
    assert split_tool_command("cargo +nightly", "cargo") == ["cargo", "+nightly"]


def test_manifest_and_artifact_helpers_ignore_blank_values() -> None:
    manifest_path = Path("/tmp/project/round_manifest.json")

    assert (
        manifest_path_from_value(
            "artifacts/output.json",
            manifest_path=manifest_path,
            cwd="",
        )
        == manifest_path.parent / "artifacts/output.json"
    )
    assert manifest_path_from_value(
        "artifacts/output.json",
        manifest_path=manifest_path,
        cwd=Path("run1"),
    ) == Path("run1/artifacts/output.json")
    assert resolved_artifact_path("", cwd=Path("/tmp/project")) is None
    assert resolved_artifact_path("artifacts/output.json", cwd=Path("/tmp/project")) == Path(
        "/tmp/project/artifacts/output.json"
    )
    assert resolved_artifact_path(
        "artifacts/output.json",
        cwd=Path("run1"),
    ) == Path("run1/artifacts/output.json")

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext
from fuzz_pipeline import (
    FuzzRunConfig,
    FuzzRunOrchestrator,
    RunStage,
    RunStageRegistry,
)


def test_run_stage_registry_builds_registered_stages_in_order() -> None:
    registry = RunStageRegistry(
        {
            "first": lambda: RunStage(
                name="first",
                handler=lambda _results: "one",
            ),
            "second": lambda: RunStage(
                name="second",
                handler=lambda results: f"{results['first']}-two",
            ),
        }
    )

    stages = registry.build_many(("first", "second"))

    assert [stage.name for stage in stages] == ["first", "second"]
    assert registry.names() == ("first", "second")


def test_run_stage_registry_rejects_duplicate_and_unknown_stage_names() -> None:
    registry = RunStageRegistry()
    registry.register("stage", lambda: RunStage(name="stage", handler=lambda _r: None))

    try:
        registry.register("stage", lambda: RunStage(name="stage", handler=lambda _r: None))
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate run stage registration should fail")

    try:
        registry.build("missing")
    except ValueError as exc:
        assert "unknown run stage" in str(exc)
    else:
        raise AssertionError("unknown run stage should fail")


def test_run_orchestrator_can_insert_custom_stage_via_registry() -> None:
    calls: list[str] = []

    class StubRun(FuzzRunOrchestrator):
        def generate_corpus(self):
            calls.append("generate")
            return "generated"

        def validate_corpus(self):
            calls.append("validate")
            return ["case"]

    with tempfile.TemporaryDirectory() as tmp:
        run = StubRun(
            FuzzRunConfig(
                target="demo",
                corpus=Path("corpus.jsonl"),
                libafl_manifest=Path("Cargo.toml"),
                cwd=Path(tmp),
            ),
            ObservationContext(),
        )

        def custom_stage() -> RunStage:
            return RunStage(
                name="custom_eval",
                handler=lambda results: calls.append(
                    f"custom:{results['corpus_generation']}"
                )
                or {"ok": True},
            )

        run.register_run_stage("custom_eval", custom_stage)
        run.set_run_plan_stage_names(
            "generate_and_validate",
            ("corpus_generation", "custom_eval", "corpus_validation"),
        )

        cases = run.generate_and_validate()

    assert cases == ["case"]
    assert calls == ["generate", "custom:generated", "validate"]

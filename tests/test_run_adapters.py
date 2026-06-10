from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext
from fuzz_pipeline import FuzzRunConfig
from fuzz_pipeline.run_adapters import (
    CorpusGeneratorAdapter,
    RunPathResolver,
    UvmReplayAdapter,
)


def test_run_adapters_build_commands_and_resolve_derived_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = FuzzRunConfig(
            target="demo",
            target_config=Path("demo.toml"),
            corpus=Path("corpus.jsonl"),
            libafl_manifest=Path("Cargo.toml"),
            directives=Path("seed_directives.json"),
            iters=7,
            max_seeds=3,
            seed=11,
            cargo="cargo +nightly",
            cwd=root,
            make="make -j2",
            verilog_sources="rtl/a.sv",
            toplevel="tb_top",
            coverage_dir=Path("cov"),
            coverage_dat=Path("cov/coverage.dat"),
            coverage_info=Path("cov/coverage.info"),
            coverage_annotated=Path("cov/annotated"),
            functional_coverage=Path("cov/functional.json"),
            directives_out=Path("feedback/directives.json"),
            feedback_corpus=Path("feedback/corpus.jsonl"),
            extra_make_vars=("EXTRA=1",),
            observation_out=Path("observe/events.jsonl"),
            monitoring_out=Path("observe/monitor.json"),
            topology_out=Path("observe/topology.json"),
        )
        paths = RunPathResolver(config)

        def add_artifact(role: str, path: Path | None) -> None:
            resolved = paths.path_from_cwd(path)
            assert resolved is not None
            paths.artifacts[role] = resolved

        add_artifact("corpus", config.corpus)
        add_artifact("target_manifest", config.target_config)
        add_artifact("directives", config.directives)
        add_artifact("replay_artifacts", config.coverage_dir)
        add_artifact("rtl_coverage_dat", config.coverage_dat)
        add_artifact("functional_coverage", config.functional_coverage)
        feedback_functional_coverage = paths.feedback_functional_coverage()
        assert feedback_functional_coverage is not None
        paths.artifacts["feedback_functional_coverage"] = feedback_functional_coverage

        generator = CorpusGeneratorAdapter(config, paths)
        replay = UvmReplayAdapter(
            config,
            paths,
            ObservationContext(run_id="run-1", parent_event_id="event-1"),
            round_id=lambda: "round-1",
        )

        generator_command = generator.default_command()
        assert generator_command[:4] == ("cargo", "+nightly", "run", "--quiet")
        assert str(root / "Cargo.toml") in generator_command
        assert str(root / "demo.toml") in generator_command
        assert str(root / "corpus.jsonl") in generator_command
        assert str(root / "seed_directives.json") in generator_command

        feedback_generator_command = generator.feedback_command()
        assert str(root / "feedback/corpus.jsonl") in feedback_generator_command
        assert str(root / "feedback/directives.json") in feedback_generator_command

        coverage_command = replay.coverage_command()
        assert coverage_command[:4] == ["make", "-j2", "-C", str(root)]
        assert "RTL_COVERAGE=1" in coverage_command
        assert f"FUZZ_DIRECTIVES={root / 'seed_directives.json'}" in coverage_command
        assert (
            f"UVM_FUNCTIONAL_COVERAGE_OUT={root / 'cov/functional.json'}"
            in coverage_command
        )
        assert "CONNECTOR_OBSERVE_RUN_ID=run-1" in coverage_command
        assert "CONNECTOR_OBSERVE_ROUND_ID=round-1" in coverage_command
        assert "CONNECTOR_OBSERVE_STAGE_ID=coverage_run" in coverage_command
        assert "CONNECTOR_OBSERVE_PARENT_EVENT_ID=event-1" in coverage_command
        assert "EXTRA=1" in coverage_command
        assert coverage_command[-1] == "sim"

        feedback_command = replay.feedback_command()
        assert "RTL_COVERAGE=1" not in feedback_command
        assert f"FUZZ_DIRECTIVES={root / 'feedback/directives.json'}" in feedback_command
        assert f"FUZZ_CORPUS={root / 'feedback/corpus.jsonl'}" in feedback_command
        assert (
            f"UVM_FUNCTIONAL_COVERAGE_OUT={root / 'cov/functional_feedback.json'}"
            in feedback_command
        )
        assert "CONNECTOR_OBSERVE_STAGE_ID=feedback_replay" in feedback_command

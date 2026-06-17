from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import JsonlObserver, ObservationContext  # noqa: E402
from fuzz_bfm.target_config import TargetConfig  # noqa: E402
from fuzz_pipeline.generated_plugins import (  # noqa: E402
    GeneratedPluginBundle,
    apply_manifest_overlay,
    build_manifest_overlay,
    build_plugin_registry,
    validate_generated_plugin_bundle,
)
from fuzz_pipeline.orchestrator import PipelineContext  # noqa: E402
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator  # noqa: E402
from fuzz_pipeline.topology import FULL_FUZZ_TOPOLOGY, GENERATED_PLUGIN_TOPOLOGY  # noqa: E402


def test_generated_plugin_topology_exposes_contract_pipeline() -> None:
    connector_names = {connector.name for connector in GENERATED_PLUGIN_TOPOLOGY.connectors}
    full_connector_names = {connector.name for connector in FULL_FUZZ_TOPOLOGY.connectors}

    assert "generation_context_to_artifact_bundle" in connector_names
    assert "artifact_bundle_to_contract_validation" in connector_names
    assert "contract_validation_to_plugin_registry" in connector_names
    assert "plugin_registry_to_manifest_overlay" in connector_names
    assert "manifest_overlay_to_target_manifest" in connector_names
    assert "manifest_to_comparator" in full_connector_names
    assert "comparator_to_scoreboard" in full_connector_names


def test_generated_plugin_bundle_validates_and_overlays_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_demo_plugins(tmp_path / "generated_demo.py")
        sys.path.insert(0, str(tmp_path))
        try:
            config = TargetConfig(
                name="demo",
                driver="demo_driver:Driver",
                path=tmp_path / "demo.toml",
            )
            bundle = GeneratedPluginBundle(
                target="demo",
                plugins={
                    "ref_model": "generated_demo:RefModel",
                    "comparator": "generated_demo:Comparator",
                    "scoreboard": "fuzz_uvm.scoreboards:ResultScoreboard",
                    "coverage_model": "generated_demo:CoverageModel",
                },
            )

            report = validate_generated_plugin_bundle(bundle, config=config)
            registry = build_plugin_registry(report)
            overlay = build_manifest_overlay(registry)
            active = apply_manifest_overlay(config, overlay)

            assert report.valid is True
            assert registry.plugins["ref_model"] == "generated_demo:RefModel"
            assert active.ref_model == "generated_demo:RefModel"
            assert active.comparator == "generated_demo:Comparator"
            assert active.scoreboard == "fuzz_uvm.scoreboards:ResultScoreboard"
            assert active.coverage_model == "generated_demo:CoverageModel"
        finally:
            sys.path.remove(str(tmp_path))


def test_generated_plugin_bundle_rejects_empty_plugin_set() -> None:
    report = validate_generated_plugin_bundle(
        {"target": "demo", "plugins": {}},
        import_plugins=False,
    )

    assert report.valid is False
    assert report.issues[0].role == "plugins"


def test_replay_orchestrator_runs_generated_plugin_connector_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = root / "demo.toml"
        manifest.write_text('name = "demo"\ndriver = "demo_driver:Driver"\n')
        events_out = root / "events.jsonl"
        observer = JsonlObserver(events_out)
        config = TargetConfig(name="demo", driver="demo_driver:Driver", path=manifest)
        pipeline = ReplayPipelineOrchestrator(
            observation_context=ObservationContext(run_id="generated-run", observer=observer),
            pipeline_context=PipelineContext(metadata={"target": "demo"}),
        )
        bundle = GeneratedPluginBundle(
            target="demo",
            plugins={
                "ref_model": "generated_demo:RefModel",
                "comparator": "generated_demo:Comparator",
            },
        )

        published = pipeline.publish_generated_plugin_bundle(bundle)
        report = pipeline.validate_generated_plugins(
            published,
            config=config,
            import_plugins=False,
        )
        registry = pipeline.register_generated_plugins(report)
        overlay = pipeline.build_generated_manifest_overlay(registry)
        active = pipeline.activate_generated_manifest(config, overlay)
        comparator = pipeline.build_comparator(active, lambda: "comparator")
        pipeline.attach_comparator_to_scoreboard(
            lambda: None,
            comparator=active.comparator,
            scoreboard=active.scoreboard,
        )
        observer.close()
        events = [json.loads(line) for line in events_out.read_text().splitlines()]

    finished = {
        event["connector"]
        for event in events
        if event["event_type"] == "connector.finished"
    }
    assert comparator == "comparator"
    assert active.ref_model == "generated_demo:RefModel"
    assert active.comparator == "generated_demo:Comparator"
    assert pipeline.context.values["plugin_registry"].plugins == registry.plugins
    assert "generation_context_to_artifact_bundle" in finished
    assert "artifact_bundle_to_contract_validation" in finished
    assert "contract_validation_to_plugin_registry" in finished
    assert "plugin_registry_to_manifest_overlay" in finished
    assert "manifest_overlay_to_target_manifest" in finished
    assert "manifest_to_comparator" in finished
    assert "comparator_to_scoreboard" in finished


def _write_demo_plugins(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "class RefModel:",
                "    def __init__(self, target=None, config=None):",
                "        self.target = target",
                "",
                "    def predict(self, case):",
                "        return {'expected': case.data.get('value')}",
                "",
                "",
                "class Comparator:",
                "    def __init__(self, target=None, config=None):",
                "        self.target = target",
                "",
                "    def compare(self, actual, expected, record):",
                "        return {'passed': actual == expected}",
                "",
                "",
                "class CoverageModel:",
                "    def __init__(self, target=None, config=None):",
                "        self.target = target",
                "",
                "    def sample(self, case):",
                "        return None",
                "",
                "    def sample_record(self, record):",
                "        return None",
                "",
                "    def to_json(self):",
                "        return {'target': self.target, 'total_cases': 0}",
                "",
            ]
        )
    )

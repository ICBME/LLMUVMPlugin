from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext  # noqa: E402
from connector_observe.trace import read_jsonl_events, trace_quality  # noqa: E402
from fuzz_pipeline.harness_trace import (  # noqa: E402
    HarnessTraceBuilder,
    HarnessTraceOutputs,
)
from fuzz_pipeline.run_adapters import RunPathResolver  # noqa: E402
from fuzz_pipeline.run_evaluation import (  # noqa: E402
    CampaignEvaluationAdapter,
    RunEvaluationAdapter,
)


@dataclass(frozen=True)
class EvalConfig:
    target: str
    mode: str | None
    round_id: str | None
    evaluation_out: Path | None
    observation_out: Path | None
    monitoring_out: Path | None
    cwd: Path | None = None


def test_harness_trace_builder_writes_records_evaluation_and_llm_dataset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        monitoring = root / "monitor.json"
        manifest = root / "round_manifest.json"
        outputs = HarnessTraceOutputs.from_evaluation_path(root / "round_eval.json")
        _write_events(events)
        monitoring.write_text('{"event_count": 4}\n', encoding="utf-8")
        manifest.write_text(
            json.dumps(
                {
                    "target": "demo",
                    "mode": "feedback_fuzz",
                    "round_id": "round-1",
                    "run_id": "run-1",
                    "coverage": {"uncovered_line_count": 3},
                    "feedback": {"directive_count": 2},
                    "artifacts": {"coverage_summary": str(root / "summary.json")},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = HarnessTraceBuilder(
            observation_events=events,
            monitoring=monitoring,
            round_manifest=manifest,
        ).write(outputs)
        records = [
            json.loads(line)
            for line in outputs.records.read_text(encoding="utf-8").splitlines()
        ]
        evaluation = json.loads(outputs.evaluation.read_text(encoding="utf-8"))
        llm_dataset = [
            json.loads(line)
            for line in outputs.llm_dataset.read_text(encoding="utf-8").splitlines()
        ]

    assert result.evaluation["summary"]["record_count"] == 2
    assert len(records) == 2
    assert records[0]["kind"] == "libafl_bfm_fuzz.harness_execution_record"
    assert records[0]["span_id"] == "span-ref-model"
    assert records[0]["case_id"] == "case-0"
    assert records[0]["directive_id"] == "dir-a"
    assert records[0]["corpus_sha256"] == "corpus-hash"
    assert records[0]["case_index"] == 0
    assert evaluation["summary"]["failed_record_count"] == 1
    assert evaluation["summary"]["hanging_span_count"] == 1
    assert evaluation["summary"]["monitor_event_count"] == 4
    assert evaluation["coverage"]["uncovered_line_count"] == 3
    assert evaluation["failure_clusters"][0]["connector"] == "case_to_dut"
    assert evaluation["trace_quality"]["hanging_spans"][0]["span_id"] == "span-hanging"
    assert evaluation["case_summary"][0]["case_id"] == "case-0"
    assert evaluation["case_summary"][0]["failed_record_count"] == 1
    assert evaluation["directive_summary"][0]["directive_id"] == "dir-a"
    assert evaluation["directive_summary"][0]["failed_record_count"] == 1
    assert llm_dataset[0]["kind"] == "libafl_bfm_fuzz.llm_harness_optimization_sample"
    assert llm_dataset[0]["connector"] == "case_to_dut"
    assert llm_dataset[0]["span_id"] == "span-dut"
    assert llm_dataset[0]["case_id"] == "case-0"
    assert llm_dataset[0]["directive_id"] == "dir-a"


def test_round_evaluation_attaches_harness_trace_when_events_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        monitoring = root / "monitor.json"
        manifest_path = root / "round_manifest.json"
        evaluation_out = root / "round_evaluation.json"
        _write_events(events, include_round_evaluation_started=True)
        monitoring.write_text('{"event_count": 4}\n', encoding="utf-8")
        manifest = {
            "target": "demo",
            "mode": "feedback_fuzz",
            "round_id": "round-1",
            "run_id": "run-1",
            "artifacts": {
                "observation_events": str(events),
                "monitoring": str(monitoring),
                "round_manifest": str(manifest_path),
            },
            "coverage": {"covered_line_count": 7},
            "feedback": {"directive_count": 1},
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        config = EvalConfig(
            target="demo",
            mode="feedback_fuzz",
            round_id="round-1",
            evaluation_out=evaluation_out,
            observation_out=events,
            monitoring_out=monitoring,
        )
        paths = RunPathResolver(config, artifacts={"round_manifest": manifest_path})
        adapter = RunEvaluationAdapter(
            config,
            paths,
            ObservationContext(run_id="run-1"),
            round_id=lambda: "round-1",
        )

        payload = adapter.run_round({"round_manifest": manifest})
        written = json.loads(evaluation_out.read_text(encoding="utf-8"))
        trace_records = evaluation_out.with_name(
            "round_evaluation_harness_records.jsonl"
        )
        trace_eval = evaluation_out.with_name(
            "round_evaluation_harness_evaluation.json"
        )
        trace_dataset = evaluation_out.with_name(
            "round_evaluation_llm_dataset.jsonl"
        )

        assert payload["harness_trace"]["status"] == "ok"
        assert payload["harness_trace"]["summary"]["record_count"] == 2
        assert payload["harness_trace"]["summary"]["hanging_span_count"] == 1
        assert written["harness_trace"]["summary"]["failed_record_count"] == 1
        assert trace_records.exists()
        assert trace_eval.exists()
        assert trace_dataset.exists()
        trace_evaluation = json.loads(trace_eval.read_text(encoding="utf-8"))
        assert trace_evaluation["trace_quality"]["ignored_hanging_span_count"] == 1
        assert trace_evaluation["trace_quality"]["ignored_hanging_spans"][0][
            "connector"
        ] == "round_artifacts_to_evaluation"


def test_campaign_evaluation_attaches_campaign_trace_rollup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "campaign_events.jsonl"
        monitoring = root / "monitor.json"
        manifest_path = root / "campaign_manifest.json"
        evaluation_out = root / "campaign_evaluation.json"
        _write_campaign_events(events)
        monitoring.write_text('{"event_count": 4}\n', encoding="utf-8")
        manifest = {
            "target": "demo",
            "run_id": "run-1",
            "artifacts": {
                "observation_events": str(events),
                "monitoring": str(monitoring),
                "campaign_manifest": str(manifest_path),
            },
            "modes": [
                {
                    "mode": "heuristic_feedback",
                    "rounds": [
                        {
                            "index": 0,
                            "round_id": "round-0",
                            "round_manifest": str(root / "round_0.json"),
                            "coverage": {"uncovered_line_count": 8},
                            "feedback": {"directive_count": 1},
                        },
                        {
                            "index": 1,
                            "round_id": "round-1",
                            "round_manifest": str(root / "round_1.json"),
                            "coverage": {"uncovered_line_count": 5},
                            "feedback": {"directive_count": 1},
                        },
                    ],
                }
            ],
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        adapter = CampaignEvaluationAdapter(
            target="demo",
            path=evaluation_out,
            observation_context=ObservationContext(run_id="run-1"),
            cwd=root,
        )

        payload = adapter.run(manifest)
        rollup_path = evaluation_out.with_name(
            "campaign_evaluation_campaign_rollup.json"
        )
        rollup = json.loads(rollup_path.read_text(encoding="utf-8"))

    assert payload["harness_trace"]["status"] == "ok"
    assert payload["harness_trace"]["artifacts"]["campaign_trace_rollup"] == str(
        rollup_path
    )
    assert payload["harness_trace"]["campaign_rollup"]["summary"]["round_count"] == 2
    assert rollup["summary"]["record_count"] == 2
    assert rollup["summary"]["failed_record_count"] == 1
    assert rollup["rounds"][1]["coverage_delta"]["uncovered_line_count"] == -3
    assert rollup["directive_effectiveness"][0]["directive_id"] == "dir-a"
    assert rollup["directive_effectiveness"][0]["failed_record_count"] == 1
    assert rollup["case_effectiveness"][0]["case_key"] == "case-0"
    assert rollup["failure_trends"][1]["connectors"][0]["connector"] == "case_to_dut"


def test_trace_quality_core_can_be_used_without_harness_analysis() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        events.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "event_type": "connector.started",
                            "connector": "probe",
                            "from_layer": "a",
                            "to_layer": "b",
                            "span_id": "span-a",
                        }
                    ),
                    "not-json",
                    json.dumps(
                        {
                            "event_type": "connector.finished",
                            "connector": "probe",
                            "from_layer": "a",
                            "to_layer": "b",
                            "span_id": "span-b",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        parsed, malformed = read_jsonl_events(events)
        quality = trace_quality(parsed, malformed)

    assert malformed == 1
    assert quality["event_count"] == 2
    assert quality["hanging_span_count"] == 1
    assert quality["orphan_final_count"] == 1


def test_harness_trace_builder_accepts_custom_analysis_layers() -> None:
    class CustomProjector:
        def project(
            self,
            events: Iterable[tuple[int, dict[str, Any]]],
            *,
            round_manifest: dict[str, Any],
            campaign_manifest: dict[str, Any],
        ) -> list[dict[str, Any]]:
            return [
                {
                    "kind": "custom.record",
                    "connector": event.get("connector"),
                    "duration_ms": 1.0,
                    "status": "ok",
                }
                for _line_no, event in events
            ]

    class CustomAnalyzer:
        def __init__(self) -> None:
            self.trace_quality: dict[str, object] | None = None

        def analyze(
            self,
            records: list[dict[str, Any]],
            **kwargs: Any,
        ) -> dict[str, Any]:
            self.trace_quality = kwargs["trace_quality"]
            return {
                "kind": "custom.harness_evaluation",
                "summary": {"record_count": len(records)},
                "manifest_artifacts": {"custom": "artifact"},
            }

    class CustomDatasetBuilder:
        def build(
            self,
            records: list[dict[str, Any]],
            evaluation: dict[str, Any],
        ) -> list[dict[str, Any]]:
            return [
                {
                    "kind": "custom.llm_dataset",
                    "record_count": len(records),
                    "evaluation_kind": evaluation["kind"],
                }
            ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        _write_events(events)
        analyzer = CustomAnalyzer()

        result = HarnessTraceBuilder(
            observation_events=events,
            record_projector=CustomProjector(),
            analyzer=analyzer,
            llm_dataset_builder=CustomDatasetBuilder(),
        ).build()

    assert analyzer.trace_quality is not None
    assert analyzer.trace_quality["hanging_span_count"] == 1
    assert result.evaluation["kind"] == "custom.harness_evaluation"
    assert result.evaluation["summary"]["record_count"] == 2
    assert result.records[0]["kind"] == "custom.record"
    assert result.llm_dataset == [
        {
            "kind": "custom.llm_dataset",
            "record_count": 2,
            "evaluation_kind": "custom.harness_evaluation",
        }
    ]


def test_harness_trace_builder_accepts_custom_metadata_extractor() -> None:
    class ExplicitOnlyMetadataExtractor:
        def extract(self, metadata: dict[str, Any]) -> dict[str, Any]:
            return {
                "case_index": metadata.get("index"),
                "case_id": metadata.get("case_id"),
                "directive_id": metadata.get("directive_id"),
                "corpus_sha256": metadata.get("corpus_sha256"),
            }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        event = {
            "schema_version": 1,
            "event_type": "connector.finished",
            "event_id": "finished-origin-only",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-origin-only",
            "timestamp_ns": 1,
            "duration_ms": 1.0,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "dut_execute",
                "index": 3,
                "case_id": "case-origin-only",
                "origin": "seed-origin",
            },
            "status": "ok",
        }
        events.write_text(json.dumps(event) + "\n", encoding="utf-8")

        default_result = HarnessTraceBuilder(observation_events=events).build()
        custom_result = HarnessTraceBuilder(
            observation_events=events,
            metadata_extractor=ExplicitOnlyMetadataExtractor(),
        ).build()

    assert default_result.records[0]["directive_id"] == "seed-origin"
    assert custom_result.records[0]["directive_id"] is None
    assert custom_result.records[0]["case_index"] == 3
    assert custom_result.records[0]["case_id"] == "case-origin-only"


def _write_events(
    path: Path,
    *,
    include_round_evaluation_started: bool = False,
) -> None:
    events = [
        {
            "schema_version": 1,
            "event_type": "connector.started",
            "event_id": "started-ref",
            "connector": "case_to_ref_model",
            "from_layer": "replay_driver",
            "to_layer": "ref_model",
            "run_id": "run-1",
            "span_id": "span-ref-model",
            "timestamp_ns": 1,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "ref_model_predict",
                "index": 0,
                "case_id": "case-0",
                "directive_id": "dir-a",
                "corpus_sha256": "corpus-hash",
            },
            "status": "running",
        },
        {
            "schema_version": 1,
            "event_type": "connector.finished",
            "event_id": "finished-ref",
            "connector": "case_to_ref_model",
            "from_layer": "replay_driver",
            "to_layer": "ref_model",
            "run_id": "run-1",
            "span_id": "span-ref-model",
            "timestamp_ns": 2,
            "duration_ms": 1.5,
            "inputs": [],
            "outputs": [],
            "metrics": {"has_expected": True},
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "ref_model_predict",
                "index": 0,
                "case_id": "case-0",
                "directive_id": "dir-a",
                "corpus_sha256": "corpus-hash",
            },
            "status": "ok",
        },
        {
            "schema_version": 1,
            "event_type": "connector.started",
            "event_id": "started-dut",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-dut",
            "timestamp_ns": 3,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "dut_execute",
                "case_index": 0,
                "case_id": "case-0",
                "directive_id": "dir-a",
                "corpus_sha256": "corpus-hash",
            },
            "status": "running",
        },
        {
            "schema_version": 1,
            "event_type": "connector.failed",
            "event_id": "failed",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-dut",
            "timestamp_ns": 4,
            "duration_ms": 3.25,
            "inputs": [],
            "outputs": [],
            "metrics": {},
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "dut_execute",
                "case_index": 0,
                "case_id": "case-0",
                "directive_id": "dir-a",
                "corpus_sha256": "corpus-hash",
            },
            "status": "failed",
            "error": {"type": "AssertionError", "message": "mismatch"},
        },
        {
            "schema_version": 1,
            "event_type": "connector.started",
            "event_id": "started-hanging",
            "connector": "driver_to_scoreboard",
            "from_layer": "replay_driver",
            "to_layer": "scoreboard",
            "run_id": "run-1",
            "span_id": "span-hanging",
            "timestamp_ns": 5,
            "inputs": [],
            "outputs": [],
            "metadata": {"step": "scoreboard_write"},
            "status": "running",
        },
    ]
    if include_round_evaluation_started:
        events.append(
            {
                "schema_version": 1,
                "event_type": "connector.started",
                "event_id": "started-round-eval",
                "connector": "round_artifacts_to_evaluation",
                "from_layer": "round_manifest",
                "to_layer": "evaluation_report",
                "run_id": "run-1",
                "span_id": "span-round-eval",
                "timestamp_ns": 6,
                "inputs": [],
                "outputs": [],
                "metadata": {"step": "round_evaluation"},
                "status": "running",
            }
        )
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _write_campaign_events(path: Path) -> None:
    events = [
        {
            "schema_version": 1,
            "event_type": "connector.started",
            "event_id": "started-round-0",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-round-0",
            "timestamp_ns": 1,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-0",
                "step": "dut_execute",
                "case_id": "case-0",
                "directive_id": "dir-a",
            },
            "status": "running",
        },
        {
            "schema_version": 1,
            "event_type": "connector.finished",
            "event_id": "finished-round-0",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-round-0",
            "timestamp_ns": 2,
            "duration_ms": 1.0,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-0",
                "step": "dut_execute",
                "case_id": "case-0",
                "directive_id": "dir-a",
            },
            "status": "ok",
        },
        {
            "schema_version": 1,
            "event_type": "connector.started",
            "event_id": "started-round-1",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-round-1",
            "timestamp_ns": 3,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "dut_execute",
                "case_id": "case-1",
                "directive_id": "dir-a",
            },
            "status": "running",
        },
        {
            "schema_version": 1,
            "event_type": "connector.failed",
            "event_id": "failed-round-1",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "span_id": "span-round-1",
            "timestamp_ns": 4,
            "duration_ms": 2.0,
            "inputs": [],
            "outputs": [],
            "metadata": {
                "target": "demo",
                "mode": "feedback_fuzz",
                "round_id": "round-1",
                "step": "dut_execute",
                "case_id": "case-1",
                "directive_id": "dir-a",
            },
            "status": "failed",
            "error": {"type": "AssertionError", "message": "mismatch"},
        },
    ]
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

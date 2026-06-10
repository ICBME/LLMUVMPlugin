from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext  # noqa: E402
from fuzz_pipeline.harness_trace import (  # noqa: E402
    HarnessTraceBuilder,
    HarnessTraceOutputs,
)
from fuzz_pipeline.run_adapters import RunPathResolver  # noqa: E402
from fuzz_pipeline.run_evaluation import RunEvaluationAdapter  # noqa: E402


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
    assert records[0]["case_index"] == 0
    assert evaluation["summary"]["failed_record_count"] == 1
    assert evaluation["summary"]["monitor_event_count"] == 4
    assert evaluation["coverage"]["uncovered_line_count"] == 3
    assert evaluation["failure_clusters"][0]["connector"] == "case_to_dut"
    assert llm_dataset[0]["kind"] == "libafl_bfm_fuzz.llm_harness_optimization_sample"
    assert llm_dataset[0]["connector"] == "case_to_dut"


def test_round_evaluation_attaches_harness_trace_when_events_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        monitoring = root / "monitor.json"
        manifest_path = root / "round_manifest.json"
        evaluation_out = root / "round_evaluation.json"
        _write_events(events)
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
        assert written["harness_trace"]["summary"]["failed_record_count"] == 1
        assert trace_records.exists()
        assert trace_eval.exists()
        assert trace_dataset.exists()


def _write_events(path: Path) -> None:
    events = [
        {
            "schema_version": 1,
            "event_type": "connector.finished",
            "event_id": "started-ignored",
            "connector": "case_to_ref_model",
            "from_layer": "replay_driver",
            "to_layer": "ref_model",
            "run_id": "run-1",
            "timestamp_ns": 1,
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
            },
            "status": "ok",
        },
        {
            "schema_version": 1,
            "event_type": "connector.failed",
            "event_id": "failed",
            "connector": "case_to_dut",
            "from_layer": "replay_driver",
            "to_layer": "dut",
            "run_id": "run-1",
            "timestamp_ns": 2,
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
            },
            "status": "failed",
            "error": {"type": "AssertionError", "message": "mismatch"},
        },
    ]
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

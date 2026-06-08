import json
import os
import sys
import tempfile
import unittest
import asyncio
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import (  # noqa: E402
    Connector,
    JsonlObserver,
    MonitoringObserver,
    ObservationContext,
    observer_from_env,
)
from connector_observe.schema import normalize_artifact_refs  # noqa: E402
from fuzz_bfm.bfm_base import ReplayResult  # noqa: E402
from fuzz_bfm.corpus import FuzzCase  # noqa: E402
from fuzz_bfm.target_config import TargetConfig  # noqa: E402
from fuzz_feedback import cli as feedback_cli  # noqa: E402
from fuzz_pipeline.harness import _reset_observation_context_for_tests, run_command  # noqa: E402
from fuzz_pipeline.orchestrator import (  # noqa: E402
    PipelineContext,
    PipelineOrchestrator,
    StepPolicy,
    StepSpec,
)
from fuzz_pipeline.replay_orchestrator import ReplayPipelineOrchestrator  # noqa: E402
from fuzz_pipeline.run_orchestrator import FuzzRunConfig, FuzzRunOrchestrator  # noqa: E402
from fuzz_pipeline.topology import ComponentNode, ConnectorEdge, PipelineTopology  # noqa: E402
from fuzz_uvm.context import ReplayContext  # noqa: E402
from fuzz_uvm.transactions import ReplayRecord  # noqa: E402


class FailingObserver:
    def on_event(self, event):
        raise RuntimeError("observer failed")

    def flush(self):
        raise RuntimeError("observer failed")

    def close(self):
        raise RuntimeError("observer failed")


def _write_and_return(path: Path, text: str, result):
    path.write_text(text)
    return result


class TestConnectorObserve(unittest.TestCase):
    def test_orchestrator_runs_step_and_records_trace_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input.txt"
            input_path.write_text("in")
            output_path = root / "output.txt"
            observation_out = root / "events.jsonl"
            topology_out = root / "topology.json"
            observer = JsonlObserver(observation_out)
            topology = PipelineTopology(
                name="demo_pipeline",
                components=(
                    ComponentNode("a", "source"),
                    ComponentNode("b", "sink"),
                ),
                connectors=(
                    ConnectorEdge(
                        "a_to_b",
                        "a",
                        "b",
                        input_roles=("input",),
                        output_roles=("output",),
                    ),
                ),
            )
            context = PipelineContext(
                artifacts={"input": input_path, "output": output_path},
                metadata={"target": "demo"},
            )
            orchestrator = PipelineOrchestrator(
                topology,
                ObservationContext(
                    run_id="run-1",
                    round_id="round-1",
                    stage_id="stage-1",
                    parent_event_id="parent-1",
                    observer=observer,
                ),
                topology_out=topology_out,
            )

            orchestrator.run(
                [
                    StepSpec(
                        name="work",
                        connector="a_to_b",
                        handler=lambda _context: _write_and_return(output_path, "out", "ok"),
                        input_roles=("input",),
                        output_roles=("output",),
                        metrics=lambda result: {"result": result},
                    )
                ],
                context,
            )
            observer.close()
            events = [json.loads(line) for line in observation_out.read_text().splitlines()]
            topology_json = json.loads(topology_out.read_text())
            output_text = output_path.read_text()

        self.assertEqual(context.values["work"], "ok")
        self.assertEqual(output_text, "out")
        self.assertEqual(events[-1]["connector"], "a_to_b")
        self.assertEqual(events[-1]["metadata"]["round_id"], "round-1")
        self.assertEqual(events[-1]["metadata"]["stage_id"], "stage-1")
        self.assertEqual(events[-1]["metadata"]["parent_event_id"], "parent-1")
        self.assertEqual(events[-1]["metadata"]["target"], "demo")
        self.assertEqual(topology_json["name"], "demo_pipeline")

    def test_orchestrator_validates_connector_and_roles(self):
        topology = PipelineTopology(
            name="demo_pipeline",
            components=(ComponentNode("a", "source"), ComponentNode("b", "sink")),
            connectors=(ConnectorEdge("a_to_b", "a", "b", input_roles=("input",)),),
        )
        orchestrator = PipelineOrchestrator(topology)

        with self.assertRaisesRegex(ValueError, "unknown connector"):
            orchestrator.run_step(
                StepSpec("missing", "missing", lambda _context: "ok"),
                PipelineContext(),
            )

        with self.assertRaisesRegex(ValueError, "required input role"):
            orchestrator.run_step(
                StepSpec("bad_roles", "a_to_b", lambda _context: "ok"),
                PipelineContext(),
            )

    def test_orchestrator_can_continue_when_step_error_policy_allows(self):
        topology = PipelineTopology(
            name="demo_pipeline",
            components=(ComponentNode("a", "source"), ComponentNode("b", "sink")),
            connectors=(ConnectorEdge("a_to_b", "a", "b"),),
        )
        context = PipelineContext()

        result = PipelineOrchestrator(topology).run_step(
            StepSpec(
                "optional_failure",
                "a_to_b",
                lambda _context: (_ for _ in ()).throw(ValueError("step failed")),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            context,
        )

        self.assertIsNone(result)
        self.assertEqual(context.metadata["step_errors"][0]["step"], "optional_failure")

    def test_orchestrator_run_async_records_result(self):
        async def work(_context):
            return "async-ok"

        topology = PipelineTopology(
            name="demo_pipeline",
            components=(ComponentNode("a", "source"), ComponentNode("b", "sink")),
            connectors=(ConnectorEdge("a_to_b", "a", "b"),),
        )
        context = PipelineContext()

        asyncio.run(
            PipelineOrchestrator(topology).run_async(
                [StepSpec("async_work", "a_to_b", work, async_step=True)],
                context,
            )
        )

        self.assertEqual(context.values["async_work"], "async-ok")

    def test_orchestrator_sync_run_rejects_async_step(self):
        topology = PipelineTopology(
            name="demo_pipeline",
            components=(ComponentNode("a", "source"), ComponentNode("b", "sink")),
            connectors=(ConnectorEdge("a_to_b", "a", "b"),),
        )

        with self.assertRaisesRegex(ValueError, "marked async"):
            PipelineOrchestrator(topology).run_step(
                StepSpec("async_work", "a_to_b", lambda _context: "ok", async_step=True),
                PipelineContext(),
            )

    def test_orchestrator_sync_timeout_returns_without_waiting_for_handler(self):
        topology = PipelineTopology(
            name="demo_pipeline",
            components=(ComponentNode("a", "source"), ComponentNode("b", "sink")),
            connectors=(ConnectorEdge("a_to_b", "a", "b"),),
        )

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            PipelineOrchestrator(topology).run_step(
                StepSpec(
                    "slow_work",
                    "a_to_b",
                    lambda _context: time.sleep(0.4),
                    policy=StepPolicy(timeout_s=0.01),
                ),
                PipelineContext(),
            )

        self.assertLess(time.monotonic() - started, 0.3)

    def test_replay_pipeline_orchestrator_runs_replay_steps(self):
        async def execute_case():
            return ReplayResult(actual="ok", detail="demo")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "demo.toml"
            manifest.write_text('name = "demo"\ndriver = "demo_driver:Driver"\n')
            corpus = root / "corpus.jsonl"
            corpus.write_text('{"target":"demo","origin":"seed"}\n')
            coverage_out = root / "functional.json"
            events_out = root / "events.jsonl"
            topology_out = root / "topology.json"
            observer = JsonlObserver(events_out)
            config = TargetConfig(name="demo", driver="demo_driver:Driver", path=manifest)
            case = FuzzCase(target="demo", data={"origin": "seed"}, line_no=1)
            record = ReplayRecord(0, case, result=ReplayResult(actual="ok", expected="ok"))
            pipeline = ReplayPipelineOrchestrator(
                observation_context=ObservationContext(run_id="replay-run", observer=observer),
                pipeline_context=PipelineContext(
                    artifacts={"corpus": corpus},
                    metadata={"target": "demo"},
                ),
                topology_out=topology_out,
            )

            driver = pipeline.build_replay_driver(config, lambda: "driver")
            result = asyncio.run(pipeline.execute_case(execute_case, case, index=0))
            pipeline.scoreboard_write(
                lambda: None,
                record,
                summary=lambda: {"checked": 1, "failures": 0},
            )
            coverage_summary = {"target": "demo", "total_cases": 1, "coverage": {"covered": 1, "total": 1, "percent": 100.0}}
            exported = pipeline.coverage_export(
                lambda: _write_and_return(
                    coverage_out,
                    json.dumps(coverage_summary),
                    coverage_summary,
                ),
                output_path=coverage_out,
            )
            observer.close()
            events = [json.loads(line) for line in events_out.read_text().splitlines()]
            topology_json = json.loads(topology_out.read_text())
            coverage_exists = coverage_out.exists()

        finished_connectors = {
            event["connector"]
            for event in events
            if event["event_type"] == "connector.finished"
        }
        self.assertEqual(driver, "driver")
        self.assertEqual(result.actual, "ok")
        self.assertEqual(exported["total_cases"], 1)
        self.assertEqual(topology_json["name"], "libafl_bfm_fuzz")
        self.assertIn("manifest_to_replay_driver", finished_connectors)
        self.assertIn("case_to_dut", finished_connectors)
        self.assertIn("driver_to_scoreboard", finished_connectors)
        self.assertIn("functional_coverage_to_summary", finished_connectors)
        self.assertTrue(coverage_exists)

    def test_fuzz_run_orchestrator_generates_and_validates_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "demo.toml"
            manifest.write_text(
                "\n".join(
                    [
                        'name = "demo"',
                        'driver = "demo_driver:Driver"',
                        "",
                        "[[field]]",
                        'name = "mode"',
                        'kind = "enum"',
                        'choices = ["read", "write"]',
                    ]
                )
                + "\n"
            )
            corpus = root / "corpus.jsonl"
            events_out = root / "events.jsonl"
            topology_out = root / "topology.json"
            observer = JsonlObserver(events_out)
            config = FuzzRunConfig(
                target="demo",
                target_config=manifest,
                corpus=corpus,
                libafl_manifest=root / "Cargo.toml",
                topology_out=topology_out,
                generator_command=(
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(corpus)!r}).write_text('{{\"target\":\"demo\",\"mode\":\"read\"}}\\n')",
                ),
            )

            cases = FuzzRunOrchestrator(
                config,
                ObservationContext(run_id="fuzz-run-demo", observer=observer),
            ).generate_and_validate()
            observer.close()
            events = [json.loads(line) for line in events_out.read_text().splitlines()]
            topology = json.loads(topology_out.read_text())

        finished_connectors = {
            event["connector"]
            for event in events
            if event["event_type"] == "connector.finished"
        }
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].data["mode"], "read")
        self.assertIn("corpus_generator_to_corpus", finished_connectors)
        self.assertIn("corpus_to_validation", finished_connectors)
        self.assertTrue(all(event.get("run_id") == "fuzz-run-demo" for event in events))
        self.assertEqual(topology["name"], "libafl_bfm_fuzz")

    def test_fuzz_run_orchestrator_resolves_relative_artifacts_from_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            work_dir.mkdir()
            (work_dir / "demo.toml").write_text(
                "\n".join(
                    [
                        'name = "demo"',
                        'driver = "demo_driver:Driver"',
                        "",
                        "[[field]]",
                        'name = "mode"',
                        'kind = "enum"',
                        'choices = ["read"]',
                    ]
                )
                + "\n"
            )
            config = FuzzRunConfig(
                target="demo",
                target_config=Path("demo.toml"),
                corpus=Path("corpus.jsonl"),
                libafl_manifest=Path("Cargo.toml"),
                cwd=work_dir,
                generator_command=(
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('corpus.jsonl').write_text("
                    "'{\"target\":\"demo\",\"mode\":\"read\"}\\n'"
                    ")",
                ),
            )

            cases = FuzzRunOrchestrator(config, ObservationContext()).generate_and_validate()
            corpus_exists = (work_dir / "corpus.jsonl").exists()

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].data["mode"], "read")
        self.assertTrue(corpus_exists)

    def test_fuzz_run_orchestrator_splits_cargo_wrapper_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = FuzzRunConfig(
                target="demo",
                target_config=Path("demo.toml"),
                corpus=Path("corpus.jsonl"),
                libafl_manifest=Path("Cargo.toml"),
                cwd=root,
                cargo="cargo +nightly",
            )

            command = FuzzRunOrchestrator(
                config,
                ObservationContext(),
            )._default_generator_command()

        self.assertEqual(command[:4], ("cargo", "+nightly", "run", "--quiet"))
        self.assertIn(str(root / "Cargo.toml"), command)
        self.assertIn(str(root / "demo.toml"), command)
        self.assertIn(str(root / "corpus.jsonl"), command)

    def test_connector_preserves_return_value_and_writes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            input_path = Path(tmp) / "input.txt"
            input_path.write_text("hello")
            output_path = Path(tmp) / "output.txt"
            observer = JsonlObserver(path)
            connector = Connector("demo", "a", "b", observer=observer, run_id="run-1")

            def work():
                output_path.write_text("world")
                return "ok"

            result = connector.run(
                work,
                inputs={"input": input_path},
                outputs=lambda _result: {"output": output_path},
                metrics=lambda _result: {"value": 7},
            )
            observer.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(result, "ok")
        self.assertEqual([event["event_type"] for event in events], ["connector.started", "connector.finished"])
        self.assertEqual(events[-1]["run_id"], "run-1")
        self.assertEqual(events[-1]["metrics"]["value"], 7)
        self.assertEqual(events[-1]["outputs"][0]["role"], "output")

    def test_observer_failure_is_isolated_by_default(self):
        connector = Connector("demo", "a", "b", observer=FailingObserver())

        self.assertEqual(connector.run(lambda: "ok"), "ok")

    def test_connector_failed_event_reraises_main_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            observer = JsonlObserver(path)
            connector = Connector("demo", "a", "b", observer=observer)

            with self.assertRaisesRegex(ValueError, "main failed"):
                connector.run(lambda: (_ for _ in ()).throw(ValueError("main failed")))
            observer.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(events[-1]["event_type"], "connector.failed")
        self.assertEqual(events[-1]["error"]["type"], "ValueError")

    def test_artifact_normalization_accepts_single_and_role_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            path.write_text("data")
            single = normalize_artifact_refs({"role": "input", "path": path})
            mapped = normalize_artifact_refs({"input": path})

        self.assertEqual(single[0].role, "input")
        self.assertEqual(mapped[0].role, "input")

    def test_observer_from_env_defaults_to_null_without_path(self):
        old_out = os.environ.pop("CONNECTOR_OBSERVE_OUT", None)
        try:
            observer = observer_from_env()
        finally:
            if old_out is not None:
                os.environ["CONNECTOR_OBSERVE_OUT"] = old_out

        self.assertEqual(type(observer).__name__, "NullObserver")

    def test_monitoring_observer_aggregates_connector_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.json"
            observer = MonitoringObserver(path)
            connector = Connector("demo", "a", "b", observer=observer)

            connector.run(lambda: "ok")
            observer.close()
            monitor = json.loads(path.read_text())

        self.assertEqual(monitor["connector_count"], 1)
        self.assertEqual(monitor["connectors"][0]["connector"], "demo")
        self.assertEqual(monitor["connectors"][0]["finished"], 1)
        self.assertEqual(monitor["edges"][0]["from_layer"], "a")
        self.assertEqual(monitor["edges"][0]["to_layer"], "b")

    def test_monitoring_observer_merges_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor.json"
            first = MonitoringObserver(path)
            Connector("first", "a", "b", observer=first).run(lambda: "ok")
            first.close()

            second = MonitoringObserver(path)
            Connector("second", "b", "c", observer=second).run(lambda: "ok")
            second.close()
            monitor = json.loads(path.read_text())

        self.assertEqual(monitor["connector_count"], 2)
        self.assertEqual({"first", "second"}, {item["connector"] for item in monitor["connectors"]})

    def test_feedback_cli_writes_connector_events_monitoring_and_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "demo.toml"
            config.write_text(
                "\n".join(
                    [
                        'name = "demo"',
                        'driver = "demo_driver:Driver"',
                        "",
                        "[[field]]",
                        'name = "mode"',
                        'kind = "enum"',
                        'choices = ["read", "write"]',
                    ]
                )
                + "\n"
            )
            corpus = root / "corpus.jsonl"
            corpus.write_text('{"target":"demo","mode":"read","origin":"seed"}\n')
            coverage_info = root / "coverage.info"
            coverage_info.write_text("")
            summary_out = root / "summary.json"
            directives_out = root / "directives.json"
            prompt_out = root / "prompt.json"
            observation_out = root / "events.jsonl"
            monitoring_out = root / "monitor.json"
            topology_out = root / "topology.json"

            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            old_argv = sys.argv
            old_async = os.environ.get("CONNECTOR_OBSERVE_ASYNC")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config)
            os.environ["CONNECTOR_OBSERVE_ASYNC"] = "0"
            try:
                sys.argv = [
                    "coverage_feedback.py",
                    "--target",
                    "demo",
                    "--coverage-info",
                    str(coverage_info),
                    "--corpus",
                    str(corpus),
                    "--summary-out",
                    str(summary_out),
                    "--directives-out",
                    str(directives_out),
                    "--prompt-out",
                    str(prompt_out),
                    "--observation-out",
                    str(observation_out),
                    "--monitoring-out",
                    str(monitoring_out),
                    "--topology-out",
                    str(topology_out),
                    "--observation-run-id",
                    "run-demo",
                ]
                status = feedback_cli.main()
            finally:
                sys.argv = old_argv
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config
                if old_async is None:
                    os.environ.pop("CONNECTOR_OBSERVE_ASYNC", None)
                else:
                    os.environ["CONNECTOR_OBSERVE_ASYNC"] = old_async

            events = [json.loads(line) for line in observation_out.read_text().splitlines()]
            monitor = json.loads(monitoring_out.read_text())
            topology = json.loads(topology_out.read_text())
            summary_exists = summary_out.exists()
            directives_exists = directives_out.exists()

        self.assertEqual(status, 0)
        self.assertTrue(summary_exists)
        self.assertTrue(directives_exists)
        self.assertIn("coverage_to_summary", {event["connector"] for event in events})
        self.assertIn("layer2_layer3_feedback_to_layer1_plan", {event["connector"] for event in events})
        self.assertIn("layer1_plan_to_directives", {event["connector"] for event in events})
        self.assertTrue(all(event.get("run_id") == "run-demo" for event in events))
        self.assertIn("coverage_to_summary", {item["connector"] for item in monitor["connectors"]})
        self.assertEqual("libafl_bfm_fuzz", topology["name"])
        self.assertIn("corpus_to_replay_context", {item["name"] for item in topology["connectors"]})
        self.assertIn("layer3_feedback_to_layer2_feedback", {item["name"] for item in topology["connectors"]})
        self.assertIn("layer1_plan_to_directives", {item["name"] for item in topology["connectors"]})

    def test_feedback_cli_observes_three_layer_feedback_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "demo.toml"
            config.write_text(
                "\n".join(
                    [
                        'name = "demo"',
                        'driver = "demo_driver:Driver"',
                        "",
                        "[[field]]",
                        'name = "mode"',
                        'kind = "enum"',
                        'choices = ["read", "write"]',
                    ]
                )
                + "\n"
            )
            previous_summary = root / "previous_summary.json"
            previous_summary.write_text(
                json.dumps(
                    {
                        "target": "demo",
                        "uncovered_line_count": 1,
                        "stimulus_summary": {"total_cases": 1, "origin_counts": {"seed": 1}},
                        "rtl_structure_coverage": {
                            "totals": {"coverage": 0.5},
                            "coverage_export": {"uncovered_points": [{"id": "p1"}]},
                        },
                        "rtl_gap_summary": {
                            "top_gaps": [
                                {
                                    "id": "gap-1",
                                    "primary_kind": "branch",
                                    "priority": 1,
                                    "file": "demo.v",
                                    "line": 1,
                                    "module": "demo",
                                    "code": "if (mode)",
                                    "advisor_hints": [{"type": "source_keyword", "value": "mode"}],
                                    "evidence": {"point_ids": ["p1"]},
                                }
                            ]
                        },
                    }
                )
            )
            previous_directives = root / "previous_directives.json"
            previous_directives.write_text(
                json.dumps(
                    {
                        "directives": [
                            {"target": "demo", "name": "mode_probe", "gap_ids": ["gap-1"], "weight": 1}
                        ]
                    }
                )
            )
            corpus = root / "corpus.jsonl"
            corpus.write_text('{"target":"demo","mode":"read","origin":"mode_probe"}\n')
            coverage_info = root / "coverage.info"
            coverage_info.write_text("")
            observation_out = root / "events.jsonl"
            monitoring_out = root / "monitor.json"

            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            old_argv = sys.argv
            old_async = os.environ.get("CONNECTOR_OBSERVE_ASYNC")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config)
            os.environ["CONNECTOR_OBSERVE_ASYNC"] = "0"
            try:
                sys.argv = [
                    "coverage_feedback.py",
                    "--target",
                    "demo",
                    "--coverage-info",
                    str(coverage_info),
                    "--corpus",
                    str(corpus),
                    "--summary-out",
                    str(root / "summary.json"),
                    "--directives-out",
                    str(root / "directives.json"),
                    "--prompt-out",
                    str(root / "prompt.json"),
                    "--previous-summary",
                    str(previous_summary),
                    "--previous-directives",
                    str(previous_directives),
                    "--gap-feedback-out",
                    str(root / "gap_feedback.json"),
                    "--mutation-feedback-out",
                    str(root / "mutation_feedback.json"),
                    "--observation-out",
                    str(observation_out),
                    "--monitoring-out",
                    str(monitoring_out),
                ]
                status = feedback_cli.main()
            finally:
                sys.argv = old_argv
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config
                if old_async is None:
                    os.environ.pop("CONNECTOR_OBSERVE_ASYNC", None)
                else:
                    os.environ["CONNECTOR_OBSERVE_ASYNC"] = old_async

            events = [json.loads(line) for line in observation_out.read_text().splitlines()]
            monitor = json.loads(monitoring_out.read_text())

        self.assertEqual(status, 0)
        finished = [event for event in events if event["event_type"] == "connector.finished"]
        connectors = {event["connector"] for event in finished}
        self.assertIn("summary_to_mutation_feedback", connectors)
        self.assertIn("layer3_feedback_to_layer2_feedback", connectors)
        self.assertIn("layer2_layer3_feedback_to_layer1_plan", connectors)
        self.assertIn("layer1_plan_to_directives", connectors)
        plan_event = next(
            event for event in finished if event["connector"] == "layer2_layer3_feedback_to_layer1_plan"
        )
        self.assertTrue(plan_event["metrics"]["consumed_gap_feedback"])
        self.assertTrue(plan_event["metrics"]["consumed_mutation_feedback"])
        self.assertEqual(monitor["failed_connector_count"], 0)

    def test_harness_run_command_writes_connector_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observation_out = root / "events.jsonl"
            monitoring_out = root / "monitor.json"
            topology_out = root / "topology.json"
            output = root / "out.txt"

            old_env = {
                name: os.environ.get(name)
                for name in (
                    "CONNECTOR_OBSERVE_OUT",
                    "CONNECTOR_MONITOR_OUT",
                    "CONNECTOR_TOPOLOGY_OUT",
                    "CONNECTOR_OBSERVE_RUN_ID",
                    "CONNECTOR_OBSERVE_ASYNC",
                )
            }
            os.environ["CONNECTOR_OBSERVE_OUT"] = str(observation_out)
            os.environ["CONNECTOR_MONITOR_OUT"] = str(monitoring_out)
            os.environ["CONNECTOR_TOPOLOGY_OUT"] = str(topology_out)
            os.environ["CONNECTOR_OBSERVE_RUN_ID"] = "harness-demo"
            os.environ["CONNECTOR_OBSERVE_ASYNC"] = "0"
            _reset_observation_context_for_tests()
            try:
                result = run_command(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(output)!r}).write_text('ok')",
                    ],
                    connector_name="corpus_generator_to_corpus",
                    from_layer="corpus_generator",
                    to_layer="corpus",
                    outputs={"corpus": output},
                    metadata={"target": "demo"},
                )
            finally:
                _reset_observation_context_for_tests()
                for name, value in old_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            events = [json.loads(line) for line in observation_out.read_text().splitlines()]
            monitor = json.loads(monitoring_out.read_text())
            topology = json.loads(topology_out.read_text())
            output_text = output.read_text()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(output_text, "ok")
        self.assertIn("corpus_generator_to_corpus", {event["connector"] for event in events})
        self.assertTrue(all(event.get("run_id") == "harness-demo" for event in events))
        self.assertEqual(monitor["failed_connector_count"], 0)
        self.assertIn("corpus_generator_to_corpus", {item["name"] for item in topology["connectors"]})

    def test_harness_run_command_validates_connector_endpoint(self):
        with self.assertRaisesRegex(ValueError, "endpoint mismatch"):
            run_command(
                [sys.executable, "-c", "pass"],
                connector_name="corpus_generator_to_corpus",
                from_layer="wrong",
                to_layer="corpus",
            )

    def test_replay_context_load_is_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "demo.toml"
            config.write_text(
                "\n".join(
                    [
                        'name = "demo"',
                        'driver = "demo_driver:Driver"',
                        "",
                        "[[field]]",
                        'name = "mode"',
                        'kind = "enum"',
                        'choices = ["read", "write"]',
                    ]
                )
                + "\n"
            )
            corpus = root / "corpus.jsonl"
            corpus.write_text('{"target":"demo","mode":"read","origin":"seed"}\n')
            observation_out = root / "events.jsonl"
            monitoring_out = root / "monitor.json"

            old_env = {
                name: os.environ.get(name)
                for name in (
                    "FUZZ_TARGET",
                    "FUZZ_TARGET_CONFIG",
                    "LIBAFL_CORPUS",
                    "CONNECTOR_OBSERVE_OUT",
                    "CONNECTOR_MONITOR_OUT",
                    "CONNECTOR_OBSERVE_ASYNC",
                )
            }
            os.environ["FUZZ_TARGET"] = "demo"
            os.environ["FUZZ_TARGET_CONFIG"] = str(config)
            os.environ["LIBAFL_CORPUS"] = str(corpus)
            os.environ["CONNECTOR_OBSERVE_OUT"] = str(observation_out)
            os.environ["CONNECTOR_MONITOR_OUT"] = str(monitoring_out)
            os.environ["CONNECTOR_OBSERVE_ASYNC"] = "0"
            _reset_observation_context_for_tests()
            try:
                context = ReplayContext.from_env()
            finally:
                _reset_observation_context_for_tests()
                for name, value in old_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            events = [json.loads(line) for line in observation_out.read_text().splitlines()]
            monitor = json.loads(monitoring_out.read_text())

        self.assertEqual(context.target, "demo")
        self.assertEqual(len(context.cases), 1)
        finished = [event for event in events if event["event_type"] == "connector.finished"]
        self.assertEqual(finished[-1]["connector"], "corpus_to_replay_context")
        self.assertEqual(finished[-1]["metrics"]["case_count"], 1)
        self.assertEqual(monitor["failed_connector_count"], 0)


if __name__ == "__main__":
    unittest.main()

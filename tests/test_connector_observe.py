import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import Connector, JsonlObserver, MonitoringObserver, observer_from_env  # noqa: E402
from connector_observe.schema import normalize_artifact_refs  # noqa: E402
from fuzz_feedback import cli as feedback_cli  # noqa: E402


class FailingObserver:
    def on_event(self, event):
        raise RuntimeError("observer failed")

    def flush(self):
        raise RuntimeError("observer failed")

    def close(self):
        raise RuntimeError("observer failed")


class TestConnectorObserve(unittest.TestCase):
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
        self.assertIn("coverage_feedback", topology["name"])
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


if __name__ == "__main__":
    unittest.main()

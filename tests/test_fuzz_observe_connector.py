import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import AsyncObserver, Connector, JsonlObserver  # noqa: E402
from connector_observe.schema import normalize_artifact_refs  # noqa: E402


class FailingObserver:
    def on_event(self, event):
        raise RuntimeError("observer failed")

    def flush(self):
        raise RuntimeError("observer failed")

    def close(self):
        raise RuntimeError("observer failed")


class TestConnector(unittest.TestCase):
    def test_normalize_artifact_refs_accepts_single_mapping_and_role_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.txt"
            path.write_text("hello")

            single = normalize_artifact_refs({"role": "input", "path": path})
            mapped = normalize_artifact_refs({"input": path})

        self.assertEqual(single[0].role, "input")
        self.assertEqual(mapped[0].role, "input")

    def test_null_observer_preserves_return_value(self):
        connector = Connector("demo", "a", "b")

        result = connector.run(lambda value: value + 1, 41)

        self.assertEqual(result, 42)

    def test_jsonl_observer_records_started_and_finished_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            input_path = Path(tmp) / "input.txt"
            input_path.write_text("hello")
            output_path = Path(tmp) / "output.txt"
            observer = JsonlObserver(path)
            connector = Connector(
                "demo_connector",
                "layer_a",
                "layer_b",
                observer=observer,
                run_id="run-1",
            )

            def work():
                output_path.write_text("world")
                return "ok"

            result = connector.run(
                work,
                inputs=[{"role": "input", "path": input_path}],
                outputs=lambda _result: [{"role": "output", "path": output_path}],
                metrics=lambda _result: {"answer": 42},
            )
            observer.close()

            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(result, "ok")
        self.assertEqual([event["event_type"] for event in events], ["connector.started", "connector.finished"])
        self.assertEqual(events[-1]["run_id"], "run-1")
        self.assertEqual(events[-1]["connector"], "demo_connector")
        self.assertEqual(events[-1]["outputs"][0]["role"], "output")
        self.assertEqual(events[-1]["metrics"]["answer"], 42)
        self.assertEqual(events[-1]["status"], "ok")

    def test_observer_failure_does_not_change_main_result_by_default(self):
        connector = Connector("demo", "a", "b", observer=FailingObserver())

        result = connector.run(lambda: "kept")

        self.assertEqual(result, "kept")

    def test_strict_observer_failure_can_fail_main_result(self):
        connector = Connector(
            "demo",
            "a",
            "b",
            observer=FailingObserver(),
            strict_observation=True,
        )

        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            connector.run(lambda: "blocked")

    def test_connector_records_failed_event_and_reraises_original_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            observer = JsonlObserver(path)
            connector = Connector("demo", "a", "b", observer=observer)

            with self.assertRaisesRegex(ValueError, "main failed"):
                connector.run(lambda: (_ for _ in ()).throw(ValueError("main failed")))
            observer.close()

            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(events[-1]["event_type"], "connector.failed")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(events[-1]["error"]["type"], "ValueError")

    def test_async_observer_flushes_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            observer = AsyncObserver(JsonlObserver(path), max_queue_size=8)
            connector = Connector("demo", "a", "b", observer=observer)

            connector.run(lambda: "ok")
            observer.close()

            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertTrue(any(event["event_type"] == "connector.finished" for event in events))

    def test_run_async_preserves_result_and_records_events(self):
        async def work(value):
            return value + 1

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            observer = JsonlObserver(path)
            connector = Connector("async_demo", "a", "b", observer=observer)

            result = asyncio.run(
                connector.run_async(
                    work,
                    41,
                    metrics=lambda value: {"answer": value},
                )
            )
            observer.close()
            events = [json.loads(line) for line in path.read_text().splitlines()]

        self.assertEqual(result, 42)
        self.assertEqual(events[-1]["connector"], "async_demo")
        self.assertEqual(events[-1]["metrics"]["answer"], 42)


if __name__ == "__main__":
    unittest.main()

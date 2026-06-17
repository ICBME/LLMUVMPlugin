import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_bfm.corpus import load_cases  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_feedback.advisors import (  # noqa: E402
    build_llm_prompt,
    normalize_llm_response,
    propose_directives,
    validate_directives,
)
from fuzz_feedback.coverage import build_summary  # noqa: E402
from fuzz_feedback.feedback_loop import (  # noqa: E402
    build_mutation_feedback,
    update_mutation_directions,
)
from fuzz_feedback.mutation_planner import plan_mutations_from_rtl_gaps  # noqa: E402
from fuzz_feedback.rtl_structure_coverage import (  # noqa: E402
    build_rtl_structure_coverage,
    build_rtl_structure_coverage_export,
)
from fuzz_uvm.functional_coverage import build_functional_coverage  # noqa: E402
from fuzz_uvm.functional_coverage import GenericCoverageModel  # noqa: E402


CONFIG_TEXT = """
name = "demo"
toplevel = "demo_top"
driver = "demo_driver:Driver"
comparator = "demo_compare:Comparator"

[[field]]
name = "mode"
kind = "enum"
choices = ["read", "write"]

[[field]]
name = "size"
kind = "int"
min = 1
max = 4
choices = [1, 2, 4]

[[field]]
name = "payload"
kind = "hex"
hex_len_by = { size = { "1" = 1, "2" = 2, "4" = 4 } }

[[coverpoint]]
name = "payload_pattern"
field = "payload"
patterns = ["zero", "ff", "increment"]

[[cross]]
name = "mode_x_payload_pattern"
coverpoints = ["mode", "payload_pattern"]
"""


VARIABLE_HEX_CONFIG_TEXT = """
name = "demo"
toplevel = "demo_top"
driver = "demo_driver:Driver"

[[field]]
name = "mode"
kind = "enum"
choices = ["sha224", "sha256"]

[[field]]
name = "message"
kind = "hex"
"""


class TestLibAflBfmGeneric(unittest.TestCase):
    def test_corpus_validation_uses_manifest_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "read", "size": 2, "payload": "aabb"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)

            cases = load_cases(corpus, "demo", config=config)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].data["payload"], "aabb")

    def test_corpus_validation_rejects_selector_length_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "write", "size": 4, "payload": "aabb"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)

            with self.assertRaisesRegex(ValueError, "payload must be 4 bytes"):
                load_cases(corpus, "demo", config=config)

    def test_functional_coverage_is_schema_driven(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "read", "size": 1, "payload": "00"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)
            cases = load_cases(corpus, "demo", config=config)

            summary = build_functional_coverage("demo", cases, config=config)

            self.assertEqual(summary["bins"]["mode"], {"read": 1})
            self.assertEqual(summary["uncovered"]["mode"], ["write"])
            self.assertEqual(summary["uncovered"]["coverpoints"]["payload_pattern"], ["ff", "increment"])
            self.assertIn("read|ff", summary["uncovered"]["crosses"]["mode_x_payload_pattern"])
            self.assertEqual(summary["coverage"]["total_bins"], 8)
            self.assertEqual(summary["coverage"]["total_crosses"], 6)

    def test_target_config_loads_functional_coverpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)

            config = load_target_config("demo", targets_dir=tmp_path)

            self.assertEqual(config.coverpoints[0].name, "payload_pattern")
            self.assertEqual(config.comparator, "demo_compare:Comparator")
            self.assertEqual(config.coverpoints[0].patterns, ("zero", "ff", "increment"))
            self.assertEqual(config.crosses[0].coverpoints, ("mode", "payload_pattern"))

    def test_coverage_model_can_sample_replay_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            config = load_target_config("demo", targets_dir=tmp_path)
            model = GenericCoverageModel("demo", config=config)
            case = SimpleNamespace(
                target="demo",
                data={"target": "demo", "mode": "read", "size": 1, "payload": "00"},
                line_no=1,
            )

            model.sample_record(SimpleNamespace(case=case, error=None))
            model.sample_record(SimpleNamespace(case=case, error="driver failed"))

            summary = model.to_json()
            self.assertEqual(summary["total_cases"], 1)
            self.assertEqual(summary["bins"]["payload_pattern"], {"zero": 1})

    def test_feedback_summary_prefers_uvm_functional_coverage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            functional = tmp_path / "uvm_functional.json"
            functional.write_text(json.dumps({"domain": "uvm_functional", "target": "demo", "marker": "replay"}))

            summary = build_summary(
                "demo",
                tmp_path / "missing.info",
                tmp_path / "missing.jsonl",
                functional_coverage=functional,
            )

            self.assertEqual(summary["uvm_functional_coverage"]["marker"], "replay")
            self.assertEqual(summary["uvm_functional_coverage_source"], str(functional))
            self.assertEqual(summary["rtl_gap_summary"]["domain"], "rtl_gap")
            self.assertEqual(summary["rtl_gap_summary"]["target"], "demo")

    def test_feedback_summary_can_ignore_functional_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            functional = tmp_path / "uvm_functional.json"
            functional.write_text(
                json.dumps(
                    {
                        "domain": "uvm_functional",
                        "target": "demo",
                        "marker": "replay",
                        "uncovered": {"fields": {"mode": ["write"]}},
                    }
                )
            )

            summary = build_summary(
                "demo",
                tmp_path / "missing.info",
                tmp_path / "missing.jsonl",
                functional_coverage=functional,
                ignore_functional_coverage=True,
            )
            prompt = build_llm_prompt(
                summary,
                {
                    "source": "generic heuristic",
                    "directives": [{"target": "demo", "name": "schema_refresh"}],
                },
            )

            self.assertEqual(summary["uvm_functional_coverage_source"], "ignored_by_request")
            self.assertTrue(summary["uvm_functional_coverage"]["ignored"])
            self.assertNotIn("marker", summary["uvm_functional_coverage"])
            self.assertEqual(summary["uvm_functional_coverage"]["uncovered"], {})
            self.assertTrue(prompt["functional_coverage_ignored"])
            self.assertIn("RTL code coverage gaps only", prompt["task"])

    def test_rtl_structure_coverage_builds_gap_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rtl = tmp_path / "demo.v"
            rtl.write_text(
                "\n".join(
                    [
                        "module demo;",
                        "  always_comb begin",
                        "    if (mode) y = a;",
                        "    assign z = bus[0];",
                        "  end",
                        "endmodule",
                    ]
                )
                + "\n"
            )
            info = tmp_path / "coverage.info"
            info.write_text(f"TN:\nSF:{rtl}\nDA:2,1\nDA:3,0\nend_of_record\n")
            dat = tmp_path / "coverage.dat"
            branch_metadata = (
                f"\x01f\x02{rtl}\x01l\x023\x01t\x02branch"
                "\x01page\x02v_branch/demo\x01o\x02if mode"
            )
            toggle_metadata = (
                f"\x01f\x02{rtl}\x01l\x024\x01t\x02toggle"
                "\x01page\x02v_toggle/demo\x01o\x02bus[0]:0->1"
            )
            dat.write_text(f"C '{branch_metadata}' 0\nC '{toggle_metadata}' 0\n")

            export = build_rtl_structure_coverage_export(
                target="demo",
                coverage_info=info,
                coverage_dat=dat,
            )
            export_json = export.to_json(max_points=None)
            self.assertEqual(export_json["schema_version"], 1)
            self.assertEqual(export_json["domain"], "rtl_structure")
            self.assertEqual(export_json["target"], "demo")
            self.assertEqual(export_json["point_count"], 4)
            self.assertIn("id", export_json["points"][0])

            summary = build_rtl_structure_coverage(
                target="demo",
                coverage_info=info,
                coverage_dat=dat,
            )

            self.assertEqual(summary["coverage_export"]["schema_version"], 1)
            gaps = summary["rtl_gap_summary"]
            self.assertEqual(gaps["schema_version"], 1)
            self.assertEqual(gaps["domain"], "rtl_gap")
            self.assertEqual(gaps["target"], "demo")
            self.assertEqual(gaps["total"], 2)
            self.assertEqual(gaps["total_points"], 3)
            self.assertEqual(gaps["by_kind"], {"branch": 1, "line": 1, "toggle": 1})

            top_gap = gaps["top_gaps"][0]
            self.assertIn("id", top_gap)
            self.assertEqual(top_gap["primary_kind"], "branch")
            self.assertEqual(top_gap["module"], "demo")
            self.assertEqual(top_gap["line"], 3)
            self.assertEqual(top_gap["kinds"], {"branch": 1, "line": 1})
            self.assertEqual(top_gap["evidence"]["kinds"], {"branch": 1, "line": 1})
            self.assertEqual(len(top_gap["evidence"]["point_ids"]), 2)
            self.assertIn({"type": "source_keyword", "value": "mode"}, top_gap["advisor_hints"])
            self.assertEqual(top_gap["code"], "if (mode) y = a;")
            self.assertEqual(top_gap["context"][2]["line"], 3)

    def test_advisor_uses_functional_uncovered_fields_and_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                directives = propose_directives(
                    {
                        "target": "demo",
                        "uncovered_line_count": 0,
                        "uvm_functional_coverage": {
                            "uncovered": {
                                "fields": {"mode": ["write"]},
                                "coverpoints": {"payload_pattern": ["ff"]},
                            }
                        },
                        "stimulus_summary": {"field_counts": {}},
                    }
                )
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            directive = directives["directives"][0]
            self.assertEqual(directive["name"], "functional_schema_refresh")
            self.assertEqual(directive["mode_values"], ["write"])
            self.assertEqual(directive["payload_patterns"], ["ff"])

    def test_advisor_converts_clear_rtl_gap_to_mutation_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                directives = propose_directives(
                    {
                        "target": "demo",
                        "uncovered_line_count": 1,
                        "uvm_functional_coverage": {},
                        "stimulus_summary": {"field_counts": {}},
                        "rtl_gap_summary": {
                            "top_gaps": [
                                {
                                    "id": "gap-mode",
                                    "primary_kind": "branch",
                                    "module": "demo",
                                    "code": "if (mode == write) begin",
                                    "context": [],
                                    "objects": ["mode"],
                                    "advisor_hints": [
                                        {"type": "source_keyword", "value": "mode"}
                                    ],
                                    "evidence": {},
                                }
                            ]
                        },
                    }
                )
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            self.assertIn("rtl_gap heuristic", directives["source"])
            directive = directives["directives"][0]
            self.assertEqual(directive["name"], "rtl_gap_structural_mutation")
            self.assertEqual(directive["gap_ids"], ["gap-mode"])
            self.assertEqual(directive["mode_values"], ["read", "write"])

    def test_advisor_converts_length_gap_to_explicit_variable_hex_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(VARIABLE_HEX_CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                directives = propose_directives(
                    {
                        "target": "demo",
                        "uncovered_line_count": 1,
                        "uvm_functional_coverage": {},
                        "stimulus_summary": {"field_counts": {}},
                        "rtl_gap_summary": {
                            "top_gaps": [
                                {
                                    "id": "gap-next",
                                    "primary_kind": "branch",
                                    "module": "demo",
                                    "code": "if (next_block && padding) begin",
                                    "context": [],
                                    "objects": ["next_block"],
                                    "advisor_hints": [
                                        {"type": "source_keyword", "value": "next"},
                                        {"type": "source_keyword", "value": "padding"},
                                        {"type": "source_keyword", "value": "block"},
                                    ],
                                    "evidence": {},
                                }
                            ]
                        },
                    }
                )
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            cases = directives["directives"][0]["cases"]
            self.assertGreaterEqual(len(cases), 3)
            self.assertEqual(cases[0]["mode"], "sha224")
            self.assertEqual(cases[0]["message"], "")
            self.assertEqual(len(bytes.fromhex(cases[-1]["message"])), 56)

    def test_llm_prompt_includes_complex_rtl_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            summary = {
                "target": "demo",
                "uncovered_line_count": 1,
                "uvm_functional_coverage": {},
                "stimulus_summary": {"field_counts": {}},
                "rtl_gap_summary": {
                    "top_gaps": [
                        {
                            "id": "gap-state",
                            "primary_kind": "branch",
                            "module": "demo_core",
                            "code": "if (round_state == DONE) begin",
                            "context": [],
                            "objects": ["round_state"],
                            "advisor_hints": [
                                {"type": "source_keyword", "value": "state"},
                                {"type": "source_keyword", "value": "round"},
                            ],
                            "evidence": {"point_ids": ["p1"]},
                        }
                    ]
                },
            }
            try:
                heuristic = propose_directives(summary)
                prompt = build_llm_prompt(summary, heuristic)
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            complex_gaps = prompt["rtl_gap_mutation_prompt"]["complex_rtl_gaps"]
            self.assertEqual(complex_gaps[0]["id"], "gap-state")
            self.assertIn("directives", prompt["rtl_gap_mutation_prompt"]["allowed_directive_schema"])

    def test_llm_prompt_includes_response_contract_and_allowed_schema(self):
        prompt = build_llm_prompt(
            {
                "target": "demo",
                "uncovered_line_count": 1,
                "uvm_functional_coverage": {},
                "stimulus_summary": {},
            },
            {"source": "generic heuristic", "directives": [{"target": "demo"}]},
        )

        self.assertIn("response_contract", prompt)
        self.assertIn("allowed_schema", prompt)
        self.assertIn("directives", prompt["allowed_schema"])
        self.assertIn("coverage_summary", prompt)

    def test_llm_response_normalizer_accepts_common_directive_aliases(self):
        value = normalize_llm_response(
            {
                "source": "llm",
                "mutation_directives": {
                    "target": "demo",
                    "name": "probe",
                    "cases": [{"mode": "write"}],
                },
            },
            target="demo",
            model="test-model",
        )

        directives = validate_directives("demo", value)

        self.assertEqual(directives["source"], "llm:langchain:test-model")
        self.assertEqual(directives["directives"][0]["name"], "probe")

    def test_validate_directives_reports_top_level_keys_for_empty_response(self):
        with self.assertRaisesRegex(ValueError, "top-level keys: analysis"):
            validate_directives("demo", {"analysis": "no directive"})

    def test_mutation_feedback_increases_effective_direction_weight(self):
        previous = {
            "target": "demo",
            "uncovered_line_count": 2,
            "stimulus_summary": {"origin_counts": {}},
            "rtl_structure_coverage": {
                "totals": {"coverage": 0.5},
                "coverage_export": {
                    "uncovered_points": [{"id": "p1"}, {"id": "p2"}],
                },
            },
            "rtl_gap_summary": {
                "top_gaps": [
                    {"id": "gap-1", "evidence": {"point_ids": ["p1"]}},
                    {"id": "gap-2", "evidence": {"point_ids": ["p2"]}},
                ]
            },
            "uvm_functional_coverage": {
                "origin_counts": {},
                "bins": {"mode": {"read": 1}},
                "crosses": {},
            },
        }
        current = {
            "target": "demo",
            "uncovered_line_count": 1,
            "stimulus_summary": {"origin_counts": {"directed": 4}},
            "rtl_structure_coverage": {
                "totals": {"coverage": 0.75},
                "coverage_export": {
                    "uncovered_points": [{"id": "p2"}],
                },
            },
            "rtl_gap_summary": {
                "top_gaps": [
                    {"id": "gap-2", "evidence": {"point_ids": ["p2"]}},
                ]
            },
            "uvm_functional_coverage": {
                "origin_counts": {"directed": 4},
                "bins": {"mode": {"read": 1, "write": 1}},
                "crosses": {},
            },
        }
        directives = {
            "source": "heuristic",
            "directives": [
                {"target": "demo", "name": "directed", "weight": 1, "gap_ids": ["gap-1"]}
            ],
        }

        feedback = build_mutation_feedback(previous, current, directives)
        direction = feedback["directions"]["directed"]

        self.assertEqual(direction["decision"], "increase_weight")
        self.assertEqual(direction["structural_resolved_points"], 1)
        self.assertEqual(direction["resolved_gap_count"], 1)
        self.assertEqual(direction["functional_new_bins"], 1)
        self.assertGreater(direction["updated_weight"], 1)

        updated = update_mutation_directions(directives, feedback)
        self.assertEqual(updated["directives"][0]["feedback_decision"], "increase_weight")
        self.assertGreater(updated["directives"][0]["weight"], 1)

    def test_mutation_feedback_suppresses_stale_direction(self):
        previous = {
            "target": "demo",
            "stimulus_summary": {"origin_counts": {}},
            "rtl_structure_coverage": {
                "totals": {"coverage": 0.5},
                "coverage_export": {"uncovered_points": [{"id": "p1"}]},
            },
            "rtl_gap_summary": {
                "top_gaps": [{"id": "gap-1", "evidence": {"point_ids": ["p1"]}}]
            },
            "uvm_functional_coverage": {"origin_counts": {}, "bins": {}, "crosses": {}},
        }
        current = {
            "target": "demo",
            "stimulus_summary": {"origin_counts": {"stale": 4}},
            "rtl_structure_coverage": {
                "totals": {"coverage": 0.5},
                "coverage_export": {"uncovered_points": [{"id": "p1"}]},
            },
            "rtl_gap_summary": {
                "top_gaps": [{"id": "gap-1", "evidence": {"point_ids": ["p1"]}}]
            },
            "uvm_functional_coverage": {
                "origin_counts": {"stale": 4},
                "bins": {},
                "crosses": {},
            },
        }
        directives = {
            "directives": [{"target": "demo", "name": "stale", "weight": 0.5}]
        }
        previous_feedback = {
            "directions": {
                "stale": {"stale_count": 2, "updated_weight": 0.5, "success_count": 0}
            }
        }

        feedback = build_mutation_feedback(
            previous,
            current,
            directives,
            previous_feedback=previous_feedback,
        )
        updated = update_mutation_directions(directives, feedback)

        self.assertEqual(feedback["directions"]["stale"]["stale_count"], 3)
        self.assertEqual(feedback["directions"]["stale"]["decision"], "suppress_temporarily")
        self.assertFalse(updated["directives"][0]["enabled"])
        self.assertEqual(updated["directives"][0]["weight"], 0.1)

    def test_layer1_plan_uses_gap_feedback_to_select_next_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            summary = {
                "target": "demo",
                "rtl_gap_summary": {
                    "top_gaps": [
                        {
                            "id": "gap-stale",
                            "primary_kind": "branch",
                            "module": "demo",
                            "code": "if (round_state == DONE) begin",
                            "context": [],
                            "objects": ["round_state"],
                            "advisor_hints": [
                                {"type": "source_keyword", "value": "state"},
                                {"type": "source_keyword", "value": "round"},
                            ],
                            "evidence": {"point_ids": ["p-stale"]},
                        },
                        {
                            "id": "gap-resolved",
                            "primary_kind": "branch",
                            "module": "demo",
                            "code": "if (mode == read) begin",
                            "context": [],
                            "objects": ["mode"],
                            "advisor_hints": [{"type": "source_keyword", "value": "mode"}],
                            "evidence": {"point_ids": ["p-resolved"]},
                        },
                        {
                            "id": "gap-mode",
                            "primary_kind": "branch",
                            "module": "demo",
                            "code": "if (mode == write) begin",
                            "context": [],
                            "objects": ["mode"],
                            "advisor_hints": [{"type": "source_keyword", "value": "mode"}],
                            "evidence": {"point_ids": ["p-mode"]},
                        },
                    ]
                },
            }
            gap_feedback = {
                "gaps": {
                    "gap-stale": {
                        "status": "stale",
                        "next_action": "escalate_to_llm",
                        "stale_count": 2,
                        "attempt_count": 2,
                    },
                    "gap-resolved": {
                        "status": "resolved",
                        "next_action": "done",
                    },
                }
            }
            try:
                plan = plan_mutations_from_rtl_gaps(summary, gap_feedback=gap_feedback)
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

        self.assertIn("gap-stale", plan["gap_selection"]["complex_for_llm"])
        self.assertIn("gap-resolved", plan["gap_selection"]["skipped"])
        self.assertEqual(plan["complex_gaps"][0]["id"], "gap-stale")
        self.assertEqual(plan["complex_gaps"][0]["gap_feedback"]["next_action"], "escalate_to_llm")
        self.assertEqual(plan["directives"][0]["gap_ids"], ["gap-mode"])
        self.assertEqual(plan["directives"][0]["mode_values"], ["read", "write"])


if __name__ == "__main__":
    unittest.main()

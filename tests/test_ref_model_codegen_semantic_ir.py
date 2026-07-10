import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from LLMPlugin import (
    CallableLLMBackend,
    LLMAgentRuntime,
    LLMBackendError,
    LLMRequest,
    LLMResponse,
    registered_backend_names,
)
from LLMPlugin.langgraph_backend import create_langgraph_backend
from Spec2Backend.BackendReadiness import analyze_backend_readiness
from Spec2Backend.RefModelPlan import build_ref_model_plan
from Spec2Backend.Spec2IR import (
    AutomationPolicyGraph,
    AutomationPolicyRule,
    collect_semantic_spec_ir_issues,
    generate_semantic_spec_ir,
    normalize_semantic_spec_ir,
    run_spec2ir_agent,
    semantic_ir_sha256,
    Spec2IRHarness,
    review_semantic_spec_ir,
    validate_semantic_spec_ir,
)
from Spec2Backend.Spec2IR.claim_extraction import extract_spec_claims
from Spec2Backend.Spec2IR.schema import SourceDocument


def _replace_semantic_element_patch(current: dict, candidate: dict) -> dict:
    replacement = candidate["semantic_elements"][0]
    return {
        "schema_version": 1,
        "base_revision": 0,
        "base_sha256": semantic_ir_sha256(current),
        "operations": [
            {
                "op": "replace",
                "target": {
                    "collection": "semantic_elements",
                    "id": replacement["id"],
                },
                "value": replacement,
            }
        ],
    }


class TestSemanticSpecIRGeneration(unittest.TestCase):
    def test_generate_semantic_spec_ir_extracts_traceable_sha_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "\n".join(
                    [
                        "# Demo SHA",
                        "The block computes SHA-224 or SHA-256 over the input message.",
                    ]
                )
                + "\n"
            )

            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
            )

            self.assertEqual(semantic_ir["schema_version"], 6)
            self.assertEqual(semantic_ir["target"], "demo_sha")
            self.assertEqual(semantic_ir["semantic_context"]["version"], 1)
            self.assertIn(
                "mode",
                {symbol["name"] for symbol in semantic_ir["semantic_context"]["symbols"]},
            )
            self.assertEqual(semantic_ir["sources"][0]["content_hash"], hashlib.sha256(spec.read_bytes()).hexdigest())
            self.assertEqual([claim["id"] for claim in semantic_ir["spec_claims"]], ["claim1"])
            claim = semantic_ir["spec_claims"][0]
            self.assertRegex(claim["fingerprint"], r"^[0-9a-f]{64}$")
            obligations = claim["decomposition"]["atomic_obligations"]
            self.assertTrue(any(item["kind"] == "operation" for item in obligations))
            self.assertEqual(
                semantic_ir["semantic_elements"][0]["claim_ids"],
                ["claim1"],
            )
            self.assertEqual(semantic_ir["evidence"][0]["line_start"], 2)
            self.assertIn("SHA-224", semantic_ir["evidence"][0]["quote"])
            self.assertEqual(semantic_ir["semantic_elements"][0]["kind"], "combinational_behavior")
            self.assertEqual(semantic_ir["semantic_elements"][0]["formalization_status"], "candidate")
            representation = semantic_ir["semantic_elements"][0]["representation"]
            self.assertEqual(representation["ast_version"], 2)
            self.assertEqual(representation["kind"], "combinational_relation")
            self.assertEqual(representation["ast"]["node"], "operation_relation")
            self.assertEqual(semantic_ir["review"]["status"], "draft")
            self.assertFalse(semantic_ir["semantic_gaps"])
            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

    def test_extract_spec_claims_handles_multiline_lists_and_truth_tables(self):
        spec_text = "\n".join(
            [
                "# Demo",
                "The module implements a combinational circuit",
                "for the following truth table:",
                "",
                " - input  x",
                " - output y",
                "",
                "  x | y",
                "  0 | 1",
                "  1 | 0",
            ]
        )

        claims = extract_spec_claims(
            (
                SourceDocument(
                    id="src1",
                    path=Path("demo_spec.md"),
                    text=spec_text,
                ),
            )
        )

        summaries = [claim["summary"] for claim in claims]
        self.assertIn("The module implements a combinational circuit for the following truth table:", summaries)
        self.assertIn("input x", summaries)
        self.assertIn("output y", summaries)
        self.assertIn("Truth table row: when x=0, y=1", summaries)
        self.assertIn("Truth table row: when x=1, y=0", summaries)
        for claim in claims:
            self.assertRegex(claim["fingerprint"], r"^[0-9a-f]{64}$")
            self.assertTrue(claim["decomposition"]["atomic_obligations"])
            self.assertIn(claim["quote"].strip(), spec_text)
        table_claim = next(
            claim
            for claim in claims
            if claim["summary"] == "Truth table row: when x=0, y=1"
        )
        self.assertEqual(table_claim["line_start"], 8)
        self.assertEqual(table_claim["line_end"], 9)
        self.assertIn("x | y", table_claim["quote"])
        self.assertIn("0 | 1", table_claim["quote"])
        self.assertEqual(table_claim["kind"], "functional_behavior")
        self.assertTrue(
            any(
                obligation["kind"] == "truth_table_row"
                for obligation in table_claim["decomposition"]["atomic_obligations"]
            )
        )

    def test_extract_spec_claims_preserves_unique_quotes_for_wrapped_sentences(self):
        spec_text = (
            "The block computes SHA-256 over\n"
            "the input message. Reset must clear\n"
            "the busy flag to zero.\n"
        )

        claims = extract_spec_claims(
            (
                SourceDocument(
                    id="src1",
                    path=Path("wrapped_spec.md"),
                    text=spec_text,
                ),
            )
        )

        self.assertEqual([claim["summary"] for claim in claims], [
            "The block computes SHA-256 over the input message.",
            "Reset must clear the busy flag to zero.",
        ])
        self.assertEqual(len({claim["quote"] for claim in claims}), 2)
        for claim in claims:
            self.assertIn(claim["quote"].strip(), spec_text)

    def test_extract_spec_claims_does_not_split_note_prefix(self):
        claims = extract_spec_claims(
            (
                SourceDocument(
                    id="src1",
                    path=Path("note_spec.md"),
                    text="Note: reset is active high.\n",
                ),
            )
        )

        self.assertEqual([claim["summary"] for claim in claims], ["Note: reset is active high."])

    def test_semantic_validation_checks_claim_decomposition_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["spec_claims"][0]["decomposition"]["atomic_obligations"][0]["kind"] = "unsupported"

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(
                any(
                    issue.path.endswith(".decomposition.atomic_obligations[0].kind")
                    for issue in issues
                )
            )

    def test_semantic_validation_requires_claim_decomposition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["spec_claims"][0].pop("fingerprint")
            semantic_ir["spec_claims"][0].pop("decomposition")

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any(issue.path.endswith(".fingerprint") for issue in issues))
            self.assertTrue(any(issue.path.endswith(".decomposition") for issue in issues))

    def test_semantic_representation_parser_respects_declared_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("input data (8 bits) computes SHA-256 over the input message.\n")

            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
            )

            element = semantic_ir["semantic_elements"][0]
            representation = element["representation"]
            self.assertEqual(element["kind"], "combinational_behavior")
            self.assertEqual(representation["kind"], "combinational_relation")
            self.assertEqual(representation["ast"]["node"], "operation_relation")
            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

    def test_semantic_claim_placeholder_requires_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")

            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
            )
            element = semantic_ir["semantic_elements"][0]
            representation = element["representation"]

            self.assertEqual(element["kind"], "descriptive")
            self.assertEqual(element["formalization_status"], "needs_human_review")
            self.assertEqual(representation["kind"], "textual_formalization")
            self.assertEqual(representation["ast"]["node"], "semantic_claim")
            self.assertEqual(semantic_ir["review"]["status"], "needs_human_input")

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["placeholder_only_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["covered_claims"])
            obligations = review["completeness"]["claim_obligations"][0]["missing_obligations"]
            self.assertTrue(any(item["code"] == "semantic_claim_placeholder" for item in obligations))

    def test_temporal_latency_claim_uses_typed_ast_but_requires_clock_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")

            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
            )
            element = semantic_ir["semantic_elements"][0]
            representation = element["representation"]

            self.assertEqual(element["kind"], "temporal_behavior")
            self.assertEqual(element["formalization_status"], "needs_human_review")
            self.assertEqual(representation["kind"], "temporal_rule")
            self.assertEqual(representation["ast"]["property"]["node"], "latency_rule")
            self.assertEqual(representation["ast"]["property"]["delay"], {"node": "delay_range", "min": 1, "max": 1})
            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["placeholder_only_claims"], ["claim1"])
            obligations = review["completeness"]["claim_obligations"][0]["missing_obligations"]
            self.assertTrue(any(item["code"] == "missing_temporal_clock" for item in obligations))
            self.assertTrue(any(item["code"] == "text_trigger" for item in obligations))

    def test_temporal_latency_ast_with_clock_context_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            element = semantic_ir["semantic_elements"][0]
            element["formalization_status"] = "candidate"
            element["representation"] = {
                "ast_version": 2,
                "kind": "temporal_rule",
                "text": element["summary"],
                "ast": {
                    "node": "temporal_rule",
                    "context": {
                        "node": "clock_reset_context",
                        "clock": {
                            "node": "clock_event",
                            "edge": "posedge",
                            "signal": {"node": "signal_ref", "name": "clk"},
                        },
                    },
                    "property": {
                        "node": "latency_rule",
                        "trigger": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "digest_complete"},
                        },
                        "response": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "done"},
                        },
                        "delay": {"node": "delay_range", "min": 1, "max": 1},
                    },
                },
            }

            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "passed")
            self.assertEqual(review["completeness"]["covered_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["partial_claims"])
            self.assertFalse(review["completeness"]["claim_obligations"][0]["missing_obligations"])
            source_coverage = review["completeness"]["source_claim_coverage"]
            self.assertEqual(source_coverage["summary"]["uncovered_span_count"], 0)
            self.assertEqual(source_coverage["spans"][0]["claim_ids"], ["claim1"])

    def test_complete_claim_still_reports_partial_duplicate_obligations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            complete_element = semantic_ir["semantic_elements"][0]
            complete_element["formalization_status"] = "candidate"
            complete_element["representation"] = {
                "ast_version": 2,
                "kind": "temporal_rule",
                "text": complete_element["summary"],
                "ast": {
                    "node": "temporal_rule",
                    "context": {
                        "node": "clock_reset_context",
                        "clock": {
                            "node": "clock_event",
                            "edge": "posedge",
                            "signal": {"node": "signal_ref", "name": "clk"},
                        },
                    },
                    "property": {
                        "node": "latency_rule",
                        "trigger": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "digest_complete"},
                        },
                        "response": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "done"},
                        },
                        "delay": {"node": "delay_range", "min": 1, "max": 1},
                    },
                },
            }
            partial_element = copy.deepcopy(complete_element)
            partial_element["id"] = "sem2"
            partial_element["formalization_status"] = "needs_human_review"
            partial_element["representation"] = {
                "ast_version": 2,
                "kind": "textual_formalization",
                "text": partial_element["summary"],
                "ast": {"node": "semantic_claim", "text": partial_element["summary"]},
            }
            semantic_ir["semantic_elements"].append(partial_element)

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertFalse(review["completeness"]["covered_claims"])
            self.assertEqual(review["completeness"]["trace_covered_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["placeholder_only_claims"])
            claim_obligation = review["completeness"]["claim_obligations"][0]
            self.assertEqual(claim_obligation["status"], "partial")
            self.assertTrue(
                any(item["code"] == "semantic_claim_placeholder" for item in claim_obligation["missing_obligations"])
            )

    def test_complete_claim_still_reports_blocking_open_question_obligation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            _mark_first_latency_element_complete(semantic_ir)
            semantic_ir["open_questions"] = [
                {
                    "id": "q1",
                    "blocking": True,
                    "status": "open",
                    "question": "Confirm the clock domain for this latency rule.",
                    "related_items": ["sem1"],
                    "claim_ids": ["claim1"],
                    "suggested_answers": ["clk"],
                }
            ]

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            self.assertEqual(review["completeness"]["trace_covered_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["placeholder_only_claims"])
            claim_obligation = review["completeness"]["claim_obligations"][0]
            self.assertEqual(claim_obligation["status"], "partial")
            self.assertTrue(
                any(item["code"] == "blocking_open_question" for item in claim_obligation["missing_obligations"])
            )

    def test_complete_claim_still_reports_semantic_gap_obligation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            _mark_first_latency_element_complete(semantic_ir)
            semantic_ir["semantic_gaps"] = [
                {
                    "id": "gap1",
                    "kind": "missing_context",
                    "reason": "Clock-domain context still needs human confirmation.",
                    "resolution": "Confirm and encode the clock context, or remove the stale gap.",
                    "claim_ids": ["claim1"],
                }
            ]

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            self.assertEqual(review["completeness"]["trace_covered_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["placeholder_only_claims"])
            claim_obligation = review["completeness"]["claim_obligations"][0]
            self.assertEqual(claim_obligation["status"], "partial")
            self.assertTrue(
                any(
                    item["code"] == "semantic_gap_requires_resolution"
                    for item in claim_obligation["missing_obligations"]
                )
            )

    def test_temporal_latency_text_trigger_still_requires_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            element = semantic_ir["semantic_elements"][0]
            element["formalization_status"] = "candidate"
            element["representation"]["ast"]["context"] = {
                "node": "clock_reset_context",
                "clock": {
                    "node": "clock_event",
                    "edge": "posedge",
                    "signal": {"node": "signal_ref", "name": "clk"},
                },
            }

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("placeholder" in issue.message for issue in issues))
            self.assertEqual(review["status"], "failed")
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            obligations = review["completeness"]["claim_obligations"][0]["missing_obligations"]
            self.assertTrue(any(item["code"] == "text_trigger" for item in obligations))

    def test_protocol_handshake_ast_checks_valid_ready_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The bus uses a valid ready handshake.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            element = semantic_ir["semantic_elements"][0]
            representation = semantic_ir["semantic_elements"][0]["representation"]

            self.assertEqual(element["formalization_status"], "needs_human_review")
            self.assertEqual(representation["kind"], "protocol_rule")
            self.assertEqual(representation["ast"]["property"]["node"], "handshake_rule")
            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            self.assertEqual(review["status"], "needs_human_input")
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["placeholder_only_claims"], ["claim1"])
            obligations = review["completeness"]["claim_obligations"][0]["missing_obligations"]
            self.assertTrue(any(item["code"] == "missing_protocol_clock" for item in obligations))

            semantic_ir["semantic_elements"][0]["representation"]["ast"]["property"]["ready"]["name"] = "valid"
            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("valid and ready" in issue.message for issue in issues))

    def test_protocol_rule_requires_property(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The bus uses a valid ready handshake.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["representation"]["ast"].pop("property")

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any(issue.path.endswith(".property") and "required" in issue.message for issue in issues))

    def test_semantic_spec_ir_validation_reports_bad_evidence_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["evidence"][0]["quote"] = "not present in the spec"

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("referenced source line range" in issue.message for issue in issues))

    def test_semantic_ir_normalization_removes_ast_noise_fields(self):
        payload = {
            "semantic_spec_ir": {
                "schema_version": 6,
                "target": "demo_sha",
                "sources": [],
                "spec_claims": [],
                "inputs": [],
                "semantic_context": {"version": 1, "symbols": [], "constraints": []},
                "semantic_elements": [
                    {
                        "id": "elem1",
                        "kind": "functional_behavior",
                        "claim_ids": [],
                        "evidence_ids": [],
                        "subjects": ["out", ""],
                        "formalization_status": "needs_human_review",
                        "representation": {
                            "ast_version": 2,
                            "kind": "textual_formalization",
                            "text": "Output is low.",
                            "ast": {
                                "node": "semantic_claim",
                                "claim_id": "claim1",
                                "claim_kind": "behavior",
                                "subjects": ["out", ""],
                                "statement": "Output is low.",
                                "blocking_issues": ["not formalized"],
                                "intended_formalization": "constant relation",
                            },
                        },
                    }
                ],
                "evidence": [],
                "open_questions": [],
                "assumptions": [],
                "semantic_gaps": [],
                "review": {"status": "draft", "human_answers": [], "reviewed_items": []},
            }
        }

        normalized = normalize_semantic_spec_ir(payload["semantic_spec_ir"])
        ast = normalized["semantic_elements"][0]["representation"]["ast"]

        self.assertNotIn("blocking_issues", ast)
        self.assertNotIn("intended_formalization", ast)
        self.assertNotIn("statement", ast)
        self.assertEqual(ast["text"], "Output is low.")
        self.assertEqual(ast["subjects"], ["out"])
        self.assertEqual(normalized["semantic_elements"][0]["subjects"], ["out"])

    def test_semantic_ir_normalization_canonicalizes_compare_operator(self):
        payload = {
            "schema_version": 6,
            "target": "demo",
            "sources": [],
            "spec_claims": [],
            "inputs": [],
            "semantic_context": {"version": 1, "symbols": [], "constraints": []},
            "semantic_elements": [
                {
                    "id": "elem1",
                    "representation": {
                        "ast": {
                            "node": "constraint",
                            "expr": {
                                "node": "compare",
                                "op": "==",
                                "left": {"node": "signal_ref", "name": "out"},
                                "right": {"node": "literal", "value": 0},
                            },
                        }
                    },
                }
            ],
            "evidence": [],
            "open_questions": [],
            "semantic_gaps": [],
            "review": {"status": "draft", "human_answers": [], "reviewed_items": []},
        }

        normalized = normalize_semantic_spec_ir(payload)

        compare = normalized["semantic_elements"][0]["representation"]["ast"]["expr"]
        self.assertEqual(compare["op"], "eq")

    def test_llmplugin_callable_backend_and_registry_are_available(self):
        def fake_llm(prompt, model):
            return {"semantic_spec_ir": {"target": prompt["target"], "model": model}}

        backend = CallableLLMBackend(fake_llm)
        response = backend.invoke(
            LLMRequest(
                prompt={"target": "demo"},
                model="test-model",
            )
        )

        self.assertIsNotNone(response)
        self.assertEqual(response.parsed_json["semantic_spec_ir"]["target"], "demo")
        self.assertIn("langchain", registered_backend_names())
        self.assertIn("langgraph", registered_backend_names())

    def test_langgraph_backend_wraps_delegate_backend(self):
        delegate = CallableLLMBackend(
            lambda prompt, model: {"semantic_spec_ir": {"target": prompt["target"]}}
        )
        backend = create_langgraph_backend(delegate=delegate)

        response = backend.invoke(LLMRequest(prompt={"target": "demo"}))

        self.assertEqual(response.parsed_json["semantic_spec_ir"]["target"], "demo")

    def test_langchain_backend_uses_env_without_langsmith_probe(self):
        from LLMPlugin.langchain_backend import LangChainBackendConfig, LangChainLLMBackend

        calls = {"headers": None, "config": None}

        class FakeMessage:
            def __init__(self, content):
                self.content = content

        class FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls["headers"] = kwargs.get("default_headers")

            def invoke(self, messages, config):
                calls["config"] = config
                return types.SimpleNamespace(content='{"ok": true}')

        messages_module = types.ModuleType("langchain_core.messages")
        messages_module.HumanMessage = FakeMessage
        messages_module.SystemMessage = FakeMessage
        openai_module = types.ModuleType("langchain_openai")
        openai_module.ChatOpenAI = FakeChatOpenAI
        langsmith_module = types.ModuleType("langsmith")

        def unexpected_client():
            raise AssertionError("LangSmith Client should not be created by the backend")

        langsmith_module.Client = unexpected_client

        with patch.dict(
            sys.modules,
            {
                "langchain_core.messages": messages_module,
                "langchain_openai": openai_module,
                "langsmith": langsmith_module,
            },
        ), patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "LANGSMITH_API_KEY": "test-langsmith-key",
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_PROJECT": "UnitSpec2IR",
            },
            clear=False,
        ):
            backend = LangChainLLMBackend(
                LangChainBackendConfig(
                    model="test-model",
                    base_url="https://example.test/v1",
                    user_agent="UnitAgent/1",
                )
            )
            response = backend.invoke(
                LLMRequest(
                    prompt={"target": "demo"},
                    run_name="unit_trace",
                    tags=("unit",),
                    metadata={"target": "demo"},
                )
            )

        self.assertEqual(response.content, '{"ok": true}')
        self.assertEqual(calls["headers"], {"User-Agent": "UnitAgent/1"})
        self.assertEqual(calls["config"]["run_name"], "unit_trace")

    def test_langchain_backend_loads_local_dotenv_before_backend_creation(self):
        import LLMPlugin.langchain_backend as langchain_backend

        old_cwd = Path.cwd()
        original_loaded = langchain_backend._DOTENV_LOADED
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".env").write_text(
                    "\n".join(
                        [
                            "OPENAI_API_KEY=dotenv-key",
                            "OPENAI_MODEL=dotenv-model",
                            "OPENAI_BASE_URL=https://dotenv.example/v1",
                            "LANGSMITH_TRACING=true",
                            "LANGSMITH_PROJECT=DotenvSpec2IR",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chdir(root)
                langchain_backend._DOTENV_LOADED = False
                with patch.dict(
                    os.environ,
                    {
                        "OPENAI_API_KEY": "",
                        "OPENAI_MODEL": "",
                        "OPENAI_BASE_URL": "",
                        "LANGSMITH_TRACING": "",
                        "LANGSMITH_PROJECT": "",
                    },
                    clear=False,
                ):
                    for key in (
                        "OPENAI_API_KEY",
                        "OPENAI_MODEL",
                        "OPENAI_BASE_URL",
                        "LANGSMITH_TRACING",
                        "LANGSMITH_PROJECT",
                    ):
                        os.environ.pop(key, None)

                    backend = langchain_backend.create_langchain_backend()

                    self.assertIsNotNone(backend)
                    self.assertEqual(os.environ["OPENAI_API_KEY"], "dotenv-key")
                    self.assertEqual(os.environ["OPENAI_MODEL"], "dotenv-model")
                    self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://dotenv.example/v1")
                    self.assertEqual(os.environ["LANGSMITH_PROJECT"], "DotenvSpec2IR")
        finally:
            os.chdir(old_cwd)
            langchain_backend._DOTENV_LOADED = original_loaded

    def test_langchain_backend_normalizes_openai_root_base_url(self):
        from LLMPlugin.langchain_backend import normalize_openai_base_url

        self.assertEqual(normalize_openai_base_url("http://127.0.0.1:18080"), "http://127.0.0.1:18080/v1")
        self.assertEqual(normalize_openai_base_url("https://example.test/v1"), "https://example.test/v1")
        self.assertEqual(
            normalize_openai_base_url("https://example.test/cpa/v1"),
            "https://example.test/cpa/v1",
        )

    def test_semantic_validation_review_reports_complete_claim_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "passed")
            self.assertEqual(review["completeness"]["normative_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["covered_claims"], ["claim1"])
            self.assertFalse(review["completeness"]["uncovered_claims"])
            self.assertEqual(review["semantic_gaps"]["count"], 0)
            coverage = review["completeness"]["obligation_coverage"]
            self.assertEqual(coverage["summary"]["claim_count"], 1)
            self.assertEqual(coverage["summary"]["uncovered_obligation_count"], 0)
            self.assertEqual(coverage["claims"][0]["status"], "covered")
            operation_obligation = next(
                obligation
                for obligation in coverage["claims"][0]["obligations"]
                if obligation["kind"] == "operation"
            )
            self.assertEqual(operation_obligation["status"], "covered")

    def test_semantic_completeness_review_blocks_operation_without_typed_operands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["representation"]["ast"]["operands"] = []

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            self.assertEqual(review["completeness"]["trace_covered_claims"], ["claim1"])
            self.assertEqual(review["completeness"]["partial_claims"], ["claim1"])
            coverage = review["completeness"]["obligation_coverage"]
            operation_obligation = next(
                obligation
                for obligation in coverage["claims"][0]["obligations"]
                if obligation["kind"] == "operation"
            )
            self.assertEqual(operation_obligation["status"], "partial")
            self.assertTrue(
                any(
                    issue["code"] == "operation_operands_missing"
                    for issue in operation_obligation["issues"]
                )
            )

    def test_semantic_completeness_review_blocks_operation_wrong_operand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest_with_two_hex_fields())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["representation"]["ast"]["operands"] = [
                {"node": "field_ref", "name": "key"}
            ]

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            operation_obligation = next(
                obligation
                for obligation in review["completeness"]["obligation_coverage"]["claims"][0]["obligations"]
                if obligation["kind"] == "operation"
            )
            self.assertEqual(operation_obligation["status"], "partial")
            self.assertTrue(
                any(
                    issue["code"] == "operation_operand_mismatch"
                    for issue in operation_obligation["issues"]
                )
            )

    def test_semantic_completeness_review_blocks_truth_table_constant_relation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "truth.toml"
            spec = root / "truth_spec.md"
            manifest.write_text(_truth_table_manifest())
            spec.write_text("x | y\n0 | 1\n")
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_truth",
            )
            semantic_ir["semantic_elements"] = [
                {
                    "id": "sem1",
                    "kind": "combinational_behavior",
                    "summary": "y is always 1",
                    "formalization_status": "candidate",
                    "confidence": 0.8,
                    "subjects": ["x", "y"],
                    "representation": {
                        "ast_version": 2,
                        "kind": "combinational_relation",
                        "text": "y is always 1",
                        "ast": {
                            "node": "constant_relation",
                            "target": {"node": "signal_ref", "name": "y"},
                            "value": {"node": "literal", "value": 1},
                        },
                    },
                    "evidence": [semantic_ir["evidence"][0]["id"]],
                    "claim_ids": ["claim1"],
                    "provenance": {"source": "test"},
                }
            ]
            semantic_ir["semantic_gaps"] = []
            semantic_ir["open_questions"] = []
            _add_semantic_symbol(semantic_ir, "y", typ={"kind": "int"}, roles=["output"])

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_truth",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            truth_obligation = review["completeness"]["obligation_coverage"]["claims"][0]["obligations"][0]
            self.assertEqual(truth_obligation["kind"], "truth_table_row")
            self.assertEqual(truth_obligation["status"], "uncovered")

    def test_semantic_completeness_review_blocks_latency_without_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            element = semantic_ir["semantic_elements"][0]
            element["formalization_status"] = "candidate"
            element["representation"] = {
                "ast_version": 2,
                "kind": "temporal_rule",
                "text": element["summary"],
                "ast": {
                    "node": "temporal_rule",
                    "context": {
                        "node": "clock_reset_context",
                        "clock": {
                            "node": "clock_event",
                            "edge": "posedge",
                            "signal": {"node": "signal_ref", "name": "clk"},
                        },
                    },
                    "property": {
                        "node": "implication",
                        "antecedent": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "digest_complete"},
                        },
                        "consequent": {
                            "node": "rose",
                            "signal": {"node": "signal_ref", "name": "done"},
                        },
                    },
                },
            }
            _add_semantic_symbol(semantic_ir, "clk", typ={"kind": "bool"}, roles=["clock"])
            _add_semantic_symbol(semantic_ir, "digest_complete", typ={"kind": "bool"}, roles=["event"])
            _add_semantic_symbol(semantic_ir, "done", typ={"kind": "bool"}, roles=["event"])

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertFalse(review["completeness"]["covered_claims"])
            timing_obligation = next(
                obligation
                for obligation in review["completeness"]["obligation_coverage"]["claims"][0]["obligations"]
                if obligation["kind"] == "timing"
            )
            self.assertEqual(timing_obligation["status"], "uncovered")

    def test_semantic_review_blocks_incomplete_formalization_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["formalization_status"] = "ambiguous"

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertEqual(review["completeness"]["placeholder_only_claims"], ["claim1"])

    def test_semantic_validation_requires_structured_representation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0].pop("representation")

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("representation" in issue.path for issue in issues))
            self.assertEqual(review["status"], "failed")

    def test_semantic_validation_rejects_legacy_representation_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["representation"] = {
                "type": "relation",
                "text": "legacy loose representation",
                "fields": ["message"],
            }

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("ast_version" in issue.path for issue in issues))
            self.assertTrue(any("is not allowed" in issue.message for issue in issues))
            self.assertEqual(review["status"], "failed")

    def test_semantic_validation_rejects_placeholder_ast_with_candidate_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["formalization_status"] = "candidate"

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertTrue(any("placeholder" in issue.message for issue in issues))
            self.assertEqual(review["status"], "failed")

    def test_semantic_validation_checks_ast_field_refs_against_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            manifest.write_text(_notgate_manifest())
            spec.write_text("When in=0, out=1.\n")
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )
            semantic_ir["semantic_elements"][0]["representation"]["ast"]["condition"]["left"]["name"] = "missing_field"

            issues = collect_semantic_spec_ir_issues(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )

            self.assertTrue(any("missing_field" in issue.message for issue in issues))
            self.assertEqual(review["status"], "failed")

    def test_semantic_completeness_review_blocks_gap_only_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "\n".join(
                    [
                        "The block computes SHA-256 over the input message.",
                        "Reset must clear the busy flag to zero before the next transaction.",
                        "The done signal must pulse exactly one cycle after digest completion.",
                    ]
                )
                + "\n"
            )
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"] = [
                item
                for item in semantic_ir["semantic_elements"]
                if "claim1" in item.get("claim_ids", [])
            ]
            semantic_ir["semantic_gaps"] = [
                {
                    "id": "gap1",
                    "kind": "unformalized",
                    "reason": "Needs semantic completion",
                    "resolution": "human review",
                    "claim_ids": ["claim2", "claim3"],
                }
            ]

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertEqual(
                review["completeness"]["placeholder_only_claims"],
                ["claim2", "claim3"],
            )
            self.assertTrue(
                any(finding["stage"] == "completeness_review" for finding in review["findings"])
            )

    def test_semantic_completeness_review_fails_uncovered_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "The block computes SHA-256 over the input message.\n"
                "Reset must clear the busy flag to zero.\n"
            )
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"] = [
                item
                for item in semantic_ir["semantic_elements"]
                if "claim2" not in item.get("claim_ids", [])
            ]
            semantic_ir["semantic_gaps"] = []

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertEqual(review["completeness"]["uncovered_claims"], ["claim2"])

    def test_semantic_completeness_review_distinguishes_wrapped_claim_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "The block computes SHA-256 over\n"
                "the input message. Reset must clear\n"
                "the busy flag to zero.\n"
            )
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"] = [
                item
                for item in semantic_ir["semantic_elements"]
                if "claim2" not in item.get("claim_ids", [])
            ]
            semantic_ir["semantic_gaps"] = []

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertEqual(review["completeness"]["uncovered_claims"], ["claim2"])

    def test_semantic_spec_ir_formalizes_comb_logic_without_backend_support_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            manifest.write_text(_notgate_manifest())
            spec.write_text(
                "\n".join(
                    [
                        "The module should implement a NOT gate.",
                        "When in=0, out=1. When in=1, out=0.",
                    ]
                )
                + "\n"
            )
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )

            self.assertTrue(semantic_ir["semantic_elements"])
            self.assertEqual(
                semantic_ir["semantic_elements"][1]["representation"]["ast"],
                {
                    "node": "conditional_assignment",
                    "condition": {
                        "node": "compare",
                        "op": "eq",
                        "left": {"node": "field_ref", "name": "in"},
                        "right": {"node": "literal", "value": 0},
                    },
                    "target": {"node": "signal_ref", "name": "out"},
                    "value": {"node": "literal", "value": 1},
                },
            )
            self.assertEqual(review["status"], "passed")
            self.assertEqual(
                sorted(review["completeness"]["covered_claims"]),
                ["claim1", "claim2", "claim3"],
            )

    def test_backend_readiness_marks_comb_logic_for_ref_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            manifest.write_text(_notgate_manifest())
            spec.write_text(
                "\n".join(
                    [
                        "The module should implement a NOT gate.",
                        "When in=0, out=1. When in=1, out=0.",
                    ]
                )
                + "\n"
            )
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )

            readiness = analyze_backend_readiness(
                semantic_ir,
                review=review,
                require_review_passed=True,
            )

            self.assertEqual(readiness["status"], "ready")
            self.assertTrue(readiness["review_gate"]["passed"])
            self.assertGreaterEqual(readiness["summary"]["ref_model_ready_count"], 1)
            self.assertTrue(
                all(
                    "ref_model" in element["recommended_backends"]
                    for element in readiness["elements"]
                )
            )

    def test_backend_readiness_marks_latency_rule_for_sva(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            _mark_first_latency_element_complete(semantic_ir)
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            readiness = analyze_backend_readiness(
                semantic_ir,
                review=review,
                require_review_passed=True,
            )

            self.assertEqual(review["status"], "passed")
            self.assertEqual(readiness["status"], "ready")
            self.assertEqual(readiness["summary"]["sva_ready_count"], 1)
            self.assertEqual(readiness["elements"][0]["recommended_backends"], ["sva"])

    def test_backend_readiness_blocks_textual_formalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            readiness = analyze_backend_readiness(
                semantic_ir,
                review=review,
                require_review_passed=False,
            )

            self.assertEqual(readiness["status"], "needs_human_input")
            self.assertEqual(readiness["elements"][0]["support_status"], "needs_human_input")
            self.assertTrue(readiness["elements"][0]["blockers"])

    def test_backend_readiness_handles_malformed_semantic_elements(self):
        readiness = analyze_backend_readiness(
            {
                "target": "demo",
                "semantic_elements": None,
            }
        )

        self.assertEqual(readiness["status"], "unsupported")
        self.assertEqual(readiness["summary"]["semantic_element_count"], 0)
        self.assertFalse(readiness["elements"])

    def test_ref_model_plan_lowers_notgate_comb_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            manifest.write_text(_notgate_manifest())
            spec.write_text(
                "\n".join(
                    [
                        "The module should implement a NOT gate.",
                        "When in=0, out=1. When in=1, out=0.",
                    ]
                )
                + "\n"
            )
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )
            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_notgate",
            )
            readiness = analyze_backend_readiness(
                semantic_ir,
                review=review,
                require_review_passed=True,
            )

            plan = build_ref_model_plan(semantic_ir, readiness=readiness)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["rule_count"], 3)
            self.assertFalse(plan["blocked_items"])
            operation_rule = next(rule for rule in plan["rules"] if rule["kind"] == "operation_relation")
            self.assertEqual(operation_rule["operation"], "not_gate")
            self.assertEqual(operation_rule["operands"], [{"kind": "field", "name": "in"}])
            conditional_rules = [rule for rule in plan["rules"] if rule["kind"] == "conditional_assignment"]
            self.assertEqual(len(conditional_rules), 2)
            self.assertEqual(conditional_rules[0]["target"], {"kind": "signal", "name": "out"})

    def test_ref_model_plan_lowers_sha_operation_operand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            plan = build_ref_model_plan(semantic_ir)

            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["rule_count"], 1)
            rule = plan["rules"][0]
            self.assertEqual(rule["kind"], "operation_relation")
            self.assertEqual(rule["operation"], "sha256")
            self.assertEqual(rule["operands"], [{"kind": "field", "name": "message"}])
            self.assertEqual(rule["claim_ids"], ["claim1"])

    def test_ref_model_plan_skips_sva_only_latency_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            _mark_first_latency_element_complete(semantic_ir)
            readiness = analyze_backend_readiness(semantic_ir)

            plan = build_ref_model_plan(semantic_ir, readiness=readiness)

            self.assertEqual(plan["status"], "empty")
            self.assertFalse(plan["rules"])
            self.assertEqual(plan["summary"]["not_applicable_count"], 1)
            self.assertEqual(plan["not_applicable"][0]["semantic_element_id"], "sem1")

    def test_ref_model_plan_blocks_textual_formalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])

            plan = build_ref_model_plan(semantic_ir)

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["summary"]["blocked_item_count"], 1)
            self.assertEqual(plan["blocked_items"][0]["semantic_element_id"], "sem1")

    def test_ref_model_plan_blocks_missing_readiness_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            stale_readiness = {
                "schema_version": 1,
                "target": "demo_sha",
                "status": "ready",
                "elements": [],
            }

            plan = build_ref_model_plan(semantic_ir, readiness=stale_readiness)

            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(plan["blocked_items"][0]["reason"], "backend readiness is missing this semantic element")

    def test_semantic_completeness_review_recomputes_source_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "The block computes SHA-256 over the input message.\n"
                "Reset must clear the busy flag to zero.\n"
            )
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["spec_claims"] = []
            for item in semantic_ir["semantic_elements"]:
                item.pop("claim_ids", None)
            semantic_ir["open_questions"] = []
            semantic_ir["semantic_gaps"] = []

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertEqual(review["completeness"]["uncovered_claims"], ["claim1", "claim2"])
            source_coverage = review["completeness"]["source_claim_coverage"]
            self.assertEqual(source_coverage["summary"]["uncovered_span_count"], 2)
            self.assertEqual(len(source_coverage["uncovered_spans"]), 2)
            self.assertTrue(
                any(finding["stage"] == "completeness_review" for finding in review["findings"])
            )
            self.assertTrue(
                any(
                    "source semantic span" in finding["message"]
                    for finding in review["findings"]
                )
            )

    def test_semantic_completeness_review_handles_malformed_ir_spec_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text(
                "The block computes SHA-256 over the input message.\n"
                "Reset must clear the busy flag to zero.\n"
            )
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["spec_claims"] = None

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertEqual(review["completeness"]["uncovered_claims"], ["claim1", "claim2"])
            source_coverage = review["completeness"]["source_claim_coverage"]
            self.assertEqual(source_coverage["summary"]["uncovered_span_count"], 2)
            self.assertFalse(
                any(
                    "could not extract expected spec claims" in finding["message"]
                    for finding in review["findings"]
                )
            )

    def test_semantic_review_requires_spec_paths_for_trusted_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertTrue(
                any(
                    finding["stage"] == "traceability_review"
                    and finding["severity"] == "error"
                    for finding in review["findings"]
                )
            )

    def test_semantic_validation_review_blocks_bad_traceability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["evidence"][0]["quote"] = "missing quote"

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "failed")
            self.assertTrue(
                any(finding["stage"] == "traceability_review" for finding in review["findings"])
            )

    def test_semantic_validation_review_flags_blocking_human_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["open_questions"] = [
                {
                    "id": "q1",
                    "blocking": True,
                    "status": "open",
                    "question": "Confirm whether this candidate formalization is complete.",
                    "related_items": ["sem1"],
                    "claim_ids": ["claim1"],
                    "suggested_answers": ["complete", "needs_more_detail"],
                }
            ]

            review = review_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(review["status"], "needs_human_input")
            self.assertTrue(
                any(finding["stage"] == "human_review_gate" for finding in review["findings"])
            )

    def test_spec2ir_agent_resumes_thread_with_delta_context_and_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            fixed_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir = copy.deepcopy(fixed_ir)
            broken_ir["semantic_elements"][0].pop("representation")
            valid_patch = _replace_semantic_element_patch(broken_ir, fixed_ir)
            runtime = LLMAgentRuntime()

            class ResumingPatchBackend:
                name = "resuming-patch"

                def __init__(self):
                    self.observations = []

                def invoke(self, request):
                    observation = request.prompt["observation"]
                    self.observations.append(observation)
                    if len(self.observations) == 1:
                        stale_patch = copy.deepcopy(valid_patch)
                        stale_patch["base_sha256"] = "0" * 64
                        return LLMResponse(
                            content=json.dumps(
                                {"action": "apply_semantic_ir_patch", "patch": stale_patch}
                            )
                        )
                    return LLMResponse(
                        content=json.dumps(
                            {"action": "apply_semantic_ir_patch", "patch": valid_patch}
                        )
                    )

            backend = ResumingPatchBackend()
            kwargs = {
                "initial_semantic_ir": broken_ir,
                "manifest_path": manifest,
                "spec_paths": [spec],
                "target": "demo_sha",
                "llm_backend": backend,
                "runtime": runtime,
                "thread_id": "spec2ir:test-resume",
                "max_attempts": 1,
            }

            first = run_spec2ir_agent(**kwargs)
            second = run_spec2ir_agent(**kwargs)

            self.assertEqual(first["status"], "resource_exhausted")
            self.assertEqual(second["status"], "repaired")
            self.assertEqual(second["artifact"]["revision"], 1)
            self.assertTrue(second["patch_history"][-1]["accepted"])
            self.assertEqual(second["semantic_ir"]["sources"], broken_ir["sources"])
            self.assertEqual(len(second["attempts"]), 2)
            self.assertEqual(backend.observations[0]["context_mode"], "snapshot")
            self.assertEqual(backend.observations[1]["context_mode"], "delta")
            self.assertNotIn("current_semantic_spec_ir", backend.observations[1])

    def test_spec2ir_harness_start_reviews_and_repairs_deterministic_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["sources"][0]["content_hash"] = "0" * 64
            semantic_ir["evidence"][0]["quote"] = "missing quote"

            harness = Spec2IRHarness(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                initial_semantic_ir=semantic_ir,
            )
            observation = harness.start()

            self.assertEqual(observation["status"], "valid")
            self.assertTrue(harness.is_done())
            self.assertTrue(harness.result()["deterministic_repairs"])
            self.assertEqual(harness.result()["review"]["status"], "passed")

    def test_spec2ir_harness_observation_includes_invalid_action_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0].pop("representation")
            harness = Spec2IRHarness(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                initial_semantic_ir=semantic_ir,
            )
            harness.start()
            harness.apply({"action": "submit_semantic_spec_ir"})

            observation = harness.observe()

            self.assertEqual(observation["last_transition"]["status"], "llm_invalid_response")
            self.assertEqual(observation["last_error"]["type"], "InvalidAction")

    def test_spec2ir_harness_rejects_invalid_patch_without_committing_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            broken_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir["semantic_elements"][0].pop("representation")
            harness = Spec2IRHarness(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                initial_semantic_ir=broken_ir,
            )
            harness.start()

            apply_result = harness.apply(
                {
                    "action": "apply_semantic_ir_patch",
                    "patch": {
                        "base_revision": 0,
                        "base_sha256": semantic_ir_sha256(broken_ir),
                        "operations": [
                            {
                                "op": "replace",
                                "target": {
                                    "collection": "semantic_elements",
                                    "id": broken_ir["semantic_elements"][0]["id"],
                                    "path": "/formalization_status",
                                },
                                "value": "formalized",
                            }
                        ],
                    },
                }
            )
            observation = harness.observe()

            self.assertEqual(apply_result["status"], "patch_rejected")
            self.assertEqual(observation["last_error"]["type"], "SemanticSpecIRValidationError")
            self.assertEqual(harness.result()["artifact"]["revision"], 0)
            self.assertEqual(len(harness.result()["review_history"]), 1)

    def test_spec2ir_harness_exposes_agent_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            harness = Spec2IRHarness(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                initial_semantic_ir=semantic_ir,
            )
            harness.start()

            tool_names = {tool["name"] for tool in harness.tool_specs()}
            unknown = harness.call_tool("get_current_semantic_ir", {})

            self.assertEqual(
                tool_names,
                {
                    "get_review_findings",
                    "get_semantic_ir_fragment",
                    "validate_semantic_ir_patch",
                },
            )
            self.assertEqual(unknown["status"], "tool_error")
            self.assertEqual(unknown["error"]["type"], "UnknownTool")

    def test_spec2ir_agent_uses_tool_result_before_modern_harness_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            fixed_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir = json.loads(json.dumps(fixed_ir))
            broken_ir["semantic_elements"][0].pop("representation")
            semantic_patch = _replace_semantic_element_patch(broken_ir, fixed_ir)
            testcase = self

            class ToolAssistedBackend:
                name = "tool-assisted"

                def __init__(self):
                    self.prompts = []

                def invoke(self, request):
                    self.prompts.append(request.prompt)
                    if len(self.prompts) == 1:
                        testcase.assertIn("tool_specs", request.prompt["observation"])
                        return LLMResponse(
                            content=json.dumps(
                                {
                                    "type": "tool_call",
                                    "tool": "validate_semantic_ir_patch",
                                    "arguments": {"patch": semantic_patch},
                                }
                            )
                        )
                    history = request.prompt["attempt_history"]
                    testcase.assertEqual(history[0]["status"], "tool_result")
                    testcase.assertTrue(history[0]["tool_result"]["valid"])
                    return LLMResponse(
                        content=json.dumps(
                            {
                                "type": "harness_action",
                                "action": "apply_semantic_ir_patch",
                                "patch": semantic_patch,
                            }
                        )
                    )

            backend = ToolAssistedBackend()
            result = run_spec2ir_agent(
                initial_semantic_ir=broken_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=backend,
                max_attempts=2,
            )

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["review"]["status"], "passed")
            self.assertEqual([attempt["status"] for attempt in result["attempts"]], ["tool_result", "repaired"])
            self.assertEqual(result["llm_provenance"]["runtime"], "langgraph")
            self.assertEqual(len(backend.prompts), 2)

    def test_spec2ir_agent_auto_formalizes_machine_resolvable_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The done signal must pulse exactly one cycle after digest completion.\n")
            broken_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            fixed_ir = copy.deepcopy(broken_ir)
            _mark_first_latency_element_complete(fixed_ir)
            semantic_patch = _replace_semantic_element_patch(broken_ir, fixed_ir)
            testcase = self

            class FakeFormalizeBackend:
                name = "fake-formalize"

                def invoke(self, request):
                    testcase.assertEqual(
                        request.prompt["workflow"],
                        "spec2ir_agent_harness",
                    )
                    testcase.assertEqual(
                        request.prompt["observation"]["automation_decision"]["route"],
                        "llm_formalize",
                    )
                    return LLMResponse(
                        content=json.dumps(
                            {
                                "action": "apply_semantic_ir_patch",
                                "patch": semantic_patch,
                            }
                        ),
                    )

            result = run_spec2ir_agent(
                initial_semantic_ir=broken_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=FakeFormalizeBackend(),
                max_attempts=1,
            )

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["review"]["status"], "passed")
            self.assertEqual(result["automation_decisions"][0]["route"], "llm_formalize")

    def test_spec2ir_agent_accepts_custom_automation_policy_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            policy = AutomationPolicyGraph(
                rules=(
                    AutomationPolicyRule(
                        name="local_policy_requires_human",
                        route="human_required",
                        reason="local policy disables LLM formalization",
                        statuses=("needs_human_input",),
                    ),
                )
            )

            class UnexpectedBackend:
                name = "unexpected"

                def invoke(self, request):
                    raise AssertionError("custom human policy must not invoke Spec2IR agent LLM")

            result = run_spec2ir_agent(
                initial_semantic_ir=semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=UnexpectedBackend(),
                automation_policy_graph=policy,
                max_attempts=1,
            )

            self.assertEqual(result["status"], "needs_human_input")
            self.assertEqual(result["attempt_count"], 0)
            self.assertEqual(result["automation_decisions"][0]["policy_rule"], "local_policy_requires_human")

    def test_spec2ir_agent_keeps_true_human_question_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["open_questions"] = [
                {
                    "id": "q1",
                    "blocking": True,
                    "status": "open",
                    "question": "Confirm whether this candidate formalization is complete.",
                    "related_items": ["sem1"],
                    "claim_ids": ["claim1"],
                    "suggested_answers": ["complete", "needs_more_detail"],
                }
            ]

            class UnexpectedBackend:
                name = "unexpected"

                def invoke(self, request):
                    raise AssertionError("human-only questions must not invoke Spec2IR agent LLM")

            result = run_spec2ir_agent(
                initial_semantic_ir=semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=UnexpectedBackend(),
                max_attempts=1,
            )

            self.assertEqual(result["status"], "needs_human_input")
            self.assertEqual(result["attempt_count"], 0)
            self.assertEqual(result["automation_decisions"][0]["route"], "human_required")

    def test_spec2ir_agent_reports_backend_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            broken_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir["semantic_elements"][0].pop("representation")

            class ErrorBackend:
                name = "error-backend"

                def invoke(self, request):
                    raise LLMBackendError("provider is unavailable")

            result = run_spec2ir_agent(
                initial_semantic_ir=broken_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=ErrorBackend(),
                max_attempts=1,
            )

            self.assertEqual(result["status"], "llm_unavailable")
            self.assertEqual(result["llm_provenance"]["attempt_count"], 1)
            self.assertEqual(result["llm_provenance"]["backend"], "error-backend")
            self.assertIn("provider is unavailable", result["attempts"][0]["error"]["message"])
def _mark_first_latency_element_complete(semantic_ir):
    element = semantic_ir["semantic_elements"][0]
    element["formalization_status"] = "candidate"
    element["representation"] = {
        "ast_version": 2,
        "kind": "temporal_rule",
        "text": element["summary"],
        "ast": {
            "node": "temporal_rule",
            "context": {
                "node": "clock_reset_context",
                "clock": {
                    "node": "clock_event",
                    "edge": "posedge",
                    "signal": {"node": "signal_ref", "name": "clk"},
                },
            },
            "property": {
                "node": "latency_rule",
                "trigger": {
                    "node": "rose",
                    "signal": {"node": "signal_ref", "name": "digest_complete"},
                },
                "response": {
                    "node": "rose",
                    "signal": {"node": "signal_ref", "name": "done"},
                },
                "delay": {"node": "delay_range", "min": 1, "max": 1},
            },
        },
    }


def _add_semantic_symbol(semantic_ir, name, *, typ, roles):
    context = semantic_ir.setdefault("semantic_context", {"version": 1, "symbols": [], "constraints": []})
    symbols = context.setdefault("symbols", [])
    for symbol in symbols:
        if symbol.get("name") == name:
            existing_roles = set(symbol.get("roles", []))
            existing_roles.update(roles)
            symbol["roles"] = sorted(existing_roles)
            symbol["type"] = typ
            return
    symbols.append(
        {
            "name": name,
            "kind": "signal",
            "type": typ,
            "direction": "internal",
            "roles": list(roles),
            "source": "test",
        }
    )


def _sha_manifest():
    return (
        "\n".join(
            [
                'name = "demo_sha"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "mode"',
                'kind = "enum"',
                'choices = ["sha224", "sha256"]',
                "",
                "[[field]]",
                'name = "message"',
                'kind = "hex"',
            ]
        )
        + "\n"
    )


def _sha_manifest_with_two_hex_fields():
    return (
        "\n".join(
            [
                'name = "demo_sha"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "mode"',
                'kind = "enum"',
                'choices = ["sha224", "sha256"]',
                "",
                "[[field]]",
                'name = "message"',
                'kind = "hex"',
                "",
                "[[field]]",
                'name = "key"',
                'kind = "hex"',
            ]
        )
        + "\n"
    )


def _notgate_manifest():
    return (
        "\n".join(
            [
                'name = "demo_notgate"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "in"',
                'kind = "int"',
                "min = 0",
                "max = 1",
            ]
        )
        + "\n"
    )


def _truth_table_manifest():
    return (
        "\n".join(
            [
                'name = "demo_truth"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "x"',
                'kind = "int"',
                "min = 0",
                "max = 1",
            ]
        )
        + "\n"
    )


if __name__ == "__main__":
    unittest.main()

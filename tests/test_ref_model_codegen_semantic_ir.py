import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

try:
    from rtlagent_bfm.codegen.cli import main as codegen_cli_main
except ModuleNotFoundError:
    codegen_cli_main = None
from LLMPlugin import (
    CallableLLMBackend,
    LLMBackendError,
    LLMRequest,
    LLMResponse,
    registered_backend_names,
)
from LLMPlugin.langgraph_backend import create_langgraph_backend
from Spec2Backend.BackendReadiness import analyze_backend_readiness
from Spec2Backend.RefModelPlan import build_ref_model_plan
from Spec2Backend.Spec2IR import (
    build_semantic_spec_ir_repair_prompt,
    build_semantic_spec_ir_prompt,
    collect_semantic_spec_ir_issues,
    generate_semantic_spec_ir,
    normalize_semantic_spec_ir_response,
    repair_semantic_spec_ir_with_review,
    review_semantic_spec_ir,
    validate_semantic_spec_ir,
)
from Spec2Backend.Spec2IR.claim_extraction import extract_spec_claims
from Spec2Backend.Spec2IR.schema import SourceDocument


def _require_legacy_codegen_cli():
    if codegen_cli_main is None:
        raise unittest.SkipTest(
            "legacy rtlagent_bfm.codegen CLI was removed; use Spec2Backend Python APIs"
        )


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

            self.assertEqual(semantic_ir["schema_version"], 5)
            self.assertEqual(semantic_ir["target"], "demo_sha")
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

    def test_semantic_spec_ir_generation_accepts_llm_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            expected = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            testcase = self

            class FakeBackend:
                name = "fake"

                def invoke(self, request):
                    testcase.assertEqual(request.model, "test-model")
                    testcase.assertEqual(
                        request.prompt["workflow"],
                        "natural_language_spec_to_traceable_semantic_ir",
                    )
                    testcase.assertIn("semantic_spec_ir", request.prompt["response_contract"])
                    return LLMResponse(
                        content=json.dumps({"result": {"semantic_spec_ir": expected}}),
                    )

            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                llm_backend=FakeBackend(),
                model="test-model",
            )

            self.assertEqual(semantic_ir, expected)
            self.assertEqual(normalize_semantic_spec_ir_response({"semantic_spec_ir": expected}), expected)

    def test_semantic_spec_ir_cli_extracts_and_validates(self):
        _require_legacy_codegen_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            out = root / "semantic_ir.json"
            prompt_out = root / "prompt.json"
            review_out = root / "semantic_review.json"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")

            extract_status = codegen_cli_main(
                [
                    "extract-semantic-ir",
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--out",
                    str(out),
                    "--prompt-out",
                    str(prompt_out),
                ]
            )
            validate_status = codegen_cli_main(
                [
                    "validate-semantic-ir",
                    "--semantic-ir",
                    str(out),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_sha",
                ]
            )
            review_status = codegen_cli_main(
                [
                    "review-semantic-ir",
                    "--semantic-ir",
                    str(out),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_sha",
                    "--out",
                    str(review_out),
                ]
            )

            self.assertEqual(extract_status, 0)
            self.assertEqual(validate_status, 0)
            self.assertEqual(review_status, 0)
            self.assertTrue(out.exists())
            self.assertTrue(review_out.exists())
            self.assertEqual(json.loads(prompt_out.read_text())["workflow"], "natural_language_spec_to_traceable_semantic_ir")
            self.assertEqual(json.loads(review_out.read_text())["status"], "passed")

    def test_semantic_prompt_includes_manifest_and_spec_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")

            prompt = build_semantic_spec_ir_prompt(
                manifest_path=manifest,
                spec_paths=[spec],
            )

            self.assertEqual(prompt["target"], "demo_sha")
            self.assertEqual(prompt["inputs"]["specs"][0]["id"], "src1")
            constraints = " ".join(prompt["constraints"])
            self.assertIn("Every semantic element", constraints)
            self.assertIn("Every normative spec_claim", constraints)
            self.assertIn("independent of backend support", constraints)
            self.assertIn("RepresentationAST", constraints)
            self.assertIn("spec_claim_schema", prompt["semantic_spec_ir_contract"])
            self.assertIn("semantic_element_schema", prompt["semantic_spec_ir_contract"])
            self.assertIn("representation_ast_schema", prompt["semantic_spec_ir_contract"])
            self.assertIn("semantic_gap_schema", prompt["semantic_spec_ir_contract"])

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

    def test_codegen_cli_writes_backend_readiness(self):
        _require_legacy_codegen_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            semantic_ir_path = root / "semantic_ir.json"
            readiness_path = root / "readiness.json"
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
            semantic_ir_path.write_text(json.dumps(semantic_ir), encoding="utf-8")

            status = codegen_cli_main(
                [
                    "analyze-backend-readiness",
                    "--semantic-ir",
                    str(semantic_ir_path),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_notgate",
                    "--require-review-passed",
                    "--out",
                    str(readiness_path),
                ]
            )

            self.assertEqual(status, 0)
            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
            self.assertEqual(readiness["status"], "ready")
            self.assertGreaterEqual(readiness["summary"]["ref_model_ready_count"], 1)

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

    def test_codegen_cli_writes_ref_model_plan(self):
        _require_legacy_codegen_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "notgate.toml"
            spec = root / "notgate_spec.md"
            semantic_ir_path = root / "semantic_ir.json"
            plan_path = root / "ref_model_plan.json"
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
            semantic_ir_path.write_text(json.dumps(semantic_ir), encoding="utf-8")

            status = codegen_cli_main(
                [
                    "build-ref-model-plan",
                    "--semantic-ir",
                    str(semantic_ir_path),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_notgate",
                    "--require-review-passed",
                    "--out",
                    str(plan_path),
                ]
            )

            self.assertEqual(status, 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "ready")
            self.assertEqual(plan["summary"]["rule_count"], 3)

    def test_codegen_cli_ref_model_plan_blocks_when_review_required(self):
        _require_legacy_codegen_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            semantic_ir_path = root / "semantic_ir.json"
            plan_path = root / "ref_model_plan.json"
            manifest.write_text(_sha_manifest())
            spec.write_text("This block has documented behavior.\n")
            semantic_ir = generate_semantic_spec_ir(
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )
            semantic_ir_path.write_text(json.dumps(semantic_ir), encoding="utf-8")

            status = codegen_cli_main(
                [
                    "build-ref-model-plan",
                    "--semantic-ir",
                    str(semantic_ir_path),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_sha",
                    "--require-review-passed",
                    "--out",
                    str(plan_path),
                ]
            )

            self.assertEqual(status, 2)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "blocked_by_readiness")

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

    def test_semantic_repair_prompt_includes_review_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["evidence"][0]["quote"] = "missing quote"

            prompt = build_semantic_spec_ir_repair_prompt(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

            self.assertEqual(prompt["workflow"], "semantic_spec_ir_validation_feedback_repair")
            self.assertEqual(prompt["review_report"]["status"], "failed")
            self.assertTrue(prompt["review_report"]["findings"])
            self.assertIn("semantic_spec_ir", prompt["response_contract"])
            self.assertIn("semantic_elements", " ".join(prompt["constraints"]))
            self.assertIn("independent of backend support", " ".join(prompt["constraints"]))
            self.assertEqual(prompt["inputs"]["specs"][0]["path"], str(spec))

    def test_semantic_repair_prompt_accepts_generator_spec_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["evidence"][0]["quote"] = "missing quote"

            prompt = build_semantic_spec_ir_repair_prompt(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=(path for path in [spec]),
                target="demo_sha",
            )

            self.assertEqual(prompt["inputs"]["specs"][0]["path"], str(spec))
            self.assertEqual(prompt["review_report"]["status"], "failed")

    def test_semantic_repair_loop_accepts_fixed_llm_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            fixed_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir = json.loads(json.dumps(fixed_ir))
            broken_ir["evidence"][0]["quote"] = "missing quote"
            testcase = self

            class FakeRepairBackend:
                name = "fake-repair"

                def invoke(self, request):
                    testcase.assertEqual(
                        request.prompt["workflow"],
                        "semantic_spec_ir_validation_feedback_repair",
                    )
                    return LLMResponse(
                        content=json.dumps({"semantic_spec_ir": fixed_ir}),
                    )

            result = repair_semantic_spec_ir_with_review(
                broken_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=FakeRepairBackend(),
                max_attempts=1,
            )

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["review"]["status"], "passed")
            self.assertEqual(result["semantic_ir"], fixed_ir)

    def test_semantic_repair_loop_reports_backend_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            broken_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            broken_ir["evidence"][0]["quote"] = "missing quote"

            class ErrorBackend:
                name = "error-backend"

                def invoke(self, request):
                    raise LLMBackendError("provider is unavailable")

            result = repair_semantic_spec_ir_with_review(
                broken_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
                llm_backend=ErrorBackend(),
                max_attempts=1,
            )

            self.assertEqual(result["status"], "llm_unavailable")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["llm_responses"][0]["backend"], "error-backend")
            self.assertIn("provider is unavailable", result["llm_responses"][0]["message"])

    def test_semantic_repair_cli_writes_prompt_and_review_without_llm(self):
        _require_legacy_codegen_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            semantic_ir_path = root / "semantic_ir.json"
            prompt_out = root / "repair_prompt.json"
            review_out = root / "repair_review.json"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["evidence"][0]["quote"] = "missing quote"
            semantic_ir_path.write_text(json.dumps(semantic_ir))

            status = codegen_cli_main(
                [
                    "repair-semantic-ir",
                    "--semantic-ir",
                    str(semantic_ir_path),
                    "--manifest",
                    str(manifest),
                    "--spec",
                    str(spec),
                    "--target",
                    "demo_sha",
                    "--prompt-out",
                    str(prompt_out),
                    "--review-out",
                    str(review_out),
                ]
            )

            self.assertEqual(status, 1)
            self.assertTrue(prompt_out.exists())
            self.assertTrue(review_out.exists())
            self.assertEqual(json.loads(review_out.read_text())["status"], "failed")


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

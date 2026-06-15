import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from rtlagent_bfm.codegen.cli import main as codegen_cli_main
from LLMPlugin import (
    CallableLLMBackend,
    LLMBackendError,
    LLMRequest,
    LLMResponse,
    registered_backend_names,
)
from LLMPlugin.langgraph_backend import create_langgraph_backend
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

            self.assertEqual(semantic_ir["schema_version"], 3)
            self.assertEqual(semantic_ir["target"], "demo_sha")
            self.assertEqual(semantic_ir["sources"][0]["content_hash"], hashlib.sha256(spec.read_bytes()).hexdigest())
            self.assertEqual([claim["id"] for claim in semantic_ir["spec_claims"]], ["claim1"])
            self.assertEqual(
                semantic_ir["semantic_elements"][0]["claim_ids"],
                ["claim1"],
            )
            self.assertEqual(semantic_ir["evidence"][0]["line_start"], 2)
            self.assertIn("SHA-224", semantic_ir["evidence"][0]["quote"])
            self.assertEqual(semantic_ir["semantic_elements"][0]["kind"], "combinational_behavior")
            self.assertEqual(semantic_ir["semantic_elements"][0]["formalization_status"], "candidate")
            self.assertEqual(semantic_ir["semantic_elements"][0]["representation"]["type"], "relation")
            self.assertEqual(semantic_ir["review"]["status"], "draft")
            self.assertFalse(semantic_ir["semantic_gaps"])
            validate_semantic_spec_ir(
                semantic_ir,
                manifest_path=manifest,
                spec_paths=[spec],
                target="demo_sha",
            )

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
            self.assertIn("spec_claim_schema", prompt["semantic_spec_ir_contract"])
            self.assertIn("semantic_element_schema", prompt["semantic_spec_ir_contract"])
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

    def test_semantic_validation_checks_representation_fields_against_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(_sha_manifest())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])
            semantic_ir["semantic_elements"][0]["representation"]["fields"] = ["missing_field"]

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
            self.assertEqual(review["status"], "passed")
            self.assertEqual(
                sorted(review["completeness"]["covered_claims"]),
                ["claim1", "claim2", "claim3"],
            )

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
            self.assertTrue(
                any(finding["stage"] == "completeness_review" for finding in review["findings"])
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


if __name__ == "__main__":
    unittest.main()

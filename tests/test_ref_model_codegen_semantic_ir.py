import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from rtlagent_bfm.codegen.cli import main as codegen_cli_main
from LLMPlugin import (
    CallableLLMBackend,
    LLMRequest,
    LLMResponse,
    registered_backend_names,
)
from LLMPlugin.langgraph_backend import create_langgraph_backend
from Spec2Backend.Spec2IR import (
    build_semantic_spec_ir_prompt,
    collect_semantic_spec_ir_issues,
    generate_semantic_spec_ir,
    normalize_semantic_spec_ir_response,
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

            self.assertEqual(semantic_ir["schema_version"], 1)
            self.assertEqual(semantic_ir["target"], "demo_sha")
            self.assertEqual(semantic_ir["sources"][0]["content_hash"], hashlib.sha256(spec.read_bytes()).hexdigest())
            self.assertEqual(semantic_ir["evidence"][0]["line_start"], 2)
            self.assertIn("SHA-224", semantic_ir["evidence"][0]["quote"])
            self.assertEqual(
                [item["effects"][0]["expr"]["call"] for item in semantic_ir["semantic_items"]],
                ["hashlib.sha224", "hashlib.sha256"],
            )
            self.assertEqual(
                semantic_ir["semantic_items"][0]["conditions"],
                [{"field": "mode", "op": "eq", "value": "sha224"}],
            )
            self.assertEqual(semantic_ir["review"]["status"], "draft")
            self.assertTrue(semantic_ir["open_questions"])
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
            self.assertIn("Every semantic item", " ".join(prompt["constraints"]))

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

    def test_semantic_validation_review_reports_lowering_ready_items(self):
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
            self.assertEqual(review["lowering"]["ready_items"], ["sem1"])
            self.assertFalse(review["lowering"]["blocked_items"])

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
            manifest.write_text(_sha_manifest_with_two_hex_fields())
            spec.write_text("The block computes SHA-256 over the input message.\n")
            semantic_ir = generate_semantic_spec_ir(manifest_path=manifest, spec_paths=[spec])

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


if __name__ == "__main__":
    unittest.main()

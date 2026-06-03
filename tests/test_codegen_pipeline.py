import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from rtlagent_bfm.codegen.artifacts import ArtifactBundle, ArtifactBundleError
from rtlagent_bfm.codegen.manifest import update_manifest_text
from rtlagent_bfm.codegen.oracle_codegen import (
    build_oracle_plugin_bundle,
    write_oracle_plugin_bundle,
)
from rtlagent_bfm.codegen.oracle_eval import evaluate_oracle_ir
from rtlagent_bfm.codegen.oracle_feedback import (
    build_oracle_ir_repair_prompt,
    normalize_oracle_ir_response,
    repair_oracle_ir_with_feedback,
)
from rtlagent_bfm.codegen.oracle_ir import (
    OracleIRValidationError,
    collect_oracle_ir_issues,
    generate_oracle_ir,
    validate_oracle_ir,
)
from rtlagent_bfm.codegen.pipeline import CodegenPipelineConfig, finalize_bundle
from rtlagent_bfm.codegen.prompt import build_generation_prompt
from rtlagent_bfm.codegen.validation import (
    ArtifactValidationError,
    GoldenCase,
    validate_artifact_dir,
)


MINIMAL_IR = {
    "design": {"top": "demo_top"},
    "interfaces": {
        "control": {
            "protocol": "demo",
            "clock": "clk",
            "reset": "rst",
            "signals": {"req": "req"},
        }
    },
    "bindings": {
        "clk": {"role": "clock", "hdl_path": "clk"},
        "rst": {"role": "reset", "hdl_path": "rst_n", "active": "low"},
        "req": {"role": "control.req", "hdl_path": "req_i", "width": 1},
    },
}


class TestCodegenPipeline(unittest.TestCase):
    def test_artifact_bundle_rejects_path_escape(self):
        with self.assertRaisesRegex(ArtifactBundleError, "escape"):
            ArtifactBundle.from_dict(
                {
                    "files": [
                        {
                            "path": "../generated/ref_model.py",
                            "content": "class RefModel: pass\n",
                        }
                    ]
                }
            )

    def test_static_validation_rejects_process_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            plugin = artifact_dir / "bad.py"
            plugin.write_text("import subprocess\nsubprocess.run(['true'])\n")

            with self.assertRaisesRegex(ArtifactValidationError, "subprocess"):
                validate_artifact_dir(artifact_dir, target="demo")

    def test_finalize_bundle_validates_promotes_and_updates_manifest(self):
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
                        'name = "op"',
                        'kind = "enum"',
                        'choices = ["read", "write"]',
                    ]
                )
                + "\n"
            )
            bundle_path = root / "llm_bundle.json"
            bundle_path.write_text(json.dumps(_valid_bundle()))

            copied = finalize_bundle(
                CodegenPipelineConfig(
                    bundle_path=bundle_path,
                    candidate_dir=root / "candidates" / "run_001",
                    final_dir=root / "final",
                    target="demo",
                    ref_model="generated.ref_model:DemoRefModel",
                    scoreboard="generated.scoreboard:DemoScoreboard",
                    manifest_path=manifest,
                    bfm_ir="generated/final/demo_ir.json",
                    golden_cases=(
                        GoldenCase(
                            target="demo",
                            data={"target": "demo", "op": "write", "value": 7},
                            expected="write:7",
                        ),
                    ),
                )
            )

            self.assertEqual(len(copied), 3)
            self.assertTrue((root / "final" / "generated" / "ref_model.py").exists())
            manifest_text = manifest.read_text()
            self.assertIn('bfm_ir = "generated/final/demo_ir.json"', manifest_text)
            self.assertIn('ref_model = "generated.ref_model:DemoRefModel"', manifest_text)
            self.assertIn('scoreboard = "generated.scoreboard:DemoScoreboard"', manifest_text)

    def test_generation_prompt_includes_validated_ir_and_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "demo.toml"
            ir_path = root / "demo_ir.json"
            spec_path = root / "spec.md"
            contracts = root / "plugin_contracts.md"
            manifest.write_text('name = "demo"\ndriver = "demo_driver:Driver"\n')
            ir_path.write_text(json.dumps(MINIMAL_IR))
            spec_path.write_text("# Demo spec\nwrite returns op:value\n")
            contracts.write_text("Reference model and scoreboard plugin contracts.\n")

            prompt = build_generation_prompt(
                manifest_path=manifest,
                ir_path=ir_path,
                spec_paths=[spec_path],
                plugin_contracts_path=contracts,
            )

            self.assertEqual(prompt["workflow"], "ir_then_direct_plugin_candidate")
            self.assertIn("Demo spec", prompt["inputs"]["specs"][0]["content"])
            self.assertIn("demo_ir.json", prompt["inputs"]["bfm_ir"]["path"])

    def test_manifest_update_replaces_existing_top_level_keys(self):
        updated = update_manifest_text(
            "\n".join(
                [
                    'name = "demo"',
                    'ref_model = "old:Ref"',
                    "",
                    "[signals]",
                    'ref_model = "not_top_level"',
                ]
            )
            + "\n",
            {
                "ref_model": "new:Ref",
                "scoreboard": "new:Scoreboard",
            },
        )

        self.assertIn('ref_model = "new:Ref"', updated)
        self.assertIn('scoreboard = "new:Scoreboard"', updated)
        self.assertIn('ref_model = "not_top_level"', updated)

    def test_generate_oracle_ir_infers_sha_hashlib_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "sha.toml"
            spec = root / "sha_spec.md"
            manifest.write_text(
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
            spec.write_text("The block computes SHA-224 or SHA-256 over the input message.\n")

            oracle_ir = generate_oracle_ir(manifest_path=manifest, spec_paths=[spec])

            self.assertEqual(oracle_ir["target"], "demo_sha")
            self.assertEqual([item["name"] for item in oracle_ir["inputs"]], ["mode", "message"])
            self.assertEqual(len(oracle_ir["rules"]), 2)
            self.assertEqual(oracle_ir["rules"][0]["expected"]["call"], "hashlib.sha224")
            self.assertEqual(oracle_ir["rules"][1]["expected"]["call"], "hashlib.sha256")
            self.assertEqual(oracle_ir["compare"], {"kind": "exact", "normalize": ["lower_hex"]})
            validate_oracle_ir(oracle_ir, manifest_path=manifest, require_rules=True)

    def test_oracle_ir_validation_reports_unknown_field_and_disallowed_call(self):
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
                        'name = "payload"',
                        'kind = "hex"',
                    ]
                )
                + "\n"
            )
            oracle_ir = {
                "schema_version": 1,
                "target": "demo",
                "inputs": [{"name": "payload", "type": "hex_bytes"}],
                "rules": [
                    {
                        "expected": {
                            "call": "os.system",
                            "args": [{"bytes_from_hex": {"field": "missing"}}],
                            "format": "hexdigest",
                        }
                    }
                ],
                "compare": {"kind": "exact", "normalize": ["lower_hex"]},
            }

            issues = collect_oracle_ir_issues(oracle_ir, manifest_path=manifest)

            self.assertTrue(any("os.system" in issue.message for issue in issues))
            self.assertTrue(any("missing" in issue.message for issue in issues))
            with self.assertRaises(OracleIRValidationError):
                validate_oracle_ir(oracle_ir, manifest_path=manifest)

    def test_oracle_ir_validation_can_require_prediction_rules(self):
        oracle_ir = {
            "schema_version": 1,
            "target": "demo",
            "inputs": [{"name": "op", "type": "enum"}],
            "rules": [],
            "compare": {"kind": "exact", "normalize": []},
            "unsupported": [
                {
                    "reason": "manual rules required",
                    "requires": "spec review",
                }
            ],
        }

        validate_oracle_ir(oracle_ir)
        issues = collect_oracle_ir_issues(oracle_ir, require_rules=True)

        self.assertTrue(any("at least one prediction rule" in issue.message for issue in issues))

    def test_oracle_ir_repair_prompt_includes_validation_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "demo.toml"
            manifest.write_text(_demo_payload_manifest())
            invalid_ir = _invalid_payload_oracle_ir()

            prompt = build_oracle_ir_repair_prompt(
                invalid_ir,
                manifest_path=manifest,
                require_rules=True,
            )

            self.assertEqual(prompt["workflow"], "oracle_ir_validation_feedback_repair")
            messages = [issue["message"] for issue in prompt["validation_issues"]]
            self.assertTrue(any("os.system" in message for message in messages))
            self.assertIn("oracle_ir", prompt["response_contract"])
            self.assertIn("hashlib.sha256", prompt["oracle_ir_contract"]["allowed_calls"])

    def test_oracle_ir_feedback_loop_accepts_repaired_llm_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "demo.toml"
            manifest.write_text(_demo_payload_manifest())
            repaired_ir = {
                "schema_version": 1,
                "target": "demo",
                "inputs": [{"name": "payload", "type": "hex_bytes"}],
                "rules": [
                    {
                        "name": "sha256_payload",
                        "expected": {
                            "call": "hashlib.sha256",
                            "args": [{"bytes_from_hex": {"field": "payload"}}],
                            "format": "hexdigest",
                        },
                    }
                ],
                "compare": {"kind": "exact", "normalize": ["lower_hex"]},
            }

            def fake_llm(prompt, model):
                self.assertEqual(model, "test-model")
                self.assertTrue(prompt["validation_issues"])
                return {"oracle_ir": repaired_ir, "changes": ["use allowlisted hashlib call"]}

            result = repair_oracle_ir_with_feedback(
                _invalid_payload_oracle_ir(),
                manifest_path=manifest,
                require_rules=True,
                llm_callable=fake_llm,
                model="test-model",
                max_attempts=1,
            )

            self.assertEqual(result["status"], "repaired")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["oracle_ir"]["rules"][0]["expected"]["call"], "hashlib.sha256")

    def test_oracle_ir_response_normalizer_accepts_nested_result(self):
        oracle_ir = {
            "schema_version": 1,
            "target": "demo",
            "inputs": [{"name": "payload", "type": "hex_bytes"}],
            "rules": [],
            "compare": {"kind": "exact", "normalize": []},
            "unsupported": [{"reason": "manual review"}],
        }

        self.assertEqual(
            normalize_oracle_ir_response({"result": {"oracle_ir": oracle_ir}}),
            oracle_ir,
        )

    def test_oracle_ir_evaluator_computes_sha_expected(self):
        oracle_ir = _sha256_payload_oracle_ir()

        expected = evaluate_oracle_ir(oracle_ir, {"payload": "616263"})

        self.assertEqual(expected, hashlib.sha256(b"abc").hexdigest())

    def test_oracle_ir_codegen_bundle_validates_with_golden_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle_path = root / "oracle_bundle.json"
            bundle = build_oracle_plugin_bundle(
                _sha256_payload_oracle_ir(),
                target="demo",
                package="generated",
            )
            write_oracle_plugin_bundle(bundle_path, bundle)

            self.assertEqual(
                bundle.metadata["ref_model"],
                "generated.demo_ref_model:GeneratedOracleRefModel",
            )
            copied = finalize_bundle(
                CodegenPipelineConfig(
                    bundle_path=bundle_path,
                    candidate_dir=root / "candidate",
                    final_dir=root / "final",
                    target="demo",
                    ref_model=bundle.metadata["ref_model"],
                    golden_cases=(
                        GoldenCase(
                            target="demo",
                            data={"target": "demo", "payload": "616263"},
                            expected=hashlib.sha256(b"abc").hexdigest(),
                        ),
                    ),
                )
            )

            self.assertEqual(len(copied), 2)
            self.assertTrue((root / "final" / "generated" / "demo_ref_model.py").exists())


def _valid_bundle():
    return {
        "files": [
            {"path": "generated/__init__.py", "content": ""},
            {
                "path": "generated/ref_model.py",
                "content": "\n".join(
                    [
                        "class DemoRefModel:",
                        "    def __init__(self, target=None, config=None):",
                        "        self.target = target",
                        "",
                        "    def predict(self, case):",
                        "        return {'expected': f\"{case.data['op']}:{case.data['value']}\"}",
                        "",
                    ]
                ),
            },
            {
                "path": "generated/scoreboard.py",
                "content": "\n".join(
                    [
                        "class DemoScoreboard:",
                        "    def __init__(self, target=None, config=None):",
                        "        self.target = target",
                        "        self.records = []",
                        "",
                        "    def write(self, record):",
                        "        self.records.append(record)",
                        "",
                        "    def check(self):",
                        "        return None",
                        "",
                        "    def summary(self):",
                        "        return {'target': self.target, 'checked': len(self.records), 'failures': 0}",
                        "",
                    ]
                ),
            },
        ],
        "assumptions": ["demo reference behavior"],
        "required_tests": ["write golden case"],
    }


def _demo_payload_manifest():
    return (
        "\n".join(
            [
                'name = "demo"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "payload"',
                'kind = "hex"',
            ]
        )
        + "\n"
    )


def _invalid_payload_oracle_ir():
    return {
        "schema_version": 1,
        "target": "demo",
        "inputs": [{"name": "payload", "type": "hex_bytes"}],
        "rules": [
            {
                "expected": {
                    "call": "os.system",
                    "args": [{"bytes_from_hex": {"field": "missing"}}],
                    "format": "hexdigest",
                }
            }
        ],
        "compare": {"kind": "exact", "normalize": ["lower_hex"]},
    }


def _sha256_payload_oracle_ir():
    return {
        "schema_version": 1,
        "target": "demo",
        "inputs": [{"name": "payload", "type": "hex_bytes"}],
        "rules": [
            {
                "name": "sha256_payload",
                "expected": {
                    "call": "hashlib.sha256",
                    "args": [{"bytes_from_hex": {"field": "payload"}}],
                    "format": "hexdigest",
                },
            }
        ],
        "compare": {"kind": "exact", "normalize": ["lower_hex"]},
    }


if __name__ == "__main__":
    unittest.main()

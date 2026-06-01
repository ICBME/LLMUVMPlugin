import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_bfm.plugin_loader import build_plugin  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_examples.secworks_aes import aes_decrypt_block, aes_encrypt_block  # noqa: E402
from fuzz_examples.secworks_sha256 import Sha256RefModel, padded_sha_blocks  # noqa: E402


class TestSecworksExamples(unittest.TestCase):
    def test_aes_reference_model_matches_nist_vectors(self):
        key128 = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        key256 = bytes.fromhex(
            "000102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f"
        )
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        cipher128 = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        cipher256 = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")

        self.assertEqual(aes_encrypt_block(key128, plaintext), cipher128)
        self.assertEqual(aes_decrypt_block(key128, cipher128), plaintext)
        self.assertEqual(aes_encrypt_block(key256, plaintext), cipher256)
        self.assertEqual(aes_decrypt_block(key256, cipher256), plaintext)

    def test_sha_reference_model_matches_hashlib(self):
        model = Sha256RefModel()
        case = SimpleNamespace(data={"mode": "sha256", "message": "616263"})

        self.assertEqual(model.predict(case).expected, hashlib.sha256(b"abc").hexdigest())

        case = SimpleNamespace(data={"mode": "sha224", "message": "616263"})
        self.assertEqual(model.predict(case).expected, hashlib.sha224(b"abc").hexdigest())
        self.assertEqual(len(padded_sha_blocks(b"abc")), 1)

    def test_example_manifests_load_plugins(self):
        aes = load_target_config("secworks_aes")
        sha = load_target_config("secworks_sha256")

        self.assertEqual(aes.toplevel, "aes")
        self.assertEqual(sha.toplevel, "sha256")
        self.assertEqual(aes.fields[0].name, "op")
        self.assertEqual(sha.fields[0].name, "mode")
        self.assertTrue(callable(build_plugin(aes.ref_model, target=aes.name, config=aes).predict))
        self.assertTrue(callable(build_plugin(sha.ref_model, target=sha.name, config=sha).predict))

    def test_example_coverage_models_sample_cases(self):
        aes = load_target_config("secworks_aes")
        model = build_plugin(aes.coverage_model, target=aes.name, config=aes)
        case = SimpleNamespace(
            target="secworks_aes",
            line_no=1,
            data={
                "target": "secworks_aes",
                "op": "encrypt",
                "key_len": 128,
                "key": "000102030405060708090a0b0c0d0e0f",
                "block": "00112233445566778899aabbccddeeff",
            },
        )

        model.sample(case)
        summary = model.to_json()

        self.assertEqual(summary["total_cases"], 1)
        self.assertEqual(summary["bins"]["op"], {"encrypt": 1})


if __name__ == "__main__":
    unittest.main()

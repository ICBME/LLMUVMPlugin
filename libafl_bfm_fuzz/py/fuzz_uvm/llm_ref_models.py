from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import sys

from fuzz_bfm.corpus import FuzzCase, bytes_to_words, hex_to_bytes, words_to_bytes
from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.ref_models import ExpectedResult


THIS_DIR = Path(__file__).resolve()
AES_MODEL_DIR = THIS_DIR.parents[3] / "example" / "aes" / "src" / "model" / "python"


class AesRefModel:
    """LLM-generated adapter around the trusted AES Python model."""

    def __init__(self, target: str = "aes", config: TargetConfig | None = None):
        self.target = target
        self.config = config
        self.model = _build_aes_model()
        self.model.VERBOSE = False
        self.model.DUMP_VARS = False

    def predict(self, case: FuzzCase) -> ExpectedResult:
        key = hex_to_bytes(str(case.data["key"]))
        block = hex_to_bytes(str(case.data["block"]))
        encipher = case.data["encdec"] == "encipher"
        key_words = tuple(bytes_to_words(key))
        block_words = tuple(bytes_to_words(block))
        with redirect_stdout(io.StringIO()):
            if encipher:
                result = self.model.aes_encipher_block(key_words, block_words)
            else:
                result = self.model.aes_decipher_block(key_words, block_words)
        return ExpectedResult(
            expected=words_to_bytes(list(result)).hex(),
            detail=f"aes_ref key_len={int(case.data['key_len'])} encdec={case.data['encdec']}",
        )


class Sha256RefModel:
    """LLM-generated adapter around Python hashlib for SHA-224/SHA-256."""

    def __init__(self, target: str = "sha256", config: TargetConfig | None = None):
        self.target = target
        self.config = config

    def predict(self, case: FuzzCase) -> ExpectedResult:
        mode = str(case.data["mode"])
        message = hex_to_bytes(str(case.data["message"]))
        if mode == "sha256":
            digest = hashlib.sha256(message).digest()
        else:
            digest = hashlib.sha224(message).digest()
        return ExpectedResult(
            expected=digest.hex(),
            detail=f"hashlib_ref mode={mode} len={len(message)}",
        )


def _build_aes_model():
    if str(AES_MODEL_DIR) not in sys.path:
        sys.path.insert(0, str(AES_MODEL_DIR))
    from aes import AES

    return AES()

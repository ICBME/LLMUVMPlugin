from __future__ import annotations

from typing import Any

from fuzz_pipeline.harness_plugins import HarnessGapActionabilityContext


class SecworksAesHarnessOptimizationPlugin:
    gap_actionability_classifiers = ()

    def __init__(self) -> None:
        self.gap_actionability_classifiers = (aes_gap_actionability_classifier,)


def build_plugin(**kwargs: Any) -> SecworksAesHarnessOptimizationPlugin:
    return SecworksAesHarnessOptimizationPlugin()


def aes_gap_actionability_classifier(
    gap: dict[str, Any],
    context: HarnessGapActionabilityContext,
) -> dict[str, Any] | None:
    target = context.target
    code = str(gap.get("code") or "")
    file_name = str(gap.get("file") or "")
    line = int(_number_value(gap.get("line")) or 0)
    result = dict(gap)
    if target != "secworks_aes":
        return None
    if file_name.endswith("/aes.v") or file_name.endswith("aes.v"):
        readback_payload = aes_readback_payload_for_gap(code=code, line=line)
        if readback_payload:
            result["actionability"] = "reachable_with_mmio_readback"
            result["actionability_reason"] = (
                "AES top-level read address gap can be exercised by sampling "
                "symbolic MMIO registers or explicit safe read addresses after "
                "the normal transaction."
            )
            result["recommended_action_type"] = "mmio_readback"
            result["suggested_payload"] = readback_payload
            return result
        if "ADDR_BLOCK" in code and "address" in code:
            result["actionability"] = "requires_mmio_write_surface"
            result["actionability_reason"] = (
                "This write-side address expression needs an explicit MMIO write "
                "surface or driver extension; readback alone cannot toggle it."
            )
            result["recommended_action_type"] = "mmio_write"
            result["suggested_payload"] = {"addresses": ["0x24"]}
            return result
    internal_files = (
        "aes_core.v",
        "aes_key_mem.v",
        "aes_encipher_block.v",
        "aes_decipher_block.v",
    )
    if "default" in code or "_ctrl" in code or file_name.endswith(internal_files):
        result["actionability"] = "requires_internal_state_surface"
        result["actionability_reason"] = (
            "Gap appears to be an internal defensive/default branch that cannot be "
            "reliably driven by the current transaction or MMIO readback action."
        )
        return result
    return None


def aes_readback_payload_for_gap(*, code: str, line: int) -> dict[str, Any]:
    if "ADDR_RESULT" in code:
        return {"addresses": ["0x34"]}
    registers = aes_readback_registers_for_gap(code=code, line=line)
    if registers:
        return {"registers": registers}
    return {}


def aes_readback_registers_for_gap(*, code: str, line: int) -> list[str]:
    if any(name in code for name in ("ADDR_NAME0", "ADDR_NAME1", "ADDR_VERSION")):
        return ["ADDR_NAME0", "ADDR_NAME1", "ADDR_VERSION"]
    if "ADDR_CTRL" in code:
        return ["ADDR_CTRL"]
    if "ADDR_STATUS" in code:
        return ["ADDR_STATUS"]
    if 253 <= line <= 257:
        return [
            "ADDR_NAME0",
            "ADDR_NAME1",
            "ADDR_VERSION",
            "ADDR_CTRL",
            "ADDR_STATUS",
        ]
    return []


def _number_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(int(value, 0))
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None

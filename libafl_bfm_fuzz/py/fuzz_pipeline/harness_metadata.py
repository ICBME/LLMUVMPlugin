"""Compatibility facade for legacy harness metadata imports.

New code should prefer ``fuzz_pipeline.harness_evidence.metadata`` for
UVM-fuzz-specific metadata and ``harness_optimization.metadata`` for the
shared protocol.
"""

from harness_optimization.metadata import HarnessMetadataExtractorProtocol

from .harness_evidence.metadata import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

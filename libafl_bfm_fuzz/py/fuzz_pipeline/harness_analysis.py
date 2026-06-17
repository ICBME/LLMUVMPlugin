"""Compatibility facade for legacy harness analysis imports.

New code should prefer ``fuzz_pipeline.harness_evidence.analysis``.
"""

from .harness_evidence.analysis import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

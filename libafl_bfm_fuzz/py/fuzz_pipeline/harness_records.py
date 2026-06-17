"""Compatibility facade for legacy harness record imports.

New code should prefer ``fuzz_pipeline.harness_evidence.records``.
"""

from .harness_evidence.records import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

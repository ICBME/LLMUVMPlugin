"""Compatibility facade for legacy harness trace imports.

New code should prefer ``fuzz_pipeline.harness_evidence.trace``.
"""

from .harness_evidence.trace import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

"""Compatibility facade for legacy harness plugin imports.

New code should prefer ``fuzz_pipeline.harness_evidence.plugins``.
"""

from .harness_evidence.plugins import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

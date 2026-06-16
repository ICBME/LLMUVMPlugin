"""Compatibility facade for legacy harness LLM task imports.

New code should prefer ``fuzz_pipeline.harness_evidence.llm_tasks``.
"""

from .harness_evidence.llm_tasks import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]

"""Coverage and LLM-guided mutation feedback for LibAFL BFM fuzzing."""

from .coverage import build_summary
from .coverage_export import CoverageExport, CoveragePoint
from .advisors import propose_directives, propose_directives_from_plan, validate_directives
from .feedback_loop import build_gap_feedback, build_mutation_feedback, update_mutation_directions
from .mutation_planner import plan_mutations_from_rtl_gaps
from .rtl_gap import RTL_GAP_DOMAIN, build_rtl_gap_export, build_rtl_gap_summary
from .rtl_structure_coverage import (
    RTL_STRUCTURAL_COVERAGE_DOMAIN,
    RTL_STRUCTURAL_COVERAGE_KINDS,
    build_rtl_structure_coverage,
    build_rtl_structure_coverage_export,
)

__all__ = [
    "CoverageExport",
    "CoveragePoint",
    "RTL_GAP_DOMAIN",
    "RTL_STRUCTURAL_COVERAGE_DOMAIN",
    "RTL_STRUCTURAL_COVERAGE_KINDS",
    "build_rtl_gap_export",
    "build_rtl_gap_summary",
    "build_rtl_structure_coverage",
    "build_rtl_structure_coverage_export",
    "build_summary",
    "build_gap_feedback",
    "build_mutation_feedback",
    "plan_mutations_from_rtl_gaps",
    "propose_directives",
    "propose_directives_from_plan",
    "update_mutation_directions",
    "validate_directives",
]

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


THIS_DIR = Path(__file__).resolve().parent
PY_DIR = THIS_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_bfm.corpus import load_cases  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_pipeline.harness import close_observation, observation_context_from_env  # noqa: E402
from fuzz_pipeline.orchestrator import PipelineContext, PipelineOrchestrator, StepSpec  # noqa: E402
from fuzz_pipeline.topology import FULL_FUZZ_TOPOLOGY  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate LibAFL BFM JSONL corpus")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()

    try:
        cases = run_corpus_validation_pipeline(args.corpus, args.target)
    finally:
        close_observation()
    print(f"validated {len(cases)} {args.target} cases in {args.corpus}")
    return 0


def validate_corpus(corpus: Path, target: str):
    config = load_target_config(target)
    return load_cases(corpus, target, config=config)


def run_corpus_validation_pipeline(corpus: Path, target: str):
    context = PipelineContext(
        run_id=observation_context_from_env().run_id,
        artifacts={"corpus": corpus},
        metadata={"target": target},
    )
    step = StepSpec(
        name="corpus_to_validation",
        connector="corpus_to_validation",
        handler=lambda _context: validate_corpus(corpus, target),
        input_roles=("corpus",),
        output_roles=("corpus",),
        metrics=lambda value: {"case_count": len(value)},
    )
    topology_out = os.getenv("CONNECTOR_TOPOLOGY_OUT")
    PipelineOrchestrator(
        FULL_FUZZ_TOPOLOGY,
        observation_context_from_env(),
        topology_out=Path(topology_out) if topology_out and topology_out.strip() else None,
    ).run([step], context)
    return context.values["corpus_to_validation"]


if __name__ == "__main__":
    raise SystemExit(main())

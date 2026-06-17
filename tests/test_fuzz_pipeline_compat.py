from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_pipeline.run_plan import RunStageHandler  # noqa: E402
from fuzz_pipeline.topology import (  # noqa: E402
    topology_out_from_env,
    validate_connector_endpoint,
    write_topology,
    write_topology_from_env,
)


def test_fuzz_pipeline_compat_exports_are_available() -> None:
    assert RunStageHandler is not None
    assert callable(topology_out_from_env)
    assert callable(validate_connector_endpoint)
    assert callable(write_topology)
    assert callable(write_topology_from_env)

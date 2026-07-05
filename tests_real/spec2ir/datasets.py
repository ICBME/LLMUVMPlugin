"""Dataset discovery for Spec2IR real-data evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VERILOGEVAL_ROOT = REPO_ROOT / "example" / "verilog-eval" / "dataset_spec-to-rtl"
VERILOGEVAL_ROOT_ENV = "SPEC2IR_VERILOGEVAL_ROOT"


@dataclass(frozen=True)
class RealDataCase:
    dataset: str
    case_id: str
    prompt_path: Path
    ref_path: Path | None = None
    test_path: Path | None = None
    metadata: dict[str, str] | None = None

    @property
    def has_reference(self) -> bool:
        return self.ref_path is not None and self.ref_path.exists()

    @property
    def has_testbench(self) -> bool:
        return self.test_path is not None and self.test_path.exists()

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "case_id": self.case_id,
            "prompt_path": str(self.prompt_path),
            "ref_path": str(self.ref_path) if self.ref_path is not None else None,
            "test_path": str(self.test_path) if self.test_path is not None else None,
            "metadata": self.metadata or {},
        }


def verilogeval_root_from_env() -> Path:
    return Path(os.environ.get(VERILOGEVAL_ROOT_ENV, str(DEFAULT_VERILOGEVAL_ROOT)))


def iter_verilogeval_cases(root: str | Path | None = None) -> Iterable[RealDataCase]:
    dataset_root = Path(root) if root is not None else verilogeval_root_from_env()
    if not dataset_root.exists():
        return
    for prompt_path in sorted(dataset_root.glob("*_prompt.txt")):
        case_id = prompt_path.name.removesuffix("_prompt.txt")
        ref_path = dataset_root / f"{case_id}_ref.sv"
        test_path = dataset_root / f"{case_id}_test.sv"
        yield RealDataCase(
            dataset="verilogeval",
            case_id=case_id,
            prompt_path=prompt_path,
            ref_path=ref_path if ref_path.exists() else None,
            test_path=test_path if test_path.exists() else None,
            metadata={"root": str(dataset_root)},
        )


def load_verilogeval_cases(
    root: str | Path | None = None,
    *,
    limit: int | None = None,
    case_ids: set[str] | None = None,
) -> list[RealDataCase]:
    cases = [
        case
        for case in iter_verilogeval_cases(root)
        if case_ids is None or case.case_id in case_ids
    ]
    if limit is not None:
        return cases[: max(0, limit)]
    return cases


def require_cases(cases: list[RealDataCase], *, dataset: str) -> list[RealDataCase]:
    if not cases:
        raise FileNotFoundError(f"no {dataset} cases were found")
    return cases


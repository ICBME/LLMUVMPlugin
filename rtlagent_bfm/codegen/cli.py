"""Command line entry points for the initial LLM codegen flow."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .oracle_ir import (
    collect_oracle_ir_issues,
    generate_oracle_ir,
    load_oracle_ir,
    write_oracle_ir,
)
from .pipeline import CodegenPipelineConfig, finalize_bundle, promote_candidate, write_candidate_bundle
from .prompt import write_generation_prompt
from .validation import load_golden_cases, validate_artifact_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rtlagent-codegen")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt = subparsers.add_parser("write-prompt")
    prompt.add_argument("--manifest", required=True, type=Path)
    prompt.add_argument("--ir", required=True, type=Path)
    prompt.add_argument("--spec", action="append", default=[], type=Path)
    prompt.add_argument("--plugin-contracts", default=Path("docs/reference/plugin_contracts.md"), type=Path)
    prompt.add_argument("--out", required=True, type=Path)

    oracle_ir = subparsers.add_parser("generate-oracle-ir")
    oracle_ir.add_argument("--manifest", required=True, type=Path)
    oracle_ir.add_argument("--spec", action="append", default=[], type=Path)
    oracle_ir.add_argument("--target")
    oracle_ir.add_argument("--out", required=True, type=Path)

    validate_oracle_ir = subparsers.add_parser("validate-oracle-ir")
    validate_oracle_ir.add_argument("--oracle-ir", required=True, type=Path)
    validate_oracle_ir.add_argument("--manifest", type=Path)
    validate_oracle_ir.add_argument("--target")
    validate_oracle_ir.add_argument("--require-rules", action="store_true")

    candidate = subparsers.add_parser("write-candidate")
    candidate.add_argument("--bundle", required=True, type=Path)
    candidate.add_argument("--candidate-dir", required=True, type=Path)

    validate = subparsers.add_parser("validate")
    _add_validation_args(validate)
    validate.add_argument("--artifact-dir", required=True, type=Path)

    promote = subparsers.add_parser("promote")
    promote.add_argument("--candidate-dir", required=True, type=Path)
    promote.add_argument("--final-dir", required=True, type=Path)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--bundle", required=True, type=Path)
    finalize.add_argument("--candidate-dir", required=True, type=Path)
    finalize.add_argument("--final-dir", required=True, type=Path)
    finalize.add_argument("--manifest", type=Path)
    finalize.add_argument("--bfm-ir")
    _add_validation_args(finalize)

    args = parser.parse_args(argv)
    if args.command == "write-prompt":
        write_generation_prompt(
            args.out,
            manifest_path=args.manifest,
            ir_path=args.ir,
            spec_paths=args.spec,
            plugin_contracts_path=args.plugin_contracts,
        )
        print(f"wrote prompt: {args.out}")
        return 0
    if args.command == "generate-oracle-ir":
        ir = generate_oracle_ir(
            manifest_path=args.manifest,
            spec_paths=tuple(args.spec),
            target=args.target,
        )
        write_oracle_ir(args.out, ir)
        print(f"wrote OracleIR: {args.out}")
        return 0
    if args.command == "validate-oracle-ir":
        issues = collect_oracle_ir_issues(
            load_oracle_ir(args.oracle_ir),
            manifest_path=args.manifest,
            target=args.target,
            require_rules=args.require_rules,
        )
        if issues:
            for issue in issues:
                print(issue.format(), file=sys.stderr)
            return 1
        print(f"validated OracleIR: {args.oracle_ir}")
        return 0
    if args.command == "write-candidate":
        written = write_candidate_bundle(args.bundle, args.candidate_dir)
        print(f"wrote {len(written)} candidate files to {args.candidate_dir}")
        return 0
    if args.command == "validate":
        validate_artifact_dir(
            args.artifact_dir,
            target=args.target,
            ref_model=args.ref_model,
            scoreboard=args.scoreboard,
            extra_python_paths=tuple(args.python_path),
            golden_cases=_load_optional_golden(args.golden_cases, args.target),
        )
        print(f"validated generated artifacts: {args.artifact_dir}")
        return 0
    if args.command == "promote":
        copied = promote_candidate(args.candidate_dir, args.final_dir)
        print(f"promoted {len(copied)} files to {args.final_dir}")
        return 0
    if args.command == "finalize":
        copied = finalize_bundle(
            CodegenPipelineConfig(
                bundle_path=args.bundle,
                candidate_dir=args.candidate_dir,
                final_dir=args.final_dir,
                target=args.target,
                ref_model=args.ref_model,
                scoreboard=args.scoreboard,
                manifest_path=args.manifest,
                bfm_ir=args.bfm_ir,
                extra_python_paths=tuple(args.python_path),
                golden_cases=_load_optional_golden(args.golden_cases, args.target),
            )
        )
        print(f"finalized {len(copied)} generated files in {args.final_dir}")
        return 0
    raise AssertionError(f"unhandled command {args.command}")


def _add_validation_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True)
    parser.add_argument("--ref-model")
    parser.add_argument("--scoreboard")
    parser.add_argument("--golden-cases", type=Path)
    parser.add_argument("--python-path", action="append", default=[], type=Path)


def _load_optional_golden(path: Path | None, target: str):
    if path is None:
        return ()
    return load_golden_cases(path, target)


if __name__ == "__main__":
    sys.exit(main())

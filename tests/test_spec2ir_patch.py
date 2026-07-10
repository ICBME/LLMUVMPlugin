import copy

import pytest

from Spec2Backend.Spec2IR import (
    SemanticIRPatchError,
    apply_semantic_ir_patch,
    semantic_ir_sha256,
)
from Spec2Backend.Spec2IR.harness import review_progress
from Spec2Backend.Spec2IR.representation_ast_validation import validate_representation


def sample_ir() -> dict:
    return {
        "schema_version": 6,
        "target": "demo",
        "sources": [{"id": "src1", "content_hash": "trusted"}],
        "spec_claims": [{"id": "claim1", "quote": "trusted"}],
        "evidence": [{"id": "ev1", "quote": "trusted"}],
        "semantic_elements": [
            {
                "id": "sem1",
                "claim_ids": ["claim1"],
                "evidence": ["ev1"],
                "formalization_status": "needs_human_review",
                "representation": {"kind": "semantic_claim", "text": "placeholder"},
            }
        ],
        "open_questions": [],
        "semantic_gaps": [],
    }


def patch_for(ir: dict, operations: list[dict], *, revision: int = 0) -> dict:
    return {
        "base_revision": revision,
        "base_sha256": semantic_ir_sha256(ir),
        "operations": operations,
    }


def test_semantic_ir_patch_replaces_item_field_without_regenerating_ir() -> None:
    ir = sample_ir()
    result = apply_semantic_ir_patch(
        ir,
        patch_for(
            ir,
            [
                {
                    "op": "replace",
                    "target": {
                        "collection": "semantic_elements",
                        "id": "sem1",
                        "path": "/formalization_status",
                    },
                    "value": "formalized",
                }
            ],
        ),
        revision=0,
    )

    assert result.changed is True
    assert result.semantic_ir["semantic_elements"][0]["formalization_status"] == "formalized"
    assert result.semantic_ir["sources"] == ir["sources"]
    assert ir["semantic_elements"][0]["formalization_status"] == "needs_human_review"


def test_semantic_ir_patch_rejects_stale_revision_and_hash() -> None:
    ir = sample_ir()
    operation = {
        "op": "remove",
        "target": {"collection": "semantic_elements", "id": "sem1", "path": "/representation"},
    }

    with pytest.raises(SemanticIRPatchError, match="base_revision"):
        apply_semantic_ir_patch(ir, patch_for(ir, [operation], revision=1), revision=0)

    stale_hash_patch = patch_for(ir, [operation])
    stale_hash_patch["base_sha256"] = "0" * 64
    with pytest.raises(SemanticIRPatchError, match="base_sha256"):
        apply_semantic_ir_patch(ir, stale_hash_patch, revision=0)

    invalid_revision_patch = patch_for(ir, [operation])
    invalid_revision_patch["base_revision"] = False
    with pytest.raises(SemanticIRPatchError, match="base_revision"):
        apply_semantic_ir_patch(ir, invalid_revision_patch, revision=0)

    invalid_hash_patch = patch_for(ir, [operation])
    invalid_hash_patch["base_sha256"] = "z" * 64
    with pytest.raises(SemanticIRPatchError, match="base_sha256"):
        apply_semantic_ir_patch(ir, invalid_hash_patch, revision=0)


def test_semantic_ir_patch_is_atomic_and_protects_identity_fields() -> None:
    ir = sample_ir()
    original = copy.deepcopy(ir)
    patch = patch_for(
        ir,
        [
            {
                "op": "replace",
                "target": {
                    "collection": "semantic_elements",
                    "id": "sem1",
                    "path": "/formalization_status",
                },
                "value": "formalized",
            },
            {
                "op": "replace",
                "target": {
                    "collection": "semantic_elements",
                    "id": "sem1",
                    "path": "/claim_ids",
                },
                "value": [],
            },
        ],
    )

    with pytest.raises(SemanticIRPatchError, match="immutable"):
        apply_semantic_ir_patch(ir, patch, revision=0)
    assert ir == original


def test_review_progress_uses_stable_finding_code_instead_of_message_literals() -> None:
    previous = {
        "status": "failed",
        "findings": [
            {
                "stage": "schema_review",
                "severity": "error",
                "blocking": True,
                "path": "semantic_elements[0].representation.ast.op",
                "message": "must be one of ['eq', 'ne']",
            }
        ],
        "completeness": {"covered_claims": []},
    }
    current = copy.deepcopy(previous)
    current["findings"][0]["message"] = "must be one of ['add', 'sub']"

    progress = review_progress(previous, current)

    assert progress["persistent_count"] == 1
    assert progress["resolved_count"] == 0
    assert progress["introduced_count"] == 0
    assert progress["classification"] == "equivalent"


def test_representation_slice_accepts_bit_zero_and_rejects_reversed_range() -> None:
    representation = {
        "ast_version": 2,
        "kind": "constraint",
        "text": "Select bit zero.",
        "ast": {
            "node": "constraint",
            "expr": {
                "node": "slice",
                "value": {"node": "signal_ref", "name": "in"},
                "msb": 0,
                "lsb": 0,
            },
        },
    }
    issues = []

    validate_representation(representation, "representation", set(), issues)

    assert issues == []

    representation["ast"]["expr"]["msb"] = 0
    representation["ast"]["expr"]["lsb"] = 1
    validate_representation(representation, "representation", set(), issues)
    assert any(issue.message == "must satisfy msb >= lsb" for issue in issues)

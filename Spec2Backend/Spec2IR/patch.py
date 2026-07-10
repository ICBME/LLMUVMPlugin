"""Transactional, revisioned patches for the LLM-editable SemanticSpecIR plane."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import Any


PATCH_SCHEMA_VERSION = 1
MAX_PATCH_OPERATIONS = 32
MAX_PATCH_BYTES = 128 * 1024
EDITABLE_COLLECTIONS = frozenset(
    {"semantic_elements", "open_questions", "semantic_gaps"}
)
IMMUTABLE_ITEM_FIELDS = frozenset({"id", "claim_ids", "evidence", "source_id"})


class SemanticIRPatchError(ValueError):
    """Raised when a SemanticIR patch is malformed, stale, or unsafe."""


@dataclass(frozen=True)
class SemanticIRPatchResult:
    semantic_ir: dict[str, Any]
    patch: dict[str, Any]
    changed: bool
    before_sha256: str
    after_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch": self.patch,
            "changed": self.changed,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
        }


def semantic_ir_sha256(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_semantic_ir_patch(
    semantic_ir: dict[str, Any],
    patch_payload: dict[str, Any],
    *,
    revision: int,
) -> SemanticIRPatchResult:
    patch = normalize_semantic_ir_patch(patch_payload)
    before_sha256 = semantic_ir_sha256(semantic_ir)
    if patch["base_revision"] != revision:
        raise SemanticIRPatchError(
            f"stale patch base_revision {patch['base_revision']}; current revision is {revision}"
        )
    if patch["base_sha256"] != before_sha256:
        raise SemanticIRPatchError(
            "stale patch base_sha256 does not match the current SemanticSpecIR"
        )

    candidate = copy.deepcopy(semantic_ir)
    for index, operation in enumerate(patch["operations"]):
        try:
            _apply_operation(candidate, operation)
        except SemanticIRPatchError as exc:
            raise SemanticIRPatchError(f"operations[{index}]: {exc}") from exc

    after_sha256 = semantic_ir_sha256(candidate)
    return SemanticIRPatchResult(
        semantic_ir=candidate,
        patch=patch,
        changed=after_sha256 != before_sha256,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
    )


def normalize_semantic_ir_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SemanticIRPatchError("patch must be an object")
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
    encoded_size = len(json.dumps(patch, ensure_ascii=False, default=str).encode("utf-8"))
    if encoded_size > MAX_PATCH_BYTES:
        raise SemanticIRPatchError(f"patch exceeds {MAX_PATCH_BYTES} bytes")
    if patch.get("schema_version", PATCH_SCHEMA_VERSION) != PATCH_SCHEMA_VERSION:
        raise SemanticIRPatchError(
            f"patch schema_version must be {PATCH_SCHEMA_VERSION}"
        )
    base_revision = patch.get("base_revision")
    base_sha256 = patch.get("base_sha256")
    operations = patch.get("operations")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool) or base_revision < 0:
        raise SemanticIRPatchError("base_revision must be a non-negative integer")
    if (
        not isinstance(base_sha256, str)
        or len(base_sha256) != 64
        or any(character not in "0123456789abcdef" for character in base_sha256.lower())
    ):
        raise SemanticIRPatchError("base_sha256 must be a sha256 string")
    if not isinstance(operations, list) or not operations:
        raise SemanticIRPatchError("operations must be a non-empty list")
    if len(operations) > MAX_PATCH_OPERATIONS:
        raise SemanticIRPatchError(
            f"patch has {len(operations)} operations; maximum is {MAX_PATCH_OPERATIONS}"
        )
    normalized_operations = [normalize_patch_operation(item) for item in operations]
    normalized = {
        "schema_version": PATCH_SCHEMA_VERSION,
        "base_revision": base_revision,
        "base_sha256": base_sha256,
        "operations": normalized_operations,
    }
    if isinstance(patch.get("resolves"), list):
        normalized["resolves"] = [str(item) for item in patch["resolves"]]
    if isinstance(patch.get("rationale"), str):
        normalized["rationale"] = patch["rationale"]
    return normalized


def normalize_patch_operation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticIRPatchError("each operation must be an object")
    op = str(value.get("op") or "")
    if op not in {"add", "replace", "remove", "test"}:
        raise SemanticIRPatchError(f"unsupported operation {op!r}")
    target = value.get("target")
    if not isinstance(target, dict):
        raise SemanticIRPatchError("operation target must be an object")
    collection = str(target.get("collection") or "")
    if collection not in EDITABLE_COLLECTIONS:
        raise SemanticIRPatchError(f"collection {collection!r} is not LLM-editable")
    item_id = target.get("id")
    if item_id is not None and not isinstance(item_id, str):
        raise SemanticIRPatchError("target id must be a string")
    path = str(target.get("path") or "")
    if path and not path.startswith("/"):
        raise SemanticIRPatchError("target path must be empty or a JSON Pointer")
    first_token = decode_json_pointer(path)[0] if path else ""
    if item_id is not None and first_token in IMMUTABLE_ITEM_FIELDS:
        raise SemanticIRPatchError(f"field {first_token!r} is immutable")
    if item_id is None and path:
        raise SemanticIRPatchError("collection-level operations cannot define path")
    if op in {"replace", "remove", "test"} and item_id is None:
        raise SemanticIRPatchError(f"{op} requires target id")
    if op in {"add", "replace", "test"} and "value" not in value:
        raise SemanticIRPatchError(f"{op} requires value")
    operation = {
        "op": op,
        "target": {
            "collection": collection,
            **({"id": item_id} if item_id is not None else {}),
            **({"path": path} if path else {}),
        },
    }
    if "value" in value:
        operation["value"] = copy.deepcopy(value["value"])
    return operation


def semantic_ir_patch_contract() -> dict[str, Any]:
    return {
        "schema_version": PATCH_SCHEMA_VERSION,
        "base_revision": "must equal artifact revision",
        "base_sha256": "must equal artifact sha256",
        "operations": [
            {
                "op": "add | replace | remove | test",
                "target": {
                    "collection": sorted(EDITABLE_COLLECTIONS),
                    "id": "stable item id; omit only when adding a new collection item",
                    "path": "/relative/JSON/Pointer inside the item",
                },
                "value": "required for add, replace, and test",
            }
        ],
        "protected_fields": sorted(IMMUTABLE_ITEM_FIELDS),
    }


def _apply_operation(document: dict[str, Any], operation: dict[str, Any]) -> None:
    op = operation["op"]
    target = operation["target"]
    collection_name = target["collection"]
    collection = document.get(collection_name)
    if not isinstance(collection, list):
        raise SemanticIRPatchError(f"{collection_name} is not a list")
    item_id = target.get("id")
    if item_id is None:
        value = copy.deepcopy(operation.get("value"))
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise SemanticIRPatchError("new collection item must be an object with string id")
        if any(isinstance(item, dict) and item.get("id") == value["id"] for item in collection):
            raise SemanticIRPatchError(f"duplicate item id {value['id']!r}")
        collection.append(value)
        return

    item_index = next(
        (
            index
            for index, item in enumerate(collection)
            if isinstance(item, dict) and item.get("id") == item_id
        ),
        None,
    )
    if item_index is None:
        raise SemanticIRPatchError(f"unknown item id {item_id!r} in {collection_name}")
    path = str(target.get("path") or "")
    if not path:
        if op == "remove":
            del collection[item_index]
            return
        if op == "test":
            if collection[item_index] != operation.get("value"):
                raise SemanticIRPatchError("test operation failed")
            return
        replacement = copy.deepcopy(operation.get("value"))
        if not isinstance(replacement, dict) or replacement.get("id") != item_id:
            raise SemanticIRPatchError("whole-item replacement must preserve id")
        for field in IMMUTABLE_ITEM_FIELDS:
            if replacement.get(field) != collection[item_index].get(field):
                raise SemanticIRPatchError(f"whole-item replacement changed immutable field {field!r}")
        collection[item_index] = replacement
        return
    _apply_pointer_operation(collection[item_index], path, op, operation.get("value"))


def _apply_pointer_operation(root: Any, path: str, op: str, value: Any) -> None:
    tokens = decode_json_pointer(path)
    if not tokens:
        raise SemanticIRPatchError("item-relative path cannot be empty here")
    parent = root
    for token in tokens[:-1]:
        parent = _lookup_pointer_token(parent, token)
    token = tokens[-1]
    if op == "test":
        if _lookup_pointer_token(parent, token) != value:
            raise SemanticIRPatchError("test operation failed")
        return
    if isinstance(parent, dict):
        if op == "remove":
            if token not in parent:
                raise SemanticIRPatchError(f"path token {token!r} does not exist")
            del parent[token]
        elif op == "replace":
            if token not in parent:
                raise SemanticIRPatchError(f"path token {token!r} does not exist")
            parent[token] = copy.deepcopy(value)
        else:
            parent[token] = copy.deepcopy(value)
        return
    if isinstance(parent, list):
        if token == "-" and op == "add":
            parent.append(copy.deepcopy(value))
            return
        index = _list_index(token, len(parent), allow_end=op == "add")
        if op == "remove":
            del parent[index]
        elif op == "add":
            parent.insert(index, copy.deepcopy(value))
        else:
            parent[index] = copy.deepcopy(value)
        return
    raise SemanticIRPatchError(f"cannot modify child of {type(parent).__name__}")


def _lookup_pointer_token(parent: Any, token: str) -> Any:
    if isinstance(parent, dict):
        if token not in parent:
            raise SemanticIRPatchError(f"path token {token!r} does not exist")
        return parent[token]
    if isinstance(parent, list):
        return parent[_list_index(token, len(parent))]
    raise SemanticIRPatchError(f"cannot traverse {type(parent).__name__}")


def _list_index(token: str, length: int, *, allow_end: bool = False) -> int:
    try:
        index = int(token)
    except ValueError as exc:
        raise SemanticIRPatchError(f"list token {token!r} is not an index") from exc
    maximum = length if allow_end else length - 1
    if index < 0 or index > maximum:
        raise SemanticIRPatchError(f"list index {index} is out of range")
    return index


def decode_json_pointer(path: str) -> list[str]:
    if not path:
        return []
    return [token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/")]


def escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


__all__ = [
    "EDITABLE_COLLECTIONS",
    "PATCH_SCHEMA_VERSION",
    "SemanticIRPatchError",
    "SemanticIRPatchResult",
    "apply_semantic_ir_patch",
    "normalize_semantic_ir_patch",
    "semantic_ir_patch_contract",
    "semantic_ir_sha256",
]

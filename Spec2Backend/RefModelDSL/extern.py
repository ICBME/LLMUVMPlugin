"""External-call runtime support for RefModelIR."""

from __future__ import annotations

import ctypes
import hashlib
from pathlib import Path
from typing import Any, Mapping

from .schema import RefModelDSLError, json_safe, normalize_extern_path


PYTHON_STDLIB_ALLOWLIST = {
    "hashlib.sha224",
    "hashlib.sha256",
    "hashlib.sha384",
    "hashlib.sha512",
}


class ExternRegistry:
    """Runtime registry for pure external functions declared by RefModelIR."""

    def __init__(self, externs: Mapping[str, Any] | None = None, *, base_dir: str | Path = "."):
        self.externs = dict(externs or {})
        self.base_dir = Path(base_dir)
        self._libraries: dict[str, ctypes.CDLL] = {}

    def call(self, extern_id: str, args: list[Any], *, result: str | None = None) -> Any:
        if extern_id not in self.externs:
            raise RefModelDSLError(f"extern {extern_id!r} is not declared")
        spec = self.externs[extern_id]
        if not isinstance(spec, Mapping):
            raise RefModelDSLError(f"extern {extern_id!r} spec must be a mapping")
        kind = str(spec.get("kind") or "")
        if kind == "python_stdlib":
            return self._call_python_stdlib(extern_id, spec, args, result=result)
        if kind == "c_abi":
            return self._call_c_abi(extern_id, spec, args)
        if kind == "systemc_worker":
            raise RefModelDSLError("systemc_worker externs are declared but not executable in v1")
        raise RefModelDSLError(f"unsupported extern kind {kind!r} for {extern_id!r}")

    def _call_python_stdlib(
        self,
        extern_id: str,
        spec: Mapping[str, Any],
        args: list[Any],
        *,
        result: str | None,
    ) -> Any:
        binding = str(spec.get("binding") or extern_id)
        if binding not in PYTHON_STDLIB_ALLOWLIST:
            raise RefModelDSLError(f"python_stdlib extern {binding!r} is not allowlisted")
        if len(args) != 1:
            raise RefModelDSLError(f"{extern_id}: hashlib externs expect one bytes argument")
        data = args[0]
        if isinstance(data, str):
            data = data.encode()
        if not isinstance(data, bytes):
            raise RefModelDSLError(f"{extern_id}: hashlib argument must be bytes")
        algorithm = binding.rsplit(".", 1)[1]
        digest = getattr(hashlib, algorithm)(data)
        if result in {None, "hex", "hex_string"}:
            return digest.hexdigest()
        if result == "bytes":
            return digest.digest()
        raise RefModelDSLError(f"{extern_id}: unsupported hashlib result codec {result!r}")

    def _call_c_abi(self, extern_id: str, spec: Mapping[str, Any], args: list[Any]) -> Any:
        library = normalize_extern_path(spec.get("library"))
        function = str(spec.get("function") or "")
        if not function:
            raise RefModelDSLError(f"{extern_id}: c_abi extern must define function")
        cdll = self._load_library(library)
        func = getattr(cdll, function)
        arg_codecs = tuple(str(item) for item in spec.get("arg_codecs", ()))
        if len(arg_codecs) != len(args):
            raise RefModelDSLError(f"{extern_id}: arg_codecs count does not match args")
        c_args: list[Any] = []
        argtypes: list[Any] = []
        for codec, value in zip(arg_codecs, args):
            c_value, c_type = _encode_c_arg(codec, value)
            c_args.append(c_value)
            argtypes.append(c_type)
        return_codec = str(spec.get("return_codec") or "int64")
        func.argtypes = argtypes
        func.restype = _ctype_for_return(return_codec)
        return json_safe(_decode_c_return(return_codec, func(*c_args)))

    def _load_library(self, library: str) -> ctypes.CDLL:
        if library not in self._libraries:
            self._libraries[library] = ctypes.CDLL(str(self.base_dir / library))
        return self._libraries[library]


def _encode_c_arg(codec: str, value: Any) -> tuple[Any, Any]:
    if codec in {"int", "int64"}:
        return ctypes.c_int64(int(value)), ctypes.c_int64
    if codec in {"uint64"}:
        return ctypes.c_uint64(int(value)), ctypes.c_uint64
    if codec in {"bytes", "hex_bytes"}:
        data = bytes.fromhex(value) if isinstance(value, str) and codec == "hex_bytes" else value
        if not isinstance(data, bytes):
            raise RefModelDSLError(f"C ABI codec {codec!r} expects bytes")
        return ctypes.c_char_p(data), ctypes.c_char_p
    raise RefModelDSLError(f"unsupported C ABI argument codec {codec!r}")


def _ctype_for_return(codec: str) -> Any:
    if codec in {"int", "int64"}:
        return ctypes.c_int64
    if codec == "uint64":
        return ctypes.c_uint64
    raise RefModelDSLError(f"unsupported C ABI return codec {codec!r}")


def _decode_c_return(codec: str, value: Any) -> Any:
    if codec in {"int", "int64", "uint64"}:
        return int(value)
    return value


__all__ = ["ExternRegistry", "PYTHON_STDLIB_ALLOWLIST"]

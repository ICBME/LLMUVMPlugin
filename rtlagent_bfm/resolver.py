"""Resolve IR bindings against a DUT object."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rtlagent_bfm.ir import DesignIR, InterfaceIR, SignalBindingIR


class SignalResolutionError(LookupError):
    """Raised when a required IR binding cannot be resolved on the DUT."""


def resolve_path(root: Any, hdl_path: str) -> Any:
    """Resolve a dotted HDL path on a cocotb handle or a Python test double."""

    current = root
    for part in hdl_path.split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                raise SignalResolutionError(f"missing path segment {part!r}")
            current = current[part]
        else:
            try:
                current = getattr(current, part)
            except AttributeError as exc:
                raise SignalResolutionError(f"missing path segment {part!r}") from exc
    return current


@dataclass(frozen=True)
class ResolvedDesign:
    """Concrete DUT handles resolved from a DesignIR."""

    ir: DesignIR
    dut: Any
    handles: dict[str, Any]

    @classmethod
    def resolve(cls, ir: DesignIR, dut: Any) -> "ResolvedDesign":
        handles: dict[str, Any] = {}
        missing: list[str] = []
        for name, binding in ir.bindings.items():
            try:
                handles[name] = resolve_path(dut, binding.hdl_path)
            except SignalResolutionError:
                if binding.required:
                    missing.append(f"{name} -> {binding.hdl_path}")
        if missing:
            raise SignalResolutionError(
                "required DUT bindings were not found: " + ", ".join(missing)
            )
        return cls(ir=ir, dut=dut, handles=handles)

    def binding(self, name: str) -> SignalBindingIR:
        return self.ir.bindings[name]

    def handle(self, name: str) -> Any:
        return self.handles[name]

    def interface(self, name: str) -> InterfaceIR:
        return self.ir.interfaces[name]

    def interface_handle(self, interface_name: str, role: str) -> Any:
        interface = self.interface(interface_name)
        return self.handle(interface.signals[role])

    def clock_handle(self, interface_name: str) -> Any:
        interface = self.interface(interface_name)
        if interface.clock is None:
            raise SignalResolutionError(f"interface {interface_name} has no clock")
        return self.handle(interface.clock)

    def reset_handle(self, interface_name: str) -> Any:
        interface = self.interface(interface_name)
        if interface.reset is None:
            raise SignalResolutionError(f"interface {interface_name} has no reset")
        return self.handle(interface.reset)

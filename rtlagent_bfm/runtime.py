"""Runtime helpers shared by agent-generated BFMs.

This module deliberately contains no protocol-specific transaction logic. The
agent-generated BFM is expected to subclass or compose `BfmRuntimeContext` and
implement the DUT-specific behavior inferred from source files and design docs.
"""

from __future__ import annotations

from typing import Any

from rtlagent_bfm.ir import DesignIR, InterfaceIR, SignalBindingIR
from rtlagent_bfm.resolver import ResolvedDesign


class BfmRuntimeContext:
    """Resolved DUT/IR context for generated BFM code."""

    def __init__(self, dut: Any, ir: DesignIR):
        self.ir = ir
        self.resolved = ResolvedDesign.resolve(ir, dut)

    @property
    def dut(self) -> Any:
        return self.resolved.dut

    def binding(self, name: str) -> SignalBindingIR:
        return self.resolved.binding(name)

    def signal(self, name: str) -> Any:
        """Return a concrete DUT handle by semantic binding name."""

        return self.resolved.handle(name)

    def interface(self, name: str) -> InterfaceIR:
        return self.resolved.interface(name)

    def interface_signal(self, interface_name: str, role: str) -> Any:
        """Return a DUT handle by interface role.

        The meaning of `role` is defined by the IR and generated BFM code, not by
        this framework.
        """

        return self.resolved.interface_handle(interface_name, role)

    def signals_by_role(self, role: str) -> dict[str, Any]:
        """Return all resolved handles whose binding role matches `role`."""

        return {
            name: self.resolved.handle(name)
            for name, binding in self.ir.bindings.items()
            if binding.role == role and name in self.resolved.handles
        }

    def read_value(self, name: str) -> int:
        """Read an integer value from a resolved signal handle."""

        try:
            return int(self.signal(name).value)
        except ValueError:
            return 0

    def drive_value(self, name: str, value: int) -> None:
        """Drive a value onto a resolved signal handle."""

        self.signal(name).value = value


class GeneratedBfmBase:
    """Base class for generated BFMs.

    Generated code should add protocol methods such as reset, send, receive, or
    monitor operations. This base class only provides access to the resolved IR
    context.
    """

    def __init__(self, dut: Any, ir: DesignIR):
        self.ctx = BfmRuntimeContext(dut, ir)

    @property
    def ir(self) -> DesignIR:
        return self.ctx.ir

    @property
    def dut(self) -> Any:
        return self.ctx.dut

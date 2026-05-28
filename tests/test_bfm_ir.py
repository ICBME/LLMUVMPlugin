import unittest
from types import SimpleNamespace

from rtlagent_bfm import (
    BfmRuntimeContext,
    DesignIR,
    ResolvedDesign,
    SignalResolutionError,
)


class Signal:
    def __init__(self, value=0):
        self.value = value


def generated_bfm_ir_dict(valid_path="valid_i"):
    return {
        "design": {"top": "example_top"},
        "sources": [
            {
                "path": "rtl/example_top.sv",
                "kind": "systemverilog",
                "role": "dut",
            },
            {
                "path": "docs/example_protocol.md",
                "kind": "markdown",
                "role": "design_doc",
            },
        ],
        "interfaces": {
            "control": {
                "protocol": "agent_inferred",
                "clock": "clk",
                "reset": "rst",
                "signals": {
                    "request_valid": "req_valid",
                    "request_payload": "req_payload",
                    "response_ready": "rsp_ready",
                },
                "metadata": {
                    "generator_note": "Roles are interpreted by generated code."
                },
            }
        },
        "bindings": {
            "clk": {"role": "clock", "hdl_path": "clock_i"},
            "rst": {"role": "reset", "hdl_path": "reset_ni", "active": "low"},
            "req_valid": {
                "role": "control.request_valid",
                "hdl_path": valid_path,
                "width": 1,
            },
            "req_payload": {
                "role": "control.request_payload",
                "hdl_path": "payload_i",
                "width": 16,
            },
            "rsp_ready": {
                "role": "control.response_ready",
                "hdl_path": "ready_o",
                "width": 1,
            },
        },
        "registers": {
            "CONTROL": {
                "offset": "0x10",
                "fields": {
                    "ENABLE": {"lsb": 0, "width": 1, "access": "rw"},
                    "MODE": {"lsb": 4, "width": 3, "access": "rw"},
                },
            }
        },
    }


def dut_with_renamed_valid(valid_name="valid_i"):
    dut = SimpleNamespace(
        clock_i=Signal(),
        reset_ni=Signal(1),
        payload_i=Signal(),
        ready_o=Signal(),
    )
    setattr(dut, valid_name, Signal())
    return dut


class TestBfmIr(unittest.TestCase):
    def test_ir_resolves_renamed_dut_signal_through_binding(self):
        ir = DesignIR.from_dict(generated_bfm_ir_dict(valid_path="renamed_valid"))
        dut = dut_with_renamed_valid(valid_name="renamed_valid")

        resolved = ResolvedDesign.resolve(ir, dut)

        self.assertIs(
            resolved.interface_handle("control", "request_valid"),
            dut.renamed_valid,
        )
        self.assertIs(resolved.clock_handle("control"), dut.clock_i)

    def test_missing_required_binding_reports_semantic_name_and_path(self):
        ir = DesignIR.from_dict(generated_bfm_ir_dict(valid_path="renamed_valid"))
        dut = dut_with_renamed_valid(valid_name="valid_i")

        with self.assertRaisesRegex(
            SignalResolutionError, "req_valid -> renamed_valid"
        ):
            ResolvedDesign.resolve(ir, dut)

    def test_ir_does_not_require_framework_known_protocol_roles(self):
        ir = DesignIR.from_dict(generated_bfm_ir_dict())

        interface = ir.interfaces["control"]

        self.assertEqual(interface.protocol, "agent_inferred")
        self.assertIn("request_payload", interface.signals)

    def test_runtime_context_exposes_basic_signal_helpers_only(self):
        ir = DesignIR.from_dict(generated_bfm_ir_dict())
        dut = dut_with_renamed_valid()
        ctx = BfmRuntimeContext(dut, ir)

        ctx.drive_value("req_payload", 0x1234)

        self.assertEqual(ctx.read_value("req_payload"), 0x1234)
        self.assertIs(ctx.interface_signal("control", "response_ready"), dut.ready_o)

    def test_register_layout_remains_generic_agent_context(self):
        ir = DesignIR.from_dict(generated_bfm_ir_dict())
        register = ir.register_by_offset(0x10)

        value = register.encode({"ENABLE": 1, "MODE": 5})

        self.assertEqual(value, 0x51)
        self.assertEqual(register.decode(value), {"ENABLE": 1, "MODE": 5})


if __name__ == "__main__":
    unittest.main()

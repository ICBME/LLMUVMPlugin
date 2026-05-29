CWD=$(shell pwd)
TINYA_REG_DIR=$(abspath $(CWD)/../pyuvm/examples/TinyALU_reg)

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD):$(TINYA_REG_DIR):$(PYTHONPATH)

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
CCACHE_DISABLE ?= 1
export CCACHE_DISABLE

ifeq ($(TOPLEVEL_LANG),verilog)
	VERILOG_SOURCES =$(TINYA_REG_DIR)/hdl/verilog/tinyalu.sv $(TINYA_REG_DIR)/hdl/verilog/TinyALUreg.sv
else ifeq ($(TOPLEVEL_LANG),vhdl)
	$(error VHDL is not supported for this design)
else
	$(error "A valid value (verilog or vhdl) was not provided for TOPLEVEL_LANG=$(TOPLEVEL_LANG)")
endif

ifeq ($(SIM), verilator)
	COMPILE_ARGS += --timing
	EXTRA_ARGS += --trace --trace-structs
endif

COCOTB_TEST_MODULES := fuzz_testbench
TOPLEVEL = tinyalu
GHDL_ARGS := --ieee=synopsys
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

include $(shell cocotb-config --makefiles)/Makefile.sim

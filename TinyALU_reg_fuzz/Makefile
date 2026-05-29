CWD=$(shell pwd)
TINYA_REG_DIR ?= $(abspath $(CWD)/../pyuvm/examples/TinyALU_reg)
TINYA_TINYALU_SV ?= $(TINYA_REG_DIR)/hdl/verilog/tinyalu.sv
TINYA_REG_SV ?= $(TINYA_REG_DIR)/hdl/verilog/TinyALUreg.sv
RTL_COVERAGE ?= 0
COVERAGE_DIR ?= $(CWD)/coverage
COVERAGE_DAT ?= $(COVERAGE_DIR)/coverage.dat
COVERAGE_INFO ?= $(COVERAGE_DIR)/coverage.info
COVERAGE_ANNOTATED ?= $(COVERAGE_DIR)/annotated
COVERAGE_SUMMARY ?= $(COVERAGE_DIR)/coverage_summary.json
LLM_PROMPT ?= $(COVERAGE_DIR)/llm_prompt.json
MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/mutation_directives.json
FUZZ_CORPUS ?= $(COVERAGE_DIR)/fuzz_corpus.jsonl
PYTHON ?= python

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD):$(TINYA_REG_DIR):$(PYTHONPATH)

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
CCACHE_DISABLE ?= 1
export CCACHE_DISABLE

ifeq ($(TOPLEVEL_LANG),verilog)
VERILOG_SOURCES =$(TINYA_TINYALU_SV) $(TINYA_REG_SV)
else ifeq ($(TOPLEVEL_LANG),vhdl)
$(error VHDL is not supported for this design)
else
$(error "A valid value (verilog or vhdl) was not provided for TOPLEVEL_LANG=$(TOPLEVEL_LANG)")
endif

ifeq ($(SIM), verilator)
COMPILE_ARGS += --timing
EXTRA_ARGS += --trace --trace-structs
endif

ifeq ($(RTL_COVERAGE),1)
$(shell mkdir -p $(COVERAGE_DIR))
COMPILE_ARGS += --coverage
SIM_ARGS += +verilator+coverage+file+$(COVERAGE_DAT)
endif

COCOTB_TEST_MODULES := fuzz_testbench
TOPLEVEL = tinyalu
GHDL_ARGS := --ieee=synopsys
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: coverage-run coverage-report coverage-feedback feedback-fuzz

coverage-run:
	mkdir -p $(COVERAGE_DIR)
	$(MAKE) clean
	$(MAKE) RTL_COVERAGE=1 FUZZ_CORPUS_OUT=$(FUZZ_CORPUS) sim

coverage-report: coverage-run
	mkdir -p $(COVERAGE_ANNOTATED)
	verilator_coverage --annotate $(COVERAGE_ANNOTATED) $(COVERAGE_DAT)
	verilator_coverage --write-info $(COVERAGE_INFO) $(COVERAGE_DAT)

coverage-feedback: coverage-report
	$(PYTHON) coverage_feedback.py \
		--coverage-info $(COVERAGE_INFO) \
		--corpus $(FUZZ_CORPUS) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--prompt-out $(LLM_PROMPT)

feedback-fuzz: coverage-feedback
	$(MAKE) FUZZ_DIRECTIVES=$(MUTATION_DIRECTIVES) \
		FUZZ_CORPUS_OUT=$(COVERAGE_DIR)/feedback_fuzz_corpus.jsonl sim

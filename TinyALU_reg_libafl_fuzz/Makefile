CWD=$(shell pwd)
TINYA_REG_DIR ?= $(abspath $(CWD)/../pyuvm/examples/TinyALU_reg)
TINYA_TINYALU_SV ?= $(TINYA_REG_DIR)/hdl/verilog/tinyalu.sv
TINYA_REG_SV ?= $(TINYA_REG_DIR)/hdl/verilog/TinyALUreg.sv
LIBAFL_MANIFEST ?= $(CWD)/Cargo.toml
COVERAGE_FEEDBACK ?= $(CWD)/../TinyALU_reg_fuzz/coverage_feedback.py
RTL_COVERAGE ?= 0
COVERAGE_DIR ?= $(CWD)/coverage
COVERAGE_DAT ?= $(COVERAGE_DIR)/coverage.dat
COVERAGE_INFO ?= $(COVERAGE_DIR)/coverage.info
COVERAGE_ANNOTATED ?= $(COVERAGE_DIR)/annotated
COVERAGE_SUMMARY ?= $(COVERAGE_DIR)/coverage_summary.json
LLM_PROMPT ?= $(COVERAGE_DIR)/llm_prompt.json
MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/mutation_directives.json
FUZZ_CORPUS ?= $(COVERAGE_DIR)/libafl_fuzz_corpus.jsonl
FUZZ_DIRECTIVES ?=
LIBAFL_ITERS ?= 256
LIBAFL_MAX_SEEDS ?= 32
LIBAFL_SEED ?= 1
CARGO ?= cargo
PYTHON ?= python3

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD):$(TINYA_REG_DIR):$(PYTHONPATH)
export LIBAFL_CORPUS := $(FUZZ_CORPUS)

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

MODULE := libafl_testbench
COCOTB_TEST_MODULES := libafl_testbench
TOPLEVEL = tinyalu
GHDL_ARGS := --ieee=synopsys
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

COCOTB_MAKEFILES := $(shell cocotb-config --makefiles 2>/dev/null)
ifneq ($(COCOTB_MAKEFILES),)
include $(COCOTB_MAKEFILES)/Makefile.sim
sim: generate-corpus
else
sim:
	@echo "cocotb-config was not found; run this target inside the pyuvm/cocotb environment." >&2
	@false
clean::
	@true
endif

.PHONY: generate-corpus check coverage-run coverage-report coverage-feedback feedback-fuzz

generate-corpus:
	mkdir -p $(dir $(FUZZ_CORPUS))
	$(CARGO) run --quiet --manifest-path $(LIBAFL_MANIFEST) -- \
		--corpus-out $(FUZZ_CORPUS) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),)
	$(PYTHON) validate_corpus.py $(FUZZ_CORPUS)

check:
	$(CARGO) test --manifest-path $(LIBAFL_MANIFEST)
	$(PYTHON) -m py_compile libafl_testbench.py validate_corpus.py
	$(MAKE) LIBAFL_ITERS=8 LIBAFL_MAX_SEEDS=8 generate-corpus

coverage-run:
	mkdir -p $(COVERAGE_DIR)
	$(MAKE) clean
	$(MAKE) RTL_COVERAGE=1 sim

coverage-report: coverage-run
	mkdir -p $(COVERAGE_ANNOTATED)
	verilator_coverage --annotate $(COVERAGE_ANNOTATED) $(COVERAGE_DAT)
	verilator_coverage --write-info $(COVERAGE_INFO) $(COVERAGE_DAT)

coverage-feedback: coverage-report
	$(PYTHON) $(COVERAGE_FEEDBACK) \
		--coverage-info $(COVERAGE_INFO) \
		--corpus $(FUZZ_CORPUS) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--prompt-out $(LLM_PROMPT)

feedback-fuzz: coverage-feedback
	$(MAKE) FUZZ_DIRECTIVES=$(MUTATION_DIRECTIVES) \
		FUZZ_CORPUS=$(COVERAGE_DIR)/feedback_libafl_fuzz_corpus.jsonl sim

CWD=$(shell pwd)
TARGET ?= dut
TARGET_CONFIG ?=
LIBAFL_MANIFEST ?= $(CWD)/Cargo.toml
COVERAGE_FEEDBACK ?= $(CWD)/coverage_feedback.py
RTL_COVERAGE ?= 0
COVERAGE_DIR ?= $(CWD)/coverage
COVERAGE_DAT ?= $(COVERAGE_DIR)/$(TARGET)_coverage.dat
COVERAGE_INFO ?= $(COVERAGE_DIR)/$(TARGET)_coverage.info
COVERAGE_ANNOTATED ?= $(COVERAGE_DIR)/$(TARGET)_annotated
COVERAGE_SUMMARY ?= $(COVERAGE_DIR)/$(TARGET)_coverage_summary.json
LLM_PROMPT ?= $(COVERAGE_DIR)/$(TARGET)_llm_prompt.json
LLM_RESPONSE ?= $(COVERAGE_DIR)/$(TARGET)_llm_response.json
MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/$(TARGET)_mutation_directives.json
FUZZ_CORPUS ?= $(COVERAGE_DIR)/$(TARGET)_corpus.jsonl
FUZZ_DIRECTIVES ?=
LIBAFL_ITERS ?= 256
LIBAFL_MAX_SEEDS ?= 32
LIBAFL_SEED ?= 1
LLM_FEEDBACK ?= 0
LLM_MODEL ?=
CARGO ?= cargo
PYTHON ?= python3
EXTRA_PYTHONPATH ?=

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD)/py:$(EXTRA_PYTHONPATH):$(PYTHONPATH)
export LIBAFL_CORPUS := $(FUZZ_CORPUS)
export FUZZ_TARGET := $(TARGET)
ifneq ($(strip $(TARGET_CONFIG)),)
export FUZZ_TARGET_CONFIG := $(TARGET_CONFIG)
endif

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
TOPLEVEL ?= $(TARGET)
VERILOG_SOURCES ?=
SIM_BUILD ?= sim_build/$(TARGET)
CCACHE_DISABLE ?= 1
export CCACHE_DISABLE

ifeq ($(SIM), verilator)
COMPILE_ARGS += --timing
EXTRA_ARGS += --trace --trace-structs
endif

ifeq ($(RTL_COVERAGE),1)
$(shell mkdir -p $(COVERAGE_DIR))
COMPILE_ARGS += --coverage
SIM_ARGS += +verilator+coverage+file+$(COVERAGE_DAT)
endif

MODULE := fuzz_uvm.testbench
COCOTB_TEST_MODULES := fuzz_uvm.testbench
GHDL_ARGS := --ieee=synopsys
COCOTB_HDL_TIMEUNIT = 1ns
COCOTB_HDL_TIMEPRECISION = 1ps

.PHONY: generate-corpus check check-corpus check-uvm check-all coverage-run coverage-report coverage-feedback llm-coverage-feedback feedback-fuzz llm-feedback-fuzz require-target-config require-rtl

COCOTB_MAKEFILES := $(shell cocotb-config --makefiles 2>/dev/null)
ifneq ($(COCOTB_MAKEFILES),)
include $(COCOTB_MAKEFILES)/Makefile.sim
sim: require-rtl generate-corpus
else
sim:
	@echo "cocotb-config was not found; run this target inside the pyuvm/cocotb environment." >&2
	@false
clean::
	@true
endif

require-target-config:
	@if [ -z "$(TARGET_CONFIG)" ] && [ ! -f "$(CWD)/targets/$(TARGET).toml" ]; then \
		echo "Set TARGET_CONFIG=/path/to/target.toml or provide targets/$(TARGET).toml." >&2; \
		false; \
	fi

require-rtl:
	@if [ -z "$(strip $(VERILOG_SOURCES))" ]; then \
		echo "Set VERILOG_SOURCES and TOPLEVEL for the DUT replay simulation." >&2; \
		false; \
	fi

generate-corpus: require-target-config
	mkdir -p $(dir $(FUZZ_CORPUS))
	$(CARGO) run --quiet --manifest-path $(LIBAFL_MANIFEST) -- \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--corpus-out $(FUZZ_CORPUS) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),)
	$(PYTHON) validate_corpus.py --target $(TARGET) $(FUZZ_CORPUS)

check:
	$(CARGO) test --manifest-path $(LIBAFL_MANIFEST)
	$(PYTHON) -m py_compile coverage_feedback.py validate_corpus.py py/fuzz_bfm/*.py py/fuzz_feedback/*.py py/fuzz_uvm/*.py

check-corpus: generate-corpus

check-uvm: sim

check-all: check

coverage-run: require-rtl
	mkdir -p $(COVERAGE_DIR)
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" clean
	$(RM) -r $(CWD)/sim_build
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" RTL_COVERAGE=1 VERILOG_SOURCES="$(VERILOG_SOURCES)" TOPLEVEL="$(TOPLEVEL)" sim

coverage-report: coverage-run
	mkdir -p $(COVERAGE_ANNOTATED)
	verilator_coverage --annotate $(COVERAGE_ANNOTATED) $(COVERAGE_DAT)
	verilator_coverage --write-info $(COVERAGE_INFO) $(COVERAGE_DAT)

coverage-feedback: coverage-report
	$(PYTHON) $(COVERAGE_FEEDBACK) \
		--target $(TARGET) \
		--coverage-info $(COVERAGE_INFO) \
		--coverage-dat $(COVERAGE_DAT) \
		--corpus $(FUZZ_CORPUS) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--prompt-out $(LLM_PROMPT) \
		--llm-response-out $(LLM_RESPONSE) \
		$(if $(filter 1,$(LLM_FEEDBACK)),--llm,) \
		$(if $(LLM_MODEL),--model $(LLM_MODEL),)

llm-coverage-feedback:
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" LLM_FEEDBACK=1 coverage-feedback

feedback-fuzz: coverage-feedback
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" FUZZ_DIRECTIVES=$(MUTATION_DIRECTIVES) \
		FUZZ_CORPUS=$(COVERAGE_DIR)/$(TARGET)_feedback_corpus.jsonl VERILOG_SOURCES="$(VERILOG_SOURCES)" TOPLEVEL="$(TOPLEVEL)" sim

llm-feedback-fuzz:
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" LLM_FEEDBACK=1 feedback-fuzz

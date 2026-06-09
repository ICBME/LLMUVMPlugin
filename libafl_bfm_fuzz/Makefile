CWD=$(shell pwd)
TARGET ?= dut
TARGET_CONFIG ?=
LIBAFL_MANIFEST ?= $(CWD)/Cargo.toml
COVERAGE_FEEDBACK ?= $(CWD)/coverage_feedback.py
FUZZ_PIPELINE_RUN ?= $(CWD)/scripts/run_fuzz_pipeline.py
RTL_COVERAGE ?= 0
COVERAGE_DIR ?= $(CWD)/coverage
COVERAGE_DAT ?= $(COVERAGE_DIR)/$(TARGET)_coverage.dat
COVERAGE_INFO ?= $(COVERAGE_DIR)/$(TARGET)_coverage.info
COVERAGE_ANNOTATED ?= $(COVERAGE_DIR)/$(TARGET)_annotated
COVERAGE_SUMMARY ?= $(COVERAGE_DIR)/$(TARGET)_coverage_summary.json
UVM_FUNCTIONAL_COVERAGE_OUT ?= $(COVERAGE_DIR)/$(TARGET)_uvm_functional_coverage.json
LLM_PROMPT ?= $(COVERAGE_DIR)/$(TARGET)_llm_prompt.json
LLM_RESPONSE ?= $(COVERAGE_DIR)/$(TARGET)_llm_response.json
MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/$(TARGET)_mutation_directives.json
GAP_FEEDBACK ?= $(COVERAGE_DIR)/$(TARGET)_gap_feedback.json
MUTATION_FEEDBACK ?= $(COVERAGE_DIR)/$(TARGET)_mutation_feedback.json
CONNECTOR_OBSERVE_OUT ?=
CONNECTOR_OBSERVE_RUN_ID ?=
CONNECTOR_MONITOR_OUT ?=
CONNECTOR_TOPOLOGY_OUT ?=
PREVIOUS_COVERAGE_SUMMARY ?=
PREVIOUS_MUTATION_DIRECTIVES ?=
PREVIOUS_GAP_FEEDBACK ?=
PREVIOUS_MUTATION_FEEDBACK ?=
FUZZ_CORPUS ?= $(COVERAGE_DIR)/$(TARGET)_corpus.jsonl
FUZZ_DIRECTIVES ?=
LIBAFL_ITERS ?= 256
LIBAFL_MAX_SEEDS ?= 32
LIBAFL_SEED ?= 1
LLM_FEEDBACK ?= 0
LLM_MODEL ?=
CARGO ?= cargo
PYTHON ?= python3
MAKE_CMD ?= make
EXTRA_PYTHONPATH ?=

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD)/py:$(EXTRA_PYTHONPATH):$(PYTHONPATH)
export LIBAFL_CORPUS := $(FUZZ_CORPUS)
export FUZZ_TARGET := $(TARGET)
export UVM_FUNCTIONAL_COVERAGE_OUT
export CONNECTOR_OBSERVE_OUT
export CONNECTOR_OBSERVE_RUN_ID
export CONNECTOR_MONITOR_OUT
export CONNECTOR_TOPOLOGY_OUT
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
	$(PYTHON) $(FUZZ_PIPELINE_RUN) generate-corpus \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--corpus-out $(FUZZ_CORPUS) \
		--libafl-manifest $(LIBAFL_MANIFEST) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		--cargo "$(CARGO)" \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),)

check:
	$(CARGO) test --manifest-path $(LIBAFL_MANIFEST)
	$(PYTHON) -m py_compile coverage_feedback.py validate_corpus.py py/fuzz_bfm/*.py py/fuzz_feedback/*.py py/fuzz_pipeline/*.py py/fuzz_uvm/*.py py/fuzz_examples/*.py scripts/*.py

check-corpus: generate-corpus

check-uvm: sim

check-all: check

coverage-run: require-rtl
	$(PYTHON) $(FUZZ_PIPELINE_RUN) coverage-run \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--corpus $(FUZZ_CORPUS) \
		--libafl-manifest $(LIBAFL_MANIFEST) \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		--cargo "$(CARGO)" \
		--make "$(MAKE_CMD)" \
		--verilog-sources "$(VERILOG_SOURCES)" \
		--toplevel "$(TOPLEVEL)" \
		--coverage-dir $(COVERAGE_DIR) \
		--coverage-dat $(COVERAGE_DAT) \
		--coverage-info $(COVERAGE_INFO) \
		--coverage-annotated $(COVERAGE_ANNOTATED) \
		--functional-coverage $(UVM_FUNCTIONAL_COVERAGE_OUT) \
		--cwd $(CWD) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),)

coverage-report: require-rtl
	$(PYTHON) $(FUZZ_PIPELINE_RUN) coverage-report \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--corpus $(FUZZ_CORPUS) \
		--libafl-manifest $(LIBAFL_MANIFEST) \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		--cargo "$(CARGO)" \
		--make "$(MAKE_CMD)" \
		--verilog-sources "$(VERILOG_SOURCES)" \
		--toplevel "$(TOPLEVEL)" \
		--coverage-dir $(COVERAGE_DIR) \
		--coverage-dat $(COVERAGE_DAT) \
		--coverage-info $(COVERAGE_INFO) \
		--coverage-annotated $(COVERAGE_ANNOTATED) \
		--functional-coverage $(UVM_FUNCTIONAL_COVERAGE_OUT) \
		--cwd $(CWD) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),)

coverage-feedback: coverage-report
	$(PYTHON) $(COVERAGE_FEEDBACK) \
		--target $(TARGET) \
		--coverage-info $(COVERAGE_INFO) \
		--coverage-dat $(COVERAGE_DAT) \
		--functional-coverage $(UVM_FUNCTIONAL_COVERAGE_OUT) \
		--corpus $(FUZZ_CORPUS) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--prompt-out $(LLM_PROMPT) \
		--gap-feedback-out $(GAP_FEEDBACK) \
		--mutation-feedback-out $(MUTATION_FEEDBACK) \
		$(if $(PREVIOUS_COVERAGE_SUMMARY),--previous-summary $(PREVIOUS_COVERAGE_SUMMARY),) \
		$(if $(PREVIOUS_MUTATION_DIRECTIVES),--previous-directives $(PREVIOUS_MUTATION_DIRECTIVES),) \
		$(if $(PREVIOUS_GAP_FEEDBACK),--previous-gap-feedback $(PREVIOUS_GAP_FEEDBACK),) \
		$(if $(PREVIOUS_MUTATION_FEEDBACK),--previous-mutation-feedback $(PREVIOUS_MUTATION_FEEDBACK),) \
		--llm-response-out $(LLM_RESPONSE) \
		$(if $(CONNECTOR_OBSERVE_OUT),--observation-out $(CONNECTOR_OBSERVE_OUT),) \
		$(if $(CONNECTOR_MONITOR_OUT),--monitoring-out $(CONNECTOR_MONITOR_OUT),) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),) \
		$(if $(CONNECTOR_OBSERVE_RUN_ID),--observation-run-id $(CONNECTOR_OBSERVE_RUN_ID),) \
		$(if $(filter 1,$(LLM_FEEDBACK)),--llm,) \
		$(if $(LLM_MODEL),--model $(LLM_MODEL),)

llm-coverage-feedback:
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" LLM_FEEDBACK=1 coverage-feedback

feedback-fuzz: coverage-feedback
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" FUZZ_DIRECTIVES=$(MUTATION_DIRECTIVES) \
		FUZZ_CORPUS=$(COVERAGE_DIR)/$(TARGET)_feedback_corpus.jsonl VERILOG_SOURCES="$(VERILOG_SOURCES)" TOPLEVEL="$(TOPLEVEL)" sim

llm-feedback-fuzz:
	$(MAKE) TARGET=$(TARGET) TARGET_CONFIG="$(TARGET_CONFIG)" LLM_FEEDBACK=1 feedback-fuzz

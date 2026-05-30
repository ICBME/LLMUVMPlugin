CWD=$(shell pwd)
TARGET ?= tinyalu
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

TINYA_REG_DIR ?= $(abspath $(CWD)/../pyuvm/examples/TinyALU_reg)
TINYA_TINYALU_SV ?= $(TINYA_REG_DIR)/hdl/verilog/tinyalu.sv
TINYA_REG_SV ?= $(TINYA_REG_DIR)/hdl/verilog/TinyALUreg.sv
AES_DIR ?= $(abspath $(CWD)/../example/aes)
SHA256_DIR ?= $(abspath $(CWD)/../example/sha256)

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD)/py:$(TINYA_REG_DIR):$(AES_DIR)/src/model/python:$(SHA256_DIR)/src/model:$(PYTHONPATH)
export LIBAFL_CORPUS := $(FUZZ_CORPUS)
export FUZZ_TARGET := $(TARGET)

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
SIM_BUILD ?= sim_build/$(TARGET)
CCACHE_DISABLE ?= 1
export CCACHE_DISABLE

ifeq ($(TARGET),tinyalu)
TOPLEVEL = tinyalu
VERILOG_SOURCES = $(TINYA_TINYALU_SV) $(TINYA_REG_SV)
else ifeq ($(TARGET),aes)
TOPLEVEL = aes
VERILOG_SOURCES = \
	$(AES_DIR)/src/rtl/aes_core.v \
	$(AES_DIR)/src/rtl/aes.v \
	$(AES_DIR)/src/rtl/aes_key_mem.v \
	$(AES_DIR)/src/rtl/aes_encipher_block.v \
	$(AES_DIR)/src/rtl/aes_inv_sbox.v \
	$(AES_DIR)/src/rtl/aes_sbox.v \
	$(AES_DIR)/src/rtl/aes_decipher_block.v
EXTRA_ARGS += -Wno-UNOPTFLAT
else ifeq ($(TARGET),sha256)
TOPLEVEL = sha256
VERILOG_SOURCES = \
	$(SHA256_DIR)/src/rtl/sha256.v \
	$(SHA256_DIR)/src/rtl/sha256_core.v \
	$(SHA256_DIR)/src/rtl/sha256_k_constants.v \
	$(SHA256_DIR)/src/rtl/sha256_w_mem.v
else
$(error TARGET must be one of tinyalu, aes, sha256; got $(TARGET))
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

MODULE := fuzz_bfm.replay_testbench
COCOTB_TEST_MODULES := fuzz_bfm.replay_testbench
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

.PHONY: generate-corpus check check-corpus check-all coverage-run coverage-report coverage-feedback llm-coverage-feedback feedback-fuzz llm-feedback-fuzz

generate-corpus:
	mkdir -p $(dir $(FUZZ_CORPUS))
	$(CARGO) run --quiet --manifest-path $(LIBAFL_MANIFEST) -- \
		--target $(TARGET) \
		--corpus-out $(FUZZ_CORPUS) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		$(if $(FUZZ_DIRECTIVES),--directives $(FUZZ_DIRECTIVES),)
	$(PYTHON) validate_corpus.py --target $(TARGET) $(FUZZ_CORPUS)

check:
	$(CARGO) test --manifest-path $(LIBAFL_MANIFEST)
	$(PYTHON) -m py_compile coverage_feedback.py validate_corpus.py py/fuzz_bfm/*.py py/fuzz_feedback/*.py py/fuzz_uvm/*.py
	$(MAKE) TARGET=$(TARGET) LIBAFL_ITERS=8 LIBAFL_MAX_SEEDS=8 generate-corpus

check-corpus:
	$(MAKE) TARGET=tinyalu LIBAFL_ITERS=8 LIBAFL_MAX_SEEDS=8 generate-corpus
	$(MAKE) TARGET=aes LIBAFL_ITERS=8 LIBAFL_MAX_SEEDS=8 generate-corpus
	$(MAKE) TARGET=sha256 LIBAFL_ITERS=8 LIBAFL_MAX_SEEDS=8 generate-corpus

check-all:
	$(CARGO) test --manifest-path $(LIBAFL_MANIFEST)
	$(PYTHON) -m py_compile coverage_feedback.py validate_corpus.py py/fuzz_bfm/*.py py/fuzz_feedback/*.py py/fuzz_uvm/*.py
	$(MAKE) check-corpus

coverage-run:
	mkdir -p $(COVERAGE_DIR)
	$(MAKE) TARGET=$(TARGET) clean
	$(RM) -r $(CWD)/sim_build
	$(MAKE) TARGET=$(TARGET) RTL_COVERAGE=1 sim

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
	$(MAKE) TARGET=$(TARGET) LLM_FEEDBACK=1 coverage-feedback

feedback-fuzz: coverage-feedback
	$(MAKE) TARGET=$(TARGET) FUZZ_DIRECTIVES=$(MUTATION_DIRECTIVES) \
		FUZZ_CORPUS=$(COVERAGE_DIR)/$(TARGET)_feedback_corpus.jsonl sim

llm-feedback-fuzz:
	$(MAKE) TARGET=$(TARGET) LLM_FEEDBACK=1 feedback-fuzz

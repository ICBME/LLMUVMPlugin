CWD=$(shell pwd)
TARGET ?= dut
TARGET_CONFIG ?=
LIBAFL_MANIFEST ?= $(CWD)/Cargo.toml
FUZZ_PIPELINE_RUN ?= $(CWD)/scripts/run_fuzz_pipeline.py
RTL_COVERAGE ?= 0
COVERAGE_DIR ?= $(CWD)/coverage
COVERAGE_DAT ?= $(COVERAGE_DIR)/$(TARGET)_coverage.dat
COVERAGE_INFO ?= $(COVERAGE_DIR)/$(TARGET)_coverage.info
COVERAGE_ANNOTATED ?= $(COVERAGE_DIR)/$(TARGET)_annotated
COVERAGE_SUMMARY ?= $(COVERAGE_DIR)/$(TARGET)_coverage_summary.json
UVM_FUNCTIONAL_COVERAGE_OUT ?= $(COVERAGE_DIR)/$(TARGET)_uvm_functional_coverage.json
FEEDBACK_UVM_FUNCTIONAL_COVERAGE_OUT ?= $(COVERAGE_DIR)/$(TARGET)_feedback_uvm_functional_coverage.json
LLM_PROMPT ?= $(COVERAGE_DIR)/$(TARGET)_llm_prompt.json
LLM_RESPONSE ?= $(COVERAGE_DIR)/$(TARGET)_llm_response.json
HEURISTIC_MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/$(TARGET)_heuristic_mutation_directives.json
MUTATION_DIRECTIVES ?= $(COVERAGE_DIR)/$(TARGET)_mutation_directives.json
GAP_FEEDBACK ?= $(COVERAGE_DIR)/$(TARGET)_gap_feedback.json
MUTATION_FEEDBACK ?= $(COVERAGE_DIR)/$(TARGET)_mutation_feedback.json
ROUND_MANIFEST ?= $(COVERAGE_DIR)/$(TARGET)_round_manifest.json
ROUND_EVALUATION ?= $(COVERAGE_DIR)/$(TARGET)_round_evaluation.json
ROUND_EVALUATION_ENABLE ?=
FEEDBACK_MODE ?= feedback_fuzz
RUN_PLAN_PROFILE ?=
ROUND_ID ?=
CAMPAIGN_DIR ?= $(COVERAGE_DIR)/$(TARGET)_campaign
CAMPAIGN_MANIFEST ?= $(CAMPAIGN_DIR)/campaign_manifest.json
CAMPAIGN_EVALUATION ?= $(CAMPAIGN_DIR)/campaign_evaluation.json
CAMPAIGN_EVALUATION_ENABLE ?=
CAMPAIGN_MODES ?= heuristic_feedback
CAMPAIGN_PLAN_PROFILE ?=
CAMPAIGN_ROUNDS ?= 2
HARNESS_OPTIMIZER_BACKEND ?=
HARNESS_CANDIDATE_BACKEND ?=
CANDIDATE_MODES ?=
CANDIDATE_ROUNDS ?=
CANDIDATE_ITERS ?=
CANDIDATE_MAX_SEEDS ?=
CANDIDATE_SEED ?=
CANDIDATE_MAX_VARIANT_REGRESSIONS ?=
CANDIDATE_RUN_PLAN_PROFILE ?=
CANDIDATE_CAMPAIGN_PLAN_PROFILE ?=
CANDIDATE_ROUND_EVALUATION ?=
CANDIDATE_MATCHED_BASELINE ?=
CANDIDATE_MAX_REGRESSED_METRICS ?=
CANDIDATE_MIN_IMPROVED_METRICS ?=
CANDIDATE_ACCEPTED_STATUSES ?=
CONNECTOR_OBSERVE_OUT ?=
CONNECTOR_OBSERVE_RUN_ID ?=
CONNECTOR_OBSERVE_ROUND_ID ?= $(ROUND_ID)
CONNECTOR_OBSERVE_STAGE_ID ?=
CONNECTOR_OBSERVE_PARENT_EVENT_ID ?=
CONNECTOR_MONITOR_OUT ?=
CONNECTOR_TOPOLOGY_OUT ?=
PREVIOUS_COVERAGE_SUMMARY ?=
PREVIOUS_MUTATION_DIRECTIVES ?=
PREVIOUS_GAP_FEEDBACK ?=
PREVIOUS_MUTATION_FEEDBACK ?=
FUZZ_CORPUS ?= $(COVERAGE_DIR)/$(TARGET)_corpus.jsonl
FEEDBACK_CORPUS ?= $(COVERAGE_DIR)/$(TARGET)_feedback_corpus.jsonl
FUZZ_DIRECTIVES ?=
LIBAFL_ITERS ?= 256
LIBAFL_MAX_SEEDS ?= 32
LIBAFL_SEED ?= 1
LLM_FEEDBACK ?= 0
LLM_MODEL ?=
IGNORE_FUNCTIONAL_COVERAGE ?= 0
VERILATOR_COVERAGE ?= verilator_coverage
CARGO ?= cargo
PYTHON ?= python3
MAKE_CMD ?= make
EXTRA_PYTHONPATH ?=

export COCOTB_REDUCED_LOG_FMT = 1
export PYTHONPATH := $(CWD)/py:$(EXTRA_PYTHONPATH):$(PYTHONPATH)
export LIBAFL_CORPUS := $(FUZZ_CORPUS)
export FUZZ_TARGET := $(TARGET)
export UVM_FUNCTIONAL_COVERAGE_OUT
export FEEDBACK_UVM_FUNCTIONAL_COVERAGE_OUT
export CONNECTOR_OBSERVE_OUT
export CONNECTOR_OBSERVE_RUN_ID
export CONNECTOR_OBSERVE_ROUND_ID
export CONNECTOR_OBSERVE_STAGE_ID
export CONNECTOR_OBSERVE_PARENT_EVENT_ID
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

.PHONY: generate-corpus check check-corpus check-uvm check-all coverage-run coverage-report coverage-feedback llm-coverage-feedback feedback-fuzz llm-feedback-fuzz feedback-campaign real-candidate-feedback-campaign llm-feedback-campaign require-target-config require-rtl

COCOTB_MAKEFILES := $(shell cocotb-config --makefiles 2>/dev/null)
ifneq ($(COCOTB_MAKEFILES),)
include $(COCOTB_MAKEFILES)/Makefile.sim
sim: require-rtl
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
	$(PYTHON) -m pytest ../tests/test_run_plan.py ../tests/test_run_stage_registry.py ../tests/test_run_adapters.py ../tests/test_harness_optimization.py ../tests/test_run_profiles_evaluation.py -q

check-corpus: generate-corpus

check-uvm: generate-corpus sim

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
	$(PYTHON) $(FUZZ_PIPELINE_RUN) coverage-feedback \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--corpus $(FUZZ_CORPUS) \
		--libafl-manifest $(LIBAFL_MANIFEST) \
		--coverage-info $(COVERAGE_INFO) \
		--coverage-dat $(COVERAGE_DAT) \
		--functional-coverage $(UVM_FUNCTIONAL_COVERAGE_OUT) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--heuristic-directives-out $(HEURISTIC_MUTATION_DIRECTIVES) \
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
		--cwd $(CWD) \
		$(if $(filter 1,$(LLM_FEEDBACK)),--llm,) \
		$(if $(LLM_MODEL),--model $(LLM_MODEL),)

llm-coverage-feedback: LLM_FEEDBACK=1
llm-coverage-feedback: coverage-feedback

feedback-fuzz: require-rtl
	$(PYTHON) $(FUZZ_PIPELINE_RUN) feedback-fuzz \
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
		--feedback-functional-coverage $(FEEDBACK_UVM_FUNCTIONAL_COVERAGE_OUT) \
		--summary-out $(COVERAGE_SUMMARY) \
		--directives-out $(MUTATION_DIRECTIVES) \
		--heuristic-directives-out $(HEURISTIC_MUTATION_DIRECTIVES) \
		--prompt-out $(LLM_PROMPT) \
		--gap-feedback-out $(GAP_FEEDBACK) \
		--mutation-feedback-out $(MUTATION_FEEDBACK) \
		$(if $(PREVIOUS_COVERAGE_SUMMARY),--previous-summary $(PREVIOUS_COVERAGE_SUMMARY),) \
		$(if $(PREVIOUS_MUTATION_DIRECTIVES),--previous-directives $(PREVIOUS_MUTATION_DIRECTIVES),) \
		$(if $(PREVIOUS_GAP_FEEDBACK),--previous-gap-feedback $(PREVIOUS_GAP_FEEDBACK),) \
		$(if $(PREVIOUS_MUTATION_FEEDBACK),--previous-mutation-feedback $(PREVIOUS_MUTATION_FEEDBACK),) \
		--llm-response-out $(LLM_RESPONSE) \
		--feedback-corpus $(FEEDBACK_CORPUS) \
		--round-manifest-out $(ROUND_MANIFEST) \
		$(if $(RUN_PLAN_PROFILE),--run-plan-profile $(RUN_PLAN_PROFILE),) \
		$(if $(filter 1,$(ROUND_EVALUATION_ENABLE)),--evaluation-out $(ROUND_EVALUATION),) \
		--mode $(FEEDBACK_MODE) \
		$(if $(CONNECTOR_OBSERVE_ROUND_ID),--round-id $(CONNECTOR_OBSERVE_ROUND_ID),) \
		$(if $(CONNECTOR_OBSERVE_OUT),--observation-out $(CONNECTOR_OBSERVE_OUT),) \
		$(if $(CONNECTOR_MONITOR_OUT),--monitoring-out $(CONNECTOR_MONITOR_OUT),) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),) \
		$(if $(CONNECTOR_OBSERVE_RUN_ID),--observation-run-id $(CONNECTOR_OBSERVE_RUN_ID),) \
		--cwd $(CWD) \
		$(if $(filter 1,$(LLM_FEEDBACK)),--llm,) \
		$(if $(LLM_MODEL),--model $(LLM_MODEL),)

llm-feedback-fuzz: LLM_FEEDBACK=1
llm-feedback-fuzz: feedback-fuzz

feedback-campaign: require-rtl
	$(PYTHON) $(FUZZ_PIPELINE_RUN) feedback-campaign \
		--target $(TARGET) \
		$(if $(TARGET_CONFIG),--target-config $(TARGET_CONFIG),) \
		--libafl-manifest $(LIBAFL_MANIFEST) \
		--out-dir $(CAMPAIGN_DIR) \
		--campaign-manifest-out $(CAMPAIGN_MANIFEST) \
		$(if $(RUN_PLAN_PROFILE),--run-plan-profile $(RUN_PLAN_PROFILE),) \
		$(if $(CAMPAIGN_PLAN_PROFILE),--campaign-plan-profile $(CAMPAIGN_PLAN_PROFILE),) \
		$(if $(filter 1,$(ROUND_EVALUATION_ENABLE)),--round-evaluation,) \
		$(if $(filter 1,$(CAMPAIGN_EVALUATION_ENABLE)),--campaign-evaluation-out $(CAMPAIGN_EVALUATION),) \
		--modes $(CAMPAIGN_MODES) \
		--rounds $(CAMPAIGN_ROUNDS) \
		$(if $(HARNESS_OPTIMIZER_BACKEND),--harness-optimizer-backend $(HARNESS_OPTIMIZER_BACKEND),) \
		$(if $(HARNESS_CANDIDATE_BACKEND),--harness-candidate-backend $(HARNESS_CANDIDATE_BACKEND),) \
		$(if $(CANDIDATE_MODES),--candidate-modes $(CANDIDATE_MODES),) \
		$(if $(CANDIDATE_ROUNDS),--candidate-rounds $(CANDIDATE_ROUNDS),) \
		$(if $(CANDIDATE_ITERS),--candidate-iters $(CANDIDATE_ITERS),) \
		$(if $(CANDIDATE_MAX_SEEDS),--candidate-max-seeds $(CANDIDATE_MAX_SEEDS),) \
		$(if $(CANDIDATE_SEED),--candidate-seed $(CANDIDATE_SEED),) \
		$(if $(CANDIDATE_MAX_VARIANT_REGRESSIONS),--candidate-max-variant-regressions $(CANDIDATE_MAX_VARIANT_REGRESSIONS),) \
		$(if $(CANDIDATE_RUN_PLAN_PROFILE),--candidate-run-plan-profile $(CANDIDATE_RUN_PLAN_PROFILE),) \
		$(if $(CANDIDATE_CAMPAIGN_PLAN_PROFILE),--candidate-campaign-plan-profile $(CANDIDATE_CAMPAIGN_PLAN_PROFILE),) \
		$(if $(filter 1,$(CANDIDATE_ROUND_EVALUATION)),--candidate-round-evaluation,) \
		$(if $(filter 0,$(CANDIDATE_ROUND_EVALUATION)),--no-candidate-round-evaluation,) \
		$(if $(filter 1,$(CANDIDATE_MATCHED_BASELINE)),--candidate-matched-baseline,) \
		$(if $(filter 0,$(CANDIDATE_MATCHED_BASELINE)),--no-candidate-matched-baseline,) \
		$(if $(CANDIDATE_MAX_REGRESSED_METRICS),--candidate-max-regressed-metrics $(CANDIDATE_MAX_REGRESSED_METRICS),) \
		$(if $(CANDIDATE_MIN_IMPROVED_METRICS),--candidate-min-improved-metrics $(CANDIDATE_MIN_IMPROVED_METRICS),) \
		$(if $(CANDIDATE_ACCEPTED_STATUSES),--candidate-accepted-statuses $(CANDIDATE_ACCEPTED_STATUSES),) \
		--iters $(LIBAFL_ITERS) \
		--max-seeds $(LIBAFL_MAX_SEEDS) \
		--seed $(LIBAFL_SEED) \
		--cargo "$(CARGO)" \
		--make "$(MAKE_CMD)" \
		--verilog-sources "$(VERILOG_SOURCES)" \
		--toplevel "$(TOPLEVEL)" \
		--verilator-coverage "$(VERILATOR_COVERAGE)" \
		$(if $(CONNECTOR_OBSERVE_OUT),--observation-out $(CONNECTOR_OBSERVE_OUT),) \
		$(if $(CONNECTOR_MONITOR_OUT),--monitoring-out $(CONNECTOR_MONITOR_OUT),) \
		$(if $(CONNECTOR_TOPOLOGY_OUT),--topology-out $(CONNECTOR_TOPOLOGY_OUT),) \
		$(if $(CONNECTOR_OBSERVE_RUN_ID),--observation-run-id $(CONNECTOR_OBSERVE_RUN_ID),) \
		--cwd $(CWD) \
		$(if $(filter 1,$(IGNORE_FUNCTIONAL_COVERAGE)),--ignore-functional-coverage,) \
		$(if $(LLM_MODEL),--model $(LLM_MODEL),)

real-candidate-feedback-campaign: CAMPAIGN_PLAN_PROFILE=campaign_with_evaluation_and_optimization_real_validation
real-candidate-feedback-campaign: CAMPAIGN_EVALUATION_ENABLE=1
real-candidate-feedback-campaign: HARNESS_CANDIDATE_BACKEND=real
real-candidate-feedback-campaign: CANDIDATE_MATCHED_BASELINE=1
real-candidate-feedback-campaign: CANDIDATE_MIN_IMPROVED_METRICS=0
real-candidate-feedback-campaign: feedback-campaign

llm-feedback-campaign: CAMPAIGN_MODES=llm_feedback
llm-feedback-campaign: feedback-campaign

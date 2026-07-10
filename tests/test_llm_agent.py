from LLMPlugin import CallableLLMBackend, LLMAgentConfig, LLMAgentRunner, LLMBackendError


class FakeHarness:
    def __init__(self, *, done_after: int = 1):
        self.done_after = done_after
        self.started = False
        self.done = False
        self.actions = []

    def start(self):
        self.started = True
        return {"status": "started"}

    def observe(self):
        return {
            "workflow": "fake_agent",
            "agent_instructions": "Return an action.",
            "action_count": len(self.actions),
        }

    def apply(self, action):
        self.actions.append(action)
        if len(self.actions) >= self.done_after:
            self.done = True
        return {"status": "accepted", "action_count": len(self.actions)}

    def is_done(self):
        return self.done

    def result(self):
        return {"status": "done" if self.done else "running", "actions": list(self.actions)}


class ToolHarness(FakeHarness):
    def __init__(self):
        super().__init__(done_after=1)
        self.tool_calls = []

    def tool_specs(self):
        return [
            {
                "name": "lookup",
                "description": "Return a value for the key.",
                "input_schema": {"type": "object"},
            }
        ]

    def call_tool(self, name, arguments):
        self.tool_calls.append({"name": name, "arguments": arguments})
        if name != "lookup":
            raise ValueError(f"unknown local tool {name}")
        return {"status": "tool_result", "value": arguments.get("key")}


def test_llm_agent_runner_invokes_harness_and_preserves_history() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        return {"action": "finish", "value": len(prompts)}

    result = LLMAgentRunner(
        FakeHarness(done_after=2),
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=2, run_name="fake_agent"),
    ).run()

    assert result["status"] == "done"
    assert len(result["attempts"]) == 2
    assert prompts[1]["attempt_history"][0]["status"] == "accepted"
    assert prompts[1]["messages"][1]["content"]
    assert [message["role"] for message in prompts[1]["messages"]] == [
        "system",
        "assistant",
        "user",
        "user",
    ]
    assert '"value": 1' in prompts[1]["messages"][1]["content"]
    assert '"remaining_model_calls_including_this_one": 1' in prompts[1]["messages"][-1]["content"]
    assert result["llm_provenance"]["runtime"] == "langgraph"
    assert result["llm_provenance"]["checkpoint"]["type"] == "memory"


def test_llm_agent_runner_invalid_json_enters_next_attempt_context() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return "not json"
        return {"action": "finish"}

    result = LLMAgentRunner(
        FakeHarness(),
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=2),
    ).run()

    assert result["status"] == "done"
    assert result["attempts"][0]["status"] == "llm_invalid_response"
    assert prompts[1]["attempt_history"][0]["error"]["type"] == "JSONDecodeError"


def test_llm_agent_runner_reports_unavailable_backend() -> None:
    class ErrorBackend:
        name = "error"

        def invoke(self, request):
            raise LLMBackendError("provider down")

    result = LLMAgentRunner(
        FakeHarness(),
        backend=ErrorBackend(),
        config=LLMAgentConfig(max_attempts=1),
    ).run()

    assert result["status"] == "llm_unavailable"
    assert result["attempts"][0]["status"] == "llm_unavailable"
    assert result["llm_provenance"]["backend"] == "error"


def test_llm_agent_runner_executes_tool_call_and_feeds_result_to_next_attempt() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"type": "tool_call", "tool": "lookup", "arguments": {"key": "answer"}}
        history = prompt["attempt_history"]
        assert history[0]["status"] == "tool_result"
        assert history[0]["tool_result"]["value"] == "answer"
        return {"type": "harness_action", "action": "finish"}

    harness = ToolHarness()
    result = LLMAgentRunner(
        harness,
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=2),
    ).run()

    assert result["status"] == "done"
    assert harness.tool_calls == [{"name": "lookup", "arguments": {"key": "answer"}}]
    assert [attempt["status"] for attempt in result["attempts"]] == ["tool_result", "accepted"]


def test_llm_agent_runner_unknown_tool_enters_next_attempt_context() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"type": "tool_call", "tool": "missing", "arguments": {}}
        history = prompt["attempt_history"]
        assert history[0]["status"] == "tool_error"
        assert history[0]["tool_result"]["error"]["type"] == "UnknownTool"
        return {"type": "harness_action", "action": "finish"}

    result = LLMAgentRunner(
        ToolHarness(),
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=2),
    ).run()

    assert result["status"] == "done"
    assert result["attempts"][0]["status"] == "tool_error"


def test_llm_agent_runner_invalid_step_enters_next_attempt_context() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return {"type": "surprise"}
        assert prompts[1]["attempt_history"][0]["error"]["type"] == "InvalidStep"
        return {"type": "harness_action", "action": "finish"}

    result = LLMAgentRunner(
        FakeHarness(),
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=2, metadata={"thread_id": "test-thread"}),
    ).run()

    assert result["status"] == "done"
    assert result["attempts"][0]["status"] == "llm_invalid_response"
    assert result["llm_provenance"]["checkpoint"]["thread_id"] == "test-thread"


def test_llm_agent_runner_exposes_langgraph_state_events_for_checkpoint_audit() -> None:
    runner = LLMAgentRunner(
        FakeHarness(),
        backend=CallableLLMBackend(lambda _prompt, _model: {"action": "finish"}),
        config=LLMAgentConfig(max_attempts=1, metadata={"checkpoint_thread_id": "audit-thread"}),
    )

    result = runner.run()

    assert result["events"]
    assert runner.last_state["events"]
    assert result["llm_provenance"]["checkpoint"] == {
        "type": "memory",
        "thread_id": "audit-thread",
    }


def test_llm_agent_runner_limits_conversation_history_window() -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        return {"action": "finish", "value": len(prompts)}

    result = LLMAgentRunner(
        FakeHarness(done_after=3),
        backend=CallableLLMBackend(llm),
        config=LLMAgentConfig(max_attempts=3, history_window=1, event_window=0),
    ).run()

    assert result["status"] == "done"
    assert len(prompts[2]["attempt_history"]) == 1
    assert [message["role"] for message in prompts[2]["messages"]] == [
        "system",
        "assistant",
        "user",
        "user",
    ]
    assert '"value": 2' in prompts[2]["messages"][1]["content"]
    assert '"value": 1' not in prompts[2]["messages"][1]["content"]
    assert '"recent_events": []' in prompts[2]["messages"][-1]["content"]
    assert result["llm_provenance"]["context_window"] == {"attempts": 1, "events": 0}

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

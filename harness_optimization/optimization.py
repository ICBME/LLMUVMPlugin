from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import request

from .records import mapping


@dataclass(frozen=True)
class HarnessOptimizationPaths:
    task: Path
    proposal: Path
    decision: Path
    advice_report: Path
    optimizer_prompt: Path
    optimizer_response: Path
    patch: Path
    candidate_manifest: Path
    candidate_evaluation: Path
    metric_delta: Path
    final_decision: Path
    sandbox_dir: Path

    def to_json(self) -> dict[str, str]:
        return {
            "harness_optimization_task": str(self.task),
            "harness_optimization_proposal": str(self.proposal),
            "harness_optimization_decision": str(self.decision),
        }

    def advice_json(self) -> dict[str, str]:
        return {
            "harness_optimization_advice_report": str(self.advice_report),
        }

    def optimizer_io_json(self) -> dict[str, str]:
        return {
            "harness_optimization_optimizer_prompt": str(self.optimizer_prompt),
            "harness_optimization_optimizer_response": str(self.optimizer_response),
        }

    def validation_json(self) -> dict[str, str]:
        return {
            "harness_optimization_patch": str(self.patch),
            "harness_optimization_candidate_manifest": str(self.candidate_manifest),
            "harness_optimization_candidate_evaluation": str(
                self.candidate_evaluation
            ),
            "harness_optimization_metric_delta": str(self.metric_delta),
            "harness_optimization_final_decision": str(self.final_decision),
            "harness_optimization_sandbox": str(self.sandbox_dir),
        }


@dataclass(frozen=True)
class HarnessOptimizerContext:
    paths: HarnessOptimizationPaths
    cwd: Path


class HarnessOptimizerBackend(Protocol):
    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        ...


class HarnessCandidateEvaluationBackend(Protocol):
    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class HarnessLlmTransport(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        ...


@dataclass(frozen=True)
class OpenAICompatibleChatTransport:
    endpoint: str
    api_key: str | None = None
    timeout: float = 60.0
    extra_headers: Mapping[str, str] | None = None

    @classmethod
    def from_env(cls) -> "OpenAICompatibleChatTransport":
        endpoint = os.getenv(
            "HARNESS_OPTIMIZER_LLM_ENDPOINT",
            "https://api.openai.com/v1/chat/completions",
        )
        api_key = os.getenv("HARNESS_OPTIMIZER_LLM_API_KEY") or os.getenv(
            "OPENAI_API_KEY"
        )
        return cls(endpoint=endpoint, api_key=api_key)

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.extra_headers:
            headers.update(dict(self.extra_headers))
        req = request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
        return json.loads(body)


def harness_optimization_paths(evaluation_path: Path) -> HarnessOptimizationPaths:
    stem = evaluation_path.stem
    sandbox_dir = evaluation_path.with_name(f"{stem}_harness_optimization_sandbox")
    return HarnessOptimizationPaths(
        task=evaluation_path.with_name(f"{stem}_harness_optimization_task.json"),
        advice_report=evaluation_path.with_name(
            f"{stem}_harness_optimization_advice_report.json"
        ),
        optimizer_prompt=evaluation_path.with_name(
            f"{stem}_harness_optimization_optimizer_prompt.json"
        ),
        optimizer_response=evaluation_path.with_name(
            f"{stem}_harness_optimization_optimizer_response.json"
        ),
        proposal=evaluation_path.with_name(
            f"{stem}_harness_optimization_proposal.json"
        ),
        decision=evaluation_path.with_name(
            f"{stem}_harness_optimization_decision.json"
        ),
        patch=evaluation_path.with_name(f"{stem}_harness_optimization_patch.json"),
        candidate_manifest=evaluation_path.with_name(
            f"{stem}_harness_optimization_candidate_manifest.json"
        ),
        candidate_evaluation=evaluation_path.with_name(
            f"{stem}_harness_optimization_candidate_evaluation.json"
        ),
        metric_delta=evaluation_path.with_name(
            f"{stem}_harness_optimization_metric_delta.json"
        ),
        final_decision=evaluation_path.with_name(
            f"{stem}_harness_optimization_final_decision.json"
        ),
        sandbox_dir=sandbox_dir,
    )


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_jsonl_samples(path: Path | None, *, limit: int) -> list[dict[str, Any]]:
    if path is None or not path.exists() or limit <= 0:
        return []
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if len(samples) >= limit:
            break
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            samples.append(value)
    return samples


def proposal_from_llm_response(
    response: Any,
    *,
    task: dict[str, Any],
    source: str,
    proposal_kind: str,
    attempt_index: int,
) -> dict[str, Any]:
    value = llm_response_json(response)
    if isinstance(value, dict) and isinstance(value.get("proposal"), dict):
        value = value["proposal"]
    if not isinstance(value, dict):
        return invalid_optimizer_proposal(
            task,
            source=source,
            proposal_kind=proposal_kind,
            reason="LLM response did not contain a JSON object proposal",
            attempt_index=attempt_index,
        )
    proposal = dict(value)
    proposal.setdefault("schema_version", 1)
    proposal.setdefault("kind", proposal_kind)
    proposal.setdefault(
        "proposal_id",
        f"{task.get('run_id') or 'unknown'}:llm:{attempt_index}",
    )
    proposal.setdefault("created_at", utc_timestamp())
    proposal.setdefault("source", source)
    proposal.setdefault("status", "proposed" if proposal.get("actions") else "no_op")
    proposal.setdefault("actions", [])
    proposal.setdefault("evidence_refs", [])
    proposal.setdefault("rationale", "LLM-generated harness optimization proposal")
    return proposal


def llm_response_json(response: Any) -> Any:
    if isinstance(response, str):
        return parse_json_text(response)
    if not isinstance(response, dict):
        return response
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            content = mapping(first.get("message")).get("content")
            return parse_json_text(content) if isinstance(content, str) else content
    content = response.get("content")
    if isinstance(content, str):
        return parse_json_text(content)
    return response


def parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return text
        return text


def invalid_optimizer_proposal(
    task: dict[str, Any],
    *,
    source: str,
    proposal_kind: str,
    reason: str,
    attempt_index: int | None = None,
) -> dict[str, Any]:
    suffix = "invalid" if attempt_index is None else f"invalid:{attempt_index}"
    return {
        "schema_version": 1,
        "kind": proposal_kind,
        "proposal_id": f"{task.get('run_id') or 'unknown'}:{suffix}",
        "created_at": utc_timestamp(),
        "source": source,
        "status": "invalid",
        "actions": [],
        "evidence_refs": [],
        "rationale": reason,
    }


def compact_llm_response(response: Any) -> Any:
    if isinstance(response, dict):
        compact = {
            key: response.get(key)
            for key in ("id", "model", "object", "created", "usage")
            if key in response
        }
        if "choices" in response:
            compact["choices"] = response["choices"]
        return compact or response
    return response


def build_repair_messages(
    prompt: dict[str, Any],
    proposal: dict[str, Any],
    validation: dict[str, Any],
) -> list[dict[str, str]]:
    messages = list(prompt["messages"])
    messages.append(
        {
            "role": "assistant",
            "content": json.dumps(proposal, indent=2, sort_keys=True),
        }
    )
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "repair_request": (
                        "The proposal failed schema validation. Return a repaired "
                        "proposal JSON object only."
                    ),
                    "validation_errors": validation.get("errors", []),
                    "proposal_schema": prompt.get("schema_hint"),
                },
                indent=2,
                sort_keys=True,
            ),
        }
    )
    return messages

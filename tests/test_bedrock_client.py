import json
import pytest

from agent.llm_client import BedrockLLMClient


class FakeBedrock:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def test_generate_plan_parses_json():
    fake = FakeBedrock('{"intent":"unknown","parameters":{},"message":"hello"}')
    client = BedrockLLMClient(client=fake, model_id="test-model")
    result = client.generate_plan("system", "user")
    assert result["intent"] == "unknown"
    assert fake.kwargs["modelId"] == "test-model"


def test_generate_plan_accepts_json_fence():
    fake = FakeBedrock('```json\n{"intent":"clarify","parameters":{},"message":"need date"}\n```')
    client = BedrockLLMClient(client=fake, model_id="test-model")
    assert client.generate_plan("system", "user")["intent"] == "clarify"


def test_generate_plan_rejects_invalid_json():
    client = BedrockLLMClient(client=FakeBedrock("not json"), model_id="test-model")
    with pytest.raises(ValueError, match="invalid JSON"):
        client.generate_plan("system", "user")


def test_prepare_worklog_missing_start_time_requests_clarification(monkeypatch):
    from agent.tool_registry import execute_plan

    result = execute_plan(
        {
            "intent": "prepare_worklog",
            "parameters": {
                "issue_key": "PROJ-123",
                "date": "2026-11-05",
                "duration": "1h",
            },
        },
        "U123",
        "C123",
    )
    assert result["intent"] == "clarify"
    assert "start_time" in result["message"]

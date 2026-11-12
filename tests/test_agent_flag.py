"""AGENT_ENABLED feature flag: the natural-language assistant can be turned off without removing code."""
import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from handlers.async_task_handler import handle_async_task
from handlers.slack_command_handler import handle_slack_request
from util.config import AGENT_DISABLED_MESSAGE, agent_enabled

RESPONSE_URL = "https://hooks.slack.com/commands/T/1/x"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASYNC_TASK_SECRET", "task-secret")
    monkeypatch.delenv("AGENT_ENABLED", raising=False)


def command(text):
    body = urllib.parse.urlencode({"command": "/jira-agent", "text": text, "team_id": "T1", "user_id": "U1", "channel_id": "C1",
                                   "response_url": RESPONSE_URL})
    lambda_client = MagicMock()
    response = handle_slack_request({"headers": {}}, SimpleNamespace(function_name="jira-agent"), body, lambda_client)
    return json.loads(response["body"]), lambda_client


@pytest.mark.parametrize("value,expected", [
    (None, True), ("true", True), ("TRUE", True), ("1", True),
    ("false", False), ("False", False), (" off ", False), ("0", False), ("no", False),
])
def test_flag_values(monkeypatch, value, expected):
    # Arrange
    if value is not None:
        monkeypatch.setenv("AGENT_ENABLED", value)

    # Act & Assert
    assert agent_enabled() is expected


def test_disabled_free_text_gets_help_without_bedrock_or_rate_limit(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with patch("handlers.slack_command_handler.agent_request_allowed") as rate_limit:
        # Act
        body, lambda_client = command("log 1h on PROJ-1 today at 9:00")

    # Assert
    assert body == {"response_type": "ephemeral", "text": AGENT_DISABLED_MESSAGE}
    lambda_client.invoke.assert_not_called()
    rate_limit.assert_not_called()


def test_enabled_free_text_still_goes_to_the_agent(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "true")
    with patch("handlers.slack_command_handler.agent_request_allowed", return_value=True):
        # Act
        body, lambda_client = command("log 1h on PROJ-1 today at 9:00")

    # Assert
    assert body["text"] == "I'm processing your Jira request..."
    assert json.loads(lambda_client.invoke.call_args.kwargs["Payload"])["_async_task"] == "agent_request"


@pytest.mark.parametrize("text", ["list", "logwork today"])
def test_fixed_commands_work_while_agent_is_disabled(monkeypatch, text):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.get_jira_account_id_for_slack_user.return_value = "acc-1"

        # Act
        body, lambda_client = command(text)

    # Assert
    assert body["text"] == "Fetching your Jira data..."
    lambda_client.invoke.assert_called_once()


def test_disconnect_works_while_agent_is_disabled(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "false")
    with patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.disconnect_slack_user.return_value = True

        # Act
        body, _ = command("disconnect")

    # Assert
    assert "disconnected" in body["text"]


def test_queued_agent_task_is_answered_without_bedrock(monkeypatch):
    # Arrange: a request accepted before the flag was turned off
    monkeypatch.setenv("AGENT_ENABLED", "false")
    event = {"_async_task": "agent_request", "team_id": "T1", "slack_user_id": "U1", "channel_id": "C1",
             "text": "list my tickets", "response_url": RESPONSE_URL}
    with patch("agent.orchestrator.AgentOrchestrator") as orchestrator, \
            patch("slack.slack_client.post_slack_response") as post:
        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": False, "error": "agent_disabled"}
    orchestrator.assert_not_called()
    post.assert_called_once_with(RESPONSE_URL, {"response_type": "ephemeral", "text": AGENT_DISABLED_MESSAGE})


def test_queued_task_notice_failure_is_not_raised(monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "false")
    event = {"_async_task": "agent_request", "team_id": "T1", "slack_user_id": "U1", "channel_id": "C1",
             "text": "x", "response_url": RESPONSE_URL}
    with patch("slack.slack_client.post_slack_response", side_effect=RuntimeError("Slack down")):
        # Act
        response = handle_async_task(event)

    # Assert: no exception, so Lambda does not retry
    assert response["statusCode"] == 200

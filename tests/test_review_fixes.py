"""Tests for review fixes: no N+1 in the assigned-issues list, no Lambda retries on async failures."""
from unittest.mock import MagicMock, patch

import pytest

from handlers.async_task_handler import handle_async_task
from jira.jira_service import JiraService
from slack.message_builder import build_assigned_issue_blocks

SITE = "https://example.atlassian.net"


def test_find_assigned_issues_requests_timespent_field():
    # Arrange
    service = JiraService.__new__(JiraService)
    service.client = MagicMock()
    service.client.request.return_value = {"issues": [{"key": "PROJ-1"}]}

    # Act
    issues = service.find_assigned_issues("acc-1")

    # Assert
    assert issues == [{"key": "PROJ-1"}]
    body = service.client.request.call_args.kwargs["data"]
    assert body["fields"] == ["summary", "status", "timespent"]


@pytest.mark.parametrize(
    "timespent,expected",
    [(5400, "*1h 30m*"), (None, "*0m*"), (0, "*0m*")],
)
def test_assigned_blocks_use_timespent_without_jira_calls(timespent, expected):
    # Arrange
    issues = [{"key": "PROJ-1", "fields": {"summary": "Fix", "status": {"name": "To Do"},
                                           "timespent": timespent}}]

    # Act
    blocks = build_assigned_issue_blocks(issues, SITE)

    # Assert
    text = blocks[1]["text"]["text"]
    assert f"<{SITE}/browse/PROJ-1|" in text
    assert f"Total logged: {expected}" in text


def test_request_worklog_approval_failure_is_not_raised():
    # Arrange
    event = {"_async_task": "request_worklog_approval", "team_id": "T1", "slack_user_id": "U1",
             "channel_id": "C1", "form": {"issue_key": "PROJ-1"}}
    with patch("approval.approval_service.ApprovalService") as service_cls:
        service_cls.return_value.request_worklog_approval.side_effect = RuntimeError("Slack down")

        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 500
    service_cls.return_value.request_worklog_approval.assert_called_once_with("T1", 
        "U1", "C1", {"issue_key": "PROJ-1"})


def test_agent_request_failure_is_not_raised():
    # Arrange
    event = {"_async_task": "agent_request", "team_id": "T1", "slack_user_id": "U1", "channel_id": "C1",
             "text": "list my tickets", "response_url": "https://hooks.slack.com/x"}
    with patch("agent.orchestrator.AgentOrchestrator") as orchestrator_cls, \
            patch("slack.slack_client.post_slack_response", side_effect=RuntimeError("timeout")) as post:
        orchestrator_cls.return_value.handle.return_value = MagicMock(message="ok")

        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 500
    orchestrator_cls.return_value.handle.assert_called_once()
    post.assert_called_once()


def test_agent_request_success_posts_reply():
    # Arrange
    event = {"_async_task": "agent_request", "team_id": "T1", "slack_user_id": "U1", "channel_id": "C1",
             "text": "list my tickets", "response_url": "https://hooks.slack.com/x"}
    with patch("agent.orchestrator.AgentOrchestrator") as orchestrator_cls, \
            patch("slack.slack_client.post_slack_response") as post:
        orchestrator_cls.return_value.handle.return_value = MagicMock(message="Here you go")

        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 200
    post.assert_called_once_with("https://hooks.slack.com/x",
                                 {"response_type": "ephemeral", "text": "Here you go"})

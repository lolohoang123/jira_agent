"""Unit tests for the Slack request path, from docs/testing/request_path_test_matrix.md (TC-01..TC-41).

AAA layout, frozen clock, Lambda/Slack/DynamoDB/Jira mocked. No network.
"""
import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from functions import read_jira_lambda
from functions.read_jira_lambda import lambda_handler
from handlers import slack_action_handler
from handlers.slack_action_handler import handle_block_actions, handle_view_submission
from handlers.slack_command_handler import handle_slack_request
from slack import slack_security
from slack.slack_security import get_raw_body, verify_slack_signature
from util.lambda_invoke import invoke_async

NOW = 1_700_000_000
SECRET = "test-signing-secret"
TASK_SECRET = "test-task-secret"
RESPONSE_URL = "https://hooks.slack.com/commands/T000/123/abc"
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("ASYNC_TASK_SECRET", TASK_SECRET)
    monkeypatch.setenv("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh")
    monkeypatch.delenv("MAX_WORKLOG_MINUTES", raising=False)


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch.object(slack_security.time, "time", return_value=NOW):
        yield


@pytest.fixture
def context():
    return SimpleNamespace(function_name="jira-agent")


@pytest.fixture
def lambda_client():
    return MagicMock(name="lambda_client")


def sign(body, timestamp=NOW, secret=SECRET):
    digest = hmac.new(secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    return f"v0={digest}"


def signed_event(body, timestamp=NOW, secret=SECRET, **extra):
    return {"headers": {"X-Slack-Request-Timestamp": str(timestamp),
                        "X-Slack-Signature": sign(body, timestamp, secret), **FORM_HEADERS},
            "body": body, **extra}


def invoked_task(lambda_client):
    kwargs = lambda_client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == "jira-agent"
    assert kwargs["InvocationType"] == "Event"
    return json.loads(kwargs["Payload"])


# --------------------------------------------------------------------------------------
# Signature verification
# --------------------------------------------------------------------------------------

def test_tc01_valid_signature_is_accepted():
    # Arrange
    body = "command=%2Fjira-agent&text=list"
    event = signed_event(body)

    # Act & Assert
    assert verify_slack_signature(event, body) is True


def test_tc02_header_names_are_case_insensitive():
    # Arrange
    body = "text=list"
    event = {"headers": {"x-slack-request-timestamp": str(NOW), "X-SLACK-SIGNATURE": sign(body)}}

    # Act & Assert
    assert verify_slack_signature(event, body) is True


@pytest.mark.parametrize("offset,expected", [(-300, True), (300, True), (-301, False), (301, False)])
def test_tc03_tc04_replay_window_is_300_seconds(offset, expected):
    # Arrange
    body = "text=list"
    event = signed_event(body, timestamp=NOW + offset)

    # Act & Assert
    assert verify_slack_signature(event, body) is expected


def test_tc05_signature_from_another_secret_is_rejected():
    # Arrange
    body = "text=list"
    event = signed_event(body, secret="attacker-secret")

    # Act & Assert
    assert verify_slack_signature(event, body) is False


def test_tc06_tampered_body_is_rejected():
    # Arrange
    event = signed_event("text=list")

    # Act & Assert
    assert verify_slack_signature(event, "text=connect") is False


@pytest.mark.parametrize("headers", [
    {"X-Slack-Signature": "v0=abc"},
    {"X-Slack-Request-Timestamp": str(NOW)},
    {},
    None,
])
def test_tc07_missing_headers_are_rejected(headers):
    # Act & Assert
    assert verify_slack_signature({"headers": headers}, "text=list") is False


@pytest.mark.parametrize("timestamp", ["abc", "1.5", " "])
def test_tc08_non_numeric_timestamp_is_rejected(timestamp):
    # Arrange
    event = {"headers": {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": "v0=abc"}}

    # Act & Assert
    assert verify_slack_signature(event, "text=list") is False


def test_tc09_missing_signing_secret_raises(monkeypatch):
    # Arrange
    monkeypatch.delenv("SLACK_SIGNING_SECRET")

    # Act & Assert
    with pytest.raises(KeyError):
        verify_slack_signature(signed_event("text=list"), "text=list")


@pytest.mark.parametrize("event,expected", [
    ({"body": base64.b64encode("text=Nguyễn ✅".encode()).decode(), "isBase64Encoded": True}, "text=Nguyễn ✅"),
    ({"body": "text=list"}, "text=list"),
    ({"body": None}, ""),
    ({}, ""),
])
def test_tc10_raw_body_is_decoded(event, expected):
    # Act & Assert
    assert get_raw_body(event) == expected


# --------------------------------------------------------------------------------------
# Lambda entry
# --------------------------------------------------------------------------------------

@pytest.fixture
def entry_mocks():
    with patch.object(read_jira_lambda, "handle_oauth_callback") as oauth, \
            patch.object(read_jira_lambda, "handle_async_task") as async_task, \
            patch.object(read_jira_lambda, "handle_slack_request") as slack, \
            patch.object(read_jira_lambda, "boto3") as boto3:
        oauth.return_value = {"statusCode": 200, "body": "oauth"}
        async_task.return_value = {"statusCode": 200, "body": "task"}
        slack.return_value = {"statusCode": 200, "body": "slack"}
        yield SimpleNamespace(oauth=oauth, async_task=async_task, slack=slack, boto3=boto3)


@pytest.mark.parametrize("event", [
    {"rawPath": "/oauth/callback", "requestContext": {"http": {"method": "GET"}}},
    {"rawPath": "/prod/oauth/callback/", "requestContext": {"http": {"method": "get"}}},
    {"path": "/oauth/callback", "httpMethod": "GET"},
])
def test_tc11_oauth_callback_skips_slack_signature(entry_mocks, event):
    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["body"] == "oauth"
    entry_mocks.oauth.assert_called_once_with(event)
    entry_mocks.slack.assert_not_called()


def test_tc12_post_to_oauth_callback_requires_signature(entry_mocks):
    # Arrange
    event = {"rawPath": "/oauth/callback", "requestContext": {"http": {"method": "POST"}}, "body": "x"}

    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["statusCode"] == 401
    entry_mocks.oauth.assert_not_called()


def test_tc13_async_task_with_correct_secret_runs(entry_mocks):
    # Arrange
    event = {"_async_task": "jira_worklog", "_task_secret": TASK_SECRET}

    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["body"] == "task"
    entry_mocks.async_task.assert_called_once_with(event)


@pytest.mark.parametrize("provided", ["wrong-secret", "", None, 12345, [TASK_SECRET]])
def test_tc14_async_task_with_bad_secret_is_forbidden(entry_mocks, provided):
    # Arrange
    event = {"_async_task": "handle_approval_action"}
    if provided is not None:
        event["_task_secret"] = provided

    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}
    entry_mocks.async_task.assert_not_called()


@pytest.mark.parametrize("configured", [None, ""])
def test_tc15_async_task_forbidden_when_secret_not_configured(entry_mocks, monkeypatch, configured):
    # Arrange
    if configured is None:
        monkeypatch.delenv("ASYNC_TASK_SECRET")
    else:
        monkeypatch.setenv("ASYNC_TASK_SECRET", configured)
    event = {"_async_task": "jira_worklog", "_task_secret": ""}

    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["statusCode"] == 403
    entry_mocks.async_task.assert_not_called()


def test_tc16_invalid_slack_signature_is_unauthorized(entry_mocks):
    # Arrange
    event = signed_event("text=list", secret="attacker-secret")

    # Act
    response = lambda_handler(event, None)

    # Assert
    assert response["statusCode"] == 401
    assert json.loads(response["body"]) == {"error": "invalid Slack signature"}
    entry_mocks.slack.assert_not_called()


def test_tc17_valid_slack_request_is_routed(entry_mocks, context):
    # Arrange
    body = "command=%2Fjira-agent&text=list"
    event = signed_event(body)

    # Act
    response = lambda_handler(event, context)

    # Assert
    assert response["body"] == "slack"
    entry_mocks.boto3.client.assert_called_once_with("lambda")
    entry_mocks.slack.assert_called_once_with(event, context, body, entry_mocks.boto3.client.return_value)


def test_tc18_handler_error_becomes_500(entry_mocks):
    # Arrange
    entry_mocks.slack.side_effect = RuntimeError("boom")

    # Act
    response = lambda_handler(signed_event("text=list"), None)

    # Assert
    assert response["statusCode"] == 500
    assert json.loads(response["body"]) == {"error": "internal_server_error"}


# --------------------------------------------------------------------------------------
# Slash commands and routing
# --------------------------------------------------------------------------------------

def slash_body(text, command="/jira-agent", response_url=RESPONSE_URL):
    return urllib.parse.urlencode({"command": command, "text": text, "user_id": "U1",
                                   "channel_id": "C1", "response_url": response_url})


def command_event():
    return {"headers": FORM_HEADERS}


def body_text(response):
    return json.loads(response["body"])["text"]


@pytest.mark.parametrize("payload_type,target", [
    ("block_actions", "handle_block_actions"), ("view_submission", "handle_view_submission"),
])
def test_tc19_tc20_interactivity_payloads_are_dispatched(context, lambda_client, payload_type, target):
    # Arrange
    payload = {"type": payload_type, "user": {"id": "U1"}}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)})
    with patch(f"handlers.slack_command_handler.{target}", return_value={"statusCode": 200}) as handler:
        # Act
        handle_slack_request(command_event(), context, body, lambda_client)

    # Assert
    handler.assert_called_once_with(payload, context, lambda_client)


def test_tc21_unknown_payload_type_is_acknowledged(context, lambda_client):
    # Arrange
    body = urllib.parse.urlencode({"payload": json.dumps({"type": "shortcut"})})

    # Act
    response = handle_slack_request(command_event(), context, body, lambda_client)

    # Assert
    assert response == {"statusCode": 200, "headers": {"Content-Type": "application/json; charset=utf-8"},
                        "body": ""}
    lambda_client.invoke.assert_not_called()


def test_tc22_other_command_gets_hint(context, lambda_client):
    # Act
    response = handle_slack_request(command_event(), context, slash_body("list", command="/other"), lambda_client)

    # Assert
    assert response["statusCode"] == 200
    assert "/jira-agent" in body_text(response)
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("url", [
    "", "http://hooks.slack.com/commands/x", "https://hooks.slack.com.evil.example/x",
    "https://evil.example/hooks.slack.com", "https://user@evil.example/x",
])
def test_tc23_invalid_response_url_is_rejected(context, lambda_client, url):
    # Act
    response = handle_slack_request(command_event(), context, slash_body("list", response_url=url), lambda_client)

    # Assert
    assert response["statusCode"] == 400
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("text", ["connect", "CONNECT", "  Connect  "])
def test_tc24_connect_builds_oauth_link(context, lambda_client, text):
    # Arrange
    with patch("handlers.slack_command_handler.AuthenticationJira") as auth:
        auth.return_value.build_connect_response.return_value = {"statusCode": 200, "body": "link"}

        # Act
        response = handle_slack_request(command_event(), context, slash_body(text), lambda_client)

    # Assert
    assert response["body"] == "link"
    auth.return_value.build_connect_response.assert_called_once_with("U1")
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("text,task", [
    ("list", "list_assigned_issues"), ("LIST", "list_assigned_issues"),
    ("logwork today", "jira_worklog"), ("  logwork   TODAY ", "jira_worklog"),
])
def test_tc25_fixed_commands_start_async_task(context, lambda_client, text, task):
    # Arrange
    with patch("handlers.slack_command_handler.DynamoHelper") as dynamo:
        dynamo.return_value.get_jira_account_id_for_slack_user.return_value = "acc-1"

        # Act
        response = handle_slack_request(command_event(), context, slash_body(text), lambda_client)

    # Assert
    assert body_text(response) == "Fetching your Jira data..."
    assert invoked_task(lambda_client) == {"_async_task": task, "_task_secret": TASK_SECRET,
                                           "slack_user_id": "U1", "account_id": "acc-1",
                                           "response_url": RESPONSE_URL}


def test_tc26_not_connected_user_gets_message(context, lambda_client):
    # Arrange
    with patch("handlers.slack_command_handler.DynamoHelper") as dynamo:
        dynamo.return_value.get_jira_account_id_for_slack_user.side_effect = ValueError("not connected")

        # Act
        response = handle_slack_request(command_event(), context, slash_body("list"), lambda_client)

    # Assert
    assert "not connected" in body_text(response)
    lambda_client.invoke.assert_not_called()


def test_tc27_free_text_goes_to_agent_with_original_casing(context, lambda_client):
    # Act
    response = handle_slack_request(command_event(), context, slash_body("Log  1h on PROJ-1"), lambda_client)

    # Assert
    assert body_text(response) == "I'm processing your Jira request..."
    assert invoked_task(lambda_client) == {"_async_task": "agent_request", "_task_secret": TASK_SECRET,
                                           "slack_user_id": "U1", "channel_id": "C1",
                                           "text": "Log 1h on PROJ-1", "response_url": RESPONSE_URL}


def test_tc28_missing_task_secret_raises(context, lambda_client, monkeypatch):
    # Arrange
    monkeypatch.delenv("ASYNC_TASK_SECRET")

    # Act & Assert
    with pytest.raises(KeyError):
        handle_slack_request(command_event(), context, slash_body("what is due"), lambda_client)
    lambda_client.invoke.assert_not_called()


# --------------------------------------------------------------------------------------
# Buttons and modal
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("action_id", ["approve_jira_write", "reject_jira_write"])
def test_tc29_approval_buttons_start_approval_task(context, lambda_client, action_id):
    # Arrange
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"},
               "message": {"ts": "1731139200.000100"},
               "actions": [{"action_id": action_id, "value": "approval-1"}]}

    # Act
    response = handle_block_actions(payload, context, lambda_client)

    # Assert
    assert response["statusCode"] == 200
    assert invoked_task(lambda_client) == {
        "_async_task": "handle_approval_action", "_task_secret": TASK_SECRET, "approval_id": "approval-1",
        "decision": action_id, "slack_user_id": "U1", "channel_id": "C1", "message_ts": "1731139200.000100"}


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        # 2026-11-08 17:30 UTC is already 2026-11-09 00:30 in Asia/Ho_Chi_Minh (UTC+7).
        return datetime(2026, 11, 8, 17, 30, tzinfo=timezone.utc).astimezone(tz)


def test_tc30_log_time_button_opens_modal_with_local_date(context, lambda_client):
    # Arrange
    payload = {"type": "block_actions", "user": {"id": "U1"}, "channel": {"id": "C1"}, "trigger_id": "trig-1",
               "actions": [{"action_id": "open_log_time", "value": "PROJ-7"}]}
    with patch.object(slack_action_handler, "datetime", _FixedDateTime), \
            patch.object(slack_action_handler, "slack_api") as slack_api:
        # Act
        handle_block_actions(payload, context, lambda_client)

    # Assert
    method, request = slack_api.call_args.args
    assert method == "views.open"
    assert request["trigger_id"] == "trig-1"
    view = request["view"]
    assert json.loads(view["private_metadata"]) == {"user_id": "U1", "channel_id": "C1"}
    assert view["blocks"][0]["element"]["initial_value"] == "PROJ-7"
    assert view["blocks"][1]["element"]["initial_date"] == "2026-11-09"
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("actions", [[], [{"action_id": "something_else"}]])
def test_tc31_unknown_actions_are_ignored(context, lambda_client, actions):
    # Arrange
    payload = {"type": "block_actions", "user": {"id": "U1"}, "actions": actions}
    with patch.object(slack_action_handler, "slack_api") as slack_api:
        # Act
        response = handle_block_actions(payload, context, lambda_client)

    # Assert
    assert response["statusCode"] == 200
    slack_api.assert_not_called()
    lambda_client.invoke.assert_not_called()


def modal_payload(issue="PROJ-12", date="2026-11-09", duration="1h 30m", start="09:00",
                  description="Review", estimate="leave", new_estimate=""):
    def text(value):
        return {"value": value}
    return {"type": "view_submission", "user": {"id": "U1"}, "view": {
        "private_metadata": json.dumps({"user_id": "U1", "channel_id": "C1"}),
        "state": {"values": {
            "issue_block": {"issue_key": text(issue)},
            "date_block": {"work_date": {"selected_date": date}},
            "duration_block": {"duration": text(duration)},
            "start_time_block": {"start_time": {"selected_time": start}},
            "description_block": {"description": text(description)},
            "estimate_block": {"remaining_estimate": {"selected_option": {"value": estimate}}},
            "new_estimate_block": {"new_estimate": text(new_estimate)},
        }}}}


def modal_errors(response):
    body = json.loads(response["body"])
    assert body["response_action"] == "errors"
    return body["errors"]


def test_tc32_valid_submission_requests_approval(context, lambda_client):
    # Act
    response = handle_view_submission(modal_payload(), context, lambda_client)

    # Assert
    assert json.loads(response["body"]) == {"response_action": "clear"}
    task = invoked_task(lambda_client)
    assert task["_async_task"] == "request_worklog_approval"
    assert task["_task_secret"] == TASK_SECRET
    assert task["slack_user_id"] == "U1" and task["channel_id"] == "C1"
    assert task["form"] == {"issue_key": "PROJ-12", "date": "2026-11-09", "duration": "1h 30m",
                            "start_time": "09:00", "description": "Review",
                            "remaining_estimate": "leave", "new_estimate": ""}


def test_tc33_lower_case_issue_key_is_normalised(context, lambda_client):
    # Act
    handle_view_submission(modal_payload(issue="  proj-12 "), context, lambda_client)

    # Assert
    assert invoked_task(lambda_client)["form"]["issue_key"] == "PROJ-12"


@pytest.mark.parametrize("overrides,block,message", [
    ({"issue": ""}, "issue_block", "Issue is required."),
    ({"issue": None}, "issue_block", "Issue is required."),
    ({"issue": "PROJ12"}, "issue_block", "Enter a valid issue key, e.g. PROJ-123."),
    ({"issue": "1PROJ-12"}, "issue_block", "Enter a valid issue key, e.g. PROJ-123."),
    ({"issue": "PROJ-12; DROP"}, "issue_block", "Enter a valid issue key, e.g. PROJ-123."),
    ({"date": None}, "date_block", "Date is required."),
    ({"start": None}, "start_time_block", "Start Time is required."),
])
def test_tc34_required_and_issue_key_errors(context, lambda_client, overrides, block, message):
    # Act
    errors = modal_errors(handle_view_submission(modal_payload(**overrides), context, lambda_client))

    # Assert
    assert errors == {block: message}
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("duration,message", [
    ("0m", "Duration must be greater than 0."),
    ("25h", "Duration must be at most 24h."),
    ("soon", "Duration must look like 30m, 1h, 1h 30m, or 1d."),
    ("", "Duration must look like 30m, 1h, 1h 30m, or 1d."),
])
def test_tc35_duration_errors(context, lambda_client, duration, message):
    # Act
    errors = modal_errors(handle_view_submission(modal_payload(duration=duration), context, lambda_client))

    # Assert
    assert errors == {"duration_block": message}
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("new_estimate,valid", [("", False), ("   ", False), ("2h", True)])
def test_tc36_new_estimate_required_when_selected(context, lambda_client, new_estimate, valid):
    # Act
    response = handle_view_submission(modal_payload(estimate="new", new_estimate=new_estimate),
                                      context, lambda_client)

    # Assert
    if valid:
        assert json.loads(response["body"]) == {"response_action": "clear"}
        assert invoked_task(lambda_client)["form"]["new_estimate"] == "2h"
    else:
        assert modal_errors(response) == {"new_estimate_block": "Enter a new remaining estimate."}
        lambda_client.invoke.assert_not_called()


def test_tc37_all_errors_are_returned_together(context, lambda_client):
    # Act
    errors = modal_errors(handle_view_submission(
        modal_payload(issue="", date=None, start=None, duration="0m", estimate="new"), context, lambda_client))

    # Assert
    assert set(errors) == {"issue_block", "date_block", "start_time_block", "duration_block",
                           "new_estimate_block"}
    lambda_client.invoke.assert_not_called()


# --------------------------------------------------------------------------------------
# Async invoke helper
# --------------------------------------------------------------------------------------

ARN = "arn:aws:lambda:ap-southeast-1:123456789012:function:jira-agent:live"


def access_denied():
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Invoke")


def test_tc38_invokes_qualified_arn(lambda_client):
    # Arrange
    ctx = SimpleNamespace(function_name="jira-agent", invoked_function_arn=ARN)

    # Act
    invoke_async(lambda_client, ctx, {"_async_task": "jira_worklog"})

    # Assert
    lambda_client.invoke.assert_called_once_with(
        FunctionName=ARN, InvocationType="Event", Payload=b'{"_async_task": "jira_worklog"}')


@pytest.mark.parametrize("ctx", [
    SimpleNamespace(function_name="jira-agent"),
    SimpleNamespace(function_name="jira-agent", invoked_function_arn=None),
])
def test_tc39_falls_back_to_function_name_without_arn(lambda_client, ctx):
    # Act
    invoke_async(lambda_client, ctx, {"a": 1})

    # Assert
    assert lambda_client.invoke.call_args.kwargs["FunctionName"] == "jira-agent"


def test_tc40_access_denied_on_arn_retries_with_function_name(lambda_client, caplog):
    # Arrange
    ctx = SimpleNamespace(function_name="jira-agent", invoked_function_arn=ARN)
    lambda_client.invoke.side_effect = [access_denied(), {"StatusCode": 202}]

    # Act
    result = invoke_async(lambda_client, ctx, {"a": 1})

    # Assert
    assert result == {"StatusCode": 202}
    assert [c.kwargs["FunctionName"] for c in lambda_client.invoke.call_args_list] == [ARN, "jira-agent"]
    assert "falling back" in caplog.text


@pytest.mark.parametrize("ctx,error", [
    (SimpleNamespace(function_name="jira-agent", invoked_function_arn=ARN),
     ClientError({"Error": {"Code": "TooManyRequestsException"}}, "Invoke")),
    (SimpleNamespace(function_name="jira-agent"), access_denied()),
])
def test_tc41_other_errors_are_reraised(lambda_client, ctx, error):
    # Arrange
    lambda_client.invoke.side_effect = error

    # Act & Assert
    with pytest.raises(ClientError) as exc_info:
        invoke_async(lambda_client, ctx, {"a": 1})
    assert exc_info.value is error
    lambda_client.invoke.assert_called_once()

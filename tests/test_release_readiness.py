"""Tests for the release-readiness changes: SSM secrets (#6), DM approvals (GAP-03), disconnect and
revoked-token cleanup (GAP-04), and the per-user agent rate limit (GAP-12). AAA layout; no network.
"""
import io
import json
import logging
import urllib.error
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from approval import approval_service
from approval.approval_repository import ApprovalRepository
from approval.approval_service import ApprovalService
from handlers.slack_command_handler import handle_slack_request
from util import config, dynamo_helper, rate_limit
from util.dynamo_helper import DynamoHelper, JiraConnectionRevoked
from util.rate_limit import RateLimiter

NOW = 1_700_000_000
OWNER = "U_OWNER"
FORM = {"issue_key": "PROJ-1", "date": "2026-11-09", "start_time": "09:00", "duration": "1h",
        "description": "secret notes", "remaining_estimate": "leave"}
SECRET_VALUES = {"SLACK_SIGNING_SECRET": "sign-SECRET", "SLACK_CLIENT_SECRET": "slack-client-SECRET",
                 "ATLASSIAN_CLIENT_SECRET": "client-SECRET", "ASYNC_TASK_SECRET": "task-SECRET"}


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    for name in SECRET_VALUES:
        monkeypatch.delenv(name, raising=False)
    for name in ("SSM_PARAMETER_PREFIX", "SECRET_CACHE_SECONDS", "APPROVAL_DELIVERY", "RATE_LIMITS_TABLE",
                 "AGENT_RATE_LIMIT_PER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    config.clear_cache()
    yield
    config.clear_cache()


def conditional_failed():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")


# --------------------------------------------------------------------------------------
# #6 Secrets from SSM Parameter Store
# --------------------------------------------------------------------------------------

@pytest.fixture
def ssm():
    client = MagicMock(name="ssm")
    client.get_parameters.return_value = {
        "Parameters": [{"Name": config.parameter_name("/jira-agent/prod/", name), "Value": value}
                       for name, value in SECRET_VALUES.items()],
        "InvalidParameters": []}
    with patch.object(config, "_client", return_value=client):
        yield client


@pytest.mark.parametrize("prefix,name,expected", [
    ("/jira-agent/prod/", "SLACK_SIGNING_SECRET", "/jira-agent/prod/slack-signing-secret"),
    ("/jira-agent/staging", "ASYNC_TASK_SECRET", "/jira-agent/staging/async-task-secret"),
])
def test_parameter_names_are_kebab_case_under_prefix(prefix, name, expected):
    # Act & Assert
    assert config.parameter_name(prefix, name) == expected


def test_without_prefix_secrets_come_from_env(monkeypatch, ssm):
    # Arrange
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "client-local")

    # Act & Assert
    assert config.get_secret("SLACK_CLIENT_SECRET") == "client-local"
    ssm.get_parameters.assert_not_called()


@pytest.mark.parametrize("prefix", [None, "/jira-agent/prod/"])
def test_missing_secret_raises_key_error_or_returns_default(monkeypatch, ssm, prefix):
    # Arrange
    if prefix:
        monkeypatch.setenv("SSM_PARAMETER_PREFIX", prefix)
        ssm.get_parameters.return_value = {"Parameters": [], "InvalidParameters": ["/jira-agent/prod/async-task-secret"]}

    # Act & Assert
    with pytest.raises(KeyError):
        config.get_secret("ASYNC_TASK_SECRET")
    assert config.get_secret("ASYNC_TASK_SECRET", default="") == ""


def test_unknown_secret_name_is_rejected():
    # Act & Assert
    with pytest.raises(ValueError):
        config.get_secret("AWS_SECRET_ACCESS_KEY")


def test_prefix_loads_all_secrets_in_one_decrypted_call(monkeypatch, ssm):
    # Arrange
    monkeypatch.setenv("SSM_PARAMETER_PREFIX", "/jira-agent/prod/")

    # Act
    values = {name: config.get_secret(name) for name in SECRET_VALUES}

    # Assert
    assert values == SECRET_VALUES
    ssm.get_parameters.assert_called_once()
    kwargs = ssm.get_parameters.call_args.kwargs
    assert kwargs["WithDecryption"] is True
    assert sorted(kwargs["Names"]) == sorted(config.parameter_name("/jira-agent/prod/", n) for n in SECRET_VALUES)


def test_prefix_ignores_plaintext_env_values(monkeypatch, ssm):
    # Arrange: a stale plaintext env var must never shadow the parameter
    monkeypatch.setenv("SSM_PARAMETER_PREFIX", "/jira-agent/prod/")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "stale-plaintext")

    # Act & Assert
    assert config.get_secret("SLACK_SIGNING_SECRET") == "sign-SECRET"


def test_cache_expires_and_reloads(monkeypatch, ssm):
    # Arrange
    monkeypatch.setenv("SSM_PARAMETER_PREFIX", "/jira-agent/prod/")
    monkeypatch.setenv("SECRET_CACHE_SECONDS", "300")
    # monotonic() calls: load at 0; check at 10 (fresh); check at 400 (stale) then reload at 400
    with patch.object(config.time, "monotonic", side_effect=[0, 10, 400, 400]):
        # Act
        config.get_secret("SLACK_CLIENT_SECRET")  # load at t=0
        config.get_secret("SLACK_CLIENT_SECRET")  # cached at t=10
        config.get_secret("SLACK_CLIENT_SECRET")  # expired at t=400 -> reload

    # Assert
    assert ssm.get_parameters.call_count == 2


def test_missing_parameters_are_logged_by_name_only(monkeypatch, ssm, caplog):
    # Arrange
    monkeypatch.setenv("SSM_PARAMETER_PREFIX", "/jira-agent/prod/")
    ssm.get_parameters.return_value["InvalidParameters"] = ["/jira-agent/prod/slack-client-secret"]

    # Act
    with caplog.at_level(logging.WARNING):
        config.get_secret("SLACK_SIGNING_SECRET")

    # Assert
    assert "/jira-agent/prod/slack-client-secret" in caplog.text
    assert all(value not in caplog.text for value in SECRET_VALUES.values())


def test_signature_check_uses_ssm_secret(monkeypatch, ssm):
    # Arrange
    import hashlib
    import hmac
    from slack import slack_security
    monkeypatch.setenv("SSM_PARAMETER_PREFIX", "/jira-agent/prod/")
    body = "text=list"
    digest = hmac.new(b"sign-SECRET", f"v0:{NOW}:{body}".encode(), hashlib.sha256).hexdigest()
    event = {"headers": {"X-Slack-Request-Timestamp": str(NOW), "X-Slack-Signature": f"v0={digest}"}}

    with patch.object(slack_security.time, "time", return_value=NOW):
        # Act & Assert
        assert slack_security.verify_slack_signature(event, body) is True


# --------------------------------------------------------------------------------------
# GAP-03 Approval requests sent as a DM by default
# --------------------------------------------------------------------------------------

@pytest.fixture
def approval_setup():
    repo = MagicMock(spec=ApprovalRepository)
    repo.create_pending.return_value = {"approval_id": "a-1"}
    service = ApprovalService(repo, cloud_id_resolver=lambda team, user: "cloud-1")
    with patch.object(approval_service, "slack_api") as slack_api:
        yield service, slack_api


@pytest.mark.parametrize("mode,target", [(None, OWNER), ("dm", OWNER), ("DM ", OWNER), ("bogus", OWNER),
                                         ("channel", "C1")])
def test_gap03_approval_goes_to_dm_unless_channel_mode(monkeypatch, approval_setup, mode, target):
    # Arrange
    service, slack_api = approval_setup
    if mode is not None:
        monkeypatch.setenv("APPROVAL_DELIVERY", mode)

    # Act
    service.request_worklog_approval("T1", OWNER, "C1", FORM)

    # Assert
    assert slack_api.call_args.args[1]["channel"] == target


@pytest.mark.parametrize("mode,expected", [(None, "direct message"), ("channel", "<#C1>")])
def test_gap03_messages_match_delivery_mode(monkeypatch, mode, expected):
    # Arrange
    if mode:
        monkeypatch.setenv("APPROVAL_DELIVERY", mode)

    # Act
    undelivered = approval_service.undelivered_approval_message(FORM, "C1")
    posted = approval_service.approval_posted_message()

    # Assert
    assert expected in undelivered and "secret notes" not in undelivered
    assert ("direct message" in posted) is (mode is None)


def test_gap03_agent_reply_uses_delivery_mode():
    # Arrange
    from agent.orchestrator import AgentOrchestrator

    # Act
    text = AgentOrchestrator._format_outcome({"intent": "prepare_worklog", "approval_id": "a-1"})

    # Assert
    assert text == approval_service.approval_posted_message()
    assert "direct message" in text


# --------------------------------------------------------------------------------------
# GAP-04 Disconnect and revoked tokens
# --------------------------------------------------------------------------------------

@pytest.fixture
def users_table():
    with patch.object(dynamo_helper, "_dynamodb_resource") as factory:
        yield factory.return_value.Table.return_value


@pytest.mark.parametrize("attributes,expected", [({"slack_user_id": OWNER}, True), (None, False)])
def test_gap04_disconnect_deletes_connection(users_table, attributes, expected):
    # Arrange
    users_table.delete_item.return_value = {"Attributes": attributes} if attributes else {}

    # Act
    removed = DynamoHelper("OAUTH_USERS_TABLE").disconnect_slack_user("T1", OWNER)

    # Assert
    assert removed is expected
    users_table.delete_item.assert_called_once_with(Key={"team_id": "T1", "slack_user_id": OWNER}, ReturnValues="ALL_OLD")


def test_gap04_disconnect_requires_user(users_table):
    # Act & Assert
    with pytest.raises(ValueError):
        DynamoHelper("OAUTH_USERS_TABLE").disconnect_slack_user("T1", "")
    users_table.delete_item.assert_not_called()


@pytest.mark.parametrize("item", [{"jiraAccountId": "acc-1", "jiraCloudId": "c-1", "connected": False}])
def test_gap04_disconnected_row_counts_as_not_connected(users_table, item):
    # Arrange
    users_table.get_item.return_value = {"Item": item}
    helper = DynamoHelper("OAUTH_USERS_TABLE")

    # Act & Assert
    with pytest.raises(ValueError, match="not connected"):
        helper.get_jira_account_id_for_slack_user("T1", OWNER)
    with pytest.raises(ValueError, match="not connected"):
        helper.get_jira_cloud_id_for_slack_user("T1", OWNER)


def refresh_failure(table, error_body, status=400):
    table.get_item.return_value = {"Item": {"slack_user_id": OWNER, "refreshToken": "refresh-SECRET",
                                            "accessTokenExpiresAt": NOW - 5}}
    error = urllib.error.HTTPError("https://auth.atlassian.com/oauth/token", status, "x", {},
                                   io.BytesIO(json.dumps(error_body).encode()))
    with patch.object(dynamo_helper, "time") as fake_time, \
            patch.object(dynamo_helper.uuid, "uuid4", return_value="owner-1"), \
            patch.object(dynamo_helper, "urlopen", side_effect=error):
        fake_time.time.return_value = NOW
        fake_time.monotonic.return_value = 0
        with pytest.raises(RuntimeError) as exc_info:
            DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("T1", OWNER)
    return exc_info.value


def forget_calls(table):
    return [c.kwargs for c in table.update_item.call_args_list
            if "REMOVE accessToken, refreshToken" in c.kwargs.get("UpdateExpression", "")]


@pytest.mark.parametrize("code", ["invalid_grant", "unauthorized_client", "access_denied"])
def test_gap04_revoked_grant_forgets_tokens(users_table, monkeypatch, code):
    # Arrange
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "client-id")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "client-SECRET")

    # Act
    error = refresh_failure(users_table, {"error": code, "error_description": "refresh-SECRET"})

    # Assert
    assert isinstance(error, JiraConnectionRevoked)
    assert "/jira-agent connect" in str(error)
    [forget] = forget_calls(users_table)
    assert forget["ConditionExpression"] == "refreshLockOwner = :owner"
    assert forget["ExpressionAttributeValues"] == {":false": False, ":now": NOW, ":owner": "owner-1"}
    assert "refresh-SECRET" not in str(error) and error.__cause__ is None


@pytest.mark.parametrize("body,status", [({"error": "invalid_request"}, 400), ({"error": "server_error"}, 503),
                                         ("not json", 502)])
def test_gap04_other_token_errors_keep_tokens(users_table, monkeypatch, body, status):
    # Arrange
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "client-id")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "client-SECRET")

    # Act
    error = refresh_failure(users_table, body, status)

    # Assert
    assert not isinstance(error, JiraConnectionRevoked)
    assert forget_calls(users_table) == []


# --------------------------------------------------------------------------------------
# GAP-12 Per-user agent rate limit
# --------------------------------------------------------------------------------------

@pytest.fixture
def limit_table():
    return MagicMock(name="rate_limits")


def test_gap12_counts_requests_in_hour_window(limit_table):
    # Arrange
    with patch.object(rate_limit.time, "time", return_value=NOW):
        # Act
        allowed = RateLimiter(limit_table).allow("agent#U1", limit=20)

    # Assert
    assert allowed is True
    window = NOW - NOW % 3600
    kwargs = limit_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"limit_key": f"agent#U1#{window}"}
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#count) OR #count < :limit"
    assert kwargs["ExpressionAttributeValues"] == {":one": 1, ":limit": 20, ":ttl": window + 7200}


def test_gap12_limit_reached(limit_table):
    # Arrange
    limit_table.update_item.side_effect = conditional_failed()

    # Act & Assert
    assert RateLimiter(limit_table).allow("agent#U1", limit=20) is False


def test_gap12_fails_open_on_dynamodb_error(limit_table, caplog):
    # Arrange
    limit_table.update_item.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")

    # Act & Assert
    assert RateLimiter(limit_table).allow("agent#U1", limit=20) is True
    assert "allowing the request" in caplog.text


@pytest.mark.parametrize("table,limit", [(None, 20), ("table", 0), ("table", -1)])
def test_gap12_disabled_without_table_or_limit(limit_table, table, limit):
    # Act
    allowed = RateLimiter(limit_table if table else None).allow("agent#U1", limit=limit)

    # Assert
    assert allowed is True
    limit_table.update_item.assert_not_called()


@pytest.mark.parametrize("value,expected", [(None, 20), ("5", 5), ("abc", 20), ("0", 0)])
def test_gap12_limit_from_env(monkeypatch, value, expected):
    # Arrange
    if value is not None:
        monkeypatch.setenv("AGENT_RATE_LIMIT_PER_HOUR", value)

    # Act & Assert
    assert rate_limit.agent_limit_per_hour() == expected


# --------------------------------------------------------------------------------------
# Slash command wiring
# --------------------------------------------------------------------------------------

def command(text):
    body = urllib.parse.urlencode({"command": "/jira-agent", "text": text, "team_id": "T1", "user_id": OWNER, "channel_id": "C1",
                                   "response_url": "https://hooks.slack.com/commands/T/1/x"})
    lambda_client = MagicMock()
    response = handle_slack_request({"headers": {}}, SimpleNamespace(function_name="jira-agent"), body,
                                    lambda_client)
    return json.loads(response["body"]), lambda_client


@pytest.mark.parametrize("removed,expected", [(True, "disconnected and its stored tokens deleted"),
                                              (False, "No Jira account is connected")])
def test_disconnect_command(removed, expected):
    # Arrange
    with patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.disconnect_slack_user.return_value = removed

        # Act
        body, lambda_client = command("  Disconnect ")

    # Assert
    assert expected in body["text"] and body["response_type"] == "ephemeral"
    helper.return_value.disconnect_slack_user.assert_called_once_with("T1", OWNER)
    lambda_client.invoke.assert_not_called()


def test_disconnect_command_failure_is_reported():
    # Arrange
    with patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.disconnect_slack_user.side_effect = RuntimeError("ddb down")

        # Act
        body, _ = command("disconnect")

    # Assert
    assert "Could not disconnect" in body["text"]


@pytest.mark.parametrize("allowed", [True, False])
def test_agent_requests_are_rate_limited(monkeypatch, allowed):
    # Arrange
    monkeypatch.setenv("ASYNC_TASK_SECRET", "task-SECRET")
    with patch("handlers.slack_command_handler.agent_request_allowed", return_value=allowed) as check:
        # Act
        body, lambda_client = command("how much did I log this week?")

    # Assert
    check.assert_called_once_with("T1", OWNER)
    assert lambda_client.invoke.called is allowed
    assert ("limit of 20 assistant requests per hour" in body["text"]) is (not allowed)


def test_fixed_commands_are_not_rate_limited(monkeypatch):
    # Arrange
    monkeypatch.setenv("ASYNC_TASK_SECRET", "task-SECRET")
    with patch("handlers.slack_command_handler.agent_request_allowed") as check, \
            patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.get_jira_account_id_for_slack_user.return_value = "acc-1"

        # Act
        command("list")

    # Assert
    check.assert_not_called()

"""Multi-workspace support: Slack OAuth v2 install, per-workspace bot tokens, (team_id, user_id) keys,
app_uninstalled / tokens_revoked events. AAA layout; DynamoDB, Slack and Lambda are mocked.
"""
import hashlib
import hmac
import json
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from approval.approval_repository import ApprovalRepository, build_dedupe_key
from functions import read_jira_lambda
from functions.read_jira_lambda import lambda_handler
from handlers import slack_install_handler
from handlers.slack_command_handler import handle_slack_request
from handlers.slack_events_handler import handle_slack_event, uninstall_workspace
from slack import installation_store, slack_client
from slack.installation_store import InstallationStore, SlackNotInstalled
from util import dynamo_helper
from util.dynamo_helper import DynamoHelper

NOW = 1_700_000_000
REDIRECT = "https://api.example.com/slack/oauth/callback"
FORM = {"issue_key": "PROJ-1", "date": "2026-11-09", "start_time": "09:00", "duration": "1h"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "111.222")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "slack-client-SECRET")
    monkeypatch.setenv("SLACK_REDIRECT_URI", REDIRECT)
    monkeypatch.setenv("ASYNC_TASK_SECRET", "task-secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sign-secret")
    monkeypatch.delenv("SLACK_APP_ID", raising=False)
    monkeypatch.delenv("SLACK_INSTALL_URL", raising=False)


def conditional_failed():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem")


def http_response(payload):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


# --------------------------------------------------------------------------------------
# Installation store (real implementation, fake table)
# --------------------------------------------------------------------------------------

pytestmark_store = pytest.mark.real_installation_store


@pytestmark_store
def test_store_saves_installation_and_returns_its_token():
    # Arrange
    table = MagicMock()
    store = InstallationStore(table)

    # Act
    item = store.save({"team_id": "T1", "team_name": "Acme", "bot_token": "xoxb-1", "bot_user_id": "B1"})
    table.get_item.return_value = {"Item": {"bot_token": "xoxb-1"}}
    token = store.get_bot_token("T1")

    # Assert
    assert table.put_item.call_args.kwargs["Item"] == item
    assert item["team_id"] == "T1" and item["bot_token"] == "xoxb-1" and item["team_name"] == "Acme"
    assert token == "xoxb-1"
    table.get_item.assert_called_once_with(Key={"team_id": "T1"}, ProjectionExpression="bot_token",
                                           ConsistentRead=True)


@pytestmark_store
def test_store_caches_token_briefly():
    # Arrange
    table = MagicMock()
    table.get_item.return_value = {"Item": {"bot_token": "xoxb-1"}}
    store = InstallationStore(table)

    # Act
    store.get_bot_token("T1")
    store.get_bot_token("T1")

    # Assert
    assert table.get_item.call_count == 1


@pytestmark_store
@pytest.mark.parametrize("team_id,response", [("T9", {}), ("T9", {"Item": {}}), ("", None)])
def test_store_raises_when_not_installed(team_id, response):
    # Arrange
    table = MagicMock()
    table.get_item.return_value = response

    # Act & Assert
    with pytest.raises(SlackNotInstalled):
        InstallationStore(table).get_bot_token(team_id)
    assert InstallationStore(table).is_installed(team_id) is False


@pytestmark_store
def test_store_delete_clears_cache():
    # Arrange
    table = MagicMock()
    table.get_item.return_value = {"Item": {"bot_token": "xoxb-1"}}
    table.delete_item.return_value = {"Attributes": {"team_id": "T1"}}
    store = InstallationStore(table)
    store.get_bot_token("T1")

    # Act
    removed = store.delete("T1")
    table.get_item.return_value = {}

    # Assert: the cached token is gone immediately
    assert removed is True
    with pytest.raises(SlackNotInstalled):
        store.get_bot_token("T1")


def test_slack_api_uses_the_token_of_the_given_workspace():
    # Arrange
    store = MagicMock()
    store.get_bot_token.side_effect = lambda team: {"T1": "xoxb-one", "T2": "xoxb-two"}[team]
    with patch.object(slack_client.urllib.request, "urlopen", return_value=http_response({"ok": True})) as urlopen:
        # Act
        slack_api_calls = [slack_client.slack_api("chat.postMessage", {"channel": "U1"}, team, store=store)
                           for team in ("T1", "T2")]

    # Assert
    assert len(slack_api_calls) == 2
    tokens = [c.args[0].get_header("Authorization") for c in urlopen.call_args_list]
    assert tokens == ["Bearer xoxb-one", "Bearer xoxb-two"]


# --------------------------------------------------------------------------------------
# Install flow
# --------------------------------------------------------------------------------------

@pytest.fixture
def states():
    with patch.object(slack_install_handler, "DynamoHelper") as helper:
        helper.return_value.create_slack_install_state.return_value = "state-xyz"
        yield helper.return_value


def test_install_redirects_to_slack_with_state(states):
    # Act
    response = slack_install_handler.handle_slack_install({})

    # Assert
    assert response["statusCode"] == 302
    location = urllib.parse.urlparse(response["headers"]["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://slack.com/oauth/v2/authorize"
    assert urllib.parse.parse_qs(location.query) == {
        "client_id": ["111.222"], "scope": ["commands,chat:write,users:read"], "redirect_uri": [REDIRECT],
        "state": ["state-xyz"]}
    assert response["headers"]["Cache-Control"] == "no-store"


def test_install_failure_page(states):
    # Arrange
    states.create_slack_install_state.side_effect = RuntimeError("ddb down")

    # Act
    response = slack_install_handler.handle_slack_install({})

    # Assert
    assert response["statusCode"] == 500


OAUTH_OK = {"ok": True, "access_token": "xoxb-NEW-SECRET", "token_type": "bot", "scope": "commands,chat:write",
            "bot_user_id": "B1", "app_id": "A1", "team": {"id": "T1", "name": "Acme"},
            "authed_user": {"id": "U_ADMIN"}, "is_enterprise_install": False}


@pytest.fixture
def install_deps(states):
    with patch.object(slack_install_handler, "exchange_install_code") as exchange, \
            patch.object(slack_install_handler, "InstallationStore") as store_cls:
        exchange.return_value = dict(OAUTH_OK)
        yield SimpleNamespace(states=states, exchange=exchange, store=store_cls.return_value)


def callback(params):
    return slack_install_handler.handle_slack_oauth_callback({"queryStringParameters": params})


def test_callback_stores_the_workspace_installation(install_deps):
    # Act
    response = callback({"code": "c-1", "state": "state-xyz"})

    # Assert
    assert response["statusCode"] == 200 and "Acme" in response["body"]
    install_deps.states.consume_oauth_state.assert_called_once_with("state-xyz", kind="slack_install")
    install_deps.exchange.assert_called_once_with("c-1", REDIRECT)
    install_deps.store.save.assert_called_once_with({
        "team_id": "T1", "team_name": "Acme", "app_id": "A1", "bot_user_id": "B1",
        "bot_token": "xoxb-NEW-SECRET", "scope": "commands,chat:write", "installed_by_user_id": "U_ADMIN"})
    assert "xoxb-NEW-SECRET" not in response["body"]


@pytest.mark.parametrize("params,status", [({"error": "access_denied"}, 400), ({}, 400), ({"code": "c"}, 400)])
def test_callback_rejects_cancelled_or_incomplete_requests(install_deps, params, status):
    # Act
    response = callback(params)

    # Assert
    assert response["statusCode"] == status
    install_deps.exchange.assert_not_called()
    install_deps.store.save.assert_not_called()


def test_callback_rejects_reused_state(install_deps):
    # Arrange
    install_deps.states.consume_oauth_state.side_effect = ValueError("This link has expired or has already been used.")

    # Act
    response = callback({"code": "c", "state": "old"})

    # Assert
    assert response["statusCode"] == 400
    install_deps.exchange.assert_not_called()


def test_callback_refuses_enterprise_org_install(install_deps):
    # Arrange
    install_deps.exchange.return_value = {**OAUTH_OK, "is_enterprise_install": True, "team": None,
                                          "enterprise": {"id": "E1"}}

    # Act
    response = callback({"code": "c", "state": "s"})

    # Assert
    assert response["statusCode"] == 400 and "Organization-wide install not supported" in response["body"]
    install_deps.store.save.assert_not_called()


@pytest.mark.parametrize("broken", [{"access_token": None}, {"token_type": "user"}, {"team": {}}])
def test_callback_requires_a_workspace_bot_token(install_deps, broken):
    # Arrange
    install_deps.exchange.return_value = {**OAUTH_OK, **broken}

    # Act
    response = callback({"code": "c", "state": "s"})

    # Assert
    assert response["statusCode"] == 500
    install_deps.store.save.assert_not_called()


def test_exchange_install_code_posts_client_credentials():
    # Arrange
    with patch.object(slack_client.urllib.request, "urlopen", return_value=http_response(OAUTH_OK)) as urlopen:
        # Act
        result = slack_client.exchange_install_code("c-1", REDIRECT)

    # Assert
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://slack.com/api/oauth.v2.access"
    assert urllib.parse.parse_qs(request.data.decode()) == {
        "client_id": ["111.222"], "client_secret": ["slack-client-SECRET"], "code": ["c-1"],
        "redirect_uri": [REDIRECT]}
    assert request.get_header("Authorization") is None
    assert result["team"]["id"] == "T1"


def test_exchange_install_code_error_hides_secret():
    # Arrange
    with patch.object(slack_client.urllib.request, "urlopen",
                      return_value=http_response({"ok": False, "error": "invalid_code"})):
        # Act & Assert
        with pytest.raises(RuntimeError) as exc_info:
            slack_client.exchange_install_code("bad", REDIRECT)
    assert "invalid_code" in str(exc_info.value) and "SECRET" not in str(exc_info.value)


# --------------------------------------------------------------------------------------
# Events API
# --------------------------------------------------------------------------------------

def event_body(inner, team="T1", app="A1"):
    return json.dumps({"type": "event_callback", "team_id": team, "api_app_id": app, "event": inner})


def send_event(raw):
    lambda_client = MagicMock()
    response = handle_slack_event({}, SimpleNamespace(function_name="jira-agent"), raw, lambda_client)
    return response, lambda_client


def test_url_verification_echoes_challenge():
    # Act
    response, lambda_client = send_event(json.dumps({"type": "url_verification", "challenge": "abc123"}))

    # Assert
    assert json.loads(response["body"]) == {"challenge": "abc123"}
    lambda_client.invoke.assert_not_called()


@pytest.mark.parametrize("inner,reason", [
    ({"type": "app_uninstalled"}, "app_uninstalled"),
    ({"type": "tokens_revoked", "tokens": {"oauth": [], "bot": ["B1"]}}, "bot_token_revoked"),
])
def test_uninstall_events_start_cleanup_task(inner, reason):
    # Act
    response, lambda_client = send_event(event_body(inner))

    # Assert
    assert response["statusCode"] == 200
    task = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert task == {"_async_task": "uninstall_workspace", "_task_secret": "task-secret", "team_id": "T1",
                    "reason": reason}


@pytest.mark.parametrize("raw", [
    event_body({"type": "tokens_revoked", "tokens": {"oauth": ["U1"], "bot": []}}),  # user tokens only
    event_body({"type": "app_mention"}),
    event_body({"type": "app_uninstalled"}, team=None),
    json.dumps({"type": "something_else"}),
])
def test_other_events_are_acknowledged_and_ignored(raw):
    # Act
    response, lambda_client = send_event(raw)

    # Assert
    assert response["statusCode"] == 200
    lambda_client.invoke.assert_not_called()


def test_events_for_another_app_are_ignored(monkeypatch):
    # Arrange
    monkeypatch.setenv("SLACK_APP_ID", "A_OURS")

    # Act
    response, lambda_client = send_event(event_body({"type": "app_uninstalled"}, app="A_OTHER"))

    # Assert
    assert response["statusCode"] == 200
    lambda_client.invoke.assert_not_called()


def test_invalid_event_json_is_rejected():
    # Act
    response, _ = send_event("not json")

    # Assert
    assert response["statusCode"] == 400


def test_app_uninstalled_removes_everything_of_the_workspace():
    # Arrange
    store, users, approvals = MagicMock(), MagicMock(), MagicMock()
    store.delete.return_value = True
    users.delete_team_connections.return_value = 3
    approvals.cancel_pending_for_team.return_value = 2

    # Act
    results = uninstall_workspace("T1", "app_uninstalled", store=store, users=users, approvals=approvals)

    # Assert
    assert results == {"installation": True, "jira_connections": 3, "pending_approvals": 2}
    store.delete.assert_called_once_with("T1")
    users.delete_team_connections.assert_called_once_with("T1")
    approvals.cancel_pending_for_team.assert_called_once_with("T1")


def test_bot_token_revoked_removes_only_the_installation():
    # Arrange
    store, users, approvals = MagicMock(), MagicMock(), MagicMock()

    # Act
    uninstall_workspace("T1", "bot_token_revoked", store=store, users=users, approvals=approvals)

    # Assert
    store.delete.assert_called_once_with("T1")
    users.delete_team_connections.assert_not_called()
    approvals.cancel_pending_for_team.assert_not_called()


def test_cleanup_runs_every_step_then_reports_failure():
    # Arrange
    store, users, approvals = MagicMock(), MagicMock(), MagicMock()
    store.delete.side_effect = RuntimeError("ddb down")

    # Act & Assert: later steps still run; the failure is raised so the task reaches the failure queue
    with pytest.raises(RuntimeError, match="installation"):
        uninstall_workspace("T1", "app_uninstalled", store=store, users=users, approvals=approvals)
    users.delete_team_connections.assert_called_once_with("T1")
    approvals.cancel_pending_for_team.assert_called_once_with("T1")


def test_uninstall_task_is_routed():
    # Arrange
    from handlers.async_task_handler import handle_async_task
    with patch("handlers.slack_events_handler.uninstall_workspace") as cleanup:
        # Act
        response = handle_async_task({"_async_task": "uninstall_workspace", "team_id": "T1",
                                      "reason": "app_uninstalled"})

    # Assert
    assert response["statusCode"] == 200
    cleanup.assert_called_once_with("T1", reason="app_uninstalled")


# --------------------------------------------------------------------------------------
# Workspace-scoped data
# --------------------------------------------------------------------------------------

@pytest.fixture
def users_table():
    with patch.object(dynamo_helper, "_dynamodb_resource") as factory:
        yield factory.return_value.Table.return_value


def test_delete_team_connections_queries_only_that_workspace(users_table):
    # Arrange: two pages
    users_table.query.side_effect = [
        {"Items": [{"team_id": "T1", "slack_user_id": "U1"}], "LastEvaluatedKey": {"k": 1}},
        {"Items": [{"team_id": "T1", "slack_user_id": "U2"}]},
    ]
    batch = users_table.batch_writer.return_value.__enter__.return_value

    # Act
    deleted = DynamoHelper("OAUTH_USERS_TABLE").delete_team_connections("T1")

    # Assert
    assert deleted == 2
    assert batch.delete_item.call_args_list == [call(Key={"team_id": "T1", "slack_user_id": "U1"}),
                                                call(Key={"team_id": "T1", "slack_user_id": "U2"})]
    assert users_table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


@pytest.mark.parametrize("team,user", [("", "U1"), ("T1", ""), (None, None)])
def test_connection_keys_require_workspace_and_user(users_table, team, user):
    # Act & Assert
    with pytest.raises(ValueError, match="workspace and user"):
        DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials(team, user)
    users_table.get_item.assert_not_called()


def test_same_user_id_in_two_workspaces_are_different_connections(users_table):
    # Arrange
    users_table.get_item.return_value = {"Item": {"jiraAccountId": "acc"}}
    helper = DynamoHelper("OAUTH_USERS_TABLE")

    # Act
    helper.get_jira_account_id_for_slack_user("T1", "U1")
    helper.get_jira_account_id_for_slack_user("T2", "U1")

    # Assert
    keys = [c.kwargs["Key"] for c in users_table.get_item.call_args_list]
    assert keys == [{"team_id": "T1", "slack_user_id": "U1"}, {"team_id": "T2", "slack_user_id": "U1"}]


def test_dedupe_key_is_per_workspace():
    # Act & Assert
    assert build_dedupe_key("T1", "U1", FORM) != build_dedupe_key("T2", "U1", FORM)


def test_claim_requires_the_same_workspace():
    # Arrange: same user id, different workspace -> the condition fails
    table = MagicMock()
    table.update_item.side_effect = conditional_failed()

    # Act
    result = ApprovalRepository(table=table).claim("a-1", "T_OTHER", "U1")

    # Assert
    assert result is None
    kwargs = table.update_item.call_args.kwargs
    assert "team_id = :team" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeValues"][":team"] == "T_OTHER"


def test_cancel_pending_for_team_scans_and_cancels():
    # Arrange
    table = MagicMock()
    table.scan.side_effect = [{"Items": [{"approval_id": "a-1"}], "LastEvaluatedKey": {"k": 1}},
                              {"Items": [{"approval_id": "a-2"}]}]
    repo = ApprovalRepository(table=table)

    # Act
    with patch.object(ApprovalRepository, "cancel", side_effect=[{"status": "CANCELLED"}, None]) as cancel:
        cancelled = repo.cancel_pending_for_team("T1")

    # Assert: a-2 was no longer PENDING (cancel returned None), so only one counts
    assert cancelled == 1
    assert [c.args[0] for c in cancel.call_args_list] == ["a-1", "a-2"]
    first = table.scan.call_args_list[0].kwargs
    assert first["FilterExpression"] == "team_id = :team AND #s = :pending"
    assert first["ExpressionAttributeValues"] == {":team": "T1", ":pending": "PENDING"}


# --------------------------------------------------------------------------------------
# Slash commands and routing
# --------------------------------------------------------------------------------------

def slash(text, team="T1"):
    data = {"command": "/jira-agent", "text": text, "user_id": "U1", "channel_id": "C1",
            "response_url": "https://hooks.slack.com/commands/T/1/x"}
    if team is not None:
        data["team_id"] = team
    lambda_client = MagicMock()
    response = handle_slack_request({"headers": {}}, SimpleNamespace(function_name="jira-agent"),
                                    urllib.parse.urlencode(data), lambda_client)
    return response, lambda_client


def test_command_without_team_is_rejected():
    # Act
    response, lambda_client = slash("list", team=None)

    # Assert
    assert response["statusCode"] == 400
    lambda_client.invoke.assert_not_called()


@pytest.mark.real_installation_store
@pytest.mark.parametrize("install_url,expected", [
    ("https://api.example.com/slack/install", "<https://api.example.com/slack/install|Install the app>"),
    (None, "Ask your Slack admin to install the app"),
])
def test_command_from_workspace_without_installation(monkeypatch, install_url, expected):
    # Arrange
    if install_url:
        monkeypatch.setenv("SLACK_INSTALL_URL", install_url)
    with patch("handlers.slack_command_handler.InstallationStore") as store_cls:
        store_cls.return_value.is_installed.return_value = False

        # Act
        response, lambda_client = slash("list")

    # Assert
    body = json.loads(response["body"])
    assert "hasn't installed the Jira app" in body["text"] and expected in body["text"]
    store_cls.return_value.is_installed.assert_called_once_with("T1")
    lambda_client.invoke.assert_not_called()


@pytest.fixture
def entry():
    with patch.object(read_jira_lambda, "handle_slack_install", return_value={"statusCode": 302}) as install, \
            patch.object(read_jira_lambda, "handle_slack_oauth_callback", return_value={"statusCode": 200, "body": "slack"}) as slack_cb, \
            patch.object(read_jira_lambda, "handle_oauth_callback", return_value={"statusCode": 200, "body": "jira"}) as jira_cb, \
            patch.object(read_jira_lambda, "handle_slack_event", return_value={"statusCode": 200, "body": "event"}) as events, \
            patch.object(read_jira_lambda, "handle_slack_request", return_value={"statusCode": 200, "body": "cmd"}) as cmd, \
            patch.object(read_jira_lambda, "boto3"):
        yield SimpleNamespace(install=install, slack_cb=slack_cb, jira_cb=jira_cb, events=events, cmd=cmd)


def get(path):
    return {"rawPath": path, "requestContext": {"http": {"method": "GET"}}}


def test_routes_slack_install_and_callback_before_atlassian(entry):
    # Act
    lambda_handler(get("/slack/install"), None)
    slack_result = lambda_handler(get("/slack/oauth/callback"), None)
    jira_result = lambda_handler(get("/oauth/callback"), None)

    # Assert: "/slack/oauth/callback" also ends with "/oauth/callback" but goes to the Slack handler
    entry.install.assert_called_once()
    assert slack_result["body"] == "slack" and jira_result["body"] == "jira"


def signed(path, body, secret="sign-secret"):
    digest = hmac.new(secret.encode(), f"v0:{NOW}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"rawPath": path, "requestContext": {"http": {"method": "POST"}}, "body": body,
            "headers": {"X-Slack-Request-Timestamp": str(NOW), "X-Slack-Signature": f"v0={digest}"}}


def test_events_endpoint_requires_signature_and_routes(entry):
    # Arrange
    from slack import slack_security
    body = json.dumps({"type": "url_verification", "challenge": "c"})
    with patch.object(slack_security.time, "time", return_value=NOW):
        # Act
        ok = lambda_handler(signed("/slack/events", body), None)
        forged = lambda_handler(signed("/slack/events", body, secret="attacker"), None)

    # Assert
    assert ok["body"] == "event" and forged["statusCode"] == 401
    entry.events.assert_called_once()
    entry.cmd.assert_not_called()

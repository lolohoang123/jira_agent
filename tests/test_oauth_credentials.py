"""Unit tests for OAuth connection and Jira credentials, from docs/testing/oauth_credentials_test_matrix.md
(TC-01..TC-54). AAA layout, frozen clock; DynamoDB, Atlassian and Jira HTTP are mocked. No network.
"""
import io
import json
import logging
import traceback
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from jira import authentication_jira as auth_module
from jira import jira_client as client_module
from jira.authentication_jira import AuthenticationJira
from jira.jira_client import JiraClient
from util import dynamo_helper
from util.constant import ATLASSIAN_RESOURCES_URL, ATLASSIAN_TOKEN_URL
from util.dynamo_helper import DynamoHelper

NOW = 1_700_000_000
ACCESS = "access-SECRET-123"
REFRESH = "refresh-SECRET-456"
NEW_ACCESS = "access-NEW-789"
NEW_REFRESH = "refresh-NEW-000"
SECRETS = (ACCESS, REFRESH, NEW_ACCESS, NEW_REFRESH, "client-secret-xyz")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OAUTH_USERS_TABLE", "users-test")
    monkeypatch.setenv("OAUTH_STATES_TABLE", "states-test")
    monkeypatch.setenv("ATLASSIAN_CLIENT_ID", "client-id")
    monkeypatch.setenv("ATLASSIAN_CLIENT_SECRET", "client-secret-xyz")
    monkeypatch.setenv("ATLASSIAN_REDIRECT_URI", "https://api.example.com/oauth/callback")
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)


@pytest.fixture
def clock():
    """Replaces the `time` module inside util.dynamo_helper (time, monotonic, sleep)."""
    with patch.object(dynamo_helper, "time") as fake_time:
        fake_time.time.return_value = NOW
        fake_time.monotonic.return_value = 0
        yield fake_time


@pytest.fixture
def resource():
    with patch.object(dynamo_helper, "_dynamodb_resource") as factory:
        yield factory.return_value


@pytest.fixture
def table(resource):
    return resource.Table.return_value


def conditional_failed(op="UpdateItem"):
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}}, op)


def assert_no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text, f"secret value leaked: {secret}"


def exception_text(exc):
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def http_response(body):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body if isinstance(body, bytes) else body.encode()
    return response


def user_row(expires_at=NOW + 3600, **overrides):
    row = {"slack_user_id": "U1", "accessToken": ACCESS, "refreshToken": REFRESH, "jiraCloudId": "cloud-1",
           "jiraSiteUrl": "https://acme.atlassian.net", "accessTokenExpiresAt": expires_at, "connected": True}
    row.update(overrides)
    return row


# --------------------------------------------------------------------------------------
# DynamoHelper – tables, account lookup, OAuth state
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("env_name,table_name", [("OAUTH_USERS_TABLE", "users-test"),
                                                 ("OAUTH_STATES_TABLE", "states-test")])
def test_tc01_table_name_comes_from_env(resource, env_name, table_name):
    # Act
    DynamoHelper(env_name).oauth_table()

    # Assert
    resource.Table.assert_called_once_with(table_name)


def test_tc02_users_table_has_legacy_fallback(resource, monkeypatch):
    # Arrange
    monkeypatch.delenv("OAUTH_USERS_TABLE")

    # Act
    DynamoHelper("OAUTH_USERS_TABLE").oauth_table()

    # Assert
    resource.Table.assert_called_once_with("read-jira-users")


def test_tc03_missing_states_table_raises(resource, monkeypatch):
    # Arrange
    monkeypatch.delenv("OAUTH_STATES_TABLE")

    # Act & Assert
    with pytest.raises(RuntimeError, match="OAUTH_STATES_TABLE"):
        DynamoHelper("OAUTH_STATES_TABLE").oauth_table()


@pytest.mark.parametrize("endpoint,expected_kwargs", [
    (None, {}),
    ("http://localhost:8000", {"endpoint_url": "http://localhost:8000", "region_name": "us-east-1",
                               "aws_access_key_id": "local", "aws_secret_access_key": "local"}),
])
def test_tc04_resource_uses_local_endpoint_when_set(monkeypatch, endpoint, expected_kwargs):
    # Arrange
    for name in ("AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    if endpoint:
        monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", endpoint)

    with patch.object(dynamo_helper, "boto3") as boto3:
        # Act
        dynamo_helper._dynamodb_resource()

    # Assert
    boto3.resource.assert_called_once_with("dynamodb", **expected_kwargs)


def test_tc05_account_id_for_connected_user(table):
    # Arrange
    table.get_item.return_value = {"Item": {"jiraAccountId": "acc-1"}}

    # Act
    account_id = DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user("U1")

    # Assert
    assert account_id == "acc-1"
    table.get_item.assert_called_once_with(Key={"slack_user_id": "U1"},
                                           ProjectionExpression="jiraAccountId, #connected",
                                           ExpressionAttributeNames={"#connected": "connected"},
                                           ConsistentRead=True)


@pytest.mark.parametrize("user,response,message", [
    ("", None, "Could not identify the Slack user"),
    ("U1", {}, "not connected"),
    ("U1", {"Item": {}}, "not connected"),
    ("U1", {"Item": {"jiraAccountId": ""}}, "not connected"),
])
def test_tc06_account_id_errors(table, user, response, message):
    # Arrange
    table.get_item.return_value = response

    # Act & Assert
    with pytest.raises(ValueError, match=message):
        DynamoHelper("OAUTH_USERS_TABLE").get_jira_account_id_for_slack_user(user)
    if not user:
        table.get_item.assert_not_called()


def test_tc07_authorization_url_stores_one_time_state(table, clock):
    # Arrange
    with patch.object(dynamo_helper.secrets, "token_urlsafe", return_value="state-123") as token:
        # Act
        url = DynamoHelper("OAUTH_STATES_TABLE").create_oauth_authorization_url("U1")

    # Assert
    token.assert_called_once_with(32)
    table.put_item.assert_called_once_with(
        Item={"state": "state-123", "slackUserId": "U1", "createdAt": NOW, "expiresAt": NOW + 600},
        ConditionExpression="attribute_not_exists(#state)", ExpressionAttributeNames={"#state": "state"})
    parsed = urllib.parse.urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.atlassian.com/authorize"
    assert urllib.parse.parse_qs(parsed.query) == {
        "audience": ["api.atlassian.com"], "client_id": ["client-id"],
        "scope": ["read:jira-user read:jira-work write:jira-work offline_access"],
        "redirect_uri": ["https://api.example.com/oauth/callback"], "state": ["state-123"],
        "response_type": ["code"], "prompt": ["consent"]}


def test_tc08_authorization_url_requires_user(table):
    # Act & Assert
    with pytest.raises(ValueError):
        DynamoHelper("OAUTH_STATES_TABLE").create_oauth_authorization_url("")
    table.put_item.assert_not_called()


def test_tc09_consume_valid_state(table, clock):
    # Arrange
    table.update_item.return_value = {"Attributes": {"state": "state-123", "slackUserId": "U1"}}

    # Act
    user_id = DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state("state-123")

    # Assert
    assert user_id == "U1"
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"state": "state-123"}
    assert kwargs["UpdateExpression"] == "SET consumedAt = :now"
    assert kwargs["ConditionExpression"] == (
        "attribute_exists(#state) AND attribute_not_exists(consumedAt) AND expiresAt > :now")
    assert kwargs["ExpressionAttributeValues"] == {":now": NOW}


def test_tc10_expired_or_reused_state_is_rejected(table, clock):
    # Arrange
    table.update_item.side_effect = conditional_failed()

    # Act & Assert
    with pytest.raises(ValueError, match="expired or has already been used"):
        DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state("state-123")


@pytest.mark.parametrize("state,attributes", [("", None), ("state-123", {}), ("state-123", {"state": "x"})])
def test_tc11_empty_state_or_missing_user_is_rejected(table, clock, state, attributes):
    # Arrange
    table.update_item.return_value = {"Attributes": attributes} if attributes is not None else {}

    # Act & Assert
    with pytest.raises(ValueError):
        DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state(state)


def test_tc12_other_dynamodb_error_is_reraised(table, clock):
    # Arrange
    error = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
    table.update_item.side_effect = error

    # Act & Assert
    with pytest.raises(ClientError) as exc_info:
        DynamoHelper("OAUTH_STATES_TABLE").consume_oauth_state("state-123")
    assert exc_info.value is error


# --------------------------------------------------------------------------------------
# DynamoHelper – expiry and credentials
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, 0), ("", 0), ("abc", 0), (NOW, NOW), (str(NOW), NOW),
    (10 ** 12, 10 ** 12), (10 ** 12 + 1, (10 ** 12 + 1) // 1000), (NOW * 1000 + 999, NOW),
])
def test_tc13_normalize_expiry_accepts_seconds_and_legacy_millis(value, expected):
    # Act & Assert
    assert DynamoHelper.normalize_expiry(value) == expected


def test_tc14_valid_token_is_returned_without_refresh(table, clock):
    # Arrange
    table.get_item.return_value = {"Item": user_row()}
    helper = DynamoHelper("OAUTH_USERS_TABLE")
    with patch.object(DynamoHelper, "refresh_access_token") as refresh:
        # Act
        credentials = helper.get_oauth_credentials("U1")

    # Assert
    assert credentials == {"access_token": ACCESS, "cloud_id": "cloud-1",
                           "jira_site_url": "https://acme.atlassian.net"}
    refresh.assert_not_called()
    table.get_item.assert_called_once_with(Key={"slack_user_id": "U1"}, ConsistentRead=True)


@pytest.mark.parametrize("expires_at,refreshed", [
    (NOW + 60, True), (NOW + 61, False), (NOW - 1, True), (None, True),
    ((NOW + 3600) * 1000, False),  # TC-16: legacy millisecond value still valid
    ((NOW + 30) * 1000, True),
])
def test_tc15_tc16_refresh_threshold_is_60_seconds(table, clock, expires_at, refreshed):
    # Arrange
    table.get_item.return_value = {"Item": user_row(expires_at=expires_at)}
    with patch.object(DynamoHelper, "refresh_access_token", return_value=user_row(accessToken=NEW_ACCESS)) as refresh:
        # Act
        credentials = DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials("U1")

    # Assert
    assert refresh.called is refreshed
    assert credentials["access_token"] == (NEW_ACCESS if refreshed else ACCESS)


@pytest.mark.parametrize("user,response,error", [
    ("", None, ValueError),
    ("U1", {}, RuntimeError),
    ("U1", {"Item": user_row(connected=False)}, RuntimeError),
])
def test_tc17_unknown_or_disconnected_user_is_rejected(table, clock, user, response, error):
    # Arrange
    table.get_item.return_value = response

    # Act & Assert
    with pytest.raises(error):
        DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials(user)


def test_tc17_missing_connected_flag_counts_as_connected(table, clock):
    # Arrange: rows written before the flag existed
    row = user_row()
    del row["connected"]
    table.get_item.return_value = {"Item": row}

    # Act & Assert
    assert DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials("U1")["access_token"] == ACCESS


@pytest.mark.parametrize("missing", ["accessToken", "jiraCloudId"])
def test_tc18_incomplete_record_is_rejected(table, clock, missing):
    # Arrange
    table.get_item.return_value = {"Item": user_row(**{missing: ""})}

    # Act & Assert
    with pytest.raises(RuntimeError, match="missing access token or cloud ID") as exc_info:
        DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials("U1")
    assert_no_secrets(exception_text(exc_info.value))


def test_tc19_expired_token_is_refreshed(table, clock):
    # Arrange
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 10)}
    fresh = user_row(accessToken=NEW_ACCESS, expires_at=NOW + 3600)
    with patch.object(DynamoHelper, "refresh_access_token", return_value=fresh) as refresh:
        # Act
        credentials = DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials("U1")

    # Assert
    refresh.assert_called_once_with("U1")
    assert credentials["access_token"] == NEW_ACCESS


# --------------------------------------------------------------------------------------
# DynamoHelper – token refresh with lock
# --------------------------------------------------------------------------------------

@pytest.fixture
def owner():
    with patch.object(dynamo_helper.uuid, "uuid4", return_value="owner-1"):
        yield "owner-1"


@pytest.fixture
def urlopen():
    with patch.object(dynamo_helper, "urlopen") as fake:
        yield fake


def lock_call(table):
    return table.update_item.call_args_list[0].kwargs


def release_calls(table):
    return [c.kwargs for c in table.update_item.call_args_list[1:]
            if c.kwargs.get("UpdateExpression") == "REMOVE refreshLockOwner, refreshLockUntil"]


def test_tc20_refresh_stores_new_tokens_and_releases_lock(table, clock, owner, urlopen):
    # Arrange
    stale, fresh = user_row(expires_at=NOW - 5), user_row(accessToken=NEW_ACCESS, refreshToken=NEW_REFRESH)
    table.get_item.side_effect = [{"Item": stale}, {"Item": fresh}]
    urlopen.return_value = http_response(json.dumps(
        {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 3600}))

    # Act
    result = DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")

    # Assert
    assert result == fresh
    assert lock_call(table)["ExpressionAttributeValues"] == {":owner": "owner-1", ":until": NOW + 30, ":now": NOW}
    request = urlopen.call_args.args[0]
    assert request.full_url == ATLASSIAN_TOKEN_URL and request.get_method() == "POST"
    assert urlopen.call_args.kwargs == {"timeout": 15}
    assert json.loads(request.data) == {"grant_type": "refresh_token", "client_id": "client-id",
                                        "client_secret": "client-secret-xyz", "refresh_token": REFRESH}
    store = table.update_item.call_args_list[1].kwargs
    assert "REMOVE refreshLockOwner, refreshLockUntil" in store["UpdateExpression"]
    assert store["ConditionExpression"] == "refreshLockOwner = :owner"
    assert store["ExpressionAttributeValues"] == {
        ":owner": "owner-1", ":a": NEW_ACCESS, ":r": NEW_REFRESH, ":e": NOW + 3600,
        ":iso": "2023-11-14T23:13:20Z", ":u": NOW}


def test_tc21_refresh_keeps_old_refresh_token_when_not_rotated(table, clock, owner, urlopen):
    # Arrange
    table.get_item.side_effect = [{"Item": user_row(expires_at=NOW - 5)}, {"Item": user_row()}]
    urlopen.return_value = http_response(json.dumps({"access_token": NEW_ACCESS, "expires_in": 3600}))

    # Act
    DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")

    # Assert
    assert table.update_item.call_args_list[1].kwargs["ExpressionAttributeValues"][":r"] == REFRESH


def test_tc22_already_refreshed_token_skips_atlassian(table, clock, owner, urlopen):
    # Arrange: lock acquired, but another request refreshed the token in the meantime
    table.get_item.return_value = {"Item": user_row(expires_at=NOW + 3600)}

    # Act
    result = DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")

    # Assert
    assert result["accessToken"] == ACCESS
    urlopen.assert_not_called()


def test_tc23_waits_for_other_refresh_then_uses_its_token(table, clock, owner, urlopen):
    # Arrange
    table.update_item.side_effect = conditional_failed()
    table.get_item.return_value = {"Item": user_row(accessToken=NEW_ACCESS, expires_at=NOW + 3600)}

    # Act
    result = DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")

    # Assert
    assert result["accessToken"] == NEW_ACCESS
    urlopen.assert_not_called()
    table.update_item.assert_called_once()  # never released a lock it did not own


def test_tc24_gives_up_after_deadline(table, clock, owner, urlopen):
    # Arrange: lock always held, token stays stale; monotonic: deadline=8, then 1 (wait), then 9 (give up)
    table.update_item.side_effect = conditional_failed()
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 5)}
    clock.monotonic.side_effect = [0, 1, 9]

    # Act & Assert
    with pytest.raises(RuntimeError, match="please retry"):
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")
    clock.sleep.assert_called_once_with(0.5)
    assert table.update_item.call_count == 2
    urlopen.assert_not_called()


def test_tc25_record_deleted_while_waiting(table, clock, owner, urlopen):
    # Arrange
    table.update_item.side_effect = conditional_failed()
    table.get_item.return_value = {}

    # Act & Assert
    with pytest.raises(RuntimeError, match="not found"):
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")


def test_tc26_unexpected_lock_error_is_reraised(table, clock, owner, urlopen):
    # Arrange
    error = ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")
    table.update_item.side_effect = error

    # Act & Assert
    with pytest.raises(ClientError) as exc_info:
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")
    assert exc_info.value is error
    table.get_item.assert_not_called()


def test_tc27_missing_refresh_token_releases_lock(table, clock, owner, urlopen):
    # Arrange
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 5, refreshToken="")}

    # Act & Assert
    with pytest.raises(RuntimeError, match="reconnect"):
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")
    urlopen.assert_not_called()
    assert release_calls(table) == [{
        "Key": {"slack_user_id": "U1"}, "UpdateExpression": "REMOVE refreshLockOwner, refreshLockUntil",
        "ConditionExpression": "refreshLockOwner = :owner", "ExpressionAttributeValues": {":owner": "owner-1"}}]


@pytest.mark.parametrize("failure,message", [
    # A temporary Atlassian error keeps the tokens (invalid_grant is covered by the revocation tests).
    (urllib.error.HTTPError(ATLASSIAN_TOKEN_URL, 503, "Service Unavailable", {},
                            io.BytesIO(json.dumps({"error": "server_error", "token": REFRESH}).encode())),
     "HTTP 503"),
    (urllib.error.URLError("timed out"), "Could not reach Atlassian"),
    (TimeoutError("read timed out"), "Could not reach Atlassian"),
])
def test_tc28_tc29_atlassian_failure_releases_lock_without_leaking(table, clock, owner, urlopen, failure, message):
    # Arrange
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 5)}
    urlopen.side_effect = failure

    # Act & Assert
    with pytest.raises(RuntimeError, match=message) as exc_info:
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")
    assert len(release_calls(table)) == 1
    assert_no_secrets(exception_text(exc_info.value))
    if isinstance(failure, urllib.error.HTTPError):
        assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


@pytest.mark.parametrize("token_response", [{"expires_in": 3600}, {"access_token": NEW_ACCESS}, {}])
def test_tc30_incomplete_token_response_stores_nothing(table, clock, owner, urlopen, token_response):
    # Arrange
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 5)}
    urlopen.return_value = http_response(json.dumps(token_response))

    # Act & Assert
    with pytest.raises(RuntimeError, match="missing access_token or expires_in"):
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")
    stored = [c for c in table.update_item.call_args_list if ":a" in c.kwargs.get("ExpressionAttributeValues", {})]
    assert stored == []
    assert len(release_calls(table)) == 1


def test_tc31_release_failure_keeps_original_error(table, clock, owner, urlopen):
    # Arrange: lock ok, Atlassian down, then the release update fails too
    table.get_item.return_value = {"Item": user_row(expires_at=NOW - 5)}
    urlopen.side_effect = urllib.error.URLError("down")
    table.update_item.side_effect = [{}, conditional_failed()]

    # Act & Assert
    with pytest.raises(RuntimeError, match="Could not reach Atlassian"):
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("U1")


# --------------------------------------------------------------------------------------
# AuthenticationJira – OAuth callback
# --------------------------------------------------------------------------------------

SITE = {"id": "cloud-1", "url": "https://acme.atlassian.net/", "name": "Acme"}


def test_tc32_html_response_is_escaped_and_not_cached():
    # Act
    response = AuthenticationJira({}).html_response(400, "<b>Title</b>", "a & <script>x</script>")

    # Assert
    assert response["statusCode"] == 400
    assert response["headers"] == {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}
    assert "<script>" not in response["body"] and "&lt;script&gt;" in response["body"]
    assert "&lt;b&gt;Title&lt;/b&gt;" in response["body"]


@pytest.mark.parametrize("resources", [
    [SITE],
    [{"id": "", "url": "https://x"}, {"id": "y"}, SITE],
])
def test_tc33_single_usable_site_is_chosen(resources):
    # Act & Assert
    assert AuthenticationJira({}).choose_jira_resource(resources) == SITE


@pytest.mark.parametrize("preferred", ["https://ACME.atlassian.net", "https://acme.atlassian.net/"])
def test_tc34_preferred_site_wins_among_many(monkeypatch, preferred):
    # Arrange
    monkeypatch.setenv("JIRA_BASE_URL", preferred)
    other = {"id": "cloud-2", "url": "https://other.atlassian.net", "name": "Other"}

    # Act & Assert
    assert AuthenticationJira({}).choose_jira_resource([other, SITE]) == SITE


@pytest.mark.parametrize("preferred", [None, "https://nomatch.atlassian.net"])
def test_tc35_many_sites_without_match_is_an_error(monkeypatch, preferred):
    # Arrange
    if preferred:
        monkeypatch.setenv("JIRA_BASE_URL", preferred)
    other = {"id": "cloud-2", "url": "https://other.atlassian.net"}

    # Act & Assert
    with pytest.raises(RuntimeError, match="Multiple Jira sites"):
        AuthenticationJira({}).choose_jira_resource([other, SITE])


@pytest.mark.parametrize("resources", [[], [{"id": "x"}]])
def test_tc36_no_site_is_an_error(resources):
    # Act & Assert
    with pytest.raises(RuntimeError, match="No Jira site"):
        AuthenticationJira({}).choose_jira_resource(resources)


@pytest.fixture
def helpers():
    """Separate fakes for the states table and the users table."""
    fakes = {"OAUTH_STATES_TABLE": MagicMock(name="states"), "OAUTH_USERS_TABLE": MagicMock(name="users")}
    fakes["OAUTH_STATES_TABLE"].consume_oauth_state.return_value = "U1"
    with patch.object(auth_module, "DynamoHelper", side_effect=lambda name: fakes[name]):
        yield fakes


def stored_record(helpers):
    put = helpers["OAUTH_USERS_TABLE"].oauth_table.return_value.put_item
    return put.call_args.kwargs["Item"] if put.called else None


@pytest.fixture
def atlassian():
    """Scripted Atlassian: token exchange, accessible resources, /myself."""
    responses = {
        "token": {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600, "scope": "read:jira-work"},
        "resources": [SITE],
        "myself": {"accountId": "acc-1"},
    }

    def fake(url, method="GET", data=None, access_token=None):
        if url == ATLASSIAN_TOKEN_URL:
            return responses["token"]
        assert access_token == ACCESS
        if url == ATLASSIAN_RESOURCES_URL:
            return responses["resources"]
        if url.endswith("/rest/api/3/myself"):
            return responses["myself"]
        raise AssertionError(url)

    with patch.object(AuthenticationJira, "atlassian_json_request", side_effect=fake) as mock:
        mock.responses = responses
        yield mock


def callback(params):
    return AuthenticationJira({"queryStringParameters": params}).finish_oauth_callback()


def test_tc37_cancelled_consent(helpers, atlassian):
    # Act
    response = callback({"error": "access_denied", "state": "s"})

    # Assert
    assert response["statusCode"] == 400 and "cancelled" in response["body"]
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.assert_not_called()
    atlassian.assert_not_called()


@pytest.mark.parametrize("params", [None, {}, {"code": "c"}, {"state": "s"}, {"code": "", "state": "s"}])
def test_tc38_missing_code_or_state(helpers, atlassian, params):
    # Act
    response = AuthenticationJira({"queryStringParameters": params}).finish_oauth_callback()

    # Assert
    assert response["statusCode"] == 400 and "Missing authorization code or state" in response["body"]
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.assert_not_called()


def test_tc39_expired_state_stops_before_token_exchange(helpers, atlassian):
    # Arrange
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.side_effect = ValueError(
        "This OAuth link has expired or has already been used. Run `/jira-agent connect` again.")

    # Act
    response = callback({"code": "c", "state": "s"})

    # Assert
    assert response["statusCode"] == 400 and "expired or has already been used" in response["body"]
    atlassian.assert_not_called()
    assert stored_record(helpers) is None


def test_tc40_valid_callback_stores_connection(helpers, atlassian, caplog):
    # Arrange
    with patch.object(auth_module.time, "time", return_value=NOW), caplog.at_level(logging.DEBUG):
        # Act
        response = callback({"code": "auth-code", "state": "state-123"})

    # Assert
    assert response["statusCode"] == 200 and "Connected to Acme" in response["body"]
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.assert_called_once_with("state-123")
    assert stored_record(helpers) == {
        "slack_user_id": "U1", "jiraAccountId": "acc-1", "jiraCloudId": "cloud-1",
        "jiraSiteUrl": "https://acme.atlassian.net", "jiraSiteName": "Acme",
        "accessToken": ACCESS, "refreshToken": REFRESH, "accessTokenExpiresAt": NOW + 3600,
        "accessTokenExpiresAtIso": "2023-11-14T23:13:20Z", "scope": "read:jira-work", "connected": True,
        "connectedAt": NOW, "updatedAt": NOW}
    assert_no_secrets(response["body"], caplog.text)


def test_tc41_default_expiry_is_one_hour(helpers, atlassian):
    # Arrange
    del atlassian.responses["token"]["expires_in"]

    with patch.object(auth_module.time, "time", return_value=NOW):
        # Act
        callback({"code": "c", "state": "s"})

    # Assert
    assert stored_record(helpers)["accessTokenExpiresAt"] == NOW + 3600


def test_tc42_missing_refresh_token_fails_without_storing(helpers, atlassian, caplog):
    # Arrange
    atlassian.responses["token"]["refresh_token"] = ""

    # Act
    response = callback({"code": "c", "state": "s"})

    # Assert
    assert response["statusCode"] == 500
    assert stored_record(helpers) is None
    assert_no_secrets(response["body"], caplog.text)


@pytest.mark.parametrize("break_it", ["http", "sites", "storage", "resources_not_list"])
def test_tc43_failures_return_generic_page(helpers, atlassian, caplog, break_it):
    # Arrange
    if break_it == "http":
        atlassian.side_effect = RuntimeError("Atlassian OAuth/API request failed with HTTP 401.")
    elif break_it == "sites":
        atlassian.responses["resources"] = [SITE, {"id": "cloud-2", "url": "https://other.atlassian.net"}]
    elif break_it == "resources_not_list":
        atlassian.responses["resources"] = {"unexpected": "object"}
    else:
        helpers["OAUTH_USERS_TABLE"].oauth_table.return_value.put_item.side_effect = RuntimeError("ddb down")

    # Act
    response = callback({"code": "c", "state": "s"})

    # Assert
    assert response["statusCode"] == 500
    assert "Connection failed" in response["body"]
    assert_no_secrets(response["body"], caplog.text)


@pytest.mark.parametrize("data,token,body,expected", [
    (None, None, b'{"a": 1}', {"a": 1}),
    ({"x": 1}, ACCESS, b"", {}),
])
def test_tc44_atlassian_json_request_builds_request(data, token, body, expected):
    # Arrange
    with patch.object(auth_module.urllib.request, "urlopen", return_value=http_response(body)) as urlopen:
        # Act
        result = AuthenticationJira({}).atlassian_json_request(
            "https://api.atlassian.com/x", method="POST" if data else "GET", data=data, access_token=token)

    # Assert
    assert result == expected
    request = urlopen.call_args.args[0]
    assert urlopen.call_args.kwargs == {"timeout": 20}
    assert request.get_header("Accept") == "application/json"
    if data:
        assert json.loads(request.data) == data and request.get_header("Content-type") == "application/json"
    else:
        assert request.data is None and request.get_header("Content-type") is None
    assert request.get_header("Authorization") == (f"Bearer {token}" if token else None)


def test_tc45_atlassian_http_error_hides_body(caplog):
    # Arrange
    error = urllib.error.HTTPError("https://api.atlassian.com/x", 401, "Unauthorized", {},
                                   io.BytesIO(f'{{"token": "{ACCESS}"}}'.encode()))
    with patch.object(auth_module.urllib.request, "urlopen", side_effect=error):
        # Act & Assert
        with pytest.raises(RuntimeError, match="HTTP 401") as exc_info:
            AuthenticationJira({}).atlassian_json_request("https://api.atlassian.com/x", access_token=ACCESS)
    assert_no_secrets(str(exc_info.value), caplog.text)


def test_tc46_exchange_code_posts_authorization_grant():
    # Arrange
    with patch.object(AuthenticationJira, "atlassian_json_request", return_value={"ok": 1}) as request:
        # Act
        AuthenticationJira({}).exchange_oauth_code("auth-code")

    # Assert
    request.assert_called_once_with(ATLASSIAN_TOKEN_URL, method="POST", data={
        "grant_type": "authorization_code", "client_id": "client-id", "client_secret": "client-secret-xyz",
        "code": "auth-code", "redirect_uri": "https://api.example.com/oauth/callback"})


@pytest.mark.parametrize("fails", [False, True])
def test_tc47_connect_response(helpers, fails):
    # Arrange
    states = helpers["OAUTH_STATES_TABLE"]
    if fails:
        states.create_oauth_authorization_url.side_effect = RuntimeError("ddb down")
    else:
        states.create_oauth_authorization_url.return_value = "https://auth.atlassian.com/authorize?state=s"

    # Act
    response = AuthenticationJira({}).build_connect_response("U1")

    # Assert
    body = json.loads(response["body"])
    assert response["statusCode"] == 200 and body["response_type"] == "ephemeral"
    if fails:
        assert "Could not start Jira connection" in body["text"]
    else:
        assert "<https://auth.atlassian.com/authorize?state=s|*Connect Jira*>" in body["text"]
        assert "expires in 10 minutes" in body["text"] and "only be used once" in body["text"]
    states.create_oauth_authorization_url.assert_called_once_with("U1")


# --------------------------------------------------------------------------------------
# JiraClient
# --------------------------------------------------------------------------------------

CREDENTIALS = {"access_token": ACCESS, "cloud_id": "cloud/1", "jira_site_url": "https://acme.atlassian.net/"}


@pytest.fixture
def credentials_store():
    with patch.object(client_module, "DynamoHelper") as helper_cls:
        helper_cls.return_value.get_oauth_credentials.return_value = dict(CREDENTIALS)
        yield helper_cls


@pytest.fixture
def jira_urlopen():
    with patch.object(client_module.urllib.request, "urlopen") as fake:
        fake.return_value = http_response(b'{"ok": true}')
        yield fake


def test_tc48_client_loads_credentials_for_user(credentials_store):
    # Act
    client = JiraClient("U1")

    # Assert
    credentials_store.assert_called_once_with("OAUTH_USERS_TABLE")
    credentials_store.return_value.get_oauth_credentials.assert_called_once_with("U1")
    assert client.slack_user_id == "U1"


@pytest.mark.parametrize("site,env_url,expected", [
    ("https://acme.atlassian.net/", "https://global.atlassian.net", "https://acme.atlassian.net"),
    (None, "https://global.atlassian.net/", "https://global.atlassian.net"),
    (None, None, ""),
])
def test_tc49_site_url_prefers_the_users_site(credentials_store, monkeypatch, site, env_url, expected):
    # Arrange
    credentials_store.return_value.get_oauth_credentials.return_value = {**CREDENTIALS, "jira_site_url": site}
    if env_url:
        monkeypatch.setenv("JIRA_BASE_URL", env_url)

    # Act & Assert
    assert JiraClient("U1").site_url == expected


@pytest.mark.parametrize("path,params,expected_url", [
    ("rest/api/3/search", {"jql": "a b", "fields": ["x", "y"]},
     "https://api.atlassian.com/ex/jira/cloud%2F1/rest/api/3/search?jql=a+b&fields=x&fields=y"),
    ("/rest/api/3/myself", None, "https://api.atlassian.com/ex/jira/cloud%2F1/rest/api/3/myself"),
    ("/rest/x?expand=1", {"y": 2}, "https://api.atlassian.com/ex/jira/cloud%2F1/rest/x?expand=1&y=2"),
])
def test_tc50_get_builds_url(credentials_store, jira_urlopen, path, params, expected_url):
    # Act
    result = JiraClient("U1").request(path, params=params)

    # Assert
    assert result == {"ok": True}
    request = jira_urlopen.call_args.args[0]
    assert request.full_url == expected_url
    assert request.get_method() == "GET" and request.data is None


def test_tc51_post_sends_json_with_bearer_token(credentials_store, jira_urlopen):
    # Act
    JiraClient("U1").request("/rest/api/3/issue/PROJ-1/worklog", method="post", data={"timeSpent": "1h"})

    # Assert
    request = jira_urlopen.call_args.args[0]
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"timeSpent": "1h"}
    assert request.get_header("Authorization") == f"Bearer {ACCESS}"
    assert request.get_header("Content-type") == "application/json"
    assert jira_urlopen.call_args.kwargs == {"timeout": 20}


def test_tc52_empty_response_body_is_empty_dict(credentials_store, jira_urlopen):
    # Arrange
    jira_urlopen.return_value = http_response(b"")

    # Act & Assert
    assert JiraClient("U1").request("/rest/x", method="DELETE") == {}


def test_tc53_http_error_includes_short_body_and_safe_log(credentials_store, jira_urlopen, caplog):
    # Arrange
    jira_urlopen.side_effect = urllib.error.HTTPError("https://api.atlassian.com/x", 404, "Not Found", {},
                                                      io.BytesIO(b"e" * 400))

    # Act & Assert
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as exc_info:
        JiraClient("U1").request("/rest/api/3/issue/NOPE-1")
    assert str(exc_info.value) == "Jira API returned HTTP 404: " + "e" * 300
    assert "HTTP 404 for Slack user U1" in caplog.text
    assert_no_secrets(caplog.text, str(exc_info.value))


def test_tc54_network_error(credentials_store, jira_urlopen):
    # Arrange
    jira_urlopen.side_effect = urllib.error.URLError("connection refused")

    # Act & Assert
    with pytest.raises(RuntimeError, match="Unable to connect to Jira API"):
        JiraClient("U1").request("/rest/x")

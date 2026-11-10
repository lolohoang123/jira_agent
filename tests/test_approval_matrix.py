"""Unit tests for approval/ generated from docs/testing/approval_test_matrix.md (TC-01..TC-31).

AAA layout, frozen clock, all AWS/Slack/Jira dependencies mocked.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from approval import approval_repository as repo_module
from approval.approval_repository import ApprovalRepository
from approval.approval_service import ApprovalService

NOW = 1_700_000_000
APPROVAL_ID = "11111111-2222-3333-4444-555555555555"
OWNER = "U_OWNER"
FORM = {
    "issue_key": "PROJ-1",
    "date": "2026-11-09",
    "start_time": "09:00",
    "duration": "1h 30m",
    "description": "Code review",
}


def client_error(code, operation="UpdateItem"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch.object(repo_module.time, "time", return_value=NOW):
        yield


RETENTION = 90 * 86400


@pytest.fixture
def table():
    table = MagicMock(name="approvals_table")
    table.name = "approvals-test"
    table.update_item.return_value = {"Attributes": {}}
    return table


@pytest.fixture
def repo(table):
    return ApprovalRepository(table=table)


@pytest.fixture
def fake_repo():
    return MagicMock(spec=ApprovalRepository)


@pytest.fixture
def slack_api():
    with patch("approval.approval_service.slack_api") as mock:
        yield mock


@pytest.fixture
def jira_service():
    with patch("approval.approval_service.JiraService") as mock:
        yield mock


def claimed_item(action="create_worklog"):
    return {"approval_id": APPROVAL_ID, "slack_user_id": OWNER, "channel_id": "C1",
            "action": action, "form": dict(FORM), "status": "EXECUTING"}


# --------------------------------------------------------------------------------------
# ApprovalRepository
# --------------------------------------------------------------------------------------

def test_tc01_init_with_injected_table_skips_boto3(table):
    # Arrange
    with patch.object(repo_module, "boto3") as boto3:
        # Act
        repo = ApprovalRepository(table=table)

    # Assert
    assert repo.table is table
    boto3.resource.assert_not_called()


def test_tc02_init_without_table_name_raises(monkeypatch):
    # Arrange
    monkeypatch.delenv("APPROVALS_TABLE", raising=False)

    # Act & Assert
    with pytest.raises(RuntimeError, match="APPROVALS_TABLE"):
        ApprovalRepository()


def test_tc03_init_from_env_uses_default_boto3_resource(monkeypatch):
    # Arrange
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-test")
    monkeypatch.delenv("DYNAMODB_ENDPOINT_URL", raising=False)

    with patch.object(repo_module, "boto3") as boto3:
        # Act
        repo = ApprovalRepository()

    # Assert
    boto3.resource.assert_called_once_with("dynamodb")
    boto3.resource.return_value.Table.assert_called_once_with("approvals-test")
    assert repo.table is boto3.resource.return_value.Table.return_value


def test_tc04_init_with_local_endpoint_passes_local_kwargs(monkeypatch):
    # Arrange
    monkeypatch.setenv("APPROVALS_TABLE", "approvals-test")
    monkeypatch.setenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000")
    for name in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)

    with patch.object(repo_module, "boto3") as boto3:
        # Act
        ApprovalRepository()

    # Assert
    boto3.resource.assert_called_once_with(
        "dynamodb", endpoint_url="http://localhost:8000", region_name="ap-southeast-1",
        aws_access_key_id="local", aws_secret_access_key="local")


def test_tc05_create_pending_default_ttl_writes_pending_item(repo, table):
    # Arrange
    approval = {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog", "form": FORM}

    with patch.object(repo_module.uuid, "uuid4", return_value=APPROVAL_ID):
        # Act
        item = repo.create_pending(approval)

    # Assert
    dedupe_key = repo_module.build_dedupe_key(OWNER, FORM)
    assert item == {
        "approval_id": APPROVAL_ID, "slack_user_id": OWNER, "channel_id": "C1",
        "action": "create_worklog", "form": FORM, "status": "PENDING", "dedupe_key": dedupe_key,
        "created_at": NOW, "expires_at": NOW + 86400, "ttl": NOW + 86400,
    }
    puts = [op["Put"] for op in table.meta.client.transact_write_items.call_args.kwargs["TransactItems"]]
    assert [p["TableName"] for p in puts] == ["approvals-test", "approvals-test"]
    # Plain Python values: the resource client serializes them (wire format: test_approval_wire_format.py).
    assert puts[0]["Item"] == item
    assert puts[0]["ConditionExpression"] == "attribute_not_exists(approval_id)"
    assert puts[1]["Item"] == {
        "approval_id": f"dedupe#{dedupe_key}", "target_approval_id": APPROVAL_ID,
        "expires_at": NOW + 86400, "ttl": NOW + 86400,
    }
    assert puts[1]["ConditionExpression"] == "attribute_not_exists(approval_id) OR expires_at <= :now"
    assert puts[1]["ExpressionAttributeValues"] == {":now": NOW}
    table.put_item.assert_not_called()


def test_tc06_create_pending_zero_ttl_expires_immediately(repo):
    # Arrange
    approval = {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog", "form": FORM}

    # Act
    item = repo.create_pending(approval, ttl_seconds=0)

    # Assert
    assert item["created_at"] == item["expires_at"] == item["ttl"] == NOW


def test_tc07_create_pending_missing_form_raises_without_write(repo, table):
    # Arrange
    approval = {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog"}

    # Act & Assert
    with pytest.raises(KeyError, match="form"):
        repo.create_pending(approval)
    table.meta.client.transact_write_items.assert_not_called()


@pytest.mark.parametrize(
    "tc_id,response,expected",
    [
        ("TC-08", {"Item": {"approval_id": APPROVAL_ID, "status": "PENDING"}},
         {"approval_id": APPROVAL_ID, "status": "PENDING"}),
        ("TC-09", {}, None),
    ],
)
def test_get_returns_item_or_none(repo, table, tc_id, response, expected):
    # Arrange
    table.get_item.return_value = response

    # Act
    result = repo.get(APPROVAL_ID)

    # Assert
    assert result == expected
    table.get_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID}, ConsistentRead=True)


@pytest.mark.parametrize(
    "tc_id,method,new_status,timestamp_field",
    [
        ("TC-10", "claim", "EXECUTING", "approved_at"),
        ("TC-13", "reject", "REJECTED", "rejected_at"),
    ],
)
def test_owner_transition_from_pending_returns_attributes(
        repo, table, tc_id, method, new_status, timestamp_field):
    # Arrange
    attributes = {"approval_id": APPROVAL_ID, "status": new_status}
    table.update_item.return_value = {"Attributes": attributes}

    # Act
    result = getattr(repo, method)(APPROVAL_ID, OWNER)

    # Assert
    assert result == attributes
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"approval_id": APPROVAL_ID}
    assert kwargs["UpdateExpression"] == f"SET #s = :new_status, {timestamp_field} = :now, #ttl = :ttl"
    assert kwargs["ConditionExpression"] == (
        "#s = :pending AND slack_user_id = :owner AND expires_at > :now")
    assert kwargs["ExpressionAttributeValues"] == {
        ":new_status": new_status, ":pending": "PENDING", ":owner": OWNER, ":now": NOW,
        ":ttl": NOW + RETENTION}
    assert kwargs["ReturnValues"] == "ALL_NEW"


@pytest.mark.parametrize("tc_id,method", [("TC-11", "claim"), ("TC-14", "reject")])
def test_conditional_check_failure_returns_none(repo, table, tc_id, method):
    # Arrange
    table.update_item.side_effect = client_error("ConditionalCheckFailedException")

    # Act
    result = getattr(repo, method)(APPROVAL_ID, "U_INTRUDER")

    # Assert
    assert result is None
    table.update_item.assert_called_once()


@pytest.mark.parametrize("tc_id,method", [("TC-12", "claim"), ("TC-15", "reject")])
def test_other_client_errors_are_reraised(repo, table, tc_id, method):
    # Arrange
    error = client_error("ProvisionedThroughputExceededException")
    table.update_item.side_effect = error

    # Act & Assert
    with pytest.raises(ClientError) as exc_info:
        getattr(repo, method)(APPROVAL_ID, OWNER)
    assert exc_info.value is error


def test_tc16_finish_without_error_only_sets_status(repo, table):
    # Act
    repo.finish(APPROVAL_ID, "APPROVED_EXECUTED")

    # Assert
    table.update_item.assert_called_once_with(
        Key={"approval_id": APPROVAL_ID},
        UpdateExpression="SET #s = :status, completed_at = :now, #ttl = :ttl",
        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
        ExpressionAttributeValues={":status": "APPROVED_EXECUTED", ":executing": "EXECUTING", ":now": NOW,
                                   ":ttl": NOW + RETENTION},
        ConditionExpression="#s = :executing",
        ReturnValues="ALL_NEW",
    )
    table.delete_item.assert_not_called()  # an executed worklog keeps its dedupe row


@pytest.mark.parametrize("length,expected_length", [(499, 499), (500, 500), (501, 500)])
def test_tc17_finish_truncates_error_to_500_chars(repo, table, length, expected_length):
    # Arrange
    error = RuntimeError("x" * length)

    # Act
    repo.finish(APPROVAL_ID, "EXECUTION_FAILED", error=error)

    # Assert
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == (
        "SET #s = :status, completed_at = :now, #ttl = :ttl, error_message = :error")
    assert kwargs["ExpressionAttributeValues"][":error"] == "x" * expected_length


def test_tc18_finish_with_empty_error_skips_error_message(repo, table):
    # Act
    repo.finish(APPROVAL_ID, "EXECUTION_FAILED", error="")

    # Assert
    kwargs = table.update_item.call_args.kwargs
    assert "error_message" not in kwargs["UpdateExpression"]
    assert ":error" not in kwargs["ExpressionAttributeValues"]


def test_tc19_finish_on_non_executing_row_propagates_client_error(repo, table):
    # Arrange
    table.update_item.side_effect = client_error("ConditionalCheckFailedException")

    # Act & Assert
    with pytest.raises(ClientError):
        repo.finish(APPROVAL_ID, "APPROVED_EXECUTED")


# --------------------------------------------------------------------------------------
# ApprovalService
# --------------------------------------------------------------------------------------

def test_tc20_request_creates_pending_and_posts_buttons(fake_repo, slack_api):
    # Arrange
    fake_repo.create_pending.return_value = {"approval_id": APPROVAL_ID, "status": "PENDING"}
    service = ApprovalService(fake_repo)

    # Act
    item = service.request_worklog_approval(OWNER, "C1", FORM)

    # Assert
    assert item["approval_id"] == APPROVAL_ID
    fake_repo.create_pending.assert_called_once_with(
        {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog", "form": FORM,
         "jira_cloud_id": "cloud-test"})
    method, payload = slack_api.call_args.args
    assert method == "chat.postMessage"
    assert payload["channel"] == "C1"
    assert payload["text"] == "Approval required: log 1h 30m to PROJ-1"
    actions = payload["blocks"][1]
    assert actions["block_id"] == f"approval_{APPROVAL_ID}"
    assert [(b["action_id"], b["value"]) for b in actions["elements"]] == [
        ("approve_jira_write", APPROVAL_ID), ("reject_jira_write", APPROVAL_ID)]


def test_tc21_request_escapes_slack_markup(fake_repo, slack_api):
    # Arrange
    fake_repo.create_pending.return_value = {"approval_id": APPROVAL_ID}
    form = {**FORM, "issue_key": "<!channel>", "duration": "1h&2h", "description": "a <b> & c"}
    service = ApprovalService(fake_repo)

    # Act
    service.request_worklog_approval(OWNER, "C1", form)

    # Assert
    payload = slack_api.call_args.args[1]
    body = payload["blocks"][0]["text"]["text"]
    assert "Issue: *&lt;!channel&gt;*" in body
    assert "Duration: *1h&amp;2h*" in body
    assert "Description: a &lt;b&gt; &amp; c" in body
    assert payload["text"] == "Approval required: log 1h&amp;2h to &lt;!channel&gt;"


@pytest.mark.parametrize("description", [None, ""])
def test_tc22_request_without_description_shows_placeholder(fake_repo, slack_api, description):
    # Arrange
    fake_repo.create_pending.return_value = {"approval_id": APPROVAL_ID}
    service = ApprovalService(fake_repo)

    # Act
    service.request_worklog_approval(OWNER, "C1", {**FORM, "description": description})

    # Assert
    body = slack_api.call_args.args[1]["blocks"][0]["text"]["text"]
    assert body.endswith("Description: —")


def test_tc23_request_does_not_post_when_create_pending_fails(fake_repo, slack_api):
    # Arrange
    fake_repo.create_pending.side_effect = client_error("ConditionalCheckFailedException", "PutItem")
    service = ApprovalService(fake_repo)

    # Act & Assert
    with pytest.raises(ClientError):
        service.request_worklog_approval(OWNER, "C1", FORM)
    slack_api.assert_not_called()


def test_tc24_approve_executes_worklog_and_marks_executed(fake_repo, jira_service):
    # Arrange
    item = claimed_item()
    fake_repo.claim.return_value = item
    jira_service.return_value.create_worklog.return_value = {"id": "10001"}
    service = ApprovalService(fake_repo)

    # Act
    result = service.approve_and_execute(APPROVAL_ID, OWNER)

    # Assert
    assert result == {"ok": True, "result": {"id": "10001"}, "item": item}
    fake_repo.claim.assert_called_once_with(APPROVAL_ID, OWNER)
    jira_service.assert_called_once_with(OWNER)
    jira_service.return_value.create_worklog.assert_called_once_with(item["form"])
    fake_repo.finish.assert_called_once_with(APPROVAL_ID, "APPROVED_EXECUTED")


def test_tc25_approve_unclaimable_returns_not_ok_without_side_effects(fake_repo, jira_service):
    # Arrange
    fake_repo.claim.return_value = None
    service = ApprovalService(fake_repo)

    # Act
    result = service.approve_and_execute(APPROVAL_ID, "U_INTRUDER")

    # Assert
    assert result == {"ok": False, "message":
                      "This approval is expired, already handled, or does not belong to you."}
    jira_service.assert_not_called()
    fake_repo.finish.assert_not_called()


def test_tc26_approve_unsupported_action_fails_without_jira(fake_repo, jira_service):
    # Arrange
    fake_repo.claim.return_value = claimed_item(action="delete_worklog")
    service = ApprovalService(fake_repo)

    # Act & Assert
    with pytest.raises(ValueError, match="Unsupported approved action") as exc_info:
        service.approve_and_execute(APPROVAL_ID, OWNER)
    jira_service.assert_not_called()
    fake_repo.finish.assert_called_once_with(APPROVAL_ID, "EXECUTION_FAILED", error=exc_info.value,
                                             release=True)


def test_tc27_approve_jira_failure_marks_failed_and_reraises(fake_repo, jira_service):
    # Arrange
    fake_repo.claim.return_value = claimed_item()
    jira_error = RuntimeError("Jira 500")
    jira_service.return_value.create_worklog.side_effect = jira_error
    service = ApprovalService(fake_repo)

    # Act & Assert
    with pytest.raises(RuntimeError) as exc_info:
        service.approve_and_execute(APPROVAL_ID, OWNER)
    assert exc_info.value is jira_error
    fake_repo.finish.assert_called_once_with(APPROVAL_ID, "EXECUTION_FAILED", error=jira_error, release=True)


def test_tc28_approve_keeps_jira_error_when_finish_also_fails(fake_repo, jira_service, caplog):
    # Arrange
    fake_repo.claim.return_value = claimed_item()
    jira_error = RuntimeError("Jira 500")
    jira_service.return_value.create_worklog.side_effect = jira_error
    fake_repo.finish.side_effect = client_error("ConditionalCheckFailedException")
    service = ApprovalService(fake_repo)

    # Act & Assert
    with caplog.at_level(logging.ERROR, logger="approval.approval_service"):
        with pytest.raises(RuntimeError) as exc_info:
            service.approve_and_execute(APPROVAL_ID, OWNER)
    assert exc_info.value is jira_error
    assert f"Could not mark approval {APPROVAL_ID} as EXECUTION_FAILED" in caplog.text


def test_tc29_finish_executed_failure_after_jira_success_marks_failed(fake_repo, jira_service):
    # Pins current behaviour: the Jira write already happened, yet the row ends EXECUTION_FAILED.
    # The dedupe row is kept (release=False) so the same worklog cannot be resubmitted.
    # Arrange
    fake_repo.claim.return_value = claimed_item()
    jira_service.return_value.create_worklog.return_value = {"id": "10001"}
    ddb_error = RuntimeError("DynamoDB unavailable")
    fake_repo.finish.side_effect = [ddb_error, None]
    service = ApprovalService(fake_repo)

    # Act & Assert
    with pytest.raises(RuntimeError) as exc_info:
        service.approve_and_execute(APPROVAL_ID, OWNER)
    assert exc_info.value is ddb_error
    jira_service.return_value.create_worklog.assert_called_once()
    assert [c.args + tuple(c.kwargs.values()) for c in fake_repo.finish.call_args_list] == [
        (APPROVAL_ID, "APPROVED_EXECUTED"), (APPROVAL_ID, "EXECUTION_FAILED", ddb_error, False)]


@pytest.mark.parametrize(
    "tc_id,user,repo_result",
    [
        ("TC-30", OWNER, {"approval_id": APPROVAL_ID, "status": "REJECTED"}),
        ("TC-31", "U_INTRUDER", None),
    ],
)
def test_reject_delegates_to_repository(fake_repo, tc_id, user, repo_result):
    # Arrange
    fake_repo.reject.return_value = repo_result
    service = ApprovalService(fake_repo)

    # Act
    result = service.reject(APPROVAL_ID, user)

    # Assert
    assert result == repo_result
    fake_repo.reject.assert_called_once_with(APPROVAL_ID, user)

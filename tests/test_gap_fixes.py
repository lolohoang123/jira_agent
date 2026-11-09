"""Tests for spec gaps GAP-01 (duplicate approvals), GAP-02 (undelivered approvals),
GAP-05 (approval retention) and GAP-08 (duration limits)."""
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from approval import approval_repository as repo_module
from approval.approval_repository import ApprovalRepository, DuplicateApprovalError, build_dedupe_key
from approval.approval_service import ApprovalDeliveryError, ApprovalService
from handlers.async_task_handler import handle_async_task
from util.common import duration_to_minutes, validate_duration

NOW = 1_700_000_000
APPROVAL_ID = "11111111-2222-3333-4444-555555555555"
OWNER = "U_OWNER"
FORM = {"issue_key": "PROJ-1", "date": "2026-11-09", "start_time": "09:00",
        "duration": "1h 30m", "description": "secret customer notes"}


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch.object(repo_module.time, "time", return_value=NOW):
        yield


@pytest.fixture
def table():
    table = MagicMock(name="approvals_table")
    table.name = "approvals-test"
    table.update_item.return_value = {"Attributes": {}}
    return table


@pytest.fixture
def repo(table):
    return ApprovalRepository(table=table)


def transaction_cancelled(reasons):
    return ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
                        "CancellationReasons": reasons}, "TransactWriteItems")


def new_approval():
    return {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog", "form": dict(FORM)}


# --------------------------------------------------------------------------------------
# GAP-01: duplicate approvals
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", [
    {"duration": "1H30M"}, {"duration": " 1h  30m "}, {"issue_key": "proj-1"}, {"description": "other"},
])
def test_gap01_dedupe_key_ignores_formatting_and_description(variant):
    # Act & Assert
    assert build_dedupe_key(OWNER, {**FORM, **variant}) == build_dedupe_key(OWNER, FORM)


@pytest.mark.parametrize("variant", [
    {"date": "2026-11-10"}, {"start_time": "10:00"}, {"duration": "2h"}, {"issue_key": "PROJ-2"},
])
def test_gap01_dedupe_key_differs_for_different_worklogs(variant):
    # Act & Assert
    assert build_dedupe_key(OWNER, {**FORM, **variant}) != build_dedupe_key(OWNER, FORM)


def test_gap01_dedupe_key_differs_per_user():
    # Act & Assert
    assert build_dedupe_key("U_OTHER", FORM) != build_dedupe_key(OWNER, FORM)


def test_gap01_duplicate_request_raises_duplicate_error(repo, table):
    # Arrange
    table.meta.client.transact_write_items.side_effect = transaction_cancelled(
        [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}])

    # Act & Assert
    with pytest.raises(DuplicateApprovalError) as exc_info:
        repo.create_pending(new_approval())
    assert exc_info.value.dedupe_key == build_dedupe_key(OWNER, FORM)


def test_gap01_other_transaction_failure_is_reraised(repo, table):
    # Arrange: the approval id itself collided, not the dedupe row
    error = transaction_cancelled([{"Code": "ConditionalCheckFailed"}, {"Code": "None"}])
    table.meta.client.transact_write_items.side_effect = error

    # Act & Assert
    with pytest.raises(ClientError) as exc_info:
        repo.create_pending(new_approval())
    assert exc_info.value is error


def test_gap01_reject_releases_dedupe_row(repo, table):
    # Arrange
    table.update_item.return_value = {"Attributes": {"approval_id": APPROVAL_ID, "dedupe_key": "abc",
                                                     "status": "REJECTED"}}

    # Act
    result = repo.reject(APPROVAL_ID, OWNER)

    # Assert
    assert result["status"] == "REJECTED"
    table.delete_item.assert_called_once_with(
        Key={"approval_id": "dedupe#abc"}, ConditionExpression="target_approval_id = :id",
        ExpressionAttributeValues={":id": APPROVAL_ID})


def test_gap01_rejected_by_non_owner_keeps_dedupe_row(repo, table):
    # Arrange
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    # Act
    result = repo.reject(APPROVAL_ID, "U_INTRUDER")

    # Assert
    assert result is None
    table.delete_item.assert_not_called()


@pytest.mark.parametrize("status,released", [("EXECUTION_FAILED", True), ("APPROVED_EXECUTED", False)])
def test_gap01_finish_releases_dedupe_only_on_failure(repo, table, status, released):
    # Arrange
    table.update_item.return_value = {"Attributes": {"approval_id": APPROVAL_ID, "dedupe_key": "abc"}}

    # Act
    repo.finish(APPROVAL_ID, status)

    # Assert
    assert table.delete_item.called is released


def test_gap01_release_failure_is_swallowed(repo, table):
    # Arrange
    table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "DeleteItem")

    # Act & Assert (no exception)
    repo.release_dedupe({"approval_id": APPROVAL_ID, "dedupe_key": "abc"})


def test_gap01_release_without_dedupe_key_is_noop(repo, table):
    # Act
    repo.release_dedupe({"approval_id": APPROVAL_ID})

    # Assert
    table.delete_item.assert_not_called()


# --------------------------------------------------------------------------------------
# GAP-02: undelivered approval requests
# --------------------------------------------------------------------------------------

def test_gap02_post_failure_cancels_and_raises_delivery_error():
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)
    repo.create_pending.return_value = {"approval_id": APPROVAL_ID}
    service = ApprovalService(repo)
    with patch("approval.approval_service.slack_api", side_effect=RuntimeError("channel_not_found")):
        # Act & Assert
        with pytest.raises(ApprovalDeliveryError):
            service.request_worklog_approval(OWNER, "C1", FORM)
    repo.cancel.assert_called_once_with(APPROVAL_ID, reason="Slack delivery failed: channel_not_found")


def test_gap02_cancel_failure_still_raises_delivery_error():
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)
    repo.create_pending.return_value = {"approval_id": APPROVAL_ID}
    repo.cancel.side_effect = RuntimeError("DynamoDB down")
    service = ApprovalService(repo)
    with patch("approval.approval_service.slack_api", side_effect=RuntimeError("channel_not_found")):
        # Act & Assert
        with pytest.raises(ApprovalDeliveryError):
            service.request_worklog_approval(OWNER, "C1", FORM)


def test_gap02_cancel_moves_pending_to_cancelled_and_releases(repo, table):
    # Arrange
    table.update_item.return_value = {"Attributes": {"approval_id": APPROVAL_ID, "dedupe_key": "abc",
                                                     "status": "CANCELLED"}}

    # Act
    result = repo.cancel(APPROVAL_ID, reason="Slack delivery failed: channel_not_found")

    # Assert
    assert result["status"] == "CANCELLED"
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "#s = :pending"
    assert kwargs["ExpressionAttributeValues"][":cancelled"] == "CANCELLED"
    assert kwargs["ExpressionAttributeValues"][":ttl"] == NOW + 90 * 86400
    table.delete_item.assert_called_once()


def test_gap02_cancel_of_non_pending_returns_none(repo, table):
    # Arrange
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    # Act & Assert
    assert repo.cancel(APPROVAL_ID) is None
    table.delete_item.assert_not_called()


@pytest.mark.parametrize(
    "error,status,expected_text",
    [
        (ApprovalDeliveryError("x"), 502, "couldn't post the approval"),
        (DuplicateApprovalError("abc"), 409, "identical worklog request"),
        (RuntimeError("boom"), 500, "could not be created"),
    ],
)
def test_gap02_modal_failures_notify_user_by_dm(error, status, expected_text):
    # Arrange
    event = {"_async_task": "request_worklog_approval", "slack_user_id": OWNER,
             "channel_id": "C1", "form": FORM}
    with patch("approval.approval_service.ApprovalService") as service_cls, \
            patch("slack.slack_client.slack_api") as slack_api:
        service_cls.return_value.request_worklog_approval.side_effect = error

        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == status
    method, payload = slack_api.call_args.args
    assert method == "chat.postMessage"
    assert payload["channel"] == OWNER
    assert expected_text in payload["text"]
    assert "secret customer notes" not in payload["text"]


def test_gap02_dm_failure_does_not_raise():
    # Arrange
    event = {"_async_task": "request_worklog_approval", "slack_user_id": OWNER,
             "channel_id": "C1", "form": FORM}
    with patch("approval.approval_service.ApprovalService") as service_cls, \
            patch("slack.slack_client.slack_api", side_effect=RuntimeError("Slack down")):
        service_cls.return_value.request_worklog_approval.side_effect = ApprovalDeliveryError("x")

        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 502


@pytest.mark.parametrize(
    "error,expected_text",
    [(DuplicateApprovalError("abc"), "identical worklog request"),
     (ApprovalDeliveryError("x"), "couldn't post the approval")],
)
def test_gap02_agent_path_returns_message_instead_of_failing(error, expected_text):
    # Arrange
    from agent.tool_registry import execute_plan
    plan = {"intent": "prepare_worklog", "parameters": {k: v for k, v in FORM.items()}}
    with patch("agent.tool_registry.DynamoHelper"), \
            patch("agent.tool_registry.ApprovalService") as service_cls:
        service_cls.return_value.request_worklog_approval.side_effect = error

        # Act
        outcome = execute_plan(plan, OWNER, "C1")

    # Assert
    assert outcome["intent"] == "unknown"
    assert expected_text in outcome["message"]
    assert "PROJ-1" in outcome["message"]


# --------------------------------------------------------------------------------------
# GAP-05: retention of finished approvals
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("env_value,expected_days", [(None, 90), ("365", 365), ("0", 1), ("abc", 90)])
def test_gap05_finish_extends_ttl_to_retention(repo, table, monkeypatch, env_value, expected_days):
    # Arrange
    if env_value is None:
        monkeypatch.delenv("APPROVAL_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("APPROVAL_RETENTION_DAYS", env_value)

    # Act
    repo.finish(APPROVAL_ID, "APPROVED_EXECUTED")

    # Assert
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":ttl"] == (
        NOW + expected_days * 86400)


# --------------------------------------------------------------------------------------
# GAP-08: duration limits
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("duration,minutes", [
    ("30m", 30), ("1h 30m", 90), ("1d", 480), ("3d", 1440), ("1w", 2400), ("1H30M", 90),
])
def test_gap08_duration_to_minutes(duration, minutes):
    # Act & Assert
    assert duration_to_minutes(duration) == minutes


@pytest.mark.parametrize("duration", ["1m", "1h", "24h", "3d", "23h 59m"])
def test_gap08_valid_durations_within_limit(duration, monkeypatch):
    # Arrange
    monkeypatch.delenv("MAX_WORKLOG_MINUTES", raising=False)

    # Act & Assert
    assert validate_duration(duration) == duration


@pytest.mark.parametrize("duration,message", [
    ("0m", "greater than 0"), ("0h 0m", "greater than 0"), ("0w", "greater than 0"),
    ("24h 1m", "at most 24h"), ("1w", "at most 24h"), ("999h", "at most 24h"),
    ("", "must look like"), ("tomorrow", "must look like"),
])
def test_gap08_invalid_durations_are_rejected(duration, message, monkeypatch):
    # Arrange
    monkeypatch.delenv("MAX_WORKLOG_MINUTES", raising=False)

    # Act & Assert
    with pytest.raises(ValueError, match=message):
        validate_duration(duration)


@pytest.mark.parametrize("env_value,duration,ok", [
    ("480", "8h", True), ("480", "8h 1m", False), ("90", "1h 30m", True),
    ("abc", "24h", True), ("-5", "24h", True),
])
def test_gap08_max_is_configurable(monkeypatch, env_value, duration, ok):
    # Arrange
    monkeypatch.setenv("MAX_WORKLOG_MINUTES", env_value)

    # Act & Assert
    if ok:
        assert validate_duration(duration) == duration
    else:
        with pytest.raises(ValueError, match="at most"):
            validate_duration(duration)


def test_gap08_modal_rejects_zero_duration():
    # Arrange
    from handlers.slack_action_handler import handle_view_submission

    def field(value):
        return {"value": value}

    view = {"private_metadata": '{"user_id": "U_OWNER", "channel_id": "C1"}', "state": {"values": {
        "issue_block": {"issue_key": field("PROJ-1")},
        "date_block": {"work_date": {"selected_date": "2026-11-09"}},
        "duration_block": {"duration": field("0m")},
        "start_time_block": {"start_time": {"selected_time": "09:00"}},
        "description_block": {"description": field("")},
        "estimate_block": {"remaining_estimate": {"selected_option": {"value": "leave"}}},
        "new_estimate_block": {"new_estimate": field("")},
    }}}
    lambda_client = MagicMock()

    # Act
    response = handle_view_submission({"view": view}, MagicMock(), lambda_client)

    # Assert
    assert '"duration_block": "Duration must be greater than 0."' in response["body"]
    lambda_client.invoke.assert_not_called()

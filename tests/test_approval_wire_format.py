"""Wire-format tests: run ApprovalRepository against a real boto3 DynamoDB resource and capture the
request just before it is sent (no network, no AWS credentials needed).

Mock-based tests cannot catch serialization bugs: a boto3 *resource* client serializes Python values
itself, so pre-serialized values get wrapped twice (regression: "operand type: M" on TransactWriteItems).
"""
import json
from unittest.mock import patch

import boto3
import pytest

from approval import approval_repository as repo_module
from approval.approval_repository import ApprovalRepository, DuplicateApprovalError

NOW = 1_700_000_000
FORM = {"issue_key": "PROJ-1", "date": "2026-11-09", "start_time": "09:00", "duration": "1h 30m",
        "description": "Code review", "remaining_estimate": "leave", "new_estimate": None}


class _Sent(Exception):
    def __init__(self, operation, params):
        super().__init__(operation)
        self.operation, self.params = operation, params


@pytest.fixture
def real_table():
    resource = boto3.resource("dynamodb", region_name="ap-southeast-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
    table = resource.Table("approvals-test")

    def capture(params, model, **_):
        # 'before-call' runs after boto3's high-level transformation and botocore's validation.
        raise _Sent(model.name, params["body"])

    table.meta.client.meta.events.register("before-call.dynamodb.*", capture)
    return table


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch.object(repo_module.time, "time", return_value=NOW):
        yield


def _send(fn):
    with pytest.raises(_Sent) as exc_info:
        fn()
    return exc_info.value


def test_create_pending_sends_typed_values_once(real_table):
    # Arrange
    repo = ApprovalRepository(table=real_table)
    approval = {"slack_user_id": "U1", "channel_id": "C1", "action": "create_worklog", "form": FORM}

    # Act
    sent = _send(lambda: repo.create_pending(approval))

    # Assert
    assert sent.operation == "TransactWriteItems"
    request = json.loads(sent.params)
    approval_put, dedupe_put = (op["Put"] for op in request["TransactItems"])
    assert approval_put["TableName"] == "approvals-test"
    assert approval_put["Item"]["status"] == {"S": "PENDING"}
    assert approval_put["Item"]["created_at"] == {"N": str(NOW)}
    assert approval_put["Item"]["form"]["M"]["issue_key"] == {"S": "PROJ-1"}
    assert approval_put["Item"]["form"]["M"]["new_estimate"] == {"NULL": True}
    assert dedupe_put["Item"]["approval_id"]["S"].startswith("dedupe#")
    assert dedupe_put["Item"]["expires_at"] == {"N": str(NOW + 86400)}
    # The regression: this used to be {"M": {"N": {"S": ...}}}
    assert dedupe_put["ExpressionAttributeValues"] == {":now": {"N": str(NOW)}}


@pytest.mark.parametrize("call", [
    lambda repo: repo.claim("a-1", "U1"),
    lambda repo: repo.reject("a-1", "U1"),
    lambda repo: repo.cancel("a-1", reason="x"),
    lambda repo: repo.finish("a-1", "APPROVED_EXECUTED"),
    lambda repo: repo.release_dedupe({"approval_id": "a-1", "dedupe_key": "abc"}),
])
def test_other_writes_send_typed_values_once(real_table, call):
    # Arrange
    repo = ApprovalRepository(table=real_table)

    # Act
    sent = _send(lambda: call(repo))

    # Assert: every expression value is a single-level typed value, never a nested map
    request = json.loads(sent.params)
    for value in request.get("ExpressionAttributeValues", {}).values():
        assert set(value) <= {"S", "N"}, value


def test_duplicate_is_detected_from_real_cancellation_response(real_table):
    # Arrange: answer the transaction with the response DynamoDB really returns for a dedupe conflict
    from botocore.stub import Stubber
    repo = ApprovalRepository(table=real_table)
    real_table.meta.client.meta.events.unregister("before-call.dynamodb.*")
    stubber = Stubber(real_table.meta.client)
    stubber.add_client_error(
        "transact_write_items", service_error_code="TransactionCanceledException",
        service_message="Transaction cancelled", http_status_code=400,
        response_meta={},
        modeled_fields={"CancellationReasons": [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}]},
    )
    approval = {"slack_user_id": "U1", "channel_id": "C1", "action": "create_worklog", "form": FORM}

    # Act & Assert
    with stubber, pytest.raises(DuplicateApprovalError):
        repo.create_pending(approval)

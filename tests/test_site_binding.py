"""SC-01: an approval is bound to the Jira site it was requested for.

If the user reconnects to another Jira site before approving, the worklog must not be written there.
"""
from unittest.mock import MagicMock, patch

import pytest

from approval import approval_repository as repo_module
from approval.approval_repository import ApprovalRepository
from approval.approval_service import SITE_CHANGED_MESSAGE, ApprovalService
from util.dynamo_helper import DynamoHelper

NOW = 1_700_000_000
APPROVAL_ID = "11111111-2222-3333-4444-555555555555"
OWNER = "U_OWNER"
FORM = {"issue_key": "PROJ-1", "date": "2026-11-09", "start_time": "09:00", "duration": "1h"}


@pytest.fixture(autouse=True)
def frozen_clock():
    with patch.object(repo_module.time, "time", return_value=NOW):
        yield


@pytest.fixture
def table():
    table = MagicMock(name="approvals_table")
    table.name = "approvals-test"
    table.update_item.return_value = {"Attributes": {"approval_id": APPROVAL_ID, "dedupe_key": "abc"}}
    return table


@pytest.fixture
def jira_service():
    with patch("approval.approval_service.JiraService") as mock:
        yield mock


def claimed(cloud_id="site-A"):
    item = {"approval_id": APPROVAL_ID, "slack_user_id": OWNER, "action": "create_worklog",
            "form": dict(FORM), "status": "EXECUTING"}
    if cloud_id:
        item["jira_cloud_id"] = cloud_id
    return item


# --- request side ---------------------------------------------------------------------

def test_request_stores_current_site_on_approval(table):
    # Arrange
    service = ApprovalService(ApprovalRepository(table=table), cloud_id_resolver=lambda user: "site-A")

    # Act
    with patch("approval.approval_service.slack_api"):
        item = service.request_worklog_approval(OWNER, "C1", FORM)

    # Assert
    assert item["jira_cloud_id"] == "site-A"
    put = table.meta.client.transact_write_items.call_args.kwargs["TransactItems"][0]["Put"]["Item"]
    assert put["jira_cloud_id"] == "site-A"
    assert "jira_cloud_id" not in put["form"]  # never sent to Jira


def test_request_without_connection_creates_nothing():
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)

    def not_connected(user):
        raise ValueError("Jira account is not connected. Run `/jira-agent connect` first.")

    service = ApprovalService(repo, cloud_id_resolver=not_connected)

    # Act & Assert
    with pytest.raises(ValueError, match="not connected"):
        service.request_worklog_approval(OWNER, "C1", FORM)
    repo.create_pending.assert_not_called()


def test_repository_omits_site_when_not_given(table):
    # Act
    item = ApprovalRepository(table=table).create_pending(
        {"slack_user_id": OWNER, "channel_id": "C1", "action": "create_worklog", "form": FORM})

    # Assert
    assert "jira_cloud_id" not in item


# --- approve side ---------------------------------------------------------------------

def test_approve_same_site_writes_worklog(jira_service):
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)
    repo.claim.return_value = claimed("site-A")
    jira_service.return_value.cloud_id = "site-A"
    jira_service.return_value.create_worklog.return_value = {"id": "10001"}

    # Act
    result = ApprovalService(repo).approve_and_execute(APPROVAL_ID, OWNER)

    # Assert
    assert result["ok"] is True
    jira_service.return_value.create_worklog.assert_called_once_with(FORM)
    repo.finish.assert_called_once_with(APPROVAL_ID, "APPROVED_EXECUTED")


def test_approve_after_reconnect_to_other_site_cancels_without_write(jira_service):
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)
    repo.claim.return_value = claimed("site-A")
    jira_service.return_value.cloud_id = "site-B"

    # Act
    result = ApprovalService(repo).approve_and_execute(APPROVAL_ID, OWNER)

    # Assert
    assert result == {"ok": False, "message": SITE_CHANGED_MESSAGE}
    jira_service.return_value.create_worklog.assert_not_called()
    repo.finish.assert_called_once_with(APPROVAL_ID, "CANCELLED", error="Jira site changed since the request")


def test_legacy_approval_without_site_is_still_executed(jira_service):
    # Arrange
    repo = MagicMock(spec=ApprovalRepository)
    repo.claim.return_value = claimed(cloud_id=None)
    jira_service.return_value.cloud_id = "site-B"

    # Act
    result = ApprovalService(repo).approve_and_execute(APPROVAL_ID, OWNER)

    # Assert
    assert result["ok"] is True
    jira_service.return_value.create_worklog.assert_called_once()


@pytest.mark.parametrize("status,released", [("CANCELLED", True), ("APPROVED_EXECUTED", False)])
def test_finish_releases_dedupe_on_cancel_from_executing(table, status, released):
    # Act
    ApprovalRepository(table=table).finish(APPROVAL_ID, status, error="x" if status == "CANCELLED" else None)

    # Assert
    assert table.delete_item.called is released


# --- DynamoHelper lookup --------------------------------------------------------------

def _helper(item):
    helper = DynamoHelper.__new__(DynamoHelper)
    helper.table_name_env = "OAUTH_USERS_TABLE"
    users = MagicMock()
    users.get_item.return_value = {"Item": item} if item is not None else {}
    helper.oauth_table = lambda: users
    return helper, users


def test_cloud_id_lookup_reads_only_cloud_id():
    # Arrange
    helper, users = _helper({"jiraCloudId": "site-A"})

    # Act
    cloud_id = helper.get_jira_cloud_id_for_slack_user(OWNER)

    # Assert
    assert cloud_id == "site-A"
    users.get_item.assert_called_once_with(Key={"slack_user_id": OWNER}, ProjectionExpression="jiraCloudId",
                                           ConsistentRead=True)


@pytest.mark.parametrize("item", [None, {}])
def test_cloud_id_lookup_requires_connection(item):
    helper, _ = _helper(item)
    with pytest.raises(ValueError, match="not connected"):
        helper.get_jira_cloud_id_for_slack_user(OWNER)

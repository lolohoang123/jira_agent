import hashlib
import logging
import os
import time
import uuid

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEDUPE_PREFIX = "dedupe#"
DEDUPE_WINDOW_SECONDS = 86400
DEFAULT_RETENTION_DAYS = 90


class DuplicateApprovalError(Exception):
    """An identical worklog approval is already pending or was approved within the dedupe window."""

    def __init__(self, dedupe_key):
        super().__init__("An identical worklog request is already pending or was recently approved.")
        self.dedupe_key = dedupe_key


def build_dedupe_key(team_id, slack_user_id, form):
    """Stable key for "the same worklog": workspace, user, issue, date, start time and normalised duration."""
    parts = [
        team_id or "",
        slack_user_id or "",
        str(form.get("issue_key") or "").strip().upper(),
        str(form.get("date") or ""),
        str(form.get("start_time") or ""),
        "".join(str(form.get("duration") or "").lower().split()),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def retention_seconds():
    try:
        days = int(os.environ.get("APPROVAL_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
    except ValueError:
        days = DEFAULT_RETENTION_DAYS
    return max(days, 1) * 86400


class ApprovalRepository:
    """DynamoDB persistence for one-time, owner-bound approvals.

    Besides the approval rows, the table holds one short-lived dedupe row per pending/approved worklog
    (``approval_id = "dedupe#<sha256>"``). It is created in the same transaction as the approval, so two
    identical requests cannot both become PENDING, and it is released when the approval is rejected,
    cancelled or fails. Dedupe rows never match the claim/reject conditions (they have no status).
    """

    def __init__(self, table=None):
        if table is None:
            table_name = os.environ.get("APPROVALS_TABLE")
            if not table_name:
                raise RuntimeError("Missing required environment variable: APPROVALS_TABLE")
            kwargs = {}
            endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
            if endpoint:
                kwargs.update(endpoint_url=endpoint,
                              region_name=os.environ.get("AWS_REGION", "ap-southeast-1"),
                              aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "local"),
                              aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "local"))
            table = boto3.resource("dynamodb", **kwargs).Table(table_name)
        self.table = table

    def create_pending(self, approval, ttl_seconds=86400):
        now = int(time.time())
        dedupe_key = build_dedupe_key(approval["team_id"], approval["slack_user_id"], approval["form"])
        item = {
            "approval_id": str(uuid.uuid4()),
            "team_id": approval["team_id"],
            "slack_user_id": approval["slack_user_id"],
            "channel_id": approval["channel_id"],
            "action": approval["action"],
            "form": approval["form"],
            "status": "PENDING",
            "dedupe_key": dedupe_key,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "ttl": now + ttl_seconds,
        }
        if approval.get("jira_cloud_id"):
            # Site the request was made for; the claim refuses to write anywhere else (SC-01).
            item["jira_cloud_id"] = approval["jira_cloud_id"]
        dedupe_item = {
            "approval_id": DEDUPE_PREFIX + dedupe_key,
            "target_approval_id": item["approval_id"],
            "expires_at": now + DEDUPE_WINDOW_SECONDS,
            "ttl": now + DEDUPE_WINDOW_SECONDS,
        }
        try:
            # Plain Python values: the client of a boto3 *resource* serializes them itself.
            # Pre-serializing ({"N": "1"}) would be wrapped again into a map ("operand type: M").
            self.table.meta.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table.name, "Item": item,
                         "ConditionExpression": "attribute_not_exists(approval_id)"}},
                {"Put": {"TableName": self.table.name, "Item": dedupe_item,
                         # An expired dedupe row (TTL deletion is lazy) no longer blocks a new request.
                         "ConditionExpression": "attribute_not_exists(approval_id) OR expires_at <= :now",
                         "ExpressionAttributeValues": {":now": now}}},
            ])
        except ClientError as exc:
            if self._dedupe_conflict(exc):
                raise DuplicateApprovalError(dedupe_key) from exc
            raise
        return item

    def get(self, approval_id):
        result = self.table.get_item(Key={"approval_id": approval_id}, ConsistentRead=True)
        return result.get("Item")

    def claim(self, approval_id, team_id, slack_user_id):
        """Atomically claim PENDING -> EXECUTING; only the owner (same workspace and user) can claim."""
        now = int(time.time())
        return self._owner_transition(
            approval_id, team_id, slack_user_id, "EXECUTING", "approved_at", now)

    def reject(self, approval_id, team_id, slack_user_id):
        now = int(time.time())
        item = self._owner_transition(approval_id, team_id, slack_user_id, "REJECTED", "rejected_at", now)
        if item:
            self.release_dedupe(item)
        return item

    def cancel(self, approval_id, reason=None):
        """PENDING -> CANCELLED, used when the approval request could not be delivered to Slack."""
        now = int(time.time())
        values = {":cancelled": "CANCELLED", ":pending": "PENDING", ":now": now,
                  ":ttl": now + retention_seconds()}
        update = "SET #s = :cancelled, completed_at = :now, #ttl = :ttl"
        if reason:
            update += ", error_message = :error"
            values[":error"] = str(reason)[:500]
        try:
            result = self.table.update_item(
                Key={"approval_id": approval_id}, UpdateExpression=update,
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        item = result["Attributes"]
        self.release_dedupe(item)
        return item

    def finish(self, approval_id, status, error=None, release=True):
        """EXECUTING -> final status. ``release=False`` keeps the dedupe row on EXECUTION_FAILED/CANCELLED."""
        now = int(time.time())
        values = {":status": status, ":executing": "EXECUTING", ":now": now,
                  ":ttl": now + retention_seconds()}
        update = "SET #s = :status, completed_at = :now, #ttl = :ttl"
        if error:
            update += ", error_message = :error"
            values[":error"] = str(error)[:500]
        result = self.table.update_item(Key={"approval_id": approval_id}, UpdateExpression=update,
                                        ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                                        ExpressionAttributeValues=values,
                                        ConditionExpression="#s = :executing",
                                        ReturnValues="ALL_NEW")
        if status in ("EXECUTION_FAILED", "CANCELLED") and release:
            # Let the user resubmit; the failure message tells them to check Jira first.
            self.release_dedupe(result.get("Attributes") or {})

    def release_dedupe(self, item):
        """Best-effort removal of the dedupe row that belongs to this approval."""
        dedupe_key, approval_id = item.get("dedupe_key"), item.get("approval_id")
        if not dedupe_key or not approval_id:
            return
        try:
            self.table.delete_item(
                Key={"approval_id": DEDUPE_PREFIX + dedupe_key},
                ConditionExpression="target_approval_id = :id",
                ExpressionAttributeValues={":id": approval_id},
            )
        except ClientError:
            # Already gone or owned by a newer request; it also expires on its own.
            logger.warning("Could not release dedupe row for approval %s", approval_id)

    def cancel_pending_for_team(self, team_id, reason="Slack app uninstalled"):
        """Cancel every PENDING approval of a workspace (app uninstalled). Returns the number cancelled.

        Scans the table (no index on team_id); used only on uninstall, which is rare.
        """
        if not team_id:
            raise ValueError("team_id is required")
        cancelled, start_key = 0, None
        while True:
            kwargs = {"FilterExpression": "team_id = :team AND #s = :pending",
                      "ExpressionAttributeNames": {"#s": "status"},
                      "ExpressionAttributeValues": {":team": team_id, ":pending": "PENDING"},
                      "ProjectionExpression": "approval_id"}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            page = self.table.scan(**kwargs)
            for row in page.get("Items", []):
                if self.cancel(row["approval_id"], reason=reason):
                    cancelled += 1
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                return cancelled

    def _owner_transition(self, approval_id, team_id, slack_user_id, new_status, timestamp_field, now):
        try:
            result = self.table.update_item(
                Key={"approval_id": approval_id},
                # Extend the TTL so the row is kept as an audit record (GAP-05).
                UpdateExpression=f"SET #s = :new_status, {timestamp_field} = :now, #ttl = :ttl",
                ConditionExpression=("#s = :pending AND slack_user_id = :owner AND team_id = :team "
                                     "AND expires_at > :now"),
                ExpressionAttributeNames={"#s": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={":new_status": new_status, ":pending": "PENDING",
                                           ":owner": slack_user_id, ":team": team_id, ":now": now,
                                           ":ttl": now + retention_seconds()},
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise

    @staticmethod
    def _dedupe_conflict(exc):
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons") or []
        return len(reasons) > 1 and (reasons[1] or {}).get("Code") == "ConditionalCheckFailed"

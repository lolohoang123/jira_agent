import os
import time
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


class ApprovalRepository:
    """DynamoDB persistence for one-time, owner-bound approvals."""

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
        item = {
            "approval_id": str(uuid.uuid4()),
            "slack_user_id": approval["slack_user_id"],
            "channel_id": approval["channel_id"],
            "action": approval["action"],
            "form": approval["form"],
            "status": "PENDING",
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "ttl": now + ttl_seconds,
        }
        self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(approval_id)")
        return item

    def get(self, approval_id):
        result = self.table.get_item(Key={"approval_id": approval_id}, ConsistentRead=True)
        return result.get("Item")

    def claim(self, approval_id, slack_user_id):
        """Atomically claim PENDING -> EXECUTING; only the owner can claim."""
        now = int(time.time())
        try:
            result = self.table.update_item(
                Key={"approval_id": approval_id},
                UpdateExpression="SET #s = :executing, approved_at = :now",
                ConditionExpression="#s = :pending AND slack_user_id = :owner AND expires_at > :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":executing": "EXECUTING", ":pending": "PENDING",
                                           ":owner": slack_user_id, ":now": now},
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise

    def reject(self, approval_id, slack_user_id):
        try:
            result = self.table.update_item(
                Key={"approval_id": approval_id},
                UpdateExpression="SET #s = :rejected, rejected_at = :now",
                ConditionExpression="#s = :pending AND slack_user_id = :owner AND expires_at > :now",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":rejected": "REJECTED", ":pending": "PENDING",
                                           ":owner": slack_user_id, ":now": int(time.time())},
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise

    def finish(self, approval_id, status, error=None):
        values = {":status": status, ":executing": "EXECUTING"}
        update = "SET #s = :status, completed_at = :now"
        values[":now"] = int(time.time())
        if error:
            update += ", error_message = :error"
            values[":error"] = str(error)[:500]
        self.table.update_item(Key={"approval_id": approval_id}, UpdateExpression=update,
                               ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues=values,
                               ConditionExpression="#s = :executing")

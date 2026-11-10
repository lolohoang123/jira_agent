import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3
from botocore.exceptions import ClientError

from util.constant import ATLASSIAN_AUTHORIZE_URL, ATLASSIAN_TOKEN_URL, OAUTH_SCOPES

REFRESH_LOCK_SECONDS = 30
REFRESH_WAIT_SECONDS = 8
REFRESH_POLL_INTERVAL = 0.5


def _dynamodb_resource():
    endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
    kwargs = {}
    if endpoint:
        kwargs.update(
            endpoint_url=endpoint,
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "local"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
        )
    return boto3.resource("dynamodb", **kwargs)


class DynamoHelper:
    def __init__(self, table_name):
        self.table_name_env = table_name
        self.dynamodb = _dynamodb_resource()

    def oauth_table(self):
        table_name = os.environ.get(self.table_name_env)
        if not table_name and self.table_name_env == "OAUTH_USERS_TABLE":
            table_name = "read-jira-users"  # backward-compatible fallback
        if not table_name:
            raise RuntimeError(f"Missing required environment variable: {self.table_name_env}")
        return self.dynamodb.Table(table_name)

    def get_jira_account_id_for_slack_user(self, slack_user_id):
        if not slack_user_id:
            raise ValueError("Could not identify the Slack user.")
        result = self.oauth_table().get_item(
            Key={"slack_user_id": slack_user_id},
            ProjectionExpression="jiraAccountId",
            ConsistentRead=True,
        )
        account_id = (result.get("Item") or {}).get("jiraAccountId")
        if not account_id:
            raise ValueError("Jira account is not connected. Run `/jira-agent connect` first.")
        return account_id

    def get_jira_cloud_id_for_slack_user(self, slack_user_id):
        """Cloud id of the Jira site this Slack user is connected to (no token refresh)."""
        if not slack_user_id:
            raise ValueError("Could not identify the Slack user.")
        result = self.oauth_table().get_item(
            Key={"slack_user_id": slack_user_id},
            ProjectionExpression="jiraCloudId",
            ConsistentRead=True,
        )
        cloud_id = (result.get("Item") or {}).get("jiraCloudId")
        if not cloud_id:
            raise ValueError("Jira account is not connected. Run `/jira-agent connect` first.")
        return cloud_id

    def create_oauth_authorization_url(self, slack_user_id):
        if not slack_user_id:
            raise ValueError("Could not identify the Slack user.")
        client_id = os.environ["ATLASSIAN_CLIENT_ID"]
        redirect_uri = os.environ["ATLASSIAN_REDIRECT_URI"]
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        self.oauth_table().put_item(
            Item={"state": state, "slackUserId": slack_user_id,
                  "createdAt": now, "expiresAt": now + 600},
            ConditionExpression="attribute_not_exists(#state)",
            ExpressionAttributeNames={"#state": "state"},
        )
        params = {
            "audience": "api.atlassian.com", "client_id": client_id,
            "scope": " ".join(OAUTH_SCOPES), "redirect_uri": redirect_uri,
            "state": state, "response_type": "code", "prompt": "consent",
        }
        return f"{ATLASSIAN_AUTHORIZE_URL}?{urlencode(params)}"

    def consume_oauth_state(self, state):
        if not state:
            raise ValueError("Missing OAuth state.")
        now = int(time.time())
        try:
            result = self.oauth_table().update_item(
                Key={"state": state},
                UpdateExpression="SET consumedAt = :now",
                ConditionExpression=(
                    "attribute_exists(#state) AND attribute_not_exists(consumedAt) AND expiresAt > :now"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":now": now},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ValueError("This OAuth link has expired or has already been used. Run `/jira-agent connect` again.") from exc
            raise
        user_id = (result.get("Attributes") or {}).get("slackUserId")
        if not user_id:
            raise ValueError("Invalid OAuth state. Run `/jira-agent connect` again.")
        return user_id

    @staticmethod
    def normalize_expiry(value):
        try:
            value = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return value // 1000 if value > 10**12 else value

    @staticmethod
    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _read_user(table, user_id):
        return table.get_item(Key={"slack_user_id": user_id}, ConsistentRead=True).get("Item")

    def get_oauth_credentials(self, slack_user_id):
        if not slack_user_id:
            raise ValueError("slack_user_id is required")
        table = self.oauth_table()
        user = self._read_user(table, slack_user_id)
        if not user or not user.get("connected", True):
            raise RuntimeError("Jira account is not connected for this Slack user")
        if self.normalize_expiry(user.get("accessTokenExpiresAt")) <= int(time.time()) + 60:
            user = self.refresh_access_token(slack_user_id)
        access_token = user.get("accessToken")
        cloud_id = user.get("jiraCloudId")
        if not access_token or not cloud_id:
            raise RuntimeError("Jira OAuth record is missing access token or cloud ID")
        return {"access_token": access_token, "cloud_id": cloud_id,
                "jira_site_url": user.get("jiraSiteUrl")}

    def refresh_access_token(self, slack_user_id):
        table = self.oauth_table()
        owner = str(uuid.uuid4())
        deadline = time.monotonic() + REFRESH_WAIT_SECONDS
        while True:
            now = int(time.time())
            try:
                table.update_item(
                    Key={"slack_user_id": slack_user_id},
                    UpdateExpression="SET refreshLockOwner = :owner, refreshLockUntil = :until",
                    ConditionExpression=(
                        "attribute_exists(slack_user_id) AND "
                        "(attribute_not_exists(refreshLockUntil) OR refreshLockUntil < :now OR refreshLockOwner = :owner)"
                    ),
                    ExpressionAttributeValues={":owner": owner, ":until": now + REFRESH_LOCK_SECONDS, ":now": now},
                )
                break
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                latest = self._read_user(table, slack_user_id)
                if not latest:
                    raise RuntimeError("Jira OAuth record was not found")
                if self.normalize_expiry(latest.get("accessTokenExpiresAt")) > int(time.time()) + 60:
                    return latest
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another request is refreshing the Jira token; please retry")
                time.sleep(REFRESH_POLL_INTERVAL)

        try:
            user = self._read_user(table, slack_user_id)
            if self.normalize_expiry(user.get("accessTokenExpiresAt")) > int(time.time()) + 60:
                return user
            refresh_token = user.get("refreshToken")
            if not refresh_token:
                raise RuntimeError("Jira refresh token is missing; reconnect the Jira account")
            payload = {
                "grant_type": "refresh_token",
                "client_id": os.environ["ATLASSIAN_CLIENT_ID"],
                "client_secret": os.environ["ATLASSIAN_CLIENT_SECRET"],
                "refresh_token": refresh_token,
            }
            request = Request(ATLASSIAN_TOKEN_URL, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=15) as response:
                    token_data = json.loads(response.read().decode())
            except HTTPError as exc:
                raise RuntimeError(f"Atlassian token refresh failed (HTTP {exc.code}); reconnect Jira if the refresh token expired") from None
            except (URLError, TimeoutError) as exc:
                raise RuntimeError("Could not reach Atlassian to refresh the Jira token") from exc
            access = token_data.get("access_token")
            rotated = token_data.get("refresh_token") or refresh_token
            expires_in = token_data.get("expires_in")
            if not access or not expires_in:
                raise RuntimeError("Atlassian token response is missing access_token or expires_in")
            expires_at = int(time.time()) + int(expires_in)
            table.update_item(
                Key={"slack_user_id": slack_user_id},
                UpdateExpression=("SET accessToken=:a, refreshToken=:r, accessTokenExpiresAt=:e, "
                                  "accessTokenExpiresAtIso=:iso, updatedAt=:u REMOVE refreshLockOwner, refreshLockUntil"),
                ConditionExpression="refreshLockOwner = :owner",
                ExpressionAttributeValues={":owner": owner, ":a": access, ":r": rotated,
                                           ":e": expires_at, ":iso": self._iso(expires_at), ":u": int(time.time())},
            )
            return self._read_user(table, slack_user_id)
        except Exception:
            try:
                table.update_item(
                    Key={"slack_user_id": slack_user_id},
                    UpdateExpression="REMOVE refreshLockOwner, refreshLockUntil",
                    ConditionExpression="refreshLockOwner = :owner",
                    ExpressionAttributeValues={":owner": owner},
                )
            except ClientError:
                pass
            raise

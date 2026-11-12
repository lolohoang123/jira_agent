import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from util.config import get_secret
from util.constant import ATLASSIAN_AUTHORIZE_URL, ATLASSIAN_TOKEN_URL, OAUTH_SCOPES

logger = logging.getLogger(__name__)

# Atlassian token errors meaning the stored refresh token can never work again.
REVOKED_GRANT_ERRORS = {"invalid_grant", "unauthorized_client", "access_denied"}
STATE_TTL_SECONDS = 600
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


class JiraConnectionRevoked(RuntimeError):
    """The stored Jira authorization was revoked or expired; the user must run /jira-agent connect."""

    def __init__(self):
        super().__init__("Your Jira authorization has expired or was revoked. Run `/jira-agent connect` again.")


def _token_error_code(exc):
    """OAuth ``error`` code from an Atlassian error response; never returns or logs anything else."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
    except (ValueError, OSError, AttributeError):
        return None
    return body.get("error") if isinstance(body, dict) else None


def user_key(team_id, slack_user_id):
    """Primary key of a Jira connection: (team_id, slack_user_id). Slack user ids are only unique per workspace."""
    if not team_id or not slack_user_id:
        raise ValueError("Could not identify the Slack workspace and user.")
    return {"team_id": team_id, "slack_user_id": slack_user_id}


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

    def _read_connection(self, team_id, slack_user_id, attribute):
        """Read one attribute of an active connection; {} when missing or disconnected."""
        result = self.oauth_table().get_item(
            Key=user_key(team_id, slack_user_id),
            ProjectionExpression=f"{attribute}, #connected",
            ExpressionAttributeNames={"#connected": "connected"},
            ConsistentRead=True,
        )
        item = result.get("Item") or {}
        return item if item.get("connected", True) else {}

    def get_jira_account_id_for_slack_user(self, team_id, slack_user_id):
        item = self._read_connection(team_id, slack_user_id, "jiraAccountId")
        account_id = item.get("jiraAccountId")
        if not account_id:
            raise ValueError("Jira account is not connected. Run `/jira-agent connect` first.")
        return account_id

    def get_jira_cloud_id_for_slack_user(self, team_id, slack_user_id):
        """Cloud id of the Jira site this Slack user is connected to (no token refresh)."""
        item = self._read_connection(team_id, slack_user_id, "jiraCloudId")
        cloud_id = item.get("jiraCloudId")
        if not cloud_id:
            raise ValueError("Jira account is not connected. Run `/jira-agent connect` first.")
        return cloud_id

    def _put_state(self, kind, **attributes):
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        self.oauth_table().put_item(
            Item={"state": state, "kind": kind, "createdAt": now, "expiresAt": now + STATE_TTL_SECONDS,
                  **attributes},
            ConditionExpression="attribute_not_exists(#state)",
            ExpressionAttributeNames={"#state": "state"},
        )
        return state

    def create_slack_install_state(self):
        """One-time state for the Slack "Add to Slack" OAuth v2 flow (CSRF protection)."""
        return self._put_state("slack_install")

    def create_oauth_authorization_url(self, team_id, slack_user_id):
        user_key(team_id, slack_user_id)  # validates both ids
        client_id = os.environ["ATLASSIAN_CLIENT_ID"]
        redirect_uri = os.environ["ATLASSIAN_REDIRECT_URI"]
        state = self._put_state("jira", teamId=team_id, slackUserId=slack_user_id)
        params = {
            "audience": "api.atlassian.com", "client_id": client_id,
            "scope": " ".join(OAUTH_SCOPES), "redirect_uri": redirect_uri,
            "state": state, "response_type": "code", "prompt": "consent",
        }
        return f"{ATLASSIAN_AUTHORIZE_URL}?{urlencode(params)}"

    def consume_oauth_state(self, state, kind="jira"):
        """Mark a state used (single use, unexpired, same flow). Jira: returns (team_id, slack_user_id)."""
        if not state:
            raise ValueError("Missing OAuth state.")
        now = int(time.time())
        try:
            result = self.oauth_table().update_item(
                Key={"state": state},
                UpdateExpression="SET consumedAt = :now",
                ConditionExpression=(
                    "attribute_exists(#state) AND attribute_not_exists(consumedAt) AND expiresAt > :now "
                    "AND #kind = :kind"
                ),
                ExpressionAttributeNames={"#state": "state", "#kind": "kind"},
                ExpressionAttributeValues={":now": now, ":kind": kind},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ValueError("This link has expired or has already been used. Please start again.") from exc
            raise
        attributes = result.get("Attributes") or {}
        if kind != "jira":
            return attributes
        team_id, user_id = attributes.get("teamId"), attributes.get("slackUserId")
        if not team_id or not user_id:
            raise ValueError("Invalid OAuth state. Run `/jira-agent connect` again.")
        return team_id, user_id

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
    def _read_user(table, team_id, user_id):
        return table.get_item(Key=user_key(team_id, user_id), ConsistentRead=True).get("Item")

    def disconnect_slack_user(self, team_id, slack_user_id):
        """Delete the user's Jira connection (tokens included). Returns True if one existed."""
        result = self.oauth_table().delete_item(Key=user_key(team_id, slack_user_id), ReturnValues="ALL_OLD")
        return bool(result.get("Attributes"))

    def delete_team_connections(self, team_id):
        """Delete every Jira connection of a workspace (app uninstalled). Returns the number deleted."""
        if not team_id:
            raise ValueError("team_id is required")
        table = self.oauth_table()
        deleted, start_key = 0, None
        with table.batch_writer() as batch:
            while True:
                kwargs = {"KeyConditionExpression": Key("team_id").eq(team_id),
                          "ProjectionExpression": "team_id, slack_user_id"}
                if start_key:
                    kwargs["ExclusiveStartKey"] = start_key
                page = table.query(**kwargs)
                for item in page.get("Items", []):
                    batch.delete_item(Key=user_key(item["team_id"], item["slack_user_id"]))
                    deleted += 1
                start_key = page.get("LastEvaluatedKey")
                if not start_key:
                    break
        return deleted

    def _forget_tokens(self, table, team_id, slack_user_id, owner):
        """Atlassian rejected the refresh token for good: drop the tokens, keep the row as disconnected."""
        now = int(time.time())
        try:
            table.update_item(
                Key=user_key(team_id, slack_user_id),
                UpdateExpression=("SET connected = :false, disconnectedAt = :now, updatedAt = :now "
                                  "REMOVE accessToken, refreshToken, accessTokenExpiresAt, accessTokenExpiresAtIso, "
                                  "refreshLockOwner, refreshLockUntil"),
                ConditionExpression="refreshLockOwner = :owner",
                ExpressionAttributeValues={":false": False, ":now": now, ":owner": owner},
            )
        except ClientError:
            logger.warning("Could not clear revoked Jira tokens for Slack user %s", slack_user_id)

    def get_oauth_credentials(self, team_id, slack_user_id):
        user_key(team_id, slack_user_id)  # validates both ids
        table = self.oauth_table()
        user = self._read_user(table, team_id, slack_user_id)
        if not user or not user.get("connected", True):
            raise RuntimeError("Jira account is not connected for this Slack user")
        if self.normalize_expiry(user.get("accessTokenExpiresAt")) <= int(time.time()) + 60:
            user = self.refresh_access_token(team_id, slack_user_id)
        access_token = user.get("accessToken")
        cloud_id = user.get("jiraCloudId")
        if not access_token or not cloud_id:
            raise RuntimeError("Jira OAuth record is missing access token or cloud ID")
        return {"access_token": access_token, "cloud_id": cloud_id,
                "jira_site_url": user.get("jiraSiteUrl"), "time_zone": user.get("timeZone")}

    def get_user_timezone_name(self, team_id, slack_user_id):
        """The user's stored IANA timezone, or None (not connected, or connected before it was stored)."""
        return self._read_connection(team_id, slack_user_id, "timeZone").get("timeZone")

    def set_user_timezone(self, team_id, slack_user_id, time_zone):
        """Store the timezone on an existing connection (backfill for connections made before it was stored)."""
        try:
            self.oauth_table().update_item(
                Key=user_key(team_id, slack_user_id),
                UpdateExpression="SET timeZone = :tz",
                ConditionExpression="attribute_exists(slack_user_id)",
                ExpressionAttributeValues={":tz": time_zone},
            )
        except ClientError:
            logger.warning("Could not store the timezone for Slack user %s", slack_user_id)

    def refresh_access_token(self, team_id, slack_user_id):
        table = self.oauth_table()
        owner = str(uuid.uuid4())
        deadline = time.monotonic() + REFRESH_WAIT_SECONDS
        while True:
            now = int(time.time())
            try:
                table.update_item(
                    Key=user_key(team_id, slack_user_id),
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
                latest = self._read_user(table, team_id, slack_user_id)
                if not latest:
                    raise RuntimeError("Jira OAuth record was not found")
                if self.normalize_expiry(latest.get("accessTokenExpiresAt")) > int(time.time()) + 60:
                    return latest
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another request is refreshing the Jira token; please retry")
                time.sleep(REFRESH_POLL_INTERVAL)

        try:
            user = self._read_user(table, team_id, slack_user_id)
            if self.normalize_expiry(user.get("accessTokenExpiresAt")) > int(time.time()) + 60:
                return user
            refresh_token = user.get("refreshToken")
            if not refresh_token:
                raise RuntimeError("Jira refresh token is missing; reconnect the Jira account")
            payload = {
                "grant_type": "refresh_token",
                "client_id": os.environ["ATLASSIAN_CLIENT_ID"],
                "client_secret": get_secret("ATLASSIAN_CLIENT_SECRET"),
                "refresh_token": refresh_token,
            }
            request = Request(ATLASSIAN_TOKEN_URL, data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=15) as response:
                    token_data = json.loads(response.read().decode())
            except HTTPError as exc:
                if _token_error_code(exc) in REVOKED_GRANT_ERRORS:
                    self._forget_tokens(table, team_id, slack_user_id, owner)
                    raise JiraConnectionRevoked() from None
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
                Key=user_key(team_id, slack_user_id),
                UpdateExpression=("SET accessToken=:a, refreshToken=:r, accessTokenExpiresAt=:e, "
                                  "accessTokenExpiresAtIso=:iso, updatedAt=:u REMOVE refreshLockOwner, refreshLockUntil"),
                ConditionExpression="refreshLockOwner = :owner",
                ExpressionAttributeValues={":owner": owner, ":a": access, ":r": rotated,
                                           ":e": expires_at, ":iso": self._iso(expires_at), ":u": int(time.time())},
            )
            return self._read_user(table, team_id, slack_user_id)
        except Exception:
            try:
                table.update_item(
                    Key=user_key(team_id, slack_user_id),
                    UpdateExpression="REMOVE refreshLockOwner, refreshLockUntil",
                    ConditionExpression="refreshLockOwner = :owner",
                    ExpressionAttributeValues={":owner": owner},
                )
            except ClientError:
                pass
            raise

"""Per-workspace Slack installations (bot tokens) stored in DynamoDB.

Table ``INSTALLATIONS_TABLE``: partition key ``team_id``. One row per workspace that installed the app through
the Slack OAuth v2 flow. The bot token is a secret: it is never logged or returned to Slack.
"""
import logging
import os
import time

import boto3

logger = logging.getLogger(__name__)

TOKEN_CACHE_SECONDS = 60
_cache = {}  # team_id -> (bot_token, loaded_at); short-lived so uninstalls take effect quickly


class SlackNotInstalled(RuntimeError):
    """The workspace has no active installation of the app."""

    def __init__(self, team_id):
        super().__init__(f"The app is not installed in Slack workspace {team_id}.")
        self.team_id = team_id


def clear_cache():
    _cache.clear()


class InstallationStore:
    def __init__(self, table=None):
        if table is None:
            table_name = os.environ.get("INSTALLATIONS_TABLE")
            if not table_name:
                raise RuntimeError("Missing required environment variable: INSTALLATIONS_TABLE")
            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    def save(self, installation):
        """Insert or replace the installation of one workspace (reinstalling refreshes the token)."""
        now = int(time.time())
        item = {
            "team_id": installation["team_id"],
            "team_name": installation.get("team_name") or "",
            "app_id": installation.get("app_id") or "",
            "bot_user_id": installation.get("bot_user_id") or "",
            "bot_token": installation["bot_token"],
            "scope": installation.get("scope") or "",
            "installed_by_user_id": installation.get("installed_by_user_id") or "",
            "installed_at": now,
            "updated_at": now,
        }
        self.table.put_item(Item=item)
        _cache.pop(item["team_id"], None)
        return item

    def get_bot_token(self, team_id):
        if not team_id:
            raise SlackNotInstalled(team_id)
        cached = _cache.get(team_id)
        if cached and time.monotonic() - cached[1] < TOKEN_CACHE_SECONDS:
            return cached[0]
        item = self.table.get_item(Key={"team_id": team_id}, ProjectionExpression="bot_token",
                                   ConsistentRead=True).get("Item") or {}
        token = item.get("bot_token")
        if not token:
            raise SlackNotInstalled(team_id)
        _cache[team_id] = (token, time.monotonic())
        return token

    def is_installed(self, team_id):
        try:
            self.get_bot_token(team_id)
            return True
        except SlackNotInstalled:
            return False

    def delete(self, team_id):
        """Remove a workspace's installation. Returns True if one existed."""
        _cache.pop(team_id, None)
        result = self.table.delete_item(Key={"team_id": team_id}, ReturnValues="ALL_OLD")
        return bool(result.get("Attributes"))

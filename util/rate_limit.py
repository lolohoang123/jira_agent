"""Per-user fixed-window rate limit backed by DynamoDB (spec gap GAP-12: cap Bedrock cost per user).

Table ``RATE_LIMITS_TABLE``: partition key ``limit_key`` (String), TTL on ``ttl``.
One item per user and window: ``agent#<slack_user_id>#<window start>`` with an atomic counter.

Fails open: if the table is not configured or DynamoDB errors, the request is allowed and a warning is
logged. Hard caps on total cost come from Lambda reserved concurrency and API Gateway throttling.
"""
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_AGENT_LIMIT_PER_HOUR = 20
WINDOW_SECONDS = 3600


def agent_limit_per_hour():
    try:
        return int(os.environ.get("AGENT_RATE_LIMIT_PER_HOUR", DEFAULT_AGENT_LIMIT_PER_HOUR))
    except ValueError:
        return DEFAULT_AGENT_LIMIT_PER_HOUR


class RateLimiter:
    def __init__(self, table=None):
        if table is None:
            table_name = os.environ.get("RATE_LIMITS_TABLE")
            table = boto3.resource("dynamodb").Table(table_name) if table_name else None
        self.table = table

    def allow(self, key, limit, window_seconds=WINDOW_SECONDS):
        """Count one request for ``key``; False once ``limit`` requests were made in the current window."""
        if limit <= 0 or self.table is None:
            return True
        now = int(time.time())
        window_start = now - now % window_seconds
        try:
            self.table.update_item(
                Key={"limit_key": f"{key}#{window_start}"},
                UpdateExpression="ADD #count :one SET #ttl = if_not_exists(#ttl, :ttl)",
                ConditionExpression="attribute_not_exists(#count) OR #count < :limit",
                ExpressionAttributeNames={"#count": "request_count", "#ttl": "ttl"},
                ExpressionAttributeValues={":one": 1, ":limit": limit, ":ttl": window_start + 2 * window_seconds},
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            logger.warning("Rate limit check failed (%s); allowing the request",
                           exc.response.get("Error", {}).get("Code"))
            return True


def agent_request_allowed(team_id, slack_user_id, limiter=None):
    return (limiter or RateLimiter()).allow(f"agent#{team_id}#{slack_user_id}", agent_limit_per_hour())


def agent_limit_message():
    return (f"You've reached the limit of {agent_limit_per_hour()} assistant requests per hour. "
            "Try again later. `/jira-agent list` and `/jira-agent logwork today` still work.")

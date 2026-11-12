"""Per-user timezone.

Each Jira connection stores the user's IANA timezone (``timeZone`` on the users table), taken from Jira
``/rest/api/3/myself`` when the user connects, with Slack ``users.info`` (``tz``) as a fallback. Jira comes first
because JQL ``worklogDate`` and the Jira UI use the Jira profile timezone, so "today" stays consistent with Jira.

``JIRA_TIMEZONE`` (default ``Asia/Ho_Chi_Minh``) is only the fallback for users without a stored timezone.
"""
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
MAX_TIMEZONE_LENGTH = 64


def valid_timezone_name(name):
    """``name`` if it is a known IANA timezone, else None (never raises)."""
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_TIMEZONE_LENGTH:
        return None
    name = name.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def default_timezone_name():
    """Deployment fallback: ``JIRA_TIMEZONE`` when valid, else ``Asia/Ho_Chi_Minh``."""
    configured = os.environ.get("JIRA_TIMEZONE", DEFAULT_TIMEZONE)
    name = valid_timezone_name(configured)
    if not name:
        logger.warning("JIRA_TIMEZONE is not a valid IANA timezone; using %s", DEFAULT_TIMEZONE)
        return DEFAULT_TIMEZONE
    return name


def resolve_timezone_name(*candidates):
    """First valid timezone among ``candidates`` (e.g. the user's stored one), else the deployment default."""
    for candidate in candidates:
        name = valid_timezone_name(candidate)
        if name:
            return name
    return default_timezone_name()


def resolve_timezone(*candidates):
    return ZoneInfo(resolve_timezone_name(*candidates))


def user_timezone_name(team_id, slack_user_id, users=None):
    """Timezone name of a Slack user: the one stored on their Jira connection, else the default.

    One DynamoDB read, no Jira/Slack call, so it is safe on the 3-second Slack ACK path. Never raises.
    """
    try:
        if users is None:
            from util.dynamo_helper import DynamoHelper
            users = DynamoHelper("OAUTH_USERS_TABLE")
        stored = users.get_user_timezone_name(team_id, slack_user_id)
    except Exception:
        logger.warning("Could not read the stored timezone of Slack user %s", slack_user_id)
        stored = None
    return resolve_timezone_name(stored)

import json
import os
import re


def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body) if not isinstance(body, str) else body,
    }


def slack_escape(value):
    value = str(value or "")
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


DURATION_RE = re.compile(
    r"(?i)(?=.*\d)\s*(?:(\d+)\s*w\s*)?(?:(\d+)\s*d\s*)?(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?"
)
# Jira's default time-tracking settings: 1d = 8h, 1w = 5d.
_UNIT_MINUTES = (5 * 8 * 60, 8 * 60, 60, 1)
DEFAULT_MAX_WORKLOG_MINUTES = 24 * 60


def duration_to_minutes(duration):
    match = DURATION_RE.fullmatch(duration or "")
    if not match:
        raise ValueError("Duration must look like 30m, 1h, 1h 30m, or 1d.")
    return sum(int(value or 0) * unit for value, unit in zip(match.groups(), _UNIT_MINUTES))


def max_worklog_minutes():
    try:
        value = int(os.environ.get("MAX_WORKLOG_MINUTES", DEFAULT_MAX_WORKLOG_MINUTES))
    except ValueError:
        return DEFAULT_MAX_WORKLOG_MINUTES
    return value if value > 0 else DEFAULT_MAX_WORKLOG_MINUTES


def validate_duration(duration):
    """Validate a Jira duration: well-formed, greater than 0 and at most MAX_WORKLOG_MINUTES (default 24h)."""
    minutes = duration_to_minutes(duration)
    if minutes <= 0:
        raise ValueError("Duration must be greater than 0.")
    limit = max_worklog_minutes()
    if minutes > limit:
        raise ValueError(f"Duration must be at most {limit // 60}h{f' {limit % 60}m' if limit % 60 else ''}.")
    return duration

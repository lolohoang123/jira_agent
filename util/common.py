import json
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


def validate_duration(duration):
    if not re.fullmatch(
        r"(?i)(?=.*\d)\s*(?:\d+\s*w\s*)?(?:\d+\s*d\s*)?(?:\d+\s*h\s*)?(?:\d+\s*m\s*)?",
        duration or "",
    ):
        raise ValueError("Duration must look like 30m, 1h, 1h 30m, or 1d.")
    return duration

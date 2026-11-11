import json
import urllib.parse
import urllib.request

from util.config import get_secret


def slack_api(method, payload):
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {get_secret("SLACK_BOT_TOKEN")}",
                 "Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API {method} failed: {result.get('error', 'unknown_error')}")
    return result


def post_slack_response(response_url, message):
    parsed = urllib.parse.urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}:
        raise ValueError("Invalid Slack response_url host")
    req = urllib.request.Request(response_url, data=json.dumps(message).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as response:
        response.read()

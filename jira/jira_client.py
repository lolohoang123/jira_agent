import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from util.dynamo_helper import DynamoHelper

logger = logging.getLogger(__name__)


class JiraClient:
    """Thin Jira REST client. It never receives raw credentials from callers."""
    def __init__(self, team_id, slack_user_id):
        self.team_id = team_id
        self.slack_user_id = slack_user_id
        self.credentials = DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials(team_id, slack_user_id)

    @property
    def cloud_id(self):
        return self.credentials["cloud_id"]

    @property
    def site_url(self):
        return (self.credentials.get("jira_site_url") or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")

    def request(self, path, params=None, method="GET", data=None):
        cloud_id = self.credentials["cloud_id"]
        path = path if path.startswith("/") else f"/{path}"
        url = f"https://api.atlassian.com/ex/jira/{urllib.parse.quote(cloud_id, safe='')}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        body = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {self.credentials['access_token']}",
            "Accept": "application/json", "Content-Type": "application/json",
        }, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            logger.warning("Jira API returned HTTP %s for Slack user %s", exc.code, self.slack_user_id)
            raise RuntimeError(f"Jira API returned HTTP {exc.code}: {details[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Unable to connect to Jira API") from exc

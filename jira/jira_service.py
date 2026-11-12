import logging
import re
import urllib.parse
from datetime import datetime, time as dt_time

from jira.jira_client import JiraClient
from util.common import validate_duration
from util.dynamo_helper import DynamoHelper
from util.timezone import resolve_timezone, valid_timezone_name

logger = logging.getLogger(__name__)


ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
SEARCH_LIMIT = 20
MAX_KEYWORD_LENGTH = 100
# JQL string delimiters and Lucene operators: removed so user text can only ever be search terms.
_JQL_SPECIAL = re.compile(r"""["'\\+\-&|!(){}\[\]^~*?:/;,<>=]""")


MAX_SEARCH_TERMS = 5


def jql_text_terms(keyword):
    """Turn free user text into safe terms for ``text ~ "<terms>"`` (never JQL syntax)."""
    cleaned = _JQL_SPECIAL.sub(" ", str(keyword or ""))[:MAX_KEYWORD_LENGTH]
    return " ".join(cleaned.split())


def jql_like_clause(keyword):
    """LIKE-style JQL for a keyword: every word must match the start of a word in the issue text.

    ``login bu`` -> ``text ~ "login*" AND text ~ "bu*"``: prefix matching per word, all words required.
    JQL has no SQL LIKE; Jira only supports a trailing wildcard, so a match inside a word ("gin" in
    "login") is not possible. One-character words are matched exactly (a lone ``x*`` matches too much).
    Wildcards are added here, after user text was reduced by ``jql_text_terms`` (which removes ``*``).
    Returns "" when the keyword has no usable term.
    """
    words = jql_text_terms(keyword).split()[:MAX_SEARCH_TERMS]
    return " AND ".join(f'text ~ "{word}*"' if len(word) > 1 else f'text ~ "{word}"' for word in words)


class JiraService:
    def __init__(self, team_id, slack_user_id):
        self.client = JiraClient(team_id, slack_user_id)
        self.team_id = team_id
        self.slack_user_id = slack_user_id
        self._timezone = None

    @property
    def timezone(self):
        """The user's timezone (stored on the connection), else the ``JIRA_TIMEZONE`` default."""
        if self._timezone is None:
            credentials = getattr(self.client, "credentials", None)
            name = credentials.get("time_zone") if isinstance(credentials, dict) else None
            if isinstance(credentials, dict) and not valid_timezone_name(name):
                name = self._backfill_timezone()
            self._timezone = resolve_timezone(name)
        return self._timezone

    def _backfill_timezone(self):
        """Connections made before timezones were stored: read it from Jira once and save it."""
        try:
            name = valid_timezone_name((self.client.request("/rest/api/3/myself") or {}).get("timeZone"))
        except Exception:
            logger.warning("Could not read the Jira timezone of Slack user %s", self.slack_user_id)
            return None
        if name:
            DynamoHelper("OAUTH_USERS_TABLE").set_user_timezone(self.team_id, self.slack_user_id, name)
        return name

    @property
    def cloud_id(self):
        """Cloud id of the site every request from this service goes to."""
        return self.client.cloud_id

    @property
    def site_url(self):
        return self.client.site_url

    def _search(self, jql, fields, max_pages=10, page_size=100):
        issues, token = [], None
        for _ in range(max_pages):
            body = {"jql": jql, "fields": fields, "maxResults": page_size}
            if token:
                body["nextPageToken"] = token
            result = self.client.request("/rest/api/3/search/jql", method="POST", data=body)
            issues.extend(result.get("issues", []))
            token = result.get("nextPageToken")
            if not token:
                break
        return issues

    def find_assigned_issues(self, account_id):
        # "timespent" is Jira's total logged seconds per issue, so callers need no per-issue worklog calls.
        return self._search(f'assignee = "{account_id}" ORDER BY updated DESC', ["summary", "status", "timespent"])

    def search_issues(self, keyword, limit=SEARCH_LIMIT):
        """Issues the user can see that match ``keyword`` LIKE-style (see ``jql_like_clause``),
        most recently updated first.

        Returns up to ``limit + 1`` issues so callers can tell whether more exist. An exact issue key
        (e.g. PROJ-12) is fetched directly and listed first, because JQL text search ignores keys.
        """
        clause = jql_like_clause(keyword)
        if not clause:
            raise ValueError("Search keyword is empty.")
        fields = ["summary", "status", "timespent"]
        issues = self._search(f"{clause} ORDER BY updated DESC", fields,
                              max_pages=1, page_size=limit + 1)
        key = keyword.strip().upper()
        if ISSUE_KEY_RE.fullmatch(key):
            exact = self._get_issue(key, fields)
            if exact:
                issues = [exact] + [i for i in issues if i.get("key") != exact.get("key")]
        return issues[:limit + 1]

    def _get_issue(self, issue_key, fields):
        try:
            return self.client.request(f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}",
                                       params={"fields": ",".join(fields)})
        except RuntimeError:
            return None  # unknown key or no permission: the text search results still apply

    def find_issues_with_worklogs_today(self, account_id, day):
        jql = f'worklogAuthor = "{account_id}" AND worklogDate = "{day:%Y/%m/%d}"'
        return self._search(jql, ["summary"])

    def get_issue_worklogs(self, issue_key, start_ms=None, end_ms=None):
        worklogs, start_at = [], 0
        for _ in range(100):
            params = {"startAt": start_at, "maxResults": 100}
            if start_ms is not None:
                params.update(startedAfter=start_ms, startedBefore=end_ms)
            result = self.client.request(
                f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog", params=params
            )
            page = result.get("worklogs", [])
            worklogs.extend(page)
            start_at += len(page)
            if not page or start_at >= result.get("total", 0):
                break
        return worklogs

    @staticmethod
    def comment_to_text(comment):
        parts = []
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "text" and node.get("text"):
                    parts.append(node["text"])
                for child in node.get("content", []):
                    walk(child)
        if isinstance(comment, dict):
            walk(comment)
        return " ".join(" ".join(parts).split())

    @staticmethod
    def format_duration(seconds):
        minutes = max(0, int(seconds)) // 60
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m" if hours and minutes else (f"{hours}h" if hours else f"{minutes}m")

    def fetch_today_worklogs(self, account_id):
        tz = self.timezone
        now = datetime.now(tz)
        day = now.date()
        start = datetime.combine(day, dt_time.min, tzinfo=tz)
        end = datetime.combine(day, dt_time.max, tzinfo=tz)
        entries = []
        for issue in self.find_issues_with_worklogs_today(account_id, day):
            key = issue["key"]
            summary = issue.get("fields", {}).get("summary", "")
            for worklog in self.get_issue_worklogs(key, int(start.timestamp()*1000), int(end.timestamp()*1000)):
                if worklog.get("author", {}).get("accountId") != account_id or not worklog.get("started"):
                    continue
                try:
                    started = datetime.fromisoformat(worklog["started"].replace("Z", "+00:00")).astimezone(tz)
                except ValueError:
                    continue
                if started.date() != day:
                    continue
                entries.append({"key": key, "summary": summary, "started": started,
                                "seconds": int(worklog.get("timeSpentSeconds", 0)),
                                "comment": self.comment_to_text(worklog.get("comment"))})
        entries.sort(key=lambda x: x["started"])
        return day, entries

    def create_worklog(self, form):
        issue_key = form["issue_key"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", issue_key):
            raise ValueError("Issue key is invalid. Example: PROJ-123.")
        duration = validate_duration(form.get("duration", ""))
        estimate = form.get("remaining_estimate", "leave")
        if estimate not in {"leave", "auto", "new"}:
            raise ValueError("Invalid remaining estimate option.")
        if estimate == "new" and not form.get("new_estimate"):
            raise ValueError("Enter a new remaining estimate or choose another option.")
        tz = self.timezone
        started =datetime.strptime(f"{form['date']} {form['start_time']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        started_text = started.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        started_text = started_text[:23] + started.strftime("%z")
        body = {"started": started_text, "timeSpent": duration}
        if form.get("description"):
            body["comment"] = {"type": "doc", "version": 1, "content":[{"type":"paragraph","content":[{"type":"text","text":form["description"]}]}]}
        params = {"adjustEstimate": estimate}
        if estimate == "new":
            params["newEstimate"] = form["new_estimate"]
        return self.client.request(f"/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}/worklog",
                                   params=params, method="POST", data=body)

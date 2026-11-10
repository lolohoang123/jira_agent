import os
import re
import urllib.parse
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from jira.jira_client import JiraClient
from util.common import validate_duration


class JiraService:
    def __init__(self, slack_user_id):
        self.client = JiraClient(slack_user_id)
        self.slack_user_id = slack_user_id

    @property
    def cloud_id(self):
        """Cloud id of the site every request from this service goes to."""
        return self.client.cloud_id

    @property
    def site_url(self):
        return self.client.site_url

    def _search(self, jql, fields, max_pages=10):
        issues, token = [], None
        for _ in range(max_pages):
            body = {"jql": jql, "fields": fields, "maxResults": 100}
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
        tz = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
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
        tz = ZoneInfo(os.environ.get("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh"))
        started = datetime.strptime(f"{form['date']} {form['start_time']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
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

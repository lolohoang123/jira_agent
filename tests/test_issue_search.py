"""Unit tests for /jira-agent logwork find <keyword>, from docs/testing/issue_search_test_matrix.md
(TC-01..TC-17). AAA layout; Jira, Slack, DynamoDB and Lambda are mocked.
"""
import json
import logging
import urllib.parse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from handlers.async_task_handler import handle_async_task
from handlers.slack_command_handler import SEARCH_USAGE, handle_slack_request, parse_search_command
from jira import jira_service as service_module
from jira.jira_service import JiraService, jql_text_terms
from jira.tasks.run_search_issues_task import RunSearchIssuesTask
from slack.message_builder import build_assigned_issue_blocks, build_search_result_blocks

SITE = "https://acme.atlassian.net"
RESPONSE_URL = "https://hooks.slack.com/commands/T/1/x"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ASYNC_TASK_SECRET", "task-secret")
    monkeypatch.delenv("AGENT_ENABLED", raising=False)


def issue(key, summary="Fix login", status="In Progress", seconds=5400):
    return {"key": key, "fields": {"summary": summary, "status": {"name": status}, "timespent": seconds}}


# --------------------------------------------------------------------------------------
# Command parsing and dispatch
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,keyword", [
    ("logwork find login bug", "login bug"),
    ("LOGWORK FIND Login Bug", "Login Bug"),
    ("find PROJ-12", "PROJ-12"),
    ("logwork find “login bug”", "login bug"),
    ('logwork find "Sửa lỗi đăng nhập"', "Sửa lỗi đăng nhập"),
    ("logwork find", ""),
    ("find", ""),
])
def test_tc01_tc02_search_command_is_parsed(text, keyword):
    # Act & Assert
    assert parse_search_command(text) == keyword


@pytest.mark.parametrize("text", ["logwork today", "list", "logwork findx", "finder x", "connect", "log 1h on X"])
def test_tc02_other_commands_are_not_searches(text):
    # Act & Assert
    assert parse_search_command(text) is None


def command(text):
    body = urllib.parse.urlencode({"command": "/jira-agent", "text": text, "user_id": "U1", "channel_id": "C1",
                                   "response_url": RESPONSE_URL})
    lambda_client = MagicMock()
    response = handle_slack_request({"headers": {}}, SimpleNamespace(function_name="jira-agent"), body, lambda_client)
    return json.loads(response["body"]), lambda_client


@pytest.fixture
def connected():
    with patch("handlers.slack_command_handler.DynamoHelper") as helper:
        helper.return_value.get_jira_account_id_for_slack_user.return_value = "acc-1"
        yield helper


def test_tc03_search_starts_async_task(connected):
    # Act
    body, lambda_client = command("logwork find  Login   Bug ")

    # Assert
    assert body == {"response_type": "ephemeral", "text": "Searching Jira tickets..."}
    task = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
    assert task == {"_async_task": "search_issues", "_task_secret": "task-secret", "slack_user_id": "U1",
                    "keyword": "Login Bug", "response_url": RESPONSE_URL}


@pytest.mark.parametrize("text", ["logwork find", "find", 'logwork find "  "', "logwork find (*)", "find ?!"])
def test_tc04_empty_keyword_shows_usage(connected, text):
    # Act
    body, lambda_client = command(text)

    # Assert
    assert body["text"] == SEARCH_USAGE
    lambda_client.invoke.assert_not_called()
    connected.assert_not_called()


def test_tc05_not_connected_user(connected):
    # Arrange
    connected.return_value.get_jira_account_id_for_slack_user.side_effect = ValueError("not connected")

    # Act
    body, lambda_client = command("logwork find login")

    # Assert
    assert "not connected" in body["text"]
    lambda_client.invoke.assert_not_called()


def test_tc06_long_keyword_is_truncated(connected):
    # Act
    _, lambda_client = command("logwork find " + "a" * 150)

    # Assert
    assert json.loads(lambda_client.invoke.call_args.kwargs["Payload"])["keyword"] == "a" * 100


def test_tc07_search_works_while_assistant_is_disabled(connected, monkeypatch):
    # Arrange
    monkeypatch.setenv("AGENT_ENABLED", "false")

    # Act
    body, lambda_client = command("logwork find login")

    # Assert
    assert body["text"] == "Searching Jira tickets..."
    assert json.loads(lambda_client.invoke.call_args.kwargs["Payload"])["_async_task"] == "search_issues"


# --------------------------------------------------------------------------------------
# JQL safety and search
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("keyword,terms", [
    ("login bug", "login bug"),
    ('" OR project = SECRET', "OR project SECRET"),
    ('a\\" OR 1=1', "a OR 1 1"),
    ("it's (fixed)", "it s fixed"),
    ("(*)", ""),
    ("Sửa lỗi đăng nhập", "Sửa lỗi đăng nhập"),
    ("  many   spaces ", "many spaces"),
    (None, ""),
])
def test_tc08_keyword_becomes_plain_terms(keyword, terms):
    # Act & Assert
    assert jql_text_terms(keyword) == terms


def test_tc08_terms_never_contain_jql_delimiters():
    # Arrange
    hostile = "\"'\\+-&|!(){}[]^~*?:/;,<>= x"

    # Act
    terms = jql_text_terms(hostile)

    # Assert
    assert terms == "x"


@pytest.fixture
def jira():
    with patch.object(service_module, "JiraClient") as client_cls:
        client = client_cls.return_value
        client.request.return_value = {"issues": [issue("PROJ-1"), issue("PROJ-2")]}
        yield client


def test_tc09_text_search_request(jira):
    # Act
    issues = JiraService("U1").search_issues("login bug")

    # Assert
    assert [i["key"] for i in issues] == ["PROJ-1", "PROJ-2"]
    jira.request.assert_called_once_with("/rest/api/3/search/jql", method="POST", data={
        "jql": 'text ~ "login bug" ORDER BY updated DESC',
        "fields": ["summary", "status", "timespent"], "maxResults": 21})


def test_tc09_only_one_page_is_fetched(jira):
    # Arrange
    jira.request.return_value = {"issues": [issue("PROJ-1")], "nextPageToken": "more"}

    # Act
    JiraService("U1").search_issues("login")

    # Assert
    assert jira.request.call_count == 1


@pytest.mark.parametrize("keyword", ["PROJ-2", "proj-2", " PROJ-2 "])
def test_tc10_issue_key_is_fetched_first_without_duplicates(jira, keyword):
    # Arrange
    exact = issue("PROJ-2", summary="Exact")

    def respond(path, **kwargs):
        if path == "/rest/api/3/issue/PROJ-2":
            return exact
        return {"issues": [issue("PROJ-1"), issue("PROJ-2")]}

    jira.request.side_effect = respond

    # Act
    issues = JiraService("U1").search_issues(keyword)

    # Assert
    assert [i["key"] for i in issues] == ["PROJ-2", "PROJ-1"]
    assert issues[0]["fields"]["summary"] == "Exact"
    get_call = [c for c in jira.request.call_args_list if c.args[0] == "/rest/api/3/issue/PROJ-2"][0]
    assert get_call.kwargs == {"params": {"fields": "summary,status,timespent"}}


def test_tc11_unknown_issue_key_is_ignored(jira):
    # Arrange
    def respond(path, **kwargs):
        if path.startswith("/rest/api/3/issue/"):
            raise RuntimeError("Jira API returned HTTP 404: not found")
        return {"issues": [issue("NOPE-1", summary="mentions NOPE-1")]}

    jira.request.side_effect = respond

    # Act
    issues = JiraService("U1").search_issues("NOPE-1")

    # Assert
    assert [i["key"] for i in issues] == ["NOPE-1"]


def test_tc11_plain_keyword_does_not_fetch_an_issue(jira):
    # Act
    JiraService("U1").search_issues("login")

    # Assert
    assert [c.args[0] for c in jira.request.call_args_list] == ["/rest/api/3/search/jql"]


def test_tc12_results_are_capped_at_limit_plus_one(jira):
    # Arrange
    jira.request.return_value = {"issues": [issue(f"PROJ-{n}") for n in range(25)]}

    # Act
    issues = JiraService("U1").search_issues("login")

    # Assert
    assert len(issues) == 21


def test_tc12_empty_terms_raise(jira):
    # Act & Assert
    with pytest.raises(ValueError):
        JiraService("U1").search_issues("(*)")
    jira.request.assert_not_called()


# --------------------------------------------------------------------------------------
# Slack blocks
# --------------------------------------------------------------------------------------

def test_tc13_result_rows():
    # Act
    blocks = build_search_result_blocks("login <b>&", [issue("PROJ-1", seconds=5400)], SITE)

    # Assert
    header = blocks[0]["text"]["text"]
    assert "login &lt;b&gt;&amp;" in header and "<b>" not in header
    row = blocks[1]
    assert row["text"]["text"] == (f"<{SITE}/browse/PROJ-1|*PROJ-1 — Fix login*>\n"
                                   "Status: In Progress · Total logged: *1h 30m*")
    assert row["accessory"]["action_id"] == "open_log_time"
    assert row["accessory"]["value"] == "PROJ-1"


def test_tc14_no_match():
    # Act
    blocks = build_search_result_blocks("zzz", [], SITE)

    # Assert
    assert "No Jira tickets match “zzz”" in blocks[1]["text"]["text"]
    assert len(blocks) == 2


def test_tc14_more_than_twenty():
    # Act
    blocks = build_search_result_blocks("login", [issue(f"PROJ-{n}") for n in range(21)], SITE)

    # Assert
    assert len([b for b in blocks if b.get("accessory")]) == 20
    assert "More than 20 tickets match" in blocks[-1]["elements"][0]["text"]


def test_tc14_missing_summary_and_time():
    # Arrange
    bare = {"key": "PROJ-9", "fields": {"summary": None, "status": None, "timespent": None}}

    # Act
    text = build_search_result_blocks("x", [bare], SITE)[1]["text"]["text"]

    # Assert
    assert "(No summary)" in text and "Unknown status" in text and "*0m*" in text


def test_tc15_assigned_list_output_is_unchanged():
    # Act
    blocks = build_assigned_issue_blocks([issue(f"PROJ-{n}") for n in range(21)], SITE)

    # Assert
    assert blocks[0]["text"]["text"] == ("*Your assigned Jira tickets*\n"
                                         "Showing up to 20 tickets, ordered by recently updated.")
    assert blocks[-1]["elements"][0]["text"] == "_Showing 20 of 21 assigned tickets._"
    assert build_assigned_issue_blocks([], SITE)[1]["text"]["text"] == "No Jira tickets are currently assigned to you."


# --------------------------------------------------------------------------------------
# Async task and routing
# --------------------------------------------------------------------------------------

EVENT = {"_async_task": "search_issues", "slack_user_id": "U1", "keyword": "secret project name",
         "response_url": RESPONSE_URL}


def test_tc16_task_posts_results():
    # Arrange
    with patch("jira.tasks.run_search_issues_task.JiraService") as service_cls, \
            patch("jira.tasks.run_search_issues_task.post_slack_response") as post:
        service_cls.return_value.search_issues.return_value = [issue("PROJ-1")]
        service_cls.return_value.site_url = SITE

        # Act
        RunSearchIssuesTask(dict(EVENT)).run()

    # Assert
    service_cls.assert_called_once_with("U1")
    service_cls.return_value.search_issues.assert_called_once_with("secret project name")
    url, message = post.call_args.args
    assert url == RESPONSE_URL
    assert message["replace_original"] is True and message["response_type"] == "ephemeral"
    assert message["blocks"][1]["accessory"]["value"] == "PROJ-1"


def test_tc16_task_failure_reports_without_logging_keyword(caplog):
    # Arrange
    with patch("jira.tasks.run_search_issues_task.JiraService") as service_cls, \
            patch("jira.tasks.run_search_issues_task.post_slack_response") as post, \
            caplog.at_level(logging.ERROR):
        service_cls.return_value.search_issues.side_effect = RuntimeError("Jira API returned HTTP 500")

        # Act
        RunSearchIssuesTask(dict(EVENT)).run()

    # Assert
    assert "Could not search Jira tickets" in post.call_args.args[1]["text"]
    assert "secret project name" not in caplog.text


def test_tc17_async_task_is_routed():
    # Arrange
    with patch("handlers.async_task_handler.RunSearchIssuesTask") as task_cls:
        # Act
        response = handle_async_task(dict(EVENT))

    # Assert
    assert response["statusCode"] == 200
    task_cls.assert_called_once_with(EVENT)
    task_cls.return_value.run.assert_called_once()

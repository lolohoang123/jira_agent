"""Unit tests for the Jira write, the agent allow-list, approval actions and the Slack client,
from docs/testing/jira_write_and_agent_test_matrix.md (TC-01..TC-37). AAA layout; no network.
"""
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from agent import tool_registry
from agent.tool_registry import ALLOWED_INTENTS, execute_plan
from handlers.async_task_handler import handle_async_task
from jira import jira_service as service_module
from jira.jira_service import JiraService
from slack import slack_client
from slack.slack_client import post_slack_response, slack_api

BOT_TOKEN = "xoxb-SECRET-token"
FORM = {"issue_key": "PROJ-12", "date": "2026-11-09", "start_time": "09:00", "duration": "1h 30m",
        "description": "Code review", "remaining_estimate": "leave"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh")
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.delenv("MAX_WORKLOG_MINUTES", raising=False)


def http_response(body):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


# --------------------------------------------------------------------------------------
# Jira worklog write
# --------------------------------------------------------------------------------------

@pytest.fixture
def jira():
    """The JiraClient.request mock behind a JiraService built for Slack user U1."""
    with patch.object(service_module, "JiraClient") as client_cls:
        client_cls.return_value.request.return_value = {"id": "10001"}
        yield client_cls.return_value.request


def make_service(jira):
    service = JiraService("U1")
    assert service.client.request is jira
    return service


def sent(request_mock):
    call = request_mock.call_args
    return call.args[0], call.kwargs


def test_tc01_worklog_payload(jira):
    # Act
    make_service(jira).create_worklog(dict(FORM))

    # Assert
    path, kwargs = sent(jira)
    assert path == "/rest/api/3/issue/PROJ-12/worklog"
    assert kwargs["method"] == "POST"
    assert kwargs["params"] == {"adjustEstimate": "leave"}
    assert kwargs["data"] == {
        "started": "2026-11-09T09:00:00.000+0700", "timeSpent": "1h 30m",
        "comment": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Code review"}]}]}}


@pytest.mark.parametrize("tz,date,expected", [
    ("UTC", "2026-11-09", "2026-11-09T09:00:00.000+0000"),
    ("Asia/Kolkata", "2026-11-09", "2026-11-09T09:00:00.000+0530"),
    ("America/New_York", "2026-11-09", "2026-11-09T09:00:00.000-0500"),
    ("America/New_York", "2026-07-15", "2026-07-15T09:00:00.000-0400"),
])
def test_tc02_started_uses_configured_timezone(jira, monkeypatch, tz, date, expected):
    # Arrange
    monkeypatch.setenv("JIRA_TIMEZONE", tz)

    # Act
    make_service(jira).create_worklog({**FORM, "date": date})

    # Assert
    assert sent(jira)[1]["data"]["started"] == expected


@pytest.mark.parametrize("form", [{**FORM, "description": ""}, {k: v for k, v in FORM.items() if k != "description"}])
def test_tc03_no_comment_without_description(jira, form):
    # Act
    make_service(jira).create_worklog(form)

    # Assert
    assert "comment" not in sent(jira)[1]["data"]


def test_tc04_description_is_sent_unchanged(jira):
    # Arrange
    text = "Sửa lỗi đăng nhập ✅ <b>&</b>\nline 2"

    # Act
    make_service(jira).create_worklog({**FORM, "description": text})

    # Assert
    assert sent(jira)[1]["data"]["comment"]["content"][0]["content"][0]["text"] == text


@pytest.mark.parametrize("extra,params", [
    ({"remaining_estimate": "auto"}, {"adjustEstimate": "auto"}),
    ({"remaining_estimate": "new", "new_estimate": "2h"}, {"adjustEstimate": "new", "newEstimate": "2h"}),
    ({}, {"adjustEstimate": "leave"}),
])
def test_tc05_estimate_parameters(jira, extra, params):
    # Arrange
    form = {k: v for k, v in FORM.items() if k != "remaining_estimate"}
    form.update(extra)

    # Act
    make_service(jira).create_worklog(form)

    # Assert
    assert sent(jira)[1]["params"] == params


@pytest.mark.parametrize("override,message", [
    ({"issue_key": "proj-12"}, "Issue key is invalid"),
    ({"issue_key": "PROJ"}, "Issue key is invalid"),
    ({"issue_key": "PROJ-12 "}, "Issue key is invalid"),
    ({"duration": "0m"}, "greater than 0"),                                  # TC-07
    ({"duration": "25h"}, "at most 24h"),
    ({"duration": "soon"}, "must look like"),
    ({"remaining_estimate": "keep"}, "Invalid remaining estimate"),          # TC-08
    ({"remaining_estimate": "new"}, "Enter a new remaining estimate"),
    ({"date": "2026-02-30"}, "day is out of range"),                         # TC-09
    ({"start_time": "25:00"}, "does not match format"),
])
def test_tc06_to_tc09_invalid_form_never_reaches_jira(jira, override, message):
    # Act & Assert
    with pytest.raises(ValueError, match=message):
        make_service(jira).create_worklog({**FORM, **override})
    jira.assert_not_called()


def test_tc10_jira_response_is_returned(jira):
    # Act & Assert
    assert make_service(jira).create_worklog(dict(FORM)) == {"id": "10001"}


# --------------------------------------------------------------------------------------
# Agent allow-list
# --------------------------------------------------------------------------------------

@pytest.fixture
def deps():
    with patch.object(tool_registry, "DynamoHelper") as dynamo, \
            patch.object(tool_registry, "ApprovalService") as approvals, \
            patch.object(tool_registry, "JiraService") as jira_cls:
        dynamo.return_value.get_jira_account_id_for_slack_user.return_value = "acc-1"
        approvals.return_value.request_worklog_approval.return_value = {"approval_id": "a-1"}
        yield MagicMock(dynamo=dynamo, approvals=approvals.return_value, jira=jira_cls)


def assert_no_side_effects(deps):
    deps.dynamo.assert_not_called()
    deps.approvals.request_worklog_approval.assert_not_called()
    deps.jira.assert_not_called()


@pytest.mark.parametrize("plan", [[], "list my tickets", None, 42])
def test_tc11_plan_must_be_an_object(deps, plan):
    # Act & Assert
    with pytest.raises(ValueError, match="must be an object"):
        execute_plan(plan, "U1", "C1")
    assert_no_side_effects(deps)


@pytest.mark.parametrize("intent", ["create_worklog", "delete_worklog", "__class__", "execute_plan", None, ""])
def test_tc12_intent_outside_allow_list_is_refused(deps, intent):
    # Act & Assert
    with pytest.raises(ValueError, match="not enabled"):
        execute_plan({"intent": intent, "parameters": dict(FORM)}, "U1", "C1")
    assert_no_side_effects(deps)


def test_tc12_allow_list_is_exactly_the_documented_intents():
    # Assert: adding an intent must be a deliberate change (CLAUDE.md)
    assert ALLOWED_INTENTS == {"list_assigned_issues", "get_today_worklogs", "get_issue_worklogs",
                               "prepare_worklog", "clarify", "unknown"}


@pytest.mark.parametrize("parameters", [["PROJ-1"], "PROJ-1", 5])
def test_tc13_parameters_must_be_an_object(deps, parameters):
    # Act & Assert
    with pytest.raises(ValueError, match="parameters must be an object"):
        execute_plan({"intent": "list_assigned_issues", "parameters": parameters}, "U1", "C1")
    assert_no_side_effects(deps)


@pytest.mark.parametrize("intent", ["clarify", "unknown"])
def test_tc14_clarify_and_unknown_only_return_text(deps, intent):
    # Act
    outcome = execute_plan({"intent": intent, "message": "Which issue?"}, "U1", "C1")

    # Assert
    assert outcome == {"intent": intent, "message": "Which issue?"}
    assert_no_side_effects(deps)


@pytest.mark.parametrize("intent,message,expected", [
    ("clarify", None, "Please provide more details."),
    ("unknown", "", "I couldn't understand that Jira request."),
    ("clarify", 123, "123"),
    ("unknown", "x" * 1501, "x" * 1500),
])
def test_tc15_message_defaults_and_limit(deps, intent, message, expected):
    # Act & Assert
    assert execute_plan({"intent": intent, "message": message}, "U1", "C1")["message"] == expected


def test_tc16_prepare_worklog_checks_connection_then_requests_approval(deps):
    # Arrange
    order = []
    deps.dynamo.return_value.get_jira_account_id_for_slack_user.side_effect = lambda u: order.append("connected")
    deps.approvals.request_worklog_approval.side_effect = lambda *a: order.append("approval") or {"approval_id": "a-1"}

    # Act
    outcome = execute_plan({"intent": "prepare_worklog", "parameters": dict(FORM)}, "U1", "C1")

    # Assert
    assert outcome == {"intent": "prepare_worklog", "approval_id": "a-1"}
    assert order == ["connected", "approval"]
    deps.approvals.request_worklog_approval.assert_called_once_with("U1", "C1", FORM)
    deps.jira.assert_not_called()  # the agent never writes to Jira itself


def test_tc17_form_is_normalised(deps):
    # Arrange
    parameters = {k: v for k, v in FORM.items() if k != "remaining_estimate"}
    parameters.update(issue_key=" PROJ-12 ", description=12345, unexpected="ignored")

    # Act
    execute_plan({"intent": "prepare_worklog", "parameters": parameters}, "U1", "C1")

    # Assert
    form = deps.approvals.request_worklog_approval.call_args.args[2]
    assert form == {**FORM, "description": "12345", "remaining_estimate": "leave"}


@pytest.mark.parametrize("override,message", [
    ({"issue_key": None}, "Issue key is invalid"),
    ({"issue_key": "PROJ12"}, "Issue key is invalid"),
    ({"issue_key": 12}, "Issue key is invalid"),
    ({"duration": None}, "must look like"),
    ({"duration": "0m"}, "greater than 0"),
    ({"date": None}, "YYYY-MM-DD"),
    ({"date": "09/11/2026"}, "YYYY-MM-DD"),
    ({"date": "2026-02-30"}, "YYYY-MM-DD"),
    ({"start_time": "9am"}, "HH:MM"),
    ({"start_time": None}, "HH:MM"),
])
def test_tc18_invalid_fields_become_clarify(deps, override, message):
    # Act
    outcome = execute_plan({"intent": "prepare_worklog", "parameters": {**FORM, **override}}, "U1", "C1")

    # Assert
    assert outcome["intent"] == "clarify"
    assert outcome["message"].startswith("I need a little more information before preparing the worklog:")
    assert message in outcome["message"]
    assert_no_side_effects(deps)


@pytest.mark.parametrize("override,message", [
    ({"remaining_estimate": "keep"}, "Invalid remaining_estimate"),
    ({"remaining_estimate": None}, "Invalid remaining_estimate"),
    ({"remaining_estimate": "new"}, "new_estimate is required"),
    ({"remaining_estimate": "new", "new_estimate": "   "}, "new_estimate is required"),
    ({"remaining_estimate": "new", "new_estimate": 2}, "new_estimate is required"),
])
def test_tc19_estimate_rules(deps, override, message):
    # Act
    outcome = execute_plan({"intent": "prepare_worklog", "parameters": {**FORM, **override}}, "U1", "C1")

    # Assert
    assert outcome["intent"] == "clarify" and message in outcome["message"]
    deps.approvals.request_worklog_approval.assert_not_called()


def test_tc19_new_estimate_is_kept_when_valid(deps):
    # Act
    execute_plan({"intent": "prepare_worklog",
                  "parameters": {**FORM, "remaining_estimate": "new", "new_estimate": " 2h "}}, "U1", "C1")

    # Assert
    assert deps.approvals.request_worklog_approval.call_args.args[2]["new_estimate"] == "2h"


def test_tc20_lower_case_issue_key_is_rejected_by_agent(deps):
    # Current behaviour (spec gap GAP-10): the modal upper-cases first, the agent does not.
    # Act
    outcome = execute_plan({"intent": "prepare_worklog", "parameters": {**FORM, "issue_key": "proj-12"}}, "U1", "C1")

    # Assert
    assert outcome["intent"] == "clarify"
    deps.approvals.request_worklog_approval.assert_not_called()


def test_tc21_unconnected_user_gets_no_approval(deps):
    # Arrange
    deps.dynamo.return_value.get_jira_account_id_for_slack_user.side_effect = ValueError("not connected")

    # Act & Assert
    with pytest.raises(ValueError, match="not connected"):
        execute_plan({"intent": "prepare_worklog", "parameters": dict(FORM)}, "U1", "C1")
    deps.approvals.request_worklog_approval.assert_not_called()


def test_tc22_list_assigned_issues(deps):
    # Arrange
    deps.jira.return_value.find_assigned_issues.return_value = [{"key": "PROJ-1"}]

    # Act
    outcome = execute_plan({"intent": "list_assigned_issues"}, "U1", "C1")

    # Assert
    assert outcome == {"intent": "list_assigned_issues", "data": [{"key": "PROJ-1"}]}
    deps.jira.assert_called_once_with("U1")
    deps.jira.return_value.find_assigned_issues.assert_called_once_with("acc-1")


def test_tc23_get_today_worklogs(deps):
    # Arrange
    from datetime import date
    deps.jira.return_value.fetch_today_worklogs.return_value = (date(2026, 11, 9), [{"key": "PROJ-1"}])

    # Act
    outcome = execute_plan({"intent": "get_today_worklogs", "parameters": None}, "U1", "C1")

    # Assert
    assert outcome == {"intent": "get_today_worklogs", "day": "2026-11-09", "data": [{"key": "PROJ-1"}]}
    deps.jira.return_value.fetch_today_worklogs.assert_called_once_with("acc-1")


def test_tc24_get_issue_worklogs_valid_key(deps):
    # Arrange
    deps.jira.return_value.get_issue_worklogs.return_value = [{"id": "1"}]

    # Act
    outcome = execute_plan({"intent": "get_issue_worklogs", "parameters": {"issue_key": " PROJ-7 "}}, "U1", "C1")

    # Assert
    assert outcome == {"intent": "get_issue_worklogs", "issue_key": "PROJ-7", "data": [{"id": "1"}]}
    deps.jira.return_value.get_issue_worklogs.assert_called_once_with("PROJ-7")


@pytest.mark.parametrize("key", [None, "PROJ", "../../admin"])
def test_tc24_get_issue_worklogs_invalid_key(deps, key):
    # Act & Assert
    with pytest.raises(ValueError, match="Issue key is invalid"):
        execute_plan({"intent": "get_issue_worklogs", "parameters": {"issue_key": key}}, "U1", "C1")
    deps.jira.return_value.get_issue_worklogs.assert_not_called()


# --------------------------------------------------------------------------------------
# Approval actions
# --------------------------------------------------------------------------------------

def action_event(decision, message_ts="1731139200.000100"):
    event = {"_async_task": "handle_approval_action", "approval_id": "a-1", "decision": decision,
             "slack_user_id": "U1", "channel_id": "C1"}
    if message_ts:
        event["message_ts"] = message_ts
    return event


@pytest.fixture
def approval_service():
    with patch("approval.approval_service.ApprovalService") as cls:
        yield cls.return_value


@pytest.fixture
def slack():
    with patch("slack.slack_client.slack_api") as fake:
        yield fake


def slack_calls(slack):
    return [(c.args[0], c.args[1]) for c in slack.call_args_list]


def test_tc25_successful_approve_notifies_and_replaces_buttons(approval_service, slack):
    # Arrange
    approval_service.approve_and_execute.return_value = {"ok": True, "result": {"id": "10001"}}

    # Act
    response = handle_async_task(action_event("approve_jira_write"))

    # Assert
    assert response["statusCode"] == 200
    approval_service.approve_and_execute.assert_called_once_with("a-1", "U1")
    (m1, p1), (m2, p2) = slack_calls(slack)
    assert (m1, p1) == ("chat.postEphemeral", {"channel": "C1", "user": "U1",
                                               "text": "✅ Jira worklog created successfully."})
    assert m2 == "chat.update" and p2["ts"] == "1731139200.000100" and p2["channel"] == "C1"
    assert p2["blocks"][0]["text"]["text"] == "✅ Jira worklog created successfully."


def test_tc26_refused_approve_keeps_original_message(approval_service, slack):
    # Arrange
    approval_service.approve_and_execute.return_value = {
        "ok": False, "message": "This approval is expired, already handled, or does not belong to you."}

    # Act
    handle_async_task(action_event("approve_jira_write"))

    # Assert
    assert slack_calls(slack) == [("chat.postEphemeral", {
        "channel": "C1", "user": "U1",
        "text": "⚠️ This approval is expired, already handled, or does not belong to you."})]


def test_tc27_approve_exception_is_reported_not_retried(approval_service, slack, caplog):
    # Arrange
    approval_service.approve_and_execute.side_effect = RuntimeError("Jira API returned HTTP 500")

    # Act
    response = handle_async_task(action_event("approve_jira_write"))

    # Assert
    assert response["statusCode"] == 200
    [(method, payload)] = slack_calls(slack)
    assert method == "chat.postEphemeral"
    assert "Check Jira before submitting again" in payload["text"]
    assert "Approval a-1 could not be completed" in caplog.text


def test_tc28_successful_reject_replaces_buttons(approval_service, slack):
    # Arrange
    approval_service.reject.return_value = {"approval_id": "a-1", "status": "REJECTED"}

    # Act
    handle_async_task(action_event("reject_jira_write"))

    # Assert
    approval_service.reject.assert_called_once_with("a-1", "U1")
    approval_service.approve_and_execute.assert_not_called()
    assert [m for m, _ in slack_calls(slack)] == ["chat.postEphemeral", "chat.update"]
    assert slack_calls(slack)[0][1]["text"] == "🚫 Approval rejected."


def test_tc29_refused_reject_keeps_original_message(approval_service, slack):
    # Arrange
    approval_service.reject.return_value = None

    # Act
    handle_async_task(action_event("reject_jira_write"))

    # Assert
    assert [m for m, _ in slack_calls(slack)] == ["chat.postEphemeral"]
    assert "does not belong to you" in slack_calls(slack)[0][1]["text"]


def test_tc30_no_message_ts_means_no_update(approval_service, slack):
    # Arrange
    approval_service.approve_and_execute.return_value = {"ok": True}

    # Act
    handle_async_task(action_event("approve_jira_write", message_ts=None))

    # Assert
    assert [m for m, _ in slack_calls(slack)] == ["chat.postEphemeral"]


@pytest.mark.parametrize("failures,expected_methods", [
    ([RuntimeError("ephemeral failed"), {"ok": True}], ["chat.postEphemeral", "chat.update"]),
    ([{"ok": True}, RuntimeError("update failed")], ["chat.postEphemeral", "chat.update"]),
])
def test_tc31_slack_failures_are_logged_not_raised(approval_service, slack, caplog, failures, expected_methods):
    # Arrange
    approval_service.approve_and_execute.return_value = {"ok": True}
    slack.side_effect = failures

    # Act
    with caplog.at_level(logging.ERROR):
        response = handle_async_task(action_event("approve_jira_write"))

    # Assert
    assert response["statusCode"] == 200
    assert [m for m, _ in slack_calls(slack)] == expected_methods
    assert "Could not" in caplog.text


@pytest.mark.parametrize("task,runner", [("jira_worklog", "RunTodayWorklogsTask"),
                                         ("list_assigned_issues", "RunListAssignedTask")])
def test_tc32_read_tasks_are_routed(task, runner):
    # Arrange
    event = {"_async_task": task, "slack_user_id": "U1", "account_id": "acc-1",
             "response_url": "https://hooks.slack.com/x"}
    with patch(f"handlers.async_task_handler.{runner}") as runner_cls:
        # Act
        response = handle_async_task(event)

    # Assert
    assert response["statusCode"] == 200
    runner_cls.assert_called_once_with(event)


def test_tc32_unknown_task_is_rejected():
    # Act
    response = handle_async_task({"_async_task": "drop_tables"})

    # Assert
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "unknown task"}


# --------------------------------------------------------------------------------------
# Slack client
# --------------------------------------------------------------------------------------

@pytest.fixture
def urlopen():
    with patch.object(slack_client.urllib.request, "urlopen") as fake:
        fake.return_value = http_response(b'{"ok": true, "ts": "1.2"}')
        yield fake


def test_tc33_slack_api_request(urlopen):
    # Act
    result = slack_api("chat.postMessage", {"channel": "C1", "text": "Xin chào ✅"})

    # Assert
    assert result == {"ok": True, "ts": "1.2"}
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {BOT_TOKEN}"
    assert request.get_header("Content-type") == "application/json; charset=utf-8"
    assert json.loads(request.data) == {"channel": "C1", "text": "Xin chào ✅"}
    assert urlopen.call_args.kwargs == {"timeout": 8}


@pytest.mark.parametrize("body,message", [
    (b'{"ok": false, "error": "channel_not_found"}', "Slack API chat.postMessage failed: channel_not_found"),
    (b'{"ok": false}', "Slack API chat.postMessage failed: unknown_error"),
])
def test_tc34_slack_error_raises_without_token(urlopen, body, message):
    # Arrange
    urlopen.return_value = http_response(body)

    # Act & Assert
    with pytest.raises(RuntimeError) as exc_info:
        slack_api("chat.postMessage", {"channel": "C1"})
    assert str(exc_info.value) == message
    assert BOT_TOKEN not in str(exc_info.value)


def test_tc35_missing_bot_token(urlopen, monkeypatch):
    # Arrange
    monkeypatch.delenv("SLACK_BOT_TOKEN")

    # Act & Assert
    with pytest.raises(KeyError):
        slack_api("chat.postMessage", {"channel": "C1"})
    urlopen.assert_not_called()


@pytest.mark.parametrize("url", ["https://hooks.slack.com/commands/T/1/x", "https://hooks.slack-gov.com/commands/T/1/x"])
def test_tc36_response_url_post(urlopen, url):
    # Act
    post_slack_response(url, {"response_type": "ephemeral", "text": "done"})

    # Assert
    request = urlopen.call_args.args[0]
    assert request.full_url == url and request.get_method() == "POST"
    assert json.loads(request.data) == {"response_type": "ephemeral", "text": "done"}
    assert request.get_header("Authorization") is None  # never send the bot token to a response_url
    assert urlopen.call_args.kwargs == {"timeout": 8}


@pytest.mark.parametrize("url", [
    "http://hooks.slack.com/commands/x",
    "https://evil.example/commands/x",
    "https://hooks.slack.com.evil.example/x",
    "https://hooks.slack.com@evil.example/x",
    "https://slack.com/api/chat.postMessage",
    "",
])
def test_tc37_response_url_host_is_enforced(urlopen, url):
    # Act & Assert
    with pytest.raises(ValueError, match="Invalid Slack response_url host"):
        post_slack_response(url, {"text": "x"})
    urlopen.assert_not_called()

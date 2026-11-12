"""Per-user timezone: resolution rules, JiraService (worklog start, "today", backfill), the agent context,
the Log Time modal default date and Slack users.info. AAA layout; no network, no AWS.
"""
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.orchestrator import AgentOrchestrator
from handlers import slack_action_handler
from handlers.slack_action_handler import handle_block_actions
from jira import jira_service as service_module
from jira.jira_service import JiraService
from slack import slack_client
from slack.slack_client import slack_user_timezone
from util import dynamo_helper
from util.timezone import resolve_timezone_name, user_timezone_name, valid_timezone_name

FORM = {"issue_key": "PROJ-12", "date": "2026-11-09", "start_time": "09:00", "duration": "1h",
        "remaining_estimate": "leave"}
INSTANT = datetime(2026, 11, 8, 17, 30, tzinfo=timezone.utc)  # Tokyo 11-09 02:30, HCM 11-09 00:30, NY 11-08 12:30


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("JIRA_TIMEZONE", "Asia/Ho_Chi_Minh")
    monkeypatch.setenv("OAUTH_USERS_TABLE", "users-test")


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return INSTANT.astimezone(tz)


# --------------------------------------------------------------------------------------
# Resolution rules
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("Asia/Tokyo", "Asia/Tokyo"), (" Europe/Paris ", "Europe/Paris"), ("UTC", "UTC"),
    ("Not/AZone", None), ("", None), (None, None), (123, None), ("../etc/passwd", None), ("A" * 100, None),
])
def test_valid_timezone_name(value, expected):
    assert valid_timezone_name(value) == expected


def test_resolve_takes_first_valid_candidate_then_default(monkeypatch):
    # Act & Assert
    assert resolve_timezone_name("bogus", "Asia/Tokyo", "Europe/Paris") == "Asia/Tokyo"
    assert resolve_timezone_name(None, "") == "Asia/Ho_Chi_Minh"
    monkeypatch.setenv("JIRA_TIMEZONE", "UTC")
    assert resolve_timezone_name(None) == "UTC"


def test_invalid_default_falls_back_to_ho_chi_minh(monkeypatch):
    # Arrange
    monkeypatch.setenv("JIRA_TIMEZONE", "Mars/Olympus")

    # Act & Assert
    assert resolve_timezone_name(None) == "Asia/Ho_Chi_Minh"


def test_user_timezone_name_uses_store_and_never_raises():
    # Arrange
    store = MagicMock()
    store.get_user_timezone_name.return_value = "America/New_York"
    broken = MagicMock()
    broken.get_user_timezone_name.side_effect = RuntimeError("dynamodb down")

    # Act & Assert
    assert user_timezone_name("T1", "U1", users=store) == "America/New_York"
    store.get_user_timezone_name.assert_called_once_with("T1", "U1")
    assert user_timezone_name("T1", "U1", users=broken) == "Asia/Ho_Chi_Minh"


# --------------------------------------------------------------------------------------
# JiraService
# --------------------------------------------------------------------------------------

@pytest.fixture
def client():
    """JiraClient fake with real dict credentials (as DynamoHelper.get_oauth_credentials returns)."""
    with patch.object(service_module, "JiraClient") as client_cls, \
            patch.object(service_module, "DynamoHelper") as helper_cls:
        fake = client_cls.return_value
        fake.credentials = {"access_token": "a", "cloud_id": "c", "jira_site_url": "https://x", "time_zone": None}
        fake.request.return_value = {"id": "10001"}
        fake.users = helper_cls.return_value
        yield fake


@pytest.mark.parametrize("tz,expected", [
    ("America/New_York", "2026-11-09T09:00:00.000-0500"),
    ("Asia/Tokyo", "2026-11-09T09:00:00.000+0900"),
])
def test_worklog_start_uses_the_users_timezone(client, tz, expected):
    # Arrange
    client.credentials["time_zone"] = tz

    # Act
    JiraService("T1", "U1").create_worklog(dict(FORM))

    # Assert
    assert client.request.call_count == 1  # no backfill call
    assert client.request.call_args.kwargs["data"]["started"] == expected
    client.users.set_user_timezone.assert_not_called()


def test_missing_timezone_is_backfilled_from_jira_once(client):
    # Arrange
    client.request.side_effect = [{"accountId": "acc", "timeZone": "Europe/Paris"}, {"id": "1"}, {"id": "2"}]
    service = JiraService("T1", "U1")

    # Act
    service.create_worklog(dict(FORM))
    service.create_worklog(dict(FORM))

    # Assert
    paths = [c.args[0] for c in client.request.call_args_list]
    assert paths == ["/rest/api/3/myself", "/rest/api/3/issue/PROJ-12/worklog", "/rest/api/3/issue/PROJ-12/worklog"]
    assert client.request.call_args.kwargs["data"]["started"] == "2026-11-09T09:00:00.000+0100"
    client.users.set_user_timezone.assert_called_once_with("T1", "U1", "Europe/Paris")


@pytest.mark.parametrize("myself", [RuntimeError("Jira API returned HTTP 500"), {"accountId": "acc"}])
def test_failed_backfill_uses_default_and_stores_nothing(client, myself):
    # Arrange
    client.request.side_effect = [myself, {"id": "1"}]

    # Act
    JiraService("T1", "U1").create_worklog(dict(FORM))

    # Assert
    assert client.request.call_args.kwargs["data"]["started"] == "2026-11-09T09:00:00.000+0700"
    client.users.set_user_timezone.assert_not_called()


@pytest.mark.parametrize("tz,day", [("America/New_York", "2026/11/08"), ("Asia/Tokyo", "2026/11/09")])
def test_today_is_the_users_today(client, tz, day):
    # Arrange
    client.credentials["time_zone"] = tz
    client.request.return_value = {"issues": []}
    with patch.object(service_module, "datetime", _FixedDateTime):
        # Act
        result_day, entries = JiraService("T1", "U1").fetch_today_worklogs("acc")

    # Assert
    assert result_day.strftime("%Y/%m/%d") == day and entries == []
    assert f'worklogDate = "{day}"' in client.request.call_args.kwargs["data"]["jql"]


# --------------------------------------------------------------------------------------
# Agent context and Log Time modal
# --------------------------------------------------------------------------------------

def test_agent_context_uses_the_users_timezone():
    # Arrange
    llm = MagicMock()
    lookup = MagicMock(return_value="America/New_York")
    agent = AgentOrchestrator(llm_client=llm, timezone_lookup=lookup)
    with patch("agent.orchestrator.execute_plan", return_value={"intent": "clarify", "message": "ok"}), \
            patch("agent.orchestrator.datetime", _FixedDateTime):
        # Act
        result = agent.handle("U1", "what did I log today", "C1", "T1")

    # Assert
    lookup.assert_called_once_with("T1", "U1")
    context = llm.generate_plan.call_args.args[1]
    assert "Current date: 2026-11-08" in context and "Timezone: America/New_York" in context
    assert result.message == "ok"


@pytest.mark.real_timezone_lookup
def test_log_time_modal_defaults_to_the_users_today(monkeypatch):
    # Arrange
    monkeypatch.setattr(dynamo_helper, "_dynamodb_resource", MagicMock())
    monkeypatch.setattr(dynamo_helper.DynamoHelper, "get_user_timezone_name",
                        lambda self, team_id, user_id: "America/New_York")
    payload = {"type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"}, "channel": {"id": "C1"},
               "trigger_id": "trig-1", "actions": [{"action_id": "open_log_time", "value": "PROJ-7"}]}
    with patch.object(slack_action_handler, "datetime", _FixedDateTime), \
            patch.object(slack_action_handler, "slack_api") as slack_api:
        # Act
        handle_block_actions(payload, MagicMock(), MagicMock())

    # Assert
    view = slack_api.call_args.args[1]["view"]
    assert view["blocks"][1]["element"]["initial_date"] == "2026-11-08"


# --------------------------------------------------------------------------------------
# Slack users.info
# --------------------------------------------------------------------------------------

def _http(body):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return response


def test_slack_user_timezone_reads_tz_with_the_workspace_token():
    # Arrange
    store = MagicMock()
    store.get_bot_token.return_value = "xoxb-T1"
    with patch.object(slack_client.urllib.request, "urlopen",
                      return_value=_http({"ok": True, "user": {"tz": "Asia/Tokyo"}})) as urlopen:
        # Act
        tz = slack_user_timezone("T1", "U1", store=store)

    # Assert
    assert tz == "Asia/Tokyo"
    store.get_bot_token.assert_called_once_with("T1")
    request = urlopen.call_args.args[0]
    assert request.full_url == "https://slack.com/api/users.info"
    assert request.data == b"user=U1"
    assert request.headers["Authorization"] == "Bearer xoxb-T1"


@pytest.mark.parametrize("outcome", [_http({"ok": False, "error": "missing_scope"}), OSError("network")])
def test_slack_user_timezone_failures_return_none(outcome):
    # Arrange
    store = MagicMock()
    kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    with patch.object(slack_client.urllib.request, "urlopen", **kwargs):
        # Act & Assert
        assert slack_user_timezone("T1", "U1", store=store) is None

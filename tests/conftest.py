"""Shared test fixtures. Tests must never reach real AWS, Slack or Jira.

- ``_fake_jira_cloud_id``: ApprovalService looks up the user's Jira site when creating an approval.
- ``installed_workspace``: every Slack workspace is "installed" by default; the installations table is replaced
  by a fake that returns BOT_TOKEN for any team. Tests of the installation store itself, or of the
  "not installed" path, use the ``real_installation_store`` marker to opt out.
"""
from unittest.mock import patch

import pytest

from slack import installation_store

TEST_CLOUD_ID = "cloud-test"
TEAM = "T1"
BOT_TOKEN = "xoxb-SECRET-token"


def pytest_configure(config):
    config.addinivalue_line("markers", "real_installation_store: do not fake Slack installations")
    config.addinivalue_line("markers", "real_timezone_lookup: do not fake per-user timezone lookups")


@pytest.fixture(autouse=True)
def _fake_jira_cloud_id():
    """ApprovalService looks up the user's Jira site when creating an approval; never hit DynamoDB in tests."""
    with patch("approval.approval_service.current_jira_cloud_id", return_value=TEST_CLOUD_ID) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _no_timezone_lookups(request, monkeypatch):
    """Per-user timezone lookups never reach DynamoDB or Slack: no stored timezone, no Slack profile tz,
    so the JIRA_TIMEZONE default applies. Tests of the lookups use the ``real_timezone_lookup`` marker."""
    if request.node.get_closest_marker("real_timezone_lookup"):
        return
    from jira import authentication_jira
    from util.dynamo_helper import DynamoHelper
    monkeypatch.setattr(DynamoHelper, "get_user_timezone_name", lambda self, team_id, slack_user_id: None)
    monkeypatch.setattr(authentication_jira, "slack_user_timezone", lambda team_id, slack_user_id: None)


@pytest.fixture(autouse=True)
def installed_workspace(request, monkeypatch):
    installation_store.clear_cache()
    if request.node.get_closest_marker("real_installation_store"):
        yield None
        return

    def fake_token(self, team_id):
        if not team_id:
            raise installation_store.SlackNotInstalled(team_id)
        return BOT_TOKEN

    monkeypatch.setattr(installation_store.InstallationStore, "__init__", lambda self, table=None: None)
    monkeypatch.setattr(installation_store.InstallationStore, "get_bot_token", fake_token)
    yield BOT_TOKEN
    installation_store.clear_cache()

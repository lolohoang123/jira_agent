from unittest.mock import patch

import pytest

TEST_CLOUD_ID = "cloud-test"


@pytest.fixture(autouse=True)
def _fake_jira_cloud_id():
    """ApprovalService looks up the user's Jira site when creating an approval; never hit DynamoDB in tests."""
    with patch("approval.approval_service.current_jira_cloud_id", return_value=TEST_CLOUD_ID) as mock:
        yield mock

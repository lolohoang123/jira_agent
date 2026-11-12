"""One-off migration scripts/encrypt_existing_secrets.py: encrypts only plaintext values, never overwrites a
concurrent refresh, never prints values. Fake table and fake seal; no AWS."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "encrypt_existing_secrets.py"
_spec = importlib.util.spec_from_file_location("encrypt_existing_secrets", SCRIPT)
migration = importlib.util.module_from_spec(_spec)
sys.modules["encrypt_existing_secrets"] = migration
_spec.loader.exec_module(migration)

CONNECTIONS = migration.SPECS[0]
SEALED = {"v": 1, "kid": "k", "ct": b"old"}


def fake_seal(value, table, field, **ids):
    return {"v": 1, "kid": "k", "ct": f"{table}|{field}|{ids}".encode()}


def table_with(pages):
    table = MagicMock()
    table.scan.side_effect = pages
    return table


def test_dry_run_counts_without_writing():
    # Arrange
    table = table_with([{"Items": [
        {"team_id": "T1", "slack_user_id": "U1", "accessToken": "plain-a", "refreshToken": "plain-r"},
        {"team_id": "T1", "slack_user_id": "U2", "accessToken": SEALED, "refreshToken": SEALED},
    ]}])

    # Act
    counts = migration.migrate_table(table, CONNECTIONS, apply=False, seal=fake_seal)

    # Assert
    assert (counts.rows, counts.plaintext, counts.already_sealed, counts.encrypted) == (2, 2, 2, 0)
    assert counts.per_field == {"accessToken": 1, "refreshToken": 1}
    table.update_item.assert_not_called()


def test_apply_encrypts_with_row_context_and_plaintext_condition():
    # Arrange
    table = table_with([{"Items": [{"team_id": "T1", "slack_user_id": "U1", "accessToken": "plain-a"}],
                         "LastEvaluatedKey": {"k": 1}},
                        {"Items": [{"team_id": "T2", "slack_user_id": "U9", "refreshToken": "plain-r"}]}])

    # Act
    counts = migration.migrate_table(table, CONNECTIONS, apply=True, seal=fake_seal)

    # Assert
    assert counts.encrypted == 2
    first = table.update_item.call_args_list[0].kwargs
    assert first["Key"] == {"team_id": "T1", "slack_user_id": "U1"}
    assert first["ConditionExpression"] == "#f = :plaintext"
    assert first["ExpressionAttributeNames"] == {"#f": "accessToken"}
    assert first["ExpressionAttributeValues"][":sealed"]["ct"] == (
        b"jira-connections|accessToken|{'team_id': 'T1', 'slack_user_id': 'U1'}")
    assert table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"k": 1}


def test_concurrent_refresh_is_not_overwritten():
    # Arrange: the Lambda refreshed (and sealed) the token between scan and update
    table = table_with([{"Items": [{"team_id": "T1", "slack_user_id": "U1", "accessToken": "plain-a"}]}])
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    # Act
    counts = migration.migrate_table(table, CONNECTIONS, apply=True, seal=fake_seal)

    # Assert
    assert counts.encrypted == 0 and counts.changed_meanwhile == 1


def test_other_errors_stop_the_migration():
    # Arrange
    table = table_with([{"Items": [{"team_id": "T1", "slack_user_id": "U1", "accessToken": "plain-a"}]}])
    table.update_item.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "UpdateItem")

    # Act & Assert
    with pytest.raises(ClientError):
        migration.migrate_table(table, CONNECTIONS, apply=True, seal=fake_seal)


def test_installations_spec():
    # Act
    spec = migration.SPECS[1]

    # Assert: same logical names as the runtime code uses in the encryption context
    from slack.installation_store import INSTALLATIONS_TABLE
    from util.dynamo_helper import CONNECTIONS_TABLE
    assert (spec.logical_name, spec.key_attributes, spec.secret_fields) == (INSTALLATIONS_TABLE, ("team_id",),
                                                                            ("bot_token",))
    assert CONNECTIONS.logical_name == CONNECTIONS_TABLE

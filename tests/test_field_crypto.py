"""Field-level KMS encryption of stored secrets (Jira OAuth tokens, Slack bot tokens).

A fake KMS enforces the encryption context exactly like KMS does: a ciphertext only decrypts with the context
(and key) it was encrypted with. AAA layout; no network.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.types import Binary, TypeSerializer
from botocore.exceptions import ClientError

from slack.installation_store import InstallationStore
from util import dynamo_helper, field_crypto
from util.dynamo_helper import DynamoHelper
from util.field_crypto import FieldEncryptionError, seal, unseal

KEY = "arn:aws:kms:ap-southeast-1:270573552412:key/test-key"
NOW = 1_700_000_000
ACCESS, REFRESH, BOT = "access-SECRET-123", "refresh-SECRET-456", "xoxb-SECRET-789"


class FakeKms:
    def __init__(self):
        self.blobs = {}
        self.encrypt_calls = 0
        self.decrypt_calls = 0
        self.fail = None

    def encrypt(self, KeyId, Plaintext, EncryptionContext):
        if self.fail:
            raise self.fail
        self.encrypt_calls += 1
        blob = f"blob-{len(self.blobs)}".encode()
        self.blobs[blob] = (KeyId, dict(EncryptionContext), Plaintext)
        return {"CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(self, CiphertextBlob, EncryptionContext, KeyId=None):
        if self.fail:
            raise self.fail
        self.decrypt_calls += 1
        key, context, plaintext = self.blobs.get(bytes(CiphertextBlob), (None, None, None))
        if key is None or context != EncryptionContext or (KeyId and KeyId != key):
            raise ClientError({"Error": {"Code": "InvalidCiphertextException", "Message": "context mismatch"}},
                              "Decrypt")
        return {"Plaintext": plaintext, "KeyId": key}


@pytest.fixture
def kms(monkeypatch):
    fake = FakeKms()
    field_crypto.set_kms_client(fake)
    monkeypatch.setenv("FIELD_ENCRYPTION", "kms")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY_ARN", KEY)
    yield fake
    field_crypto.set_kms_client(None)


IDS = {"team_id": "T1", "slack_user_id": "U1"}


# --------------------------------------------------------------------------------------
# seal / unseal
# --------------------------------------------------------------------------------------

def test_disabled_encryption_stores_values_as_given(monkeypatch):
    # Arrange
    monkeypatch.delenv("FIELD_ENCRYPTION", raising=False)

    # Act & Assert
    assert seal(ACCESS, "jira-connections", "accessToken", **IDS) == ACCESS


def test_seal_produces_versioned_ciphertext_bound_to_the_row(kms):
    # Act
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)

    # Assert
    assert sealed == {"v": 1, "kid": KEY, "ct": b"blob-0"}
    key, context, plaintext = kms.blobs[b"blob-0"]
    assert key == KEY and plaintext == ACCESS.encode()
    assert context == {"app": "jira-agent", "table": "jira-connections", "field": "accessToken",
                       "team_id": "T1", "slack_user_id": "U1"}
    assert ACCESS not in json.dumps({k: str(v) for k, v in sealed.items()})


def test_unseal_round_trip_and_cache(kms):
    # Arrange
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)
    field_crypto.clear_cache()

    # Act
    first = unseal(sealed, "jira-connections", "accessToken", **IDS)
    second = unseal(sealed, "jira-connections", "accessToken", **IDS)

    # Assert: one KMS call, the second read comes from the in-memory cache
    assert first == second == ACCESS
    assert kms.decrypt_calls == 1


def test_unseal_accepts_dynamodb_binary(kms):
    # Arrange: boto3 returns stored binaries as Binary(...)
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)
    field_crypto.clear_cache()
    stored = {**sealed, "ct": Binary(sealed["ct"])}

    # Act & Assert
    assert unseal(stored, "jira-connections", "accessToken", **IDS) == ACCESS


@pytest.mark.parametrize("other", [
    {"team_id": "T2", "slack_user_id": "U1"},      # same user id, another workspace
    {"team_id": "T1", "slack_user_id": "U2"},      # another user
])
def test_ciphertext_copied_to_another_row_cannot_be_decrypted(kms, other):
    # Arrange
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)
    field_crypto.clear_cache()

    # Act & Assert
    with pytest.raises(FieldEncryptionError) as exc_info:
        unseal(sealed, "jira-connections", "accessToken", **other)
    assert ACCESS not in str(exc_info.value) and "context mismatch" not in repr(exc_info.value.__cause__)


def test_ciphertext_of_another_field_cannot_be_decrypted(kms):
    # Arrange: a refresh token pasted into the accessToken field
    sealed = seal(REFRESH, "jira-connections", "refreshToken", **IDS)
    field_crypto.clear_cache()

    # Act & Assert
    with pytest.raises(FieldEncryptionError):
        unseal(sealed, "jira-connections", "accessToken", **IDS)


@pytest.mark.parametrize("stored", [None, ACCESS, ""])
def test_legacy_plaintext_and_empty_values_are_returned_as_is(kms, stored):
    # Act & Assert: rows written before encryption keep working
    assert unseal(stored, "jira-connections", "accessToken", **IDS) == stored
    assert kms.decrypt_calls == 0


def test_unknown_format_is_rejected(kms):
    # Act & Assert
    with pytest.raises(FieldEncryptionError, match="Unsupported stored format"):
        unseal({"v": 99, "ct": b"x"}, "jira-connections", "accessToken", **IDS)


def test_fails_closed_without_key_arn(kms, monkeypatch):
    # Arrange
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_ARN")

    # Act & Assert: never falls back to storing plaintext
    with pytest.raises(FieldEncryptionError, match="FIELD_ENCRYPTION_KEY_ARN"):
        seal(ACCESS, "jira-connections", "accessToken", **IDS)
    assert kms.encrypt_calls == 0


@pytest.mark.parametrize("operation", ["seal", "unseal"])
def test_kms_errors_are_wrapped_without_details(kms, operation):
    # Arrange
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)
    field_crypto.clear_cache()
    kms.fail = ClientError({"Error": {"Code": "AccessDeniedException", "Message": f"denied for {ACCESS}"}},
                           "Encrypt")

    # Act & Assert
    with pytest.raises(FieldEncryptionError) as exc_info:
        if operation == "seal":
            seal(REFRESH, "jira-connections", "refreshToken", **IDS)
        else:
            unseal(sealed, "jira-connections", "accessToken", **IDS)
    assert str(exc_info.value.__cause__) == "AccessDeniedException"
    assert ACCESS not in str(exc_info.value) and ACCESS not in str(exc_info.value.__cause__)


@pytest.mark.parametrize("ids", [{"team_id": "", "slack_user_id": "U1"}, {"team_id": "T1", "slack_user_id": None}])
def test_context_requires_every_id(kms, ids):
    # Act & Assert
    with pytest.raises(FieldEncryptionError, match="Missing"):
        seal(ACCESS, "jira-connections", "accessToken", **ids)


def test_sealed_value_is_stored_as_a_dynamodb_map_with_binary(kms):
    # Act
    serialized = TypeSerializer().serialize(seal(ACCESS, "jira-connections", "accessToken", **IDS))

    # Assert
    assert serialized == {"M": {"v": {"N": "1"}, "kid": {"S": KEY}, "ct": {"B": b"blob-0"}}}


def test_already_sealed_values_are_not_encrypted_twice(kms):
    # Arrange
    sealed = seal(ACCESS, "jira-connections", "accessToken", **IDS)

    # Act & Assert
    assert seal(sealed, "jira-connections", "accessToken", **IDS) is sealed
    assert kms.encrypt_calls == 1


# --------------------------------------------------------------------------------------
# Where the tokens are written and read
# --------------------------------------------------------------------------------------

@pytest.fixture
def users_table():
    with patch.object(dynamo_helper, "_dynamodb_resource") as factory:
        yield factory.return_value.Table.return_value


def test_credentials_are_decrypted_for_jira_calls(kms, users_table):
    # Arrange: a row written with encryption on
    row = {"team_id": "T1", "slack_user_id": "U1", "jiraCloudId": "cloud-1", "connected": True,
           "accessTokenExpiresAt": 4_000_000_000,
           "accessToken": seal(ACCESS, "jira-connections", "accessToken", **IDS)}
    field_crypto.clear_cache()
    users_table.get_item.return_value = {"Item": row}

    # Act
    credentials = DynamoHelper("OAUTH_USERS_TABLE").get_oauth_credentials("T1", "U1")

    # Assert
    assert credentials["access_token"] == ACCESS
    assert kms.decrypt_calls == 1


def test_refresh_reads_sealed_refresh_token_and_stores_sealed_tokens(kms, users_table):
    # Arrange: expired access token, sealed refresh token
    stale = {"team_id": "T1", "slack_user_id": "U1", "jiraCloudId": "cloud-1", "accessTokenExpiresAt": NOW - 5,
             "refreshToken": seal(REFRESH, "jira-connections", "refreshToken", **IDS)}
    field_crypto.clear_cache()
    users_table.get_item.side_effect = [{"Item": stale}, {"Item": stale}]
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"access_token": "access-NEW", "refresh_token": "refresh-NEW", "expires_in": 3600}).encode()
    with patch.object(dynamo_helper, "time") as fake_time, \
            patch.object(dynamo_helper, "urlopen", return_value=response) as urlopen, \
            patch.dict("os.environ", {"ATLASSIAN_CLIENT_ID": "cid", "ATLASSIAN_CLIENT_SECRET": "csec"}):
        fake_time.time.return_value = NOW
        fake_time.monotonic.return_value = 0

        # Act
        DynamoHelper("OAUTH_USERS_TABLE").refresh_access_token("T1", "U1")

    # Assert: Atlassian receives the decrypted refresh token; DynamoDB receives only ciphertexts
    assert json.loads(urlopen.call_args.args[0].data)["refresh_token"] == REFRESH
    stored = users_table.update_item.call_args_list[1].kwargs["ExpressionAttributeValues"]
    assert stored[":access_token"]["v"] == 1 and stored[":refresh_token"]["v"] == 1
    assert "access-NEW" not in repr(stored) and "refresh-NEW" not in repr(stored)
    assert kms.blobs[stored[":refresh_token"]["ct"]][1]["field"] == "refreshToken"


def test_oauth_callback_stores_only_ciphertexts(kms):
    # Arrange
    from jira import authentication_jira as auth
    helpers = {"OAUTH_STATES_TABLE": MagicMock(), "OAUTH_USERS_TABLE": MagicMock()}
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.return_value = ("T1", "U1")
    responses = {"token": {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600},
                 "resources": [{"id": "cloud-1", "url": "https://acme.atlassian.net", "name": "Acme"}],
                 "myself": {"accountId": "acc-1"}}

    def fake_request(self, url, method="GET", data=None, access_token=None):
        if url.endswith("/oauth/token"):
            return responses["token"]
        return responses["resources"] if url.endswith("accessible-resources") else responses["myself"]

    with patch.object(auth, "DynamoHelper", side_effect=lambda name: helpers[name]), \
            patch.object(auth.AuthenticationJira, "atlassian_json_request", fake_request), \
            patch.object(auth.time, "time", return_value=NOW):
        # Act
        page = auth.AuthenticationJira({"queryStringParameters": {"code": "c", "state": "s"}}).finish_oauth_callback()

    # Assert
    assert page["statusCode"] == 200
    item = helpers["OAUTH_USERS_TABLE"].oauth_table.return_value.put_item.call_args.kwargs["Item"]
    assert item["accessToken"]["v"] == 1 and item["refreshToken"]["v"] == 1
    assert ACCESS not in repr(item) and REFRESH not in repr(item)


def test_oauth_callback_fails_closed_when_kms_fails(kms):
    # Arrange
    from jira import authentication_jira as auth
    helpers = {"OAUTH_STATES_TABLE": MagicMock(), "OAUTH_USERS_TABLE": MagicMock()}
    helpers["OAUTH_STATES_TABLE"].consume_oauth_state.return_value = ("T1", "U1")
    kms.fail = ClientError({"Error": {"Code": "KMSInvalidStateException"}}, "Encrypt")

    def fake_request(self, url, method="GET", data=None, access_token=None):
        if url.endswith("/oauth/token"):
            return {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 3600}
        if url.endswith("accessible-resources"):
            return [{"id": "cloud-1", "url": "https://acme.atlassian.net"}]
        return {"accountId": "acc-1"}

    with patch.object(auth, "DynamoHelper", side_effect=lambda name: helpers[name]), \
            patch.object(auth.AuthenticationJira, "atlassian_json_request", fake_request):
        # Act
        page = auth.AuthenticationJira({"queryStringParameters": {"code": "c", "state": "s"}}).finish_oauth_callback()

    # Assert: nothing stored, generic error page
    assert page["statusCode"] == 500
    helpers["OAUTH_USERS_TABLE"].oauth_table.return_value.put_item.assert_not_called()


@pytest.mark.real_installation_store
def test_installation_bot_token_is_sealed_and_decrypted(kms):
    # Arrange
    table = MagicMock()
    store = InstallationStore(table)

    # Act
    item = store.save({"team_id": "T1", "bot_token": BOT})
    table.get_item.return_value = {"Item": {"bot_token": item["bot_token"]}}
    field_crypto.clear_cache()
    from slack import installation_store
    installation_store.clear_cache()
    token = store.get_bot_token("T1")

    # Assert
    assert item["bot_token"]["v"] == 1 and BOT not in repr(item)
    assert kms.blobs[item["bot_token"]["ct"]][1] == {"app": "jira-agent", "table": "installations",
                                                     "field": "bot_token", "team_id": "T1"}
    assert token == BOT


@pytest.mark.real_installation_store
def test_legacy_plaintext_bot_token_still_works(kms):
    # Arrange: installed before encryption was enabled
    table = MagicMock()
    table.get_item.return_value = {"Item": {"bot_token": BOT}}

    # Act & Assert
    assert InstallationStore(table).get_bot_token("T1") == BOT

"""Field-level encryption of secrets stored in DynamoDB (OAuth tokens, Slack bot tokens) with AWS KMS.

Each value is encrypted directly with ``kms:Encrypt`` (values must stay under 4 KB, which tokens do) under the
stage's customer managed key, with an encryption context that binds the ciphertext to its row and field:

    {"app": "jira-agent", "table": "jira-connections", "field": "accessToken", "team_id": "T1", "slack_user_id": "U1"}

A ciphertext copied to another row or field therefore cannot be decrypted, and CloudTrail shows which row was
decrypted. Stored format (a DynamoDB map): ``{"v": 1, "kid": <key arn>, "ct": <binary ciphertext>}``.

Configuration:
- ``FIELD_ENCRYPTION=kms`` (set by template.yaml) with ``FIELD_ENCRYPTION_KEY_ARN``: encrypt every write. A missing
  key ARN fails closed: nothing is ever written in plaintext by mistake.
- unset / ``off`` (local development and tests): values are stored as given.
Reads accept both formats, so rows written before encryption keep working until they are re-encrypted.
Never log plaintext or ciphertext values.
"""
import hashlib
import os
from collections import OrderedDict

import boto3
from botocore.exceptions import BotoCoreError, ClientError

APP = "jira-agent"      # fixed part of every encryption context (the IAM policy requires it)
FORMAT_VERSION = 1
_CACHE_SIZE = 256       # decrypted values kept per container, to avoid a KMS call on every request

_kms_client = None
_plaintext_cache = OrderedDict()  # sha256(ciphertext) -> plaintext, per Lambda container


class FieldEncryptionError(RuntimeError):
    """Encrypting or decrypting a stored secret failed. The message never contains the value."""


def encryption_enabled():
    """True when FIELD_ENCRYPTION=kms (deployed stacks); off for local development and tests."""
    return os.environ.get("FIELD_ENCRYPTION", "off").strip().lower() == "kms"


def set_kms_client(client):
    """Inject a KMS client (tests); ``None`` resets to the default boto3 client."""
    global _kms_client
    _kms_client = client
    _plaintext_cache.clear()


def clear_cache():
    """Forget cached plaintexts (tests)."""
    _plaintext_cache.clear()


def encryption_context(table, field, **ids):
    """Context that binds a ciphertext to app + table + field + row ids; every id must be non-empty."""
    context = {"app": APP, "table": table, "field": field}
    for name, value in ids.items():
        if not value:
            raise FieldEncryptionError(f"Missing {name} for the encryption context of {table}.{field}")
        context[name] = str(value)
    return context


def is_sealed(value):
    """True for a value in the stored encrypted format ({"v": 1, "ct": ...})."""
    return isinstance(value, dict) and value.get("v") == FORMAT_VERSION and "ct" in value


def seal(value, table, field, **ids):
    """Encrypt ``value`` for storage in ``table.field`` of the row identified by ``ids``."""
    # Nothing to encrypt, already encrypted, or encryption switched off (local/tests).
    if value is None or value == "" or is_sealed(value):
        return value
    if not encryption_enabled():
        return value
    # Fail closed: with encryption on, a missing key must stop the write instead of storing plaintext.
    key_arn = os.environ.get("FIELD_ENCRYPTION_KEY_ARN", "").strip()
    if not key_arn:
        raise FieldEncryptionError("FIELD_ENCRYPTION=kms but FIELD_ENCRYPTION_KEY_ARN is not set")
    context = encryption_context(table, field, **ids)
    try:
        response = _kms().encrypt(KeyId=key_arn, Plaintext=str(value).encode("utf-8"), EncryptionContext=context)
    except (ClientError, BotoCoreError) as exc:
        raise FieldEncryptionError(f"Could not encrypt {table}.{field}") from _without_details(exc)
    blob = response["CiphertextBlob"]
    _remember(blob, str(value))  # the next unseal of this blob needs no KMS call
    return {"v": FORMAT_VERSION, "kid": response.get("KeyId", key_arn), "ct": blob}


def unseal(stored, table, field, **ids):
    """Return the plaintext of a stored value; plaintext rows written before encryption are returned as is."""
    # Legacy plaintext rows (written before encryption) are returned unchanged.
    if stored is None or isinstance(stored, str):
        return stored
    if not is_sealed(stored):
        raise FieldEncryptionError(f"Unsupported stored format for {table}.{field}")
    blob = _bytes(stored["ct"])
    cached = _recall(blob)
    if cached is not None:
        return cached
    # KMS checks the context: a ciphertext moved to another row or field fails to decrypt.
    context = encryption_context(table, field, **ids)
    kwargs = {"CiphertextBlob": blob, "EncryptionContext": context}
    if stored.get("kid"):
        kwargs["KeyId"] = stored["kid"]  # pin the key: a blob for another key is refused
    try:
        response = _kms().decrypt(**kwargs)
    except (ClientError, BotoCoreError) as exc:
        raise FieldEncryptionError(f"Could not decrypt {table}.{field}") from _without_details(exc)
    plaintext = response["Plaintext"].decode("utf-8")
    _remember(blob, plaintext)
    return plaintext


def _kms():
    """Lazily created KMS client (or the one injected by tests)."""
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _bytes(value):
    """Raw bytes of a ciphertext."""
    return bytes(getattr(value, "value", value))  # boto3 returns DynamoDB binaries as Binary(value=...)


def _cache_key(blob):
    """Cache key = hash of the ciphertext (the ciphertext itself is not kept as a key)."""
    return hashlib.sha256(_bytes(blob)).hexdigest()


def _remember(blob, plaintext):
    """Store a plaintext in the LRU cache, evicting the oldest entries above _CACHE_SIZE."""
    key = _cache_key(blob)
    _plaintext_cache[key] = plaintext
    _plaintext_cache.move_to_end(key)
    while len(_plaintext_cache) > _CACHE_SIZE:
        _plaintext_cache.popitem(last=False)


def _recall(blob):
    """Cached plaintext for this ciphertext, or None."""
    key = _cache_key(blob)
    if key in _plaintext_cache:
        _plaintext_cache.move_to_end(key)
        return _plaintext_cache[key]
    return None


def _without_details(exc):
    """Keep the AWS error code for diagnosis, drop everything else from the exception chain."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
    return RuntimeError(code or type(exc).__name__)

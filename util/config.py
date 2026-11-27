"""Secret loading from AWS Systems Manager Parameter Store (SecureString).

Production: set ``SSM_PARAMETER_PREFIX`` (e.g. ``/jira-agent/prod/``). Each secret is read from
``<prefix><name in kebab-case>`` with decryption, e.g. ``/jira-agent/prod/slack-signing-secret``.
All secrets are fetched in one ``GetParameters`` call and cached for ``SECRET_CACHE_SECONDS``
(default 300) so rotated values are picked up without a redeploy.

Local development and tests: leave ``SSM_PARAMETER_PREFIX`` unset and the value is read from the
environment variable with the same name (``SLACK_SIGNING_SECRET``...). When a prefix is set,
environment variables are ignored, so a stale plaintext value can never shadow the parameter.

Never log the returned values.
"""
import logging
import os
import threading
import time

import boto3

logger = logging.getLogger(__name__)

# Bot tokens are per workspace (installations table, Slack OAuth v2), so there is no global bot token secret.
SECRET_NAMES = ("SLACK_SIGNING_SECRET", "SLACK_CLIENT_SECRET", "ATLASSIAN_CLIENT_SECRET", "ASYNC_TASK_SECRET")
DEFAULT_CACHE_SECONDS = 300
_MISSING = object()

# Process-wide cache shared by all invocations of a warm Lambda container.
_lock = threading.Lock()
_cache = {}          # secret name -> value, plus "__prefix__" (the prefix the values were loaded for)
_loaded_at = None    # time.monotonic() of the last SSM load
_ssm_client = None


def parameter_name(prefix, name):
    """``/jira-agent/prod/`` + ``SLACK_SIGNING_SECRET`` -> ``/jira-agent/prod/slack-signing-secret``."""
    return prefix.rstrip("/") + "/" + name.lower().replace("_", "-")


def get_secret(name, default=_MISSING):
    """Return the secret ``name``; raise KeyError (like ``os.environ[name]``) when it is not configured."""
    if name not in SECRET_NAMES:
        raise ValueError(f"Unknown secret name: {name}")
    prefix = os.environ.get("SSM_PARAMETER_PREFIX", "").strip()
    value = _from_ssm(prefix, name) if prefix else os.environ.get(name)
    if value:
        return value
    if default is not _MISSING:
        return default
    raise KeyError(name)


def agent_enabled():
    """Feature flag for the natural-language assistant (Bedrock). ``AGENT_ENABLED=false`` disables it.

    Unset means enabled (local development and tests); the SAM template sets it explicitly per stage.
    The agent code stays in place: only the entry points check this flag.
    """
    return os.environ.get("AGENT_ENABLED", "true").strip().lower() not in {"false", "0", "no", "off"}


# Usage shown for `/jira-agent help`, an empty `/jira-agent`, and unknown text while the assistant is off.
HELP_MESSAGE = (
    "*Jira Agent commands*\n"
    "• `/jira-agent connect` – connect your Jira account (one time)\n"
    "• `/jira-agent list` – see the tickets assigned to you\n"
    "• `/jira-agent logwork find <keyword>` – find a ticket by keyword or key, e.g. `PROJ-123`\n"
    "• `/jira-agent logwork today` – see the time you logged today\n"
    "• `/jira-agent disconnect` – disconnect your Jira account\n"
    "• `/jira-agent help` – show this message\n"
    "To log time, click *Log Time* next to a ticket, fill in the form, then click *Approve* in the "
    "message I send you. Nothing is written to Jira before you approve."
)

AGENT_DISABLED_MESSAGE = "Sorry, I don't recognize that command.\n" + HELP_MESSAGE


def clear_cache():
    """Drop cached secrets (tests, or after a known rotation)."""
    global _loaded_at
    with _lock:
        _cache.clear()
        _loaded_at = None


def _cache_seconds():
    """How long loaded secrets are reused (SECRET_CACHE_SECONDS, default 300)."""
    try:
        return max(0, int(os.environ.get("SECRET_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)))
    except ValueError:
        return DEFAULT_CACHE_SECONDS


def _from_ssm(prefix, name):
    """Cached SSM value of ``name``; reloads everything when the cache is stale or the prefix changed."""
    global _loaded_at
    with _lock:
        fresh = _loaded_at is not None and time.monotonic() - _loaded_at < _cache_seconds()
        if not fresh or _cache.get("__prefix__") != prefix:
            _load_all(prefix)
        return _cache.get(name)


def _load_all(prefix):
    """Fetch all known secrets in a single GetParameters call (decrypted) and replace the cache."""
    global _loaded_at
    names = {parameter_name(prefix, secret): secret for secret in SECRET_NAMES}
    response = _client().get_parameters(Names=list(names), WithDecryption=True)
    loaded = {"__prefix__": prefix}
    for parameter in response.get("Parameters", []):
        loaded[names[parameter["Name"]]] = parameter["Value"]
    missing = response.get("InvalidParameters") or []
    if missing:
        # Names only, never values.
        logger.warning("SSM parameters not found: %s", ", ".join(sorted(missing)))
    _cache.clear()
    _cache.update(loaded)
    _loaded_at = time.monotonic()


def _client():
    """Lazily created SSM client (reused across invocations)."""
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client

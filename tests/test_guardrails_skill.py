"""Tests for .claude/skills/jira-agent-guardrails/scripts/check_guardrails.py.

Each test copies the real source tree into tmp_path, injects one violation and checks that exactly the
expected rule fires. The clean copy doubles as a false-positive check against the real code.
"""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".claude" / "skills" / "jira-agent-guardrails" / "scripts" / "check_guardrails.py"
_spec = importlib.util.spec_from_file_location("check_guardrails", SCRIPT)
guardrails = importlib.util.module_from_spec(_spec)
sys.modules["check_guardrails"] = guardrails  # dataclasses resolve their module via sys.modules
_spec.loader.exec_module(guardrails)


@pytest.fixture
def repo(tmp_path):
    for folder in guardrails.SOURCE_DIRS:
        shutil.copytree(REPO / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "requirements.txt", tmp_path / "requirements.txt")
    # Known, reported violation in the real repo (unused write path); remove it for a clean baseline.
    (tmp_path / "jira" / "tasks" / "run_create_worklog_task.py").unlink(missing_ok=True)
    return tmp_path


def run(root):
    return guardrails.Checker(root).run()


def rules(findings, level="ERROR"):
    return {f.rule for f in findings if f.level == level}


def append(root, rel, code):
    path = root / rel
    path.write_text(path.read_text(encoding="utf-8") + "\n" + code + "\n", encoding="utf-8")


def replace(root, rel, old, new):
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {rel}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_clean_codebase_has_no_findings(repo):
    # Act
    findings = run(repo)

    # Assert
    assert findings == []


def test_real_repo_reports_the_unapproved_write_path():
    # Act
    findings = run(REPO)

    # Assert: the only error today is the known bypass task (remove this test once it is deleted)
    if (REPO / "jira" / "tasks" / "run_create_worklog_task.py").exists():
        assert [(f.rule, f.path) for f in findings if f.level == "ERROR"] == [
            ("G1", "jira/tasks/run_create_worklog_task.py")]
    else:
        assert rules(findings) == set()


@pytest.mark.parametrize("rel,code,rule", [
    # G1: second write path
    ("handlers/async_task_handler.py",
     "def quick_log(service, form):\n    return service.create_worklog(form)", "G1"),
    ("jira/tasks/run_list_assigned_task.py",
     "def create_worklog(form):\n    pass", "G1"),
    ("agent/tool_registry.py",
     "def raw_write(client, key):\n    return client.request(f'/rest/api/3/issue/{key}/worklog', method='POST')", "G1"),
    # G2: dynamic dispatch
    ("agent/tool_registry.py", "def run(plan):\n    return getattr(JiraService, plan['intent'])()", "G2"),
    ("agent/orchestrator.py", "def run(plan):\n    return globals()[plan['intent']]()", "G2"),
    ("util/common.py", "def calc(expr):\n    return eval(expr)", "G2"),
    # G3: sensitive logging
    ("agent/llm_client.py", "def dbg(content):\n    print(content)", "G3"),
    ("jira/jira_client.py",
     "def dbg(self):\n    logger.info('token %s', self.credentials['access_token'])", "G3"),
    ("approval/approval_service.py",
     "def dbg(form):\n    logger.warning('form %s', form['description'])", "G3"),
    ("handlers/slack_command_handler.py",
     "def dbg(raw_body):\n    logger.info(f'body={raw_body}')", "G3"),
    # G4: async payload without secret
    ("handlers/slack_action_handler.py",
     "def bad(client):\n    return {'_async_task': 'jira_worklog', 'slack_user_id': 'U1'}", "G4"),
    # G6: pre-serialized values through a resource client
    ("approval/approval_repository.py",
     "def bad(table, now):\n    table.meta.client.put_item(Item={'ttl': {'N': str(now)}})", "G6"),
    # G7: no timeout
    ("slack/slack_client.py",
     "def bad(req):\n    return urllib.request.urlopen(req)", "G7"),
    # G8: third-party HTTP client
    ("jira/jira_client.py", "import requests", "G8"),
    # G9: secret read from the environment instead of SSM
    ("slack/slack_client.py", "def bad():\n    return os.environ['SLACK_BOT_TOKEN']", "G9"),
    ("handlers/slack_command_handler.py", "def bad():\n    return os.environ.get('ASYNC_TASK_SECRET')", "G9"),
    ("jira/authentication_jira.py", "def bad():\n    return os.getenv('ATLASSIAN_CLIENT_SECRET')", "G9"),
])
def test_injected_violation_is_reported(repo, rel, code, rule):
    # Arrange
    append(repo, rel, code)

    # Act
    findings = run(repo)

    # Assert
    assert rules(findings) == {rule}
    assert all(f.path == rel for f in findings if f.level == "ERROR")


@pytest.mark.parametrize("rel,old,new,rule", [
    ("functions/read_jira_lambda.py", "if not verify_slack_signature(event,raw_body):", "if False:", "G4"),
    ("functions/read_jira_lambda.py",
     "hmac.compare_digest(provided_secret.encode(\"utf-8\"),expected_secret.encode(\"utf-8\"))",
     "provided_secret==expected_secret", "G4"),
    ("slack/slack_security.py", "> 300", "> 3000", "G4"),
    ("approval/approval_repository.py", " AND slack_user_id = :owner", "", "G5"),
    ("approval/approval_repository.py", " AND team_id = :team", "", "G5"),
    ("approval/approval_repository.py", "ConditionExpression=\"#s = :executing\",", "", "G5"),
])
def test_removed_safeguard_is_reported(repo, rel, old, new, rule):
    # Arrange
    replace(repo, rel, old, new)

    # Act
    findings = run(repo)

    # Assert
    assert rule in rules(findings)


def test_extra_runtime_dependency_is_a_warning(repo):
    # Arrange
    (repo / "requirements.txt").write_text("boto3\nrequests>=2\n", encoding="utf-8")

    # Act
    findings = run(repo)

    # Assert
    assert rules(findings) == set()
    assert rules(findings, "WARN") == {"G8"}


def test_safe_logging_is_not_flagged(repo):
    # Arrange: ids and counts are fine to log
    append(repo, "approval/approval_service.py",
           "def ok(approval_id, user_id, count):\n"
           "    logger.info('approval %s for %s (%d items)', approval_id, user_id, count)")

    # Act & Assert
    assert run(repo) == []

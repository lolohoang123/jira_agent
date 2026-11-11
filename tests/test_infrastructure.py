"""Invariants of template.yaml and the CI/CD workflows. cfn-lint (run in CI) checks CloudFormation syntax;
these tests pin the security and reliability decisions so a later edit cannot silently undo them.
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN", "ATLASSIAN_CLIENT_SECRET", "ASYNC_TASK_SECRET")


class _CfnLoader(yaml.SafeLoader):
    """Loads CloudFormation short-form tags (!Ref, !Sub, !If ...) as {"Tag": value}."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template():
    return yaml.load((ROOT / "template.yaml").read_text(encoding="utf-8"), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def function(template):
    return template["Resources"]["JiraAgentFunction"]["Properties"]


def test_secrets_are_never_in_the_template():
    # Arrange
    text = (ROOT / "template.yaml").read_text(encoding="utf-8")

    # Act & Assert: secret names may not appear as environment variables or parameters
    for name in SECRET_NAMES:
        assert re.search(rf"^\s*{name}\s*:", text, re.M) is None, name
    assert "xoxb-" not in text


def test_function_reads_secrets_from_its_own_ssm_path(function):
    # Arrange
    statements = [s for p in function["Policies"] if "Statement" in p for s in p["Statement"] if isinstance(s, dict)]
    ssm = [s for s in statements if s.get("Sid") == "ReadOwnSecrets"]

    # Assert
    assert function["Environment"]["Variables"]["SSM_PARAMETER_PREFIX"] == {"Sub": "/jira-agent/${Stage}/"}
    assert len(ssm) == 1 and ssm[0]["Action"] == "ssm:GetParameters"
    assert ssm[0]["Resource"] == {"Sub": "arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/jira-agent/${Stage}/*"}


def test_async_retries_are_disabled_and_failures_are_kept(template, function):
    # Assert: tasks are not idempotent (duplicate approvals / replies on retry)
    config = function["EventInvokeConfig"]
    assert config["MaximumRetryAttempts"] == 0
    assert config["DestinationConfig"]["OnFailure"] == {"Type": "SQS",
                                                        "Destination": {"GetAtt": "AsyncFailureQueue.Arn"}}
    queue = template["Resources"]["AsyncFailureQueue"]["Properties"]
    assert queue["SqsManagedSseEnabled"] is True
    assert queue["MessageRetentionPeriod"] <= 4 * 86400


def test_cost_and_abuse_caps(template, function):
    # Assert
    assert function["ReservedConcurrentExecutions"] == {"Ref": "ReservedConcurrency"}
    throttle = template["Resources"]["JiraAgentApi"]["Properties"]["DefaultRouteSettings"]
    assert set(throttle) == {"ThrottlingRateLimit", "ThrottlingBurstLimit"}
    variables = function["Environment"]["Variables"]
    assert variables["RATE_LIMITS_TABLE"] == {"Ref": "RateLimitsTable"}
    assert variables["AGENT_RATE_LIMIT_PER_HOUR"] == {"Ref": "AgentRateLimitPerHour"}


def test_approvals_are_private_by_default(template):
    # Assert (GAP-03)
    assert template["Parameters"]["ApprovalDelivery"]["Default"] == "dm"


@pytest.mark.parametrize("table,ttl_attribute,pitr", [
    ("OAuthStatesTable", "expiresAt", False),
    ("OAuthUsersTable", None, True),
    ("ApprovalsTable", "ttl", True),
    ("RateLimitsTable", "ttl", False),
])
def test_tables_ttl_encryption_and_backup(template, table, ttl_attribute, pitr):
    # Arrange
    resource = template["Resources"][table]
    properties = resource["Properties"]

    # Assert: TTL attribute names must match the code (CLAUDE.md conventions)
    if ttl_attribute:
        assert properties["TimeToLiveSpecification"] == {"AttributeName": ttl_attribute, "Enabled": True}
    else:
        assert "TimeToLiveSpecification" not in properties
    assert properties["SSESpecification"] == {"SSEEnabled": True}
    assert ("PointInTimeRecoverySpecification" in properties) is pitr
    if table != "RateLimitsTable":
        assert resource.get("DeletionPolicy") == "Retain"


def test_routes_and_callback_url(template, function):
    # Arrange
    events = function["Events"]

    # Assert
    assert {(e["Properties"]["Path"], e["Properties"]["Method"]) for e in events.values()} == {
        ("/", "POST"), ("/oauth/callback", "GET")}
    redirect = function["Environment"]["Variables"]["ATLASSIAN_REDIRECT_URI"]["Sub"]
    callback_output = template["Outputs"]["OAuthCallbackUrl"]["Value"]["Sub"]
    assert redirect == callback_output and redirect.endswith("/oauth/callback")


def test_runtime_and_handler(template, function):
    # Assert
    assert template["Parameters"]["Runtime"]["Default"] == "python3.12"
    assert function["Handler"] == "functions.read_jira_lambda.lambda_handler"
    assert function["Timeout"] == 60


def test_alarms_notify_the_topic(template):
    # Arrange
    alarms = {name: r["Properties"] for name, r in template["Resources"].items()
              if r["Type"] == "AWS::CloudWatch::Alarm"}

    # Assert
    assert {"FunctionErrorsAlarm", "FunctionThrottlesAlarm", "FunctionDurationAlarm", "AsyncFailuresAlarm"} <= set(alarms)
    assert all(a["AlarmActions"] == [{"Ref": "AlarmTopic"}] for a in alarms.values())


def test_deploy_workflow_uses_oidc_and_runs_checks_first():
    # Arrange
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["deploy"]
    steps = [s.get("name") or s.get("uses") for s in job["steps"]]

    # Assert: short-lived OIDC credentials, gated by tests and guardrails, no secret values in GitHub
    assert workflow["permissions"]["id-token"] == "write"
    assert steps.index("Tests and guardrails must pass before deploying") < steps.index("Deploy")
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "aws-access-key-id" not in text
    for name in SECRET_NAMES:
        assert f"secrets.{name}" not in text


# --------------------------------------------------------------------------------------
# infra/github-oidc-bootstrap.yaml (GitHub OIDC deploy roles)
# --------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def bootstrap():
    return yaml.load((ROOT / "infra" / "github-oidc-bootstrap.yaml").read_text(encoding="utf-8"), Loader=_CfnLoader)


@pytest.mark.parametrize("path", ["template.yaml", "infra/github-oidc-bootstrap.yaml"])
def test_templates_are_ascii_for_the_windows_aws_cli(path):
    # The AWS CLI reads file:// templates with the system code page (cp932 here): non-ASCII breaks it.
    assert (ROOT / path).read_bytes().isascii()


@pytest.mark.parametrize("role,stage", [("StagingDeployRole", "staging"), ("ProdDeployRole", "prod")])
def test_deploy_role_trusts_only_this_repo_and_environment(bootstrap, role, stage):
    # Arrange
    [statement] = bootstrap["Resources"][role]["Properties"]["AssumeRolePolicyDocument"]["Statement"]

    # Assert: exact match (StringEquals, no wildcard) on audience and on repo + GitHub environment
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    condition = statement["Condition"]
    assert set(condition) == {"StringEquals"}
    assert condition["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert condition["StringEquals"]["token.actions.githubusercontent.com:sub"] == {
        "Sub": f"repo:${{GitHubRepository}}:environment:{stage}"}
    assert bootstrap["Parameters"]["GitHubRepository"]["Default"] == "lolohoang123/jira_agent"


@pytest.mark.parametrize("policy,stage,other", [("StagingDeployPolicy", "staging", "prod"),
                                                ("ProdDeployPolicy", "prod", "staging")])
def test_deploy_policy_is_scoped_to_its_stage(bootstrap, policy, stage, other):
    # Arrange
    statements = bootstrap["Resources"][policy]["Properties"]["PolicyDocument"]["Statement"]
    by_sid = {s["Sid"]: s for s in statements}
    text = str(statements)

    # Assert: only its own stage, roles only with the boundary, PassRole only to Lambda, no admin actions
    assert f"jira-agent-{other}" not in text
    create = by_sid["FunctionRoleWithBoundaryOnly"]
    assert "iam:CreateRole" in create["Action"]
    assert create["Condition"] == {"StringEquals": {"iam:PermissionsBoundary": {"Ref": "FunctionPermissionsBoundary"}}}
    assert by_sid["PassFunctionRoleToLambdaOnly"]["Condition"] == {
        "StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}
    assert by_sid["NeverRemoveTheBoundary"]["Effect"] == "Deny"
    actions = [a for s in statements if s["Effect"] == "Allow"
               for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    assert "*" not in actions and "iam:*" not in actions
    assert not any(a.startswith(("iam:CreateUser", "iam:CreatePolicy", "iam:AttachUserPolicy")) for a in actions)


def test_function_carries_the_bootstrap_boundary(bootstrap, function):
    # Assert: the name the app template uses is the one the bootstrap creates
    name = bootstrap["Resources"]["FunctionPermissionsBoundary"]["Properties"]["ManagedPolicyName"]
    assert function["PermissionsBoundary"] == {"Sub": f"arn:aws:iam::${{AWS::AccountId}}:policy/{name}"}


def test_boundary_allows_what_the_function_needs_and_nothing_admin(bootstrap):
    # Arrange
    statements = bootstrap["Resources"]["FunctionPermissionsBoundary"]["Properties"]["PolicyDocument"]["Statement"]
    actions = {a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}

    # Assert
    assert {"dynamodb:UpdateItem", "ssm:GetParameters", "lambda:InvokeFunction", "bedrock:InvokeModel",
            "sqs:SendMessage", "logs:PutLogEvents", "kms:Decrypt"} <= actions
    assert not any(a.startswith("iam:") or a.endswith(":*") for a in actions)

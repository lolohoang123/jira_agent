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
    # Optional: an account with the default limit (10) cannot reserve any concurrency at all.
    assert function["ReservedConcurrentExecutions"] == {
        "If": ["HasReservedConcurrency", {"Ref": "ReservedConcurrency"}, {"Ref": "AWS::NoValue"}]}
    assert template["Parameters"]["ReservedConcurrency"]["Default"] == 0
    throttle = template["Resources"]["JiraAgentApiStage"]["Properties"]["DefaultRouteSettings"]
    assert set(throttle) == {"ThrottlingRateLimit", "ThrottlingBurstLimit"}
    variables = function["Environment"]["Variables"]
    assert variables["RATE_LIMITS_TABLE"] == {"Ref": "RateLimitsTable"}
    assert variables["AGENT_RATE_LIMIT_PER_HOUR"] == {"Ref": "AgentRateLimitPerHour"}


def test_approvals_are_private_by_default(template):
    # Assert (GAP-03)
    assert template["Parameters"]["ApprovalDelivery"]["Default"] == "dm"


@pytest.mark.parametrize("table,ttl_attribute,pitr", [
    ("OAuthStatesTable", "expiresAt", False),
    ("JiraConnectionsTable", None, True),
    ("InstallationsTable", None, True),
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
    resources = template["Resources"]
    routes = {r["Properties"]["RouteKey"]: r["Properties"]["Target"] for r in resources.values()
              if r["Type"] == "AWS::ApiGatewayV2::Route"}
    integration = resources["JiraAgentIntegration"]["Properties"]
    permission = resources["ApiInvokePermission"]["Properties"]

    # Assert: exactly the two public routes, both proxied to the function, invokable only from this API
    target = {"Sub": "integrations/${JiraAgentIntegration}"}
    assert routes == {"POST /": target, "GET /oauth/callback": target, "GET /slack/install": target,
                      "GET /slack/oauth/callback": target, "POST /slack/events": target}
    assert integration["IntegrationUri"] == {"GetAtt": "JiraAgentFunction.Arn"}
    assert integration["PayloadFormatVersion"] == "2.0"  # handler reads rawPath / requestContext.http
    assert permission["Principal"] == "apigateway.amazonaws.com"
    assert permission["SourceArn"] == {"Sub": "arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${JiraAgentApi}/*"}
    redirect = function["Environment"]["Variables"]["ATLASSIAN_REDIRECT_URI"]["Sub"]
    callback_output = template["Outputs"]["OAuthCallbackUrl"]["Value"]["Sub"]
    assert redirect == callback_output and redirect.endswith("/oauth/callback")


def _references(node):
    """Logical ids referenced by Ref, GetAtt and ${...} in Sub (pseudo parameters excluded)."""
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "GetAtt":
                found.add((value.split(".")[0] if isinstance(value, str) else value[0]))
            elif key == "Sub":
                text = value if isinstance(value, str) else value[0]
                found |= {m.split(".")[0] for m in re.findall(r"\$\{([^}!]+)\}", text)}
            found |= _references(value)
    elif isinstance(node, list):
        for item in node:
            found |= _references(item)
    return found


def test_no_circular_dependencies_including_sam_implicit_ones(template):
    # Arrange: explicit edges (Ref/GetAtt/Sub/DependsOn) plus the edges SAM adds when a function has
    # API events: the API resource embeds the routes, so the API depends on the function.
    resources = template["Resources"]
    graph = {}
    for name, resource in resources.items():
        depends = resource.get("DependsOn", [])
        depends = [depends] if isinstance(depends, str) else depends
        graph[name] = (_references(resource.get("Properties", {})) | set(depends)) & set(resources)
    for name, resource in resources.items():
        for event in (resource.get("Properties", {}).get("Events") or {}).values():
            api = _references(event.get("Properties", {}).get("ApiId", {}))
            for api_name in api & set(resources):
                graph[api_name].add(name)

    # Act: depth-first search for a back edge
    state, cycle = {}, []

    def visit(node, path):
        state[node] = "visiting"
        for nxt in graph[node]:
            if state.get(nxt) == "visiting":
                cycle.extend(path + [node, nxt])
                return True
            if nxt not in state and visit(nxt, path + [node]):
                return True
        state[node] = "done"
        return False

    has_cycle = any(visit(n, []) for n in graph if n not in state)

    # Assert
    assert not has_cycle, " -> ".join(cycle)


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
        "Sub": f"repo:${{GitHubSubjectRepository}}:environment:{stage}"}
    # GitHub sends immutable ids (owner@id/repo@id); verified from CloudTrail on the first deploy.
    assert bootstrap["Parameters"]["GitHubSubjectRepository"]["Default"] == "lolohoang123@6188421/jira_agent@1445373052"


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


def test_agent_flag_is_wired_and_off_by_default(template, function):
    # Assert: the assistant is temporarily disabled in AWS; GitHub var AGENT_ENABLED can turn it back on
    assert template["Parameters"]["AgentEnabled"]["Default"] == "false"
    assert template["Parameters"]["AgentEnabled"]["AllowedValues"] == ["true", "false"]
    assert function["Environment"]["Variables"]["AGENT_ENABLED"] == {"Ref": "AgentEnabled"}
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert 'overrides+=("AgentEnabled=$AGENT_ENABLED")' in workflow


def test_jira_connections_are_keyed_by_workspace_and_user(template, function):
    # Assert: Slack user ids are only unique per workspace
    props = template["Resources"]["JiraConnectionsTable"]["Properties"]
    assert props["KeySchema"] == [{"AttributeName": "team_id", "KeyType": "HASH"},
                                  {"AttributeName": "slack_user_id", "KeyType": "RANGE"}]
    assert template["Resources"]["InstallationsTable"]["Properties"]["KeySchema"] == [
        {"AttributeName": "team_id", "KeyType": "HASH"}]
    assert "OAuthUsersTable" not in template["Resources"]
    variables = function["Environment"]["Variables"]
    assert variables["OAUTH_USERS_TABLE"] == {"Ref": "JiraConnectionsTable"}
    assert variables["INSTALLATIONS_TABLE"] == {"Ref": "InstallationsTable"}


def test_slack_oauth_urls_come_from_this_api(template, function):
    # Assert: redirect, install link and outputs all point at this stack's API
    variables = function["Environment"]["Variables"]
    base = "https://${JiraAgentApi}.execute-api.${AWS::Region}.amazonaws.com"
    assert variables["SLACK_REDIRECT_URI"] == {"Sub": f"{base}/slack/oauth/callback"}
    assert variables["SLACK_INSTALL_URL"] == {"Sub": f"{base}/slack/install"}
    assert variables["SLACK_CLIENT_ID"] == {"Ref": "SlackClientId"}
    outputs = template["Outputs"]
    assert outputs["SlackRedirectUrl"]["Value"] == {"Sub": f"{base}/slack/oauth/callback"}
    assert outputs["SlackEventsUrl"]["Value"] == {"Sub": f"{base}/slack/events"}

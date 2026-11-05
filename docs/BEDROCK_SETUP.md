# Amazon Bedrock setup (Slack + Jira Lambda)

## 1. Select a model

In the AWS account and region used by the Lambda function, open Amazon Bedrock Model catalog and select a text model that supports the Converse API. Record the exact model ID and verify that it is available in that region. Model availability, access requirements, and pricing vary by region and provider.

## 2. Lambda environment variables

Add:

- `BEDROCK_MODEL_ID`: exact model ID from the model details page.
- `BEDROCK_REGION`: Bedrock region (prefer the same region as Lambda when the model is available there).
- `JIRA_TIMEZONE`: `Asia/Ho_Chi_Minh` (if this is the Jira team's timezone).

Keep existing Slack, Jira OAuth, DynamoDB, `ASYNC_TASK_SECRET`, and approval environment variables.

## 3. Lambda execution role

For initial testing, attach this policy and replace `*` with the exact foundation-model or inference-profile ARN after confirming the selected model's ARN format:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": "*"
  }]
}
```

The Converse API uses `bedrock:InvokeModel`. Do not grant model invocation permissions to Slack users; the permission belongs to the Lambda execution role.

## 4. Deploy

Deploy the contents of this project directory as the Lambda package, preserving the configured handler module path. `requirements.txt` already includes boto3. If building a deployment package with dependencies, install the requirements into the package directory as usual for the Lambda Python runtime.

## 5. Test

After setting environment variables, test the model adapter in the Lambda environment or a local environment with AWS credentials and the same region:

```python
from agent.llm_client import BedrockLLMClient

llm = BedrockLLMClient()
print(llm.generate_plan(
    'Return only JSON: {"intent":"unknown","parameters":{},"message":"ok"}',
    'User request: hello'
))
```

Expected output is a JSON object. If the model returns invalid JSON, the adapter raises an error and no Jira tool runs.

## 6. Slack examples

- `/jira-agent list` and `/jira-agent logwork today` continue using the existing deterministic handlers.
- `/jira-agent show my assigned tickets` invokes the LLM asynchronously and runs the allow-listed read tool.
- `/jira-agent log 1h to PROJ-123 today starting at 09:00` may create an approval proposal only if the model supplies all required fields. Missing/ambiguous fields must be clarified. Jira is not changed until the same Slack user approves the request.

## Security model

- Bedrock only proposes a JSON intent and parameters. It does not receive Jira tokens and cannot call Jira APIs.
- `agent/tool_registry.py` validates the intent against a fixed allow-list and validates write parameters.
- Jira write requests go through `ApprovalService`; the existing owner, expiry, and atomic `PENDING -> EXECUTING` checks remain in place.
- Natural-language commands are invoked asynchronously so Slack receives an acknowledgement before Bedrock runs.
- The async Lambda event is authenticated using the existing `ASYNC_TASK_SECRET`.

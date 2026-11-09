import json
import logging

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def invoke_async(lambda_client, context, task):
    """Invoke this Lambda asynchronously, targeting the exact version/alias that is running.

    ``context.invoked_function_arn`` keeps async tasks on the same version/alias as the request
    that created them. If the execution role is only allowed to invoke the unqualified function
    ARN, fall back to the previous behaviour (``context.function_name``) instead of failing.
    """
    payload = json.dumps(task).encode()
    target = getattr(context, "invoked_function_arn", None) or context.function_name
    try:
        return lambda_client.invoke(FunctionName=target, InvocationType="Event", Payload=payload)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if target != context.function_name and code == "AccessDeniedException":
            logger.warning("Not allowed to invoke %s; falling back to the unqualified function name. "
                           "Add the qualified ARN (function:NAME:*) to the execution role.", target)
            return lambda_client.invoke(FunctionName=context.function_name, InvocationType="Event", Payload=payload)
        raise

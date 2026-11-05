"""Amazon Bedrock Converse API adapter. The model proposes plans; it never runs tools."""
import json
import os
import re

import boto3


class BedrockLLMClient:
    def __init__(self, client=None, model_id=None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID")
        if not self.model_id:
            raise RuntimeError("Missing required environment variable: BEDROCK_MODEL_ID")
        region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "ap-southeast-1")
        self.client = client or boto3.client("bedrock-runtime", region_name=region)

    def generate_plan(self, system_prompt, user_message):
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": 450, "temperature": 0},
        )
        content = response.get("output", {}).get("message", {}).get("content", [])
        raw = "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
        # Accept a fenced JSON response, but no prose or repair heuristics.
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            raw = fenced.group(1)
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Bedrock returned invalid JSON") from exc
        if not isinstance(plan, dict):
            raise ValueError("Bedrock response must be a JSON object")
        return plan

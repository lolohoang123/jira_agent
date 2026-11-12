#!/usr/bin/env python3
"""One-off migration: encrypt plaintext tokens written before field-level encryption was enabled.

Run from the repo root with an administrator MFA session (it is not part of the Lambda package):

    python scripts/encrypt_existing_secrets.py --stage staging --profile mfa            # dry run: counts only
    python scripts/encrypt_existing_secrets.py --stage staging --profile mfa --apply    # encrypt

Each value is written with a condition that it is still the same plaintext, so a token refreshed concurrently
by the Lambda is never overwritten. Only counts are printed: never token values.
"""
import argparse
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from botocore.exceptions import ClientError  # noqa: E402

from util import field_crypto  # noqa: E402


@dataclass(frozen=True)
class TableSpec:
    physical_suffix: str        # jira-agent-<stage>-<suffix>
    logical_name: str           # table name in the encryption context
    key_attributes: tuple       # primary key attributes (also the encryption context ids)
    secret_fields: tuple


SPECS = (
    TableSpec("jira-connections", "jira-connections", ("team_id", "slack_user_id"), ("accessToken", "refreshToken")),
    TableSpec("installations", "installations", ("team_id",), ("bot_token",)),
)


@dataclass
class Counts:
    rows: int = 0
    already_sealed: int = 0
    plaintext: int = 0
    encrypted: int = 0
    changed_meanwhile: int = 0
    per_field: dict = field(default_factory=dict)


def migrate_table(table, spec, apply, seal=field_crypto.seal):
    """Encrypt every plaintext secret field of ``table``. Returns Counts (no values)."""
    counts = Counts()
    projection = ", ".join(f"#a{i}" for i in range(len(spec.key_attributes) + len(spec.secret_fields)))
    names = {f"#a{i}": name for i, name in enumerate(spec.key_attributes + spec.secret_fields)}
    start_key = None
    while True:
        kwargs = {"ProjectionExpression": projection, "ExpressionAttributeNames": names}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = table.scan(**kwargs)
        for row in page.get("Items", []):
            counts.rows += 1
            key = {name: row[name] for name in spec.key_attributes}
            for secret in spec.secret_fields:
                value = row.get(secret)
                if not value:
                    continue
                if field_crypto.is_sealed(value):
                    counts.already_sealed += 1
                    continue
                counts.plaintext += 1
                counts.per_field[secret] = counts.per_field.get(secret, 0) + 1
                if not apply:
                    continue
                sealed = seal(value, spec.logical_name, secret, **key)
                try:
                    table.update_item(
                        Key=key,
                        UpdateExpression="SET #f = :sealed",
                        ConditionExpression="#f = :plaintext",
                        ExpressionAttributeNames={"#f": secret},
                        ExpressionAttributeValues={":sealed": sealed, ":plaintext": value},
                    )
                    counts.encrypted += 1
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    counts.changed_meanwhile += 1  # refreshed by the Lambda in the meantime: already sealed
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", required=True, choices=["staging", "prod"])
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--apply", action="store_true", help="write the encrypted values (default: dry run)")
    args = parser.parse_args()

    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    exports = {e["Name"]: e["Value"] for page in session.client("cloudformation").get_paginator("list_exports").paginate()
               for e in page["Exports"]}
    key_arn = exports.get(f"jira-agent-{args.stage}-data-key-arn")
    if not key_arn:
        sys.exit(f"Export jira-agent-{args.stage}-data-key-arn not found: deploy the bootstrap stack first.")
    os.environ["FIELD_ENCRYPTION"] = "kms"
    os.environ["FIELD_ENCRYPTION_KEY_ARN"] = key_arn
    field_crypto.set_kms_client(session.client("kms"))

    dynamodb = session.resource("dynamodb")
    print(f"{'APPLY' if args.apply else 'DRY RUN'} - stage {args.stage}")
    for spec in SPECS:
        name = f"jira-agent-{args.stage}-{spec.physical_suffix}"
        counts = migrate_table(dynamodb.Table(name), spec, args.apply)
        print(f"{name}: rows={counts.rows} already_encrypted={counts.already_sealed} plaintext={counts.plaintext} "
              f"{counts.per_field} encrypted={counts.encrypted} changed_meanwhile={counts.changed_meanwhile}")


if __name__ == "__main__":
    main()

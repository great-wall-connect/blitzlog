#!/usr/bin/env python3
"""Release bot-pool lock(s) in S3 for assisted-mode Telegram bots."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

import boto3
from botocore.exceptions import ClientError

BOT_POOL_LOCK_PREFIX = "bot-pool-locks"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Release stuck bot-pool lock files from S3."
    )
    parser.add_argument(
        "--sender",
        required=True,
        help="GitHub login of the sender who owns the bot pool.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--bot",
        help="Name of the bot whose lock should be released.",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Release all active bot locks for the specified sender.",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("S3_LOGS_BUCKET"),
        help="S3 bucket storing agent logs and locks (defaults to ).",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="AWS region (defaults to  or ).",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and release locks immediately.",
    )
    return parser.parse_args(argv)


def find_locks(
    s3_client: boto3.client, bucket: str, sender: str, bot: str | None, all_locks: bool
) -> list[dict[str, str]]:
    locks: list[dict[str, str]] = []

    if bot:
        key = f"{BOT_POOL_LOCK_PREFIX}/{sender}/{bot}.json"
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key)
            data = json.loads(resp["Body"].read().decode("utf-8"))
            locks.append(
                {
                    "key": key,
                    "bot": bot,
                    "acquired_at": data.get("acquired_at", "unknown"),
                    "repo": data.get("repo", "unknown"),
                    "issue": str(data.get("issue_number", "unknown")),
                }
            )
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return []
            raise
    elif all_locks:
        prefix = f"{BOT_POOL_LOCK_PREFIX}/{sender}/"
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                bot_name = key[len(prefix) : -5]
                try:
                    resp = s3_client.get_object(Bucket=bucket, Key=key)
                    data = json.loads(resp["Body"].read().decode("utf-8"))
                    acquired_at = data.get("acquired_at", "unknown")
                    repo = data.get("repo", "unknown")
                    issue = str(data.get("issue_number", "unknown"))
                except (
                    ClientError,
                    json.JSONDecodeError,
                    KeyError,
                    UnicodeDecodeError,
                ):
                    acquired_at = "unparseable"
                    repo = "unknown"
                    issue = "unknown"
                locks.append(
                    {
                        "key": key,
                        "bot": bot_name,
                        "acquired_at": acquired_at,
                        "repo": repo,
                        "issue": issue,
                    }
                )

    return locks


def release_locks(
    s3_client: boto3.client, bucket: str, locks: list[dict[str, str]]
) -> int:
    deleted_count = 0
    for lock in locks:
        key = lock["key"]
        s3_client.delete_object(Bucket=bucket, Key=key)
        print(f"Deleted s3://{bucket}/{key} (bot: {lock['bot']})")
        deleted_count += 1
    return deleted_count


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    bucket = args.bucket
    if not bucket:
        print(
            "Error: S3 bucket not specified. Set S3_LOGS_BUCKET env var or pass --bucket.",
            file=sys.stderr,
        )
        return 1

    s3_kwargs = {}
    if args.region:
        s3_kwargs["region_name"] = args.region

    s3_client = boto3.client("s3", **s3_kwargs)

    try:
        locks = find_locks(s3_client, bucket, args.sender, args.bot, args.all)
    except ClientError as e:
        print(f"Error checking S3 locks: {e}", file=sys.stderr)
        return 1

    if not locks:
        target = f"bot '{args.bot}'" if args.bot else f"sender '{args.sender}'"
        print(f"No active locks found for {target} in bucket '{bucket}'.")
        return 0

    print(f"Found {len(locks)} lock(s) in bucket '{bucket}':")
    for lock in locks:
        print(
            f"  - Bot: {lock['bot']} | Acquired: {lock['acquired_at']} | Repo: {lock['repo']}#{lock['issue']} | Key: {lock['key']}"
        )

    if not args.yes:
        confirm = input(f"\nRelease {len(locks)} lock(s)? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            return 0

    released = release_locks(s3_client, bucket, locks)
    print(f"\nSuccessfully released {released} lock(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

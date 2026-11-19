"""Provider-neutral aliases for the AWS SDK exception types.

`handler.py` and `bot_pool.py` wrap the store calls in
`except ClientError` — the same clause has to mean the same thing on
both clouds, but the two bundles ship different SDKs: the AWS bundle
has botocore, the Alicloud bundle has Alicloud's Tea.

Resolving the alias to botocore's class when botocore is importable
and to the SDK's `TeaException` otherwise keeps the `except` clauses
meaningful on both. It is also what makes the Alicloud bundle
importable at all: `handler.py` and `bot_pool.py` imported botocore
at module level, so on Alicloud the handler failed to import with
`ModuleNotFoundError: No module named 'boto3'` and every invocation
answered `502 Bad Gateway` (the function never started).

`BotoCoreError` has no Alicloud counterpart — it is only raised by
botocore's credential/endpoint resolution — so the fallback is a
stand-in that is never raised, which leaves the `except BotoCoreError`
clauses inert rather than broken on the Alicloud path.
"""

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # the Alicloud bundle ships no botocore
    from Tea.exceptions import TeaException as ClientError

    class BotoCoreError(Exception):
        """Stand-in for botocore's BotoCoreError on the Alicloud path."""


__all__ = ["BotoCoreError", "ClientError"]

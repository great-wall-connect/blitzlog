#!/usr/bin/env python3
"""Read one OOS encrypted parameter from the aliyun CLI's JSON output.

The CLI answers an API error with a JSON document on stdout and a
ZERO exit code, so its output cannot be trusted to be a successful
response: a caller that only looks for `Parameter.Value` reads
"the parameter is missing" when the truth is "you are not allowed to
read it". This parses the response, writes the value on stdout and the
diagnostic on stderr, and exits non-zero with the API's own error
message when the read was refused.

The diagnostic also names the CMK the parameter is encrypted with
(`KeyId`). That is the only place it is visible: the OOS API returns
it in the same response as the value, and the terraform provider reads
the parameter back WITHOUT decryption (so its state copy of the value
is always empty).

Usage:

    aliyun oos GetSecretParameter --Name <name> --WithDecryption true \\
        --region <region> | python3 scripts/oos_read_parameter.py
"""

import json
import sys


def _describe_error(doc: dict) -> str:
    """The API's own error code and message, whichever it uses."""
    code = doc.get("error_code") or doc.get("ErrorCode") or doc.get("code") or "unknown"
    message = (
        doc.get("message") or doc.get("Message") or doc.get("Recommendation") or ""
    )
    request_id = doc.get("request_id") or doc.get("RequestId") or ""
    detail = f"OOS refused the read ({code}): {message}"
    if request_id:
        detail += f" (RequestId: {request_id})"
    return detail


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        # The CLI answers an API error on stderr and exits non-zero,
        # so there is nothing on stdout to parse — its own error is
        # already in the log.
        print(
            "the aliyun CLI returned no output — its error is above",
            file=sys.stderr,
        )
        return 1
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"the aliyun CLI did not return JSON: {exc}", file=sys.stderr)
        print(f"  raw output: {raw.strip()[:500]}", file=sys.stderr)
        return 1

    if not isinstance(doc, dict):
        print(
            f"the aliyun CLI returned {type(doc).__name__}, expected an object",
            file=sys.stderr,
        )
        return 1

    param = doc.get("Parameter")
    if param is None:
        print(_describe_error(doc), file=sys.stderr)
        return 1

    key_id = param.get("KeyId") or ""
    # A PLAIN parameter ("Type": "String") is not KMS-encrypted and
    # has no key to name; only an encrypted one does.
    if key_id or param.get("Type") != "String":
        print(
            f"    KeyId: {key_id or 'none (the OOS default service CMK)'}",
            file=sys.stderr,
        )
    sys.stdout.write(param.get("Value") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
